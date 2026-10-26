"""Act as the host for a ticket redemption: authorise it, choosing the type
and the amount.

THIS CREATES CREDITS ON THE MACHINE. It is the ticket-in counterpart of
aft_push_credits.py: long poll 0x71 with a transfer code of 00, 01 or 02
tells the machine the ticket is good and for how much, and the machine
stacks it and credits the player. Use a lab machine.

What a CMS does in production, this does by hand, which is the point: it
exercises the gateway's half of section 15.11/15.12 end to end -- 0x67, LP
0x70, LP 0x71, exception 0x68, LP 0x71/FF -- against a real validator.

ARMED IN ADVANCE, deliberately. The spec gives the host ten seconds from
insertion to send LP 0x70 and thirty to send LP 0x71, after which the machine
returns the ticket. A tool that asked "what type? how much?" once the ticket
was in escrow would spend that budget on typing. So the decision is made on
the command line, confirmed before the watch starts, and applied the instant
a ticket appears.

    python3 examples/ticket_redeem.py /dev/ttyS0 --address 1 --as cashable --amount 25.00
    python3 examples/ticket_redeem.py /dev/ttyS0 --address 1 --as nonrestricted --amount 10.00
    python3 examples/ticket_redeem.py /dev/ttyS0 --address 1 --as restricted --amount 5.00 --pool-id 1

On the amount: Table 15.11a's escrow amount is "all zeros if no amount
available", which is the normal case for system validation -- the barcode
carries a validation number, not a value, and the host is what knows the
amount. When the machine DOES report an amount, this uses that instead of
--amount and says so, because a mismatch is status 86 and a returned ticket.
--force-amount overrides that, which is how you provoke an 86 on purpose.

The ticket's own printed value and date are irrelevant to this: the machine
reads the barcode for a validation number, and everything else about what the
ticket is worth comes from here.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.bcd import decode_bcd
from saspy.client import SASClient
from saspy.constants import GENERAL_POLL_EXCEPTIONS
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

# A ticket that the validator never recognises AS a ticket comes back as this,
# not as a ticket rejection. Exception 0x2B is "bill rejected": the paper went
# in, was processed down the BANKNOTE path, failed banknote validation, and
# was returned. Section 15.11 only issues 0x67 for a ticket "in an acceptable
# condition with a machine readable validation number", so a 0x2B with no 0x67
# means the barcode was never read at all -- the host is not involved and no
# amount of host-side configuration changes it.
BILL_REJECTED = 0x2B
TICKET_INSERTED = 0x67
TICKET_TRANSFER_COMPLETE = 0x68

# Table 15.12c, the three that authorise. The 80-88 codes reject with a
# reason and are reachable through --deny.
TRANSFER_CODES = {"cashable": 0x00, "restricted": 0x01, "nonrestricted": 0x02}
DENY_CODES = {
    "other": 0x80, "bad-number": 0x81, "not-in-system": 0x82, "pending": 0x83,
    "already-redeemed": 0x84, "expired": 0x85, "no-info": 0x86,
    "amount-mismatch": 0x87, "over-limit": 0x88,
}

# Table 15.12d. The top three bits give the category, so an unlisted code
# still lands in the right bucket rather than reading as "unknown".
STATUS = {
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
CATEGORY = {0b000: "SUCCESS", 0b001: "waiting", 0b010: "pending",
            0b100: "FAILED", 0b110: "incompatible", 0b111: "no information"}


def describe(status: int) -> str:
    text = STATUS.get(status, "unassigned code")
    return f"{CATEGORY.get(status >> 5, 'unknown category')}: {text}"


WATCH_METERS = [
    (0x15, "total ticket in (credits)"),
    (0x28, "total cashable ticket in (credits)"),
    (0x29, "total regular cashable ticket in (credits)"),
    (0x2A, "total restricted promotional ticket in (credits)"),
    (0x2B, "total nonrestricted promotional ticket in (credits)"),
    (0x11, "cashable ticket in (quantity)"),
    (0x0C, "current credits"),
    (0x1B, "current restricted credits"),
]


VALIDATION_STYLE = {0: "standard or none", 1: "system", 2: "secure enhanced",
                    3: "reserved"}


def ticket_preflight(client) -> bool:
    """LP 0xA0 bit 7: is ticket redemption enabled at all?

    Worth asking before waiting for a 0x67 that cannot arrive. Section 15.11:
    a machine not configured for ticket redemption "will never issue exception
    67, and will ignore long poll 70"; section 15.12 says the same of 0x71. So
    a disabled machine is indistinguishable from a patient one -- it simply
    never answers, and on the lab cabinet the validator took the ticket,
    failed to recognise it, and reported 0x2B BILL rejected instead.
    """
    try:
        features = client.send_enabled_features()
    except SASError as e:
        print(f"  LP 0xA0 did not answer ({type(e).__name__}: {e}), so whether")
        print("  ticket redemption is enabled is unknown. Carrying on.")
        return True

    style = VALIDATION_STYLE.get(features.validation_style, "unknown")
    print(f"  ticket redemption      {features.ticket_redemption}")
    print(f"  validation style       {style}")
    print(f"  validation extensions  {features.validation_extensions}")
    if features.ticket_redemption:
        return True

    print()
    print("  Ticket redemption is DISABLED on this machine (LP 0xA0 bit 7).")
    print("  Section 15.11: such a machine never issues exception 67 and ignores")
    print("  long poll 70; section 15.12 says it ignores 0x71 too. Inserting a")
    print("  ticket will get it read as a bill and rejected (exception 0x2B),")
    print("  which is not a fault in the ticket or the barcode.")
    print("  Enable ticket/voucher acceptance in the operator menu first.")
    return False


def explain_bill_rejected(count: int) -> None:
    """Said once, on the first 0x2B, so a repeated insert does not repeat the
    essay."""
    if count != 1:
        return
    print("    -> This is the BANKNOTE path, not the ticket path. The validator")
    print("       took the paper, tried to read it as a note, and returned it;")
    print("       section 15.11 would have issued 0x67 instead if it had read a")
    print("       machine-readable validation number. Nothing host-side can")
    print("       change that. Worth checking, in order:")
    print("         1. Does a real BANKNOTE get accepted? That separates a")
    print("            validator that is not working from one that is working")
    print("            but not reading barcodes.")
    print("         2. Is barcode/ticket reading enabled in the VALIDATOR's own")
    print("            configuration? It is separate from the machine's SAS")
    print("            Voucher in setting, which LP 0xA0 already reports as on.")
    print("         3. Do these tickets match the validator's expected ticket")
    print("            length and width? A dimension mismatch is rejected before")
    print("            anything is read.")


def read_meters(client) -> dict[int, int | None]:
    codes = [c for c, _ in WATCH_METERS]
    try:
        got = client.send_extended_meters(codes).meters
    except SASError as e:
        print(f"  (meters unreadable: {type(e).__name__}: {e})", file=sys.stderr)
        return {c: None for c in codes}
    return {c: got.get(c) for c in codes}


def show_changes(before: dict, after: dict) -> bool:
    moved = False
    for code, label in WATCH_METERS:
        was, now = before.get(code), after.get(code)
        if was is None or now is None or was == now:
            continue
        moved = True
        print(f"    {code:04X}  {label:<44} {was:>10,} -> {now:>10,}  {now - was:+,}")
    if not moved:
        print("    nothing moved.")
    return moved


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--as", dest="kind", choices=sorted(TRANSFER_CODES),
                    help="what to redeem the ticket AS (Table 15.12c)")
    ap.add_argument("--amount", type=float, default=0.0,
                    help="value to credit, in currency units, used when the "
                         "machine reports no amount of its own")
    ap.add_argument("--force-amount", action="store_true",
                    help="use --amount even when the machine reports its own "
                         "escrow amount. Expect status 86.")
    ap.add_argument("--pool-id", type=int, default=0,
                    help="restricted pool ID (only read for --as restricted)")
    ap.add_argument("--expiration", type=int, default=0,
                    help="restricted expiration, MMDDYYYY or 0000NNNN days")
    ap.add_argument("--deny", choices=sorted(DENY_CODES),
                    help="reject the ticket with this reason instead of "
                         "authorising it (Table 15.12c codes 80-88)")
    ap.add_argument("--interval", type=float, default=0.2)
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = ap.parse_args()

    if bool(args.kind) == bool(args.deny):
        print("Choose exactly one of --as (authorise) or --deny (reject).",
              file=sys.stderr)
        return 2

    code = DENY_CODES[args.deny] if args.deny else TRANSFER_CODES[args.kind]
    wanted_cents = int(round(args.amount * 100))

    if args.kind and not wanted_cents and not args.force_amount:
        print("No --amount given. That is fine only if the machine reports an")
        print("escrow amount of its own; otherwise the transfer is for zero.")

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(
        SASTransport(port, byte_timeout=args.timeout), args.address, timeout=args.timeout)

    print(f"\n{args.port}, address {args.address}")
    if args.deny:
        print(f"\nArmed to REJECT the next ticket: transfer code 0x{code:02X}"
              f" ({args.deny})")
        print("Nothing is credited. The machine returns the ticket.")
    else:
        print(f"\nArmed to REDEEM the next ticket AS {args.kind.upper()}"
              f" (transfer code 0x{code:02X})")
        print(f"  amount        {wanted_cents:,} cents ({wanted_cents / 100:,.2f})")
        if args.kind == "restricted":
            print(f"  pool id       {args.pool_id}")
            print(f"  expiration    {args.expiration}")
        print("\n  THIS CREATES CREDITS ON THE MACHINE, and moves the ticket-in")
        print("  meters permanently. A meter cannot be un-rung without an RAM clear.")

    print("\nMachine configuration (LP 0xA0):")
    enabled = ticket_preflight(client)
    if not enabled:
        print("\n  Arming anyway would wait for an exception that cannot arrive.")
        return 1

    before = read_meters(client)
    print("\nTicket meters now:")
    for code_m, label in WATCH_METERS:
        value = before.get(code_m)
        print(f"    {code_m:04X}  {label:<44} {'n/a' if value is None else f'{value:,}'}")

    if not args.yes:
        try:
            if input("\nType GO to arm, anything else to abort: ").strip() != "GO":
                print("Aborted; nothing sent.")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\nAborted; nothing sent.")
            return 1

    print("\nInsert a ticket. Ctrl-C to stop.")
    print("(10s from insertion to LP 0x70, 30s to LP 0x71, per section 15.12.)\n")

    pending = False
    rejected = 0
    try:
        while True:
            try:
                exception = client.general_poll()
            except SASError as e:
                print(f"  (poll failed: {type(e).__name__}: {e})", file=sys.stderr)
                time.sleep(args.interval)
                continue

            if not exception:
                time.sleep(args.interval)
                continue

            stamp = time.strftime("%H:%M:%S")
            name = GENERAL_POLL_EXCEPTIONS.get(exception, "unknown exception")
            print(f"[{stamp}] 0x{exception:02X} {name}")

            if exception == BILL_REJECTED:
                rejected += 1
                explain_bill_rejected(rejected)
                time.sleep(args.interval)
                continue

            if exception == TICKET_INSERTED:
                started = time.monotonic()
                try:
                    data = client.send_ticket_validation_data()
                except SASError as e:
                    print(f"    LP 0x70 failed: {type(e).__name__}: {e}")
                    continue
                print(f"    escrow           {data.ticket_in_escrow}")
                print(f"    machine's amount {data.amount_cents:,}")
                print(f"    parsing code     0x{data.parsing_code:02X}")
                print(f"    validation data  {data.validation_data.hex()}")

                if not data.ticket_in_escrow:
                    print("    -> no ticket in escrow (status FF); nothing to redeem.")
                    continue
                if data.parsing_code != 0x00 or len(data.validation_data) != 9:
                    print("    -> parsing code is not Table 15.11b's 00 with 9 BCD bytes.")
                    print("       This tool only re-encodes that form; not authorising.")
                    continue

                # Status 81 is "validation number does not match (response must
                # include correct validation number)", so echo back exactly what
                # 0x70 reported rather than anything printed on the ticket.
                validation_number = decode_bcd(data.validation_data)

                amount = wanted_cents
                if data.amount_cents and not args.force_amount:
                    if wanted_cents and wanted_cents != data.amount_cents:
                        print(f"    -> the machine reports {data.amount_cents:,} and")
                        print(f"       --amount is {wanted_cents:,}. Using the machine's:")
                        print("       a mismatch is status 86 and a returned ticket.")
                        print("       --force-amount to send yours anyway.")
                    amount = data.amount_cents

                try:
                    result = client.redeem_ticket(
                        transfer_code=code,
                        transfer_amount_cents=amount,
                        validation_number=validation_number,
                        restricted_expiration=(args.expiration
                                               if args.kind == "restricted" else 0),
                        pool_id=args.pool_id if args.kind == "restricted" else 0,
                        parsing_code=data.parsing_code,
                    )
                except SASError as e:
                    print(f"    LP 0x71 failed: {type(e).__name__}: {e}")
                    continue
                elapsed = time.monotonic() - started
                print(f"    LP 0x71 sent {amount:,} cents, answered in {elapsed:.2f}s")
                print(f"    status 0x{result.machine_status:02X}"
                      f"  {describe(result.machine_status)}")
                pending = result.machine_status == 0x40
                if pending:
                    print("    -> pending: the ticket is not redeemed until it is")
                    print("       irretrievably stacked. Waiting for 0x68.")
                else:
                    print("\n  meters now:")
                    after = read_meters(client)
                    show_changes(before, after)
                    before = after
                    print()

            elif exception == TICKET_TRANSFER_COMPLETE:
                # Reissued every 15s until the host reads the completion
                # status, so this read is what ends the cycle.
                try:
                    final = client.redeem_ticket_status()
                except SASError as e:
                    print(f"    LP 0x71/FF failed: {type(e).__name__}: {e}")
                    continue
                print(f"    final  0x{final.machine_status:02X}"
                      f"  {describe(final.machine_status)}")
                print(f"    amount {final.amount_cents:,}")
                pending = False
                print("\n  meters now:")
                after = read_meters(client)
                show_changes(before, after)
                before = after
                print()

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
        if pending:
            print("A redemption was still PENDING. The machine reissues 0x68 every")
            print("15 seconds until a host reads the completion status, so re-run")
            print("this (or ticket_in_watch.py) to clear it.")

    print("\nTicket meters at exit:")
    show_changes(before, read_meters(client))
    return 0


if __name__ == "__main__":
    sys.exit(main())
