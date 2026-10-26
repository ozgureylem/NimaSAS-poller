"""The machine's own record of every ticket it printed and every ticket it
took, read back out of it.

Read-only, and non-destructively so: long poll 0x4D is walked by buffer INDEX
(0x01-0x1F), which §15.10 leaves the read/unread state alone. Function code
0x00 would return the next unread record and mark it read, which is how a
gateway drains the buffer in normal operation -- and exactly what you do not
want when the point is to look. Nothing here advances that pointer, so a
poller can be stopped, this run, and the poller restarted without it having
lost a record.

Two independent views, which is the value of it:

  THE RECORDS -- up to 31 buffer entries, each with its validation type, its
  validation number, amount, ticket number, date and time. Table 15.13c's
  types cover both directions: 00-04 are tickets the machine PRINTED, 80-82
  are tickets it REDEEMED, and 10-60 are handpay receipts. One poll per
  index, so one walk is 31 exchanges.

  THE METERS -- long poll 0x50 per validation type, giving the cumulative
  cents and count for each. The buffer holds 31 records; the meters hold
  everything since the last RAM clear. When the buffer has wrapped, the
  meters are the only complete account, and the difference between the two
  is how you know it wrapped.

The two should agree where the buffer has not yet wrapped, and that
agreement is worth checking: the records come from a ring buffer the machine
maintains and the meters from counters it maintains separately.

    python3 examples/ticket_history.py /dev/ttyS0 --address 1
    python3 examples/ticket_history.py /dev/ttyS0 --address 1 --json records.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.client import SASClient
from saspy.constants import ValidationType
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

# Table 15.13c, grouped by what the record actually is. The grouping is the
# point: "ticket history" means two opposite things and the type byte is the
# only thing that distinguishes them.
PRINTED = {0x00, 0x01, 0x02, 0x03, 0x04}
REDEEMED = {0x80, 0x81, 0x82}
HANDPAY = {0x10, 0x20, 0x40, 0x60}

DIRECTION = {}
DIRECTION.update({t: "printed" for t in PRINTED})
DIRECTION.update({t: "redeemed" for t in REDEEMED})
DIRECTION.update({t: "handpay" for t in HANDPAY})

BUFFER_INDICES = range(0x01, 0x20)   # 1-31, per §15.10

# Table 15.12c, the LP 0x71 machine status byte. Same tables as
# ticket_redeem.py and ticket_in_watch.py carry: one byte, one vocabulary,
# so a refusal reads the same whichever tool reports it. The top three bits
# give the category and the whole byte gives the reason.
REDEEM_STATUS = {
    0x00: "cashable ticket redeemed",
    0x01: "restricted promotional ticket redeemed",
    0x02: "nonrestricted promotional ticket redeemed",
    0x20: "waiting for long poll 71",
    0x40: "ticket redemption PENDING (not complete)",
    0x80: "ticket rejected by host, or unknown",
    0x81: "validation number does not match",
    0x82: "not a valid transfer function",
    0x83: "not a valid transfer amount (non-BCD)",
    0x84: "transfer amount exceeded the gaming machine credit limit",
    0x85: "transfer amount not an even multiple of the machine denomination",
    0x86: "transfer amount does not match ticket amount",
    0x87: "gaming machine unable to accept transfer at this time",
    0x88: "ticket rejected due to timeout",
    0x89: "ticket rejected due to comm link down",
    0x8A: "ticket redemption disabled",
    0x8B: "ticket rejected due to validator failure",
    0xC0: "not compatible with the current redemption cycle (ignored)",
    0xFF: "no validation information available",
}
REDEEM_CATEGORY = {0b000: "SUCCESS", 0b001: "waiting", 0b010: "pending",
                   0b100: "FAILED", 0b110: "incompatible",
                   0b111: "no information"}


def describe_redeem(status: int) -> str:
    text = REDEEM_STATUS.get(status, "unassigned code")
    return f"{REDEEM_CATEGORY.get(status >> 5, 'unknown category')}: {text}"


def type_name(value: int) -> str:
    try:
        return ValidationType(value).name.lower().replace("_", " ")
    except ValueError:
        return f"unassigned type 0x{value:02X}"


def is_empty(record) -> bool:
    """§15.10: every field is zero when the buffer position holds no record."""
    return (record.validation_type == 0 and record.validation_number == 0
            and record.amount_cents == 0 and record.ticket_number == 0
            and record.date.strip("0") == "" and record.time.strip("0") == "")


def fmt_validation(system_id: int, number: int) -> str:
    """The 18-digit printed form, when the parts really are 2 + 16.

    Table 15.10b carries the system ID and the number as separate fields, so
    the printed form is the one concatenated with the other. Blindly
    concatenating is still wrong when a machine puts all 18 digits in the
    number field -- then the system ID is already in there and prefixing it
    again produces a 20-digit number that matches no ticket. So the parts are
    shown as parts, and joined only when they fit.
    """
    digits = f"{number:d}"
    if len(digits) > 16:
        return f"{digits}  (18+ digits in the number field; system ID {system_id:02d} shown separately)"
    return f"{system_id:02d}-{number:016d}"


def fmt_when(record) -> str:
    """MMDDYYYY and HHMMSS as the machine stores them."""
    d, t = record.date, record.time
    if len(d) == 8 and len(t) == 6:
        return f"{d[2:4]}/{d[0:2]}/{d[4:8]} {t[0:2]}:{t[2:4]}:{t[4:6]}"
    return f"{d} {t}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--json", metavar="PATH",
                    help="also write the records and meters to a JSON file")
    ap.add_argument("--skip-meters", action="store_true",
                    help="skip the LP 0x50 sweep (12 extra exchanges)")
    args = ap.parse_args()

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(
        SASTransport(port, byte_timeout=args.timeout), args.address, timeout=args.timeout)

    print(f"\n=== {args.port}, address {args.address} ===")
    print("\nReading the validation buffer by index (LP 0x4D, 0x01-0x1F).")
    print("Non-destructive: the unread pointer is not touched, so a poller")
    print("draining this buffer loses nothing.\n")

    records: list[dict] = []
    silent = 0
    for index in BUFFER_INDICES:
        try:
            record = client.send_enhanced_validation_information(function_code=index)
        except SASError as e:
            silent += 1
            if silent == 1:
                print(f"  index {index:02X}: no answer ({type(e).__name__}). "
                      "Section 4.4 lets a machine ignore a long poll it does")
                print("  not support, so this may mean enhanced validation is off"
                      " rather than that")
                print("  the buffer is empty. Continuing.")
            continue
        if is_empty(record):
            continue
        records.append({
            "buffer_index": index,
            "validation_type": record.validation_type,
            "direction": DIRECTION.get(record.validation_type, "unknown"),
            "type_name": type_name(record.validation_type),
            "validation_number": record.validation_number,
            "validation_system_id": record.validation_system_id,
            "amount_cents": record.amount_cents,
            "ticket_number": record.ticket_number,
            "when": fmt_when(record),
            "expiration": record.expiration,
            "pool_id": record.pool_id,
        })

    if not records:
        print("  No records in the buffer."
              + (" Every index was silent, so this says nothing about whether"
                 " any ticket" if silent == len(BUFFER_INDICES) else ""))
        if silent == len(BUFFER_INDICES):
            print("  activity happened -- only that the machine does not answer"
                  " LP 0x4D.")
    else:
        for direction, label in (("printed", "TICKETS THIS MACHINE PRINTED"),
                                 ("redeemed", "TICKETS THIS MACHINE REDEEMED"),
                                 ("handpay", "HANDPAY RECEIPTS"),
                                 ("unknown", "UNRECOGNISED VALIDATION TYPES")):
            group = [r for r in records if r["direction"] == direction]
            if not group:
                continue
            print(f"  {label}")
            print(f"    {'idx':>3}  {'when':<19} {'amount':>12}  "
                  f"{'ticket':>6}  validation number")
            for r in group:
                printed = fmt_validation(r["validation_system_id"],
                                         r["validation_number"])
                print(f"    {r['buffer_index']:>3}  {r['when']:<19} "
                      f"{r['amount_cents'] / 100:>12,.2f}  "
                      f"{r['ticket_number']:>6}  {printed}")
                print(f"         {r['type_name']}")
            print()

    # The most recent redemption cycle, which is the ONLY place a refused
    # ticket survives. Section 15.12: "All fields in the response will be set
    # to the current status of the most recent ticket redemption cycle" -- so
    # LP 0x71 with transfer code FF reports a rejection's status, amount and
    # validation data, where the 0x4D buffer records only validated ones and
    # no meter counts a denial at all.
    #
    # It is exactly one cycle deep and is replaced by the next ticket's LP
    # 0x70, so it answers "what happened to the last ticket" and never "what
    # happened to the one before it".
    print("Most recent redemption cycle (LP 0x71, transfer code FF).")
    print("The only long poll that retains a REJECTED ticket -- and only the")
    print("most recent one; the next ticket's cycle replaces it.\n")
    try:
        last = client.redeem_ticket_status()
    except SASError as e:
        print(f"  no answer ({type(e).__name__}: {e})\n")
    else:
        status = last.machine_status
        category = REDEEM_CATEGORY.get(status >> 5)
        print(f"  machine status   0x{status:02X}  {describe_redeem(status)}")
        if status == 0xFF:
            print("  -> no redemption cycle to report. Either none has happened")
            print("     since the last RAM clear, or the machine has already")
            print("     been asked and has nothing further to say.")
        else:
            print(f"  amount           {last.amount_cents:,}"
                  f"  ({last.amount_cents / 100:,.2f})")
            print(f"  parsing code     0x{last.parsing_code:02X}")
            # BCD, so the hex digits ARE the decimal digits -- a 9-byte
            # field is the 18-digit printed number (§15.12b). Shown as digits
            # rather than as a hex blob so it can be read off a paper ticket,
            # and left as raw hex if it is not the expected length.
            raw = last.validation_data.hex()
            shown = raw if len(raw) != 18 else f"{raw}  (18 digits, as printed)"
            print(f"  validation data  {shown or '(none returned)'}")
            if category == "FAILED":
                print()
                print("  -> this ticket was REJECTED, and this is the only place")
                print("     that survives. No meter counts a refusal, and the 0x4D")
                print("     buffer above records only tickets that were accepted,")
                print("     so one cycle of this poll is the whole of the SAS-side")
                print("     evidence. The machine's own event log keeps its own")
                print("     record ('Voucher In Rejected'), which is not SAS.")
        print()

    meters: list[dict] = []
    if not args.skip_meters:
        print("Validation meters (LP 0x50), cumulative since the last RAM clear.")
        print("The buffer holds 31 records; these hold everything. Where they")
        print("disagree with the records above, the buffer has wrapped.\n")
        print(f"  {'type':>5}  {'cents':>14}  {'count':>8}  what")
        for validation_type in ValidationType:
            try:
                result = client.send_validation_meters(int(validation_type))
            except SASError:
                continue
            if not result.cumulative_amount_cents and not result.total_validations:
                continue
            meters.append({
                "validation_type": int(validation_type),
                "type_name": type_name(int(validation_type)),
                "direction": DIRECTION.get(int(validation_type), "unknown"),
                "cumulative_amount_cents": result.cumulative_amount_cents,
                "total_validations": result.total_validations,
            })
            print(f"  0x{int(validation_type):02X}  {result.cumulative_amount_cents / 100:>14,.2f}"
                  f"  {result.total_validations:>8,}  {type_name(int(validation_type))}")
        if not meters:
            print("  every validation meter is zero or unanswered.")
        print()

        # Cross-check the two views against each other, per direction.
        for direction, label in (("printed", "printed"), ("redeemed", "redeemed")):
            from_records = sum(r["amount_cents"] for r in records
                               if r["direction"] == direction)
            from_meters = sum(m["cumulative_amount_cents"] for m in meters
                              if m["direction"] == direction)
            if not from_records and not from_meters:
                continue
            agree = from_records == from_meters
            print(f"  {label}: buffer {from_records / 100:,.2f} vs meters "
                  f"{from_meters / 100:,.2f}"
                  + ("  agree" if agree else "  DIFFER -- the buffer has very"
                     " likely wrapped, and the meters are the complete account"))

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"records": records, "validation_meters": meters}, indent=2) + "\n")
        print(f"\nwrote {args.json}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
