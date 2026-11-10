"""Every note the machine takes, with the currency it thinks the note is in.

Read-only. General polls plus LP 0x48 and LP 0x1E; nothing changes machine
state and nothing is enabled or disabled.

This exists for a question SAS answers badly. The cumulative bill meters are
by FACE VALUE only -- Table C-7's 0059 is "Total number of $1.00 bills to
drop" and nothing in it says which currency -- so on a floor accepting more
than one, a BZD 5 and a USD 5 increment the same counter while being worth
different money. Table C-6 concedes the problem and declines to solve it:

    "For currencies other than US, the value of a '$1 bill' is generally
    equivalent to 100 of the minor units of the reporting currency, as
    defined by the ISO-4217 standard. However, legacy bill reporting issues
    may take precedence over this rule. Please consult system providers
    operating in the local jurisdiction for guidance."

The ONLY per-note currency tag in SAS 6.02 is the country code in LP 0x48,
and §7.11 is blunt about how easily it is lost: "the gaming machine only
saves the most recently accepted bill information. It is up to the host to
request the bill information in a timely manner." The exception is queued;
the payload behind it is not. Two notes between polls and the first one's
country code is gone for good.

So this watches for a bill-accepted exception and reads LP 0x48 immediately,
rather than on a meter cadence. When it sees two bill exceptions without a
successful read in between it says so, because that is a lost note and the
run should not quietly report one fewer.

BELIZE IS NOT IN TABLE C-5. The table defines 39 country codes, 00 Unknown
through 39 Euro, with 40-47 reserved. A validator reflashed for BZD has
nothing correct to report, so it will send 00, or 37 United States -- which
is wrong by exactly half at the 2:1 peg -- or a reserved code. Whatever it
sends is recorded here verbatim alongside the name, if it has one. Finding
that out before the floor runs on it is the point of the exercise.

    python3 examples/bill_in_watch.py /dev/ttyS0 --address 1
    python3 examples/bill_in_watch.py /dev/ttyS0 --address 1 --json bills.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.client import SASClient
from saspy.constants import describe_exception
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

# Table A-1. 47-4E and 50 are the per-denomination forms used in standard
# reporting mode; 4F is the catch-all, and §7.11 prefers it exclusively in
# RTE mode. A host must treat all of them as "a note went in".
BILL_EXCEPTIONS = {0x47, 0x48, 0x49, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F, 0x50}

# Table C-5, transcribed from the specification. Codes 40-47 are reserved;
# anything outside the table is reported as unnamed rather than guessed.
COUNTRY_CODES = {
    0: "Unknown country code", 1: "Argentina", 2: "Australia", 3: "Austria",
    4: "Belgium", 5: "Brazil", 6: "Bulgaria", 7: "Canada", 8: "Columbia",
    9: "Cyprus", 10: "Czechoslovakia", 11: "Denmark", 12: "Finland",
    13: "France", 14: "Germany", 15: "Great Britain", 16: "Gibraltar",
    17: "Greece", 18: "Guernsey", 19: "Hungary", 20: "Ireland", 21: "Italy",
    22: "Jersey", 23: "Luxembourg", 24: "Malta", 25: "Mexico", 26: "Morocco",
    27: "Norway", 28: "Poland", 29: "Portugal", 30: "Romania", 31: "Russia",
    32: "Spain", 33: "South Africa", 34: "Sweden", 35: "Switzerland",
    36: "Turkey", 37: "United States", 38: "Holland", 39: "Euro",
}

# Table C-6. The face values are labelled "US Denomination" in the spec and
# mean "this many units of the reporting currency" elsewhere -- see the note
# quoted in the module docstring, which is why these are kept as integers of
# whole currency units rather than formatted as dollars.
DENOMINATION_UNITS = {
    0: 1, 1: 2, 2: 5, 3: 10, 4: 20, 5: 25, 6: 50, 7: 100, 8: 200, 9: 250,
    10: 500, 11: 1000, 12: 2000, 13: 2500, 14: 5000, 15: 10000, 16: 20000,
    17: 25000, 18: 50000, 19: 100000, 20: 200000, 21: 250000, 22: 500000,
    23: 1000000,
}


def country(code: int) -> str:
    if code in COUNTRY_CODES:
        return COUNTRY_CODES[code]
    if 40 <= code <= 47:
        return f"reserved for future use (code {code})"
    return f"NOT IN TABLE C-5 (code {code})"


def denomination(code: int) -> str:
    units = DENOMINATION_UNITS.get(code)
    if units is None:
        return f"NOT IN TABLE C-6 (code {code})"
    return f"{units:,} units of the reporting currency"


def read_bill_meters(client) -> dict | None:
    """LP 0x1E, six per-denomination COUNTS. Read before and after so the
    run can say which counter each note landed in -- which is the whole
    ambiguity when two currencies share one validator."""
    try:
        m = client.send_total_bill_meters()
    except SASError:
        return None
    return {k: v for k, v in vars(m).items()}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--interval", type=float, default=0.25,
                    help="seconds between general polls (§2.3.3 floor is 0.2)")
    ap.add_argument("--json", metavar="PATH", help="write the notes to PATH as they arrive")
    args = ap.parse_args()

    if args.interval < 0.2:
        print("--interval below the §2.3.3 floor of 200 ms; refusing.", file=sys.stderr)
        return 2

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(SASTransport(port, byte_timeout=args.timeout),
                       args.address, timeout=args.timeout)

    before = read_bill_meters(client)
    notes: list[dict] = []
    lost = 0
    pending_unread = False

    print(f"\nWatching {args.port} address {args.address} for bills.")
    print("Insert notes one at a time. Ctrl-C to finish.\n")
    print("  LP 0x1E before:", before if before else "not supported")
    print()

    try:
        while True:
            try:
                code = client.general_poll()
            except SASError:
                time.sleep(args.interval)
                continue

            if code not in BILL_EXCEPTIONS:
                time.sleep(args.interval)
                continue

            now = dt.datetime.now(dt.timezone.utc)
            if pending_unread:
                # §7.11 keeps only the most recent note's data, so the one
                # before this is unrecoverable. Say it rather than silently
                # reporting one note fewer.
                lost += 1
                print("  *** a second bill exception arrived before LP 0x48 was read.")
                print("      §7.11 keeps only the most recent bill, so the previous")
                print("      note's country code is gone permanently.")
            pending_unread = True

            try:
                info = client.send_last_accepted_bill_information()
            except SASError as e:
                print(f"  {now:%H:%M:%S}  0x{code:02X} {describe_exception(code)}"
                      f"\n      LP 0x48 failed: {type(e).__name__}: {e}")
                time.sleep(args.interval)
                continue
            pending_unread = False

            note = {"at_utc": now.isoformat(timespec="seconds"),
                    "exception": f"0x{code:02X}",
                    "country_code": info.country_code,
                    "country": country(info.country_code),
                    "denomination_code": info.denomination_code,
                    "denomination": denomination(info.denomination_code),
                    "bill_meter_for_this_type": info.bill_meter}
            notes.append(note)
            print(f"  {now:%H:%M:%S}  0x{code:02X} {describe_exception(code)}")
            print(f"      country code {info.country_code:>3}  {note['country']}")
            print(f"      denom code   {info.denomination_code:>3}  {note['denomination']}")
            print(f"      this type has now been accepted {info.bill_meter:,} time(s)")
            if info.country_code not in COUNTRY_CODES:
                print("      ^ the validator is reporting a country SAS does not define.")
            print()

            if args.json:
                Path(args.json).write_text(json.dumps(
                    {"notes": notes, "lost_to_late_reads": lost,
                     "bill_meters_before": before}, indent=2) + "\n")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass

    after = read_bill_meters(client)
    print(f"\n{len(notes)} note(s) recorded, {lost} lost to a late LP 0x48 read.")
    print("  LP 0x1E after :", after if after else "not supported")
    if before and after:
        moved = {k: after[k] - before[k] for k in before if after[k] != before[k]}
        print("  counters that moved:", moved or "none")
        print("\nEvery note above landed in a counter chosen by FACE VALUE alone.")
        print("If more than one currency is accepted here, those counters cannot")
        print("be separated afterwards -- only the per-note country code above can,")
        print("and only while it is read in time (§7.11).")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"notes": notes, "lost_to_late_reads": lost,
             "bill_meters_before": before, "bill_meters_after": after}, indent=2) + "\n")
        print(f"\nwritten to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
