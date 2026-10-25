"""Print the machine's accounting figures as SAS reports them, grouped the
way the machine's own Machine Accounting screen groups them.

Read-only. Meter polls plus one query-only 0x74 (lock code 0xFF, which asks
for status without touching the lock); nothing changes machine state.

Comparing the gateway's numbers against the machine's own screen has been
this project's one validation that does not come from inside its own code.
Until now that comparison was done by eye, across two screens, with the SAS
side spread over Table C-7 codes that do not resemble the screen's labels.
This does the mapping, and recomputes the screen's subtotals from the SAS
meters so they can be checked rather than trusted.

The mapping that is not obvious -- confirmed on an EGT Premier by pushing
and cashing out each amount type and watching which line moved:

    machine's screen label          SAS / Table C-7
    "Cashable Electronic In"        002E  electronic regular cashable to GM
    "Promotional Electronic In"     0030  electronic NONRESTRICTED to GM
    "Non-cashable Electronic In"    002F  electronic RESTRICTED to GM
    "Cashable Electronic Out"       0032  electronic regular cashable to host
    "Promotional Electronic Out"    0034  electronic NONRESTRICTED to host
    "Non-cashable Electronic Out"   0033  electronic RESTRICTED to host

So the screen's "non-cashable" is SAS's RESTRICTED, and the screen's
"promotional" is SAS's NONRESTRICTED. Reading those the other way round --
which the words invite -- swaps the two promotional categories.

    python3 examples/accounting_summary.py /dev/ttyS0 --address 1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.client import SASClient
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

# (screen label, Table C-7 code, indent). Subtotals are computed, not read,
# so a mismatch against the machine's own screen is visible rather than
# hidden behind agreement on the components.
IN_LINES = [
    # 0x0B, total credits from bills ACCEPTED -- not 0x58, total credits from
    # bills TO DROP, which is where accepted bills end up afterwards and which
    # a cashless machine with no drop box does not report at all. The first
    # version used 0x58 and printed "n/a" against a screen showing 694.00.
    ("Bill In", 0x0B, 1),
    ("Cashless In", None, 1),            # computed
    ("Cashable Electronic In", 0x2E, 2),
    ("Promotional Electronic In", 0x30, 2),
    ("Non-cashable Electronic In", 0x2F, 2),
]
OUT_LINES = [
    ("Cancelled Credits Paid by Attendant", 0x03, 1),
    ("Cashless Out", None, 1),           # computed
    ("Cashable Electronic Out", 0x32, 2),
    ("Promotional Electronic Out", 0x34, 2),
    ("Non-cashable Electronic Out", 0x33, 2),
]
CORE = [
    ("Total Bet (Coin In)", 0x00),
    ("Total credits from bills to drop", 0x58),
    # 0x01 is total coin OUT. Table C-7 keeps "total won" for 0x22, which it
    # defines as "sum of total coin out and total jackpot" -- so a screen line
    # labelled Total Won is 0x22, and printing 0x01 under that label understates
    # it by every jackpot the machine has paid. Same for hand paid: 0x03 is the
    # cancelled-credit part only, 0x23 is "sum of total hand paid cancelled
    # credits and total jackpot".
    ("Total Coin Out", 0x01),
    ("Total Won", 0x22),
    ("Total Hand Paid", 0x23),
    ("Total Cancelled Credits", 0x04),
    ("Total Drop", 0x24),
    ("Total Jackpot", 0x02),
]
# The screen's breakdown under Total Bet. These are what make a promotional
# balance explicable: a machine holding 7.00 restricted cashed out 6.60 of it
# because 0.40 had been wagered, and 0019 is where that 0.40 is. Cashable
# played has no meter of its own -- the screen's "Total Cashable Played" is
# total bet minus the other two.
PLAYED = [
    ("Non-cashable Played (SAS RESTRICTED)", 0x19),
    ("Promotional Played (SAS NONrestricted)", 0x1A),
]

# Each promotional type is a closed ledger: everything that came in either went
# back out or was wagered. These close to the cent on a healthy machine, and
# are the one check here that needs no second screen to read.
LEDGERS = [
    ("Non-cashable (SAS RESTRICTED)", 0x2F, 0x33, 0x19),
    ("Promotional  (SAS NONrestricted)", 0x30, 0x34, 0x1A),
]

CURRENT = [
    ("Current credits", 0x0C),
    ("Current restricted credits", 0x1B),
]


def read(client, codes: list[int]) -> dict[int, int | None]:
    """Read in batches: LP 0x6F takes 12 codes per exchange."""
    out: dict[int, int | None] = {}
    for i in range(0, len(codes), 12):
        chunk = codes[i:i + 12]
        try:
            result = client.send_extended_meters(chunk)
        except SASError as e:
            print(f"  (chunk {i // 12 + 1} failed: {type(e).__name__}: {e})", file=sys.stderr)
            out.update({c: None for c in chunk})
            continue
        out.update({c: result.meters.get(c) for c in chunk})
    return out


def money(value: int | None) -> str:
    return "       n/a" if value is None else f"{value / 100:>10,.2f}"


def subtotal(values: dict, codes: tuple[int, ...]) -> tuple[int | None, list[int]]:
    """Sum the components, and name any that did not answer.

    A subtotal silently reported as n/a hides WHICH meter is missing, and the
    answer is usually that the machine does not implement it -- which is a
    fact about the machine worth printing, not an error.
    """
    missing = [c for c in codes if values.get(c) is None]
    if missing:
        return None, missing
    return sum(values[c] for c in codes), []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=1.0)
    args = ap.parse_args()

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(
        SASTransport(port, byte_timeout=args.timeout), args.address, timeout=args.timeout
    )

    wanted = [c for _l, c, _i in IN_LINES + OUT_LINES if c is not None]
    wanted += [c for _l, c in CORE + CURRENT + PLAYED]
    wanted += [0x7F]                       # weighted average theoretical payback
    wanted = list(dict.fromkeys(wanted))   # de-duplicate, keep order
    values = read(client, wanted)

    print(f"\n=== {args.port}, address {args.address} ===\n")

    bill_in = values.get(0x0B)
    print("Total In")
    print(f"    {'Bill In':<38} {money(bill_in)}   (C-7 000B)")
    sub_in, missing_in = subtotal(values, (0x2E, 0x30, 0x2F))
    print(f"    {'Cashless In  [computed]':<38} {money(sub_in)}"
          + (f"   missing {' '.join(f'{c:04X}' for c in missing_in)}" if missing_in else ""))
    for label, code, _ in IN_LINES[2:]:
        print(f"      {label:<36} {money(values.get(code))}   (C-7 {code:04X})")
    total_in = sub_in + bill_in if (sub_in is not None and bill_in is not None) else None
    print(f"  {'TOTAL IN  [computed]':<40} {money(total_in)}"
          + ("   (Bill In did not answer)" if bill_in is None else ""))
    native_in = values.get(0x24)
    print(f"  {'  vs 0024 total drop [native]':<40} {money(native_in)}"
          + ("   same" if native_in is not None and native_in == total_in else ""))

    attendant = values.get(0x03)
    print("\nTotal Out")
    print(f"    {'Cancelled Credits Paid by Attendant':<38} {money(attendant)}   (C-7 0003)")
    sub_out, missing_out = subtotal(values, (0x32, 0x34, 0x33))
    print(f"    {'Cashless Out  [computed]':<38} {money(sub_out)}"
          + (f"   missing {' '.join(f'{c:04X}' for c in missing_out)}" if missing_out else ""))
    for label, code, _ in OUT_LINES[2:]:
        print(f"      {label:<36} {money(values.get(code))}   (C-7 {code:04X})")
    total_out = None
    if sub_out is not None and attendant is not None:
        total_out = sub_out + attendant
    print(f"  {'TOTAL OUT  [computed]':<40} {money(total_out)}")
    native_out = values.get(0x04)
    print(f"  {'  vs 0004 cancelled credits [native]':<40} {money(native_out)}"
          + ("   same" if native_out is not None and native_out == total_out else ""))

    print("\nCore meters")
    for label, code in CORE:
        print(f"    {label:<38} {money(values.get(code))}   (C-7 {code:04X})")
    for label, code in PLAYED:
        print(f"    {label:<38} {money(values.get(code))}   (C-7 {code:04X})")
    trtp = values.get(0x7F)
    print(f"    {'Weighted Average TRTP %':<38} "
          + ("       n/a" if trtp is None else f"{trtp / 100:>9,.2f}%")
          + "   (C-7 007F)")

    print("\nPromotional ledgers  (in = out + played)")
    for label, in_code, out_code, played_code in LEDGERS:
        got_in, got_out = values.get(in_code), values.get(out_code)
        played = values.get(played_code)
        print(f"    {label:<38} {money(got_in)} in")
        print(f"      {'':<36} {money(got_out)} out + {money(played).strip()} played")
        if None in (got_in, got_out, played):
            print("      -> a component is not reported, so this ledger cannot be closed.")
        elif got_in == got_out + played:
            print("      -> closes exactly.")
        else:
            short = got_in - got_out - played
            print(f"      -> does NOT close: {money(short).strip()} unaccounted."
                  " Credit still on the machine")
            print("         shows up here too, so compare against the balances below"
                  " before")
            print("         treating this as a fault.")

    print("\nOn the machine now")
    for label, code in CURRENT:
        print(f"    {label:<38} {money(values.get(code))}   (C-7 {code:04X})")

    # Table C-7 has no code for the NONRESTRICTED balance -- it stops at 000C
    # and 001B, and 000C - 001B is cashable and nonrestricted together, so no
    # subtraction recovers it. 000C itself is the WHOLE balance on the EGT
    # Premier (it read 57.00 against a screen showing 57.00, and 60.00 after a
    # 3.00 nonrestricted push), so the figure above already matches the screen;
    # what the meters cannot do is split the third amount out of it. LP 0x74
    # reports all three as separate native fields in one poll. See
    # examples/meter_crosscheck.py for the comparison this is drawn from.
    try:
        lock = client.aft_game_lock_and_status_request(lock_code=0xFF)
    except SASError as e:
        print(f"    (0x74 did not answer: {type(e).__name__}: {e} -- so the")
        print("     nonrestricted balance has no source and the figures above")
        print("     cannot be expected to match the screen's credit display)")
    else:
        three = (lock.current_cashable_amount, lock.current_restricted_amount,
                 lock.current_non_restricted_amount)
        print("\n    From LP 0x74, the three live balances natively:")
        for label, amount in zip(("cashable", "restricted", "nonrestricted"), three):
            print(f"      {label:<36} {money(amount)}")
        print(f"      {'TOTAL  [computed]':<36} {money(sum(three))}"
              "   <- the screen's credit display")

    print("\n" + "=" * 68)
    print("Compare against the machine's Machine Accounting screen. The two")
    print("subtotals above are COMPUTED from the SAS meters, not read, so if")
    print("they match the screen's own Cashless In/Out the mapping is right")
    print("as well as the individual figures.")
    print("\nReminder on the two promotional lines, which the wording inverts:")
    print('  the screen\'s "Non-cashable Electronic" is SAS RESTRICTED (002F/0033)')
    print('  the screen\'s "Promotional Electronic"  is SAS NONRESTRICTED (0030/0034)')
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
