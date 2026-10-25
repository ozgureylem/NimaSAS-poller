"""Put cashable credits on a machine by AFT, with the checks done first.

THIS MOVES MONEY. Long poll 0x72 is the one command in this library that
does, and until this tool was written it had never run against hardware.
Use a lab machine. Read POLL_TEST_TOOL_WARNING.md.

It sends an in-house transfer from host to gaming machine: Table 8.3d
transfer type 00, Table 8.3a transfer code 00 (full transfer only, so the
machine either moves the whole amount or none of it -- no partial surprise).
Registration is not required: Table 8.3a says "Registration key (0 =
registration not required)", and section 8.1 confirms "a POS ID is never
required for in-house transfers".

Before sending it checks the things that otherwise turn into a status code
you have to go and look up:

  - AFT enabled and in-house transfers enabled (0x74 AFT status bits 7, 4)
  - transfer TO gaming machine currently available (available transfers
    bit 0) -- all-zero here usually means the machine is in its operator
    menu or has a door open, per section 8.3, and that becomes status 87
  - the amount is within the machine's transfer limit (status 84)
  - the asset number read from the machine is used, not guessed (status 93)
  - a transaction ID unique per run (status 81 is "same as last successful
    transfer logged in history")

Afterwards it reads the meters again and shows the deltas, because the
status byte says what the machine decided and the meters say what actually
happened. Table C-7 meters 00A0/00A1 count in-house cashable transfers to
the gaming machine specifically, so a successful push has to show there.

    python3 examples/aft_push_credits.py /dev/ttyS0 --address 1 --amount 20.00
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.client import SASClient
from saspy.constants import (
    AFT_TRANSFER_CODE_FULL_ONLY,
    AftTransferType,
    MeterCode,
    describe_aft_transfer_status,
)
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

# Table C-7: the meters an in-house push to the machine must move. All three
# amount types are read every time, so a transfer of one kind proves the other
# two did NOT move -- which is half of what the test is for.
VERIFY_METERS = (
    (MeterCode.CURRENT_CREDITS, "current credits"),
    (MeterCode.CURRENT_RESTRICTED_CREDITS, "current RESTRICTED credits"),
    (0x00A0, "cashable transfers to GM (cents)"),
    (0x00A1, "cashable transfers to GM (quantity)"),
    (0x00A2, "RESTRICTED transfers to GM (cents)"),
    (0x00A3, "RESTRICTED transfers to GM (quantity)"),
    (0x00A4, "NONRESTRICTED transfers to GM (cents)"),
    (0x00A5, "NONRESTRICTED transfers to GM (quantity)"),
    (MeterCode.TOTAL_RESTRICTED_AMOUNT_PLAYED_CREDITS, "restricted amount played"),
    (MeterCode.TOTAL_NONRESTRICTED_AMOUNT_PLAYED_CREDITS, "nonrestricted amount played"),
)


def read_meters(client):
    out = {}
    for code, label in VERIFY_METERS:
        try:
            result = client.send_extended_meters([int(code)])
            out[int(code)] = (label, result.meters.get(int(code)))
        except SASError as e:
            out[int(code)] = (label, f"({type(e).__name__})")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--amount", type=float, default=None,
                    help="CASHABLE amount to transfer, in currency units (e.g. 20.00)")
    ap.add_argument("--restricted", type=float, default=0.0,
                    help="RESTRICTED promotional amount: not redeemable for cash and must be "
                         "wagered (section 8). Needs --pool-id, and is played BEFORE any "
                         "cashable credits already on the machine.")
    ap.add_argument("--nonrestricted", type=float, default=0.0,
                    help="NONRESTRICTED promotional amount: promotional, but becomes ordinary "
                         "cashable if cashed out by a means that cannot preserve the status.")
    ap.add_argument("--pool-id", type=int, default=1,
                    help="restricted pool ID (default 1). Ignored unless a restricted amount is "
                         "included. A machine already holding restricted credits from a "
                         "DIFFERENT pool rejects the transfer with status 8F.")
    ap.add_argument("--expiration", type=int, default=0,
                    help="restricted expiration, MMDDYYYY or 0000NNNN days (default 0 = none). "
                         "Per section 8 a machine must NOT refuse a transfer because the date "
                         "has already passed -- it only governs cashing out later.")
    ap.add_argument("--transaction-id", default=None,
                    help="ASCII, 1-20 chars; defaults to a timestamp, which keeps it "
                         "unique (status 81 rejects a repeat of the last successful one)")
    ap.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    ap.add_argument("--acknowledge-only", action="store_true",
                    help="send nothing but the interrogation poll (0x72 transfer code FF) "
                         "to acknowledge an already-completed transfer and stop a machine "
                         "reissuing exception 0x69 every 15 seconds. Moves no money.")
    args = ap.parse_args()

    if args.acknowledge_only:
        return acknowledge_only(args)

    cents = int(round((args.amount or 0) * 100))
    restricted = int(round(args.restricted * 100))
    nonrestricted = int(round(args.nonrestricted * 100))
    if cents + restricted + nonrestricted <= 0:
        print("Give at least one of --amount, --restricted or --nonrestricted "
              "(or use --acknowledge-only).", file=sys.stderr)
        return 2
    if min(cents, restricted, nonrestricted) < 0:
        print("Amounts cannot be negative.", file=sys.stderr)
        return 2

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(
        SASTransport(port, byte_timeout=args.timeout), args.address, timeout=args.timeout
    )

    # --- pre-flight -----------------------------------------------------
    print("Pre-flight (0x74, query only -- lock untouched):")
    try:
        lock = client.aft_game_lock_and_status_request(lock_code=0xFF)
    except SASError as e:
        print(f"  0x74 failed: {type(e).__name__}: {e}", file=sys.stderr)
        print("  Cannot establish the machine's state. Not sending a transfer.", file=sys.stderr)
        return 1

    aft_on = bool(lock.aft_status & 0x80)
    in_house = bool(lock.aft_status & 0x10)
    to_machine = bool(lock.available_transfers & 0x01)
    limit = lock.gaming_machine_transfer_limit

    print(f"  asset number           {lock.asset_number}")
    print(f"  AFT enabled            {aft_on}")
    print(f"  in-house enabled       {in_house}")
    print(f"  transfer to GM now OK  {to_machine}")
    print(f"  transfer limit         {limit / 100:,.2f}")
    print(f"  credits on machine     {lock.current_cashable_amount / 100:,.2f}")

    blockers = []
    if not aft_on:
        blockers.append("AFT is disabled on the machine (0x74 AFT status bit 7)")
    if not in_house:
        blockers.append("in-house transfers are disabled (AFT status bit 4)")
    if not to_machine:
        blockers.append(
            "transfer to gaming machine is not currently available (available "
            "transfers bit 0). Section 8.3: door open, operator menu, tilt, "
            "disabled or a cashout in progress all do this. Sending anyway "
            "returns status 87"
        )
    if lock.asset_number == 0:
        blockers.append("asset number is zero, which returns status 93")
    total = cents + restricted + nonrestricted
    if limit and total > limit:
        blockers.append(f"{total / 100:,.2f} exceeds the transfer limit "
                        f"{limit / 100:,.2f}, which returns status 84")
    if restricted and lock.current_restricted_amount and lock.restricted_pool_id != args.pool_id:
        # Section 15: "If the gaming machine currently has restricted amounts,
        # it may not accept restricted amounts from a different pool."
        blockers.append(
            f"the machine already holds {lock.current_restricted_amount / 100:,.2f} "
            f"restricted from pool {lock.restricted_pool_id}, and this transfer names pool "
            f"{args.pool_id}. A different pool returns status 8F -- pass "
            f"--pool-id {lock.restricted_pool_id}, or play off the existing balance first"
        )

    if blockers:
        print("\nNot sending. Resolve these first:")
        for b in blockers:
            print(f"  - {b}")
        return 1

    transaction_id = args.transaction_id or f"NIMA{int(time.time())}"
    if not 1 <= len(transaction_id) <= 20:
        print("Transaction ID must be 1-20 ASCII characters.", file=sys.stderr)
        return 2

    parts = []
    if cents:
        parts.append(f"{cents / 100:,.2f} cashable")
    if restricted:
        parts.append(f"{restricted / 100:,.2f} RESTRICTED (pool {args.pool_id})")
    if nonrestricted:
        parts.append(f"{nonrestricted / 100:,.2f} NONRESTRICTED")
    print(f"\nAbout to transfer {' + '.join(parts)} to address {args.address}")
    if restricted or nonrestricted:
        # Worth stating before the confirmation, because it decides what the
        # meters will show next and surprises people who expect FIFO.
        print("  Note the play order (section 8): restricted is wagered first, then")
        print("  nonrestricted, then cashable. So promotional credits are consumed")
        print(f"  before the {lock.current_cashable_amount / 100:,.2f} cashable already on the machine.")
    if restricted:
        print("  Restricted credits are NOT redeemable for cash and must be wagered;")
        print("  they can only leave by a means that preserves that status.")
    print(f"  transfer type  0x{AftTransferType.IN_HOUSE_TO_GAMING_MACHINE:02X} "
          f"(in-house, host -> gaming machine)")
    print(f"  transfer code  0x{AFT_TRANSFER_CODE_FULL_ONLY:02X} (full transfer only)")
    print(f"  transaction ID {transaction_id}")
    if not args.yes:
        if input("\nType TRANSFER to proceed: ").strip() != "TRANSFER":
            print("Aborted. Nothing sent.")
            return 0

    before = read_meters(client)

    # --- the transfer ---------------------------------------------------
    try:
        result = client.aft_transfer_funds(
            transfer_code=AFT_TRANSFER_CODE_FULL_ONLY,
            transaction_index=0x00,
            transfer_type=int(AftTransferType.IN_HOUSE_TO_GAMING_MACHINE),
            cashable_amount=cents,
            restricted_amount=restricted,
            nonrestricted_amount=nonrestricted,
            # Ignored unless a restricted amount is included (section 8).
            pool_id=args.pool_id if restricted else 0,
            expiration=args.expiration if restricted else 0,
            asset_number=lock.asset_number,
            registration_key=bytes(20),  # zero = registration not required
            transaction_id=transaction_id,
        )
    except SASError as e:
        print(f"\n0x72 failed: {type(e).__name__}: {e}", file=sys.stderr)
        print("The transfer may or may not have been applied -- an exchange that"
              " fails after the machine acted looks the same from here. Check the"
              " meters below and the machine's credit display before retrying,"
              " and reuse the SAME transaction ID if you do: status 81 rejecting"
              " it as non-unique is how you learn the first attempt landed.",
              file=sys.stderr)
        after = read_meters(client)
        _show_deltas(before, after)
        return 1

    category, description = describe_aft_transfer_status(result.transfer_status)
    print(f"\ntransfer status  0x{result.transfer_status:02X}  {category}")
    print(f"                 {description}")
    # Report whichever amount types came back, not just the cashable one: a
    # successful $5 restricted push printed "amount reported 0.00", which
    # reads like nothing moved.
    reported = [
        (label, getattr(result, attr, 0) or 0)
        for attr, label in (("cashable_amount", "cashable"),
                            ("restricted_amount", "restricted"),
                            ("nonrestricted_amount", "nonrestricted"))
    ]
    moved = [f"{amount / 100:,.2f} {label}" for label, amount in reported if amount]
    print(f"amount reported  {' + '.join(moved) if moved else 'nothing'}")

    if category == "PENDING":
        # Mandatory, not optional. Section 8.8: a transfer answered with
        # pending 40 that then completes makes the machine issue exception 69
        # and "reissue exception 69 every 15 seconds until the host polls for
        # and acknowledges the completion status". Measured at 15.2s,
        # indefinitely. Leaving it unacknowledged puts a permanent beat in the
        # machine's exception stream -- and as a priority exception it is
        # delivered ahead of anything queued.
        print("\nPending is not done. Acknowledging with the interrogation poll"
              " (0x72 transfer code FF) so the machine does not reissue")
        print("exception 0x69 every 15 seconds forever...")
        for attempt in range(1, 13):
            time.sleep(1.0)
            try:
                final = client.aft_interrogate_transfer(0x00, acknowledge=True)
            except SASError as e:
                print(f"  interrogation failed: {type(e).__name__}: {e}", file=sys.stderr)
                break
            cat, desc = describe_aft_transfer_status(final.transfer_status)
            if cat != "PENDING":
                print(f"  final status   0x{final.transfer_status:02X}  {cat}")
                print(f"                 {desc}")
                print(f"  buffer position {final.transaction_buffer_position}")
                result = final
                category, description = cat, desc
                break
            print(f"  still pending (attempt {attempt})...")
        else:
            print("  Still pending after 12 attempts. The machine will keep issuing"
                  " 0x69 every 15s until acknowledged -- re-run the interrogation"
                  " poll, or see the note in the README.", file=sys.stderr)

    time.sleep(0.5)
    after = read_meters(client)
    _show_deltas(before, after)

    print("\nThe status byte is what the machine decided; the meter deltas are what"
          "\nhappened. Check the machine's own credit display too -- that is the"
          "\nthird source, and the only one not coming from this client.")
    return 0


def acknowledge_only(args) -> int:
    """Clear a stuck exception 0x69 without transferring anything."""
    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2
    client = SASClient(
        SASTransport(port, byte_timeout=args.timeout), args.address, timeout=args.timeout
    )
    print("Sending the interrogation poll (0x72, transfer code FF, index 00).")
    print("This acknowledges the most recent transfer's completion status and")
    print("moves no money.\n")
    try:
        result = client.aft_interrogate_transfer(0x00, acknowledge=True)
    except SASError as e:
        print(f"Interrogation failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    category, description = describe_aft_transfer_status(result.transfer_status)
    print(f"  transfer status  0x{result.transfer_status:02X}  {category}")
    print(f"                   {description}")
    print(f"  buffer position  {result.transaction_buffer_position}")
    print(f"  amount           {result.cashable_amount / 100:,.2f}")
    print("\nIf that reported a completion status, exception 0x69 should stop.")
    print("Confirm with exception_monitor.py -- it is the only way to be sure.")
    return 0


def _show_deltas(before: dict, after: dict) -> None:
    print("\nmeters before -> after:")
    for code, (label, old) in before.items():
        new = after.get(code, (label, None))[1]
        delta = ""
        if isinstance(old, int) and isinstance(new, int):
            delta = f"   delta {new - old:+}"
        print(f"  {label:52} {old} -> {new}{delta}")


if __name__ == "__main__":
    sys.exit(main())
