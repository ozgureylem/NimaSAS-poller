"""Answer a machine's request to cash out to the host.

THIS MOVES MONEY, off the machine. Lab machines only.

When host cashouts are enabled and a player presses cash out, section 8.7
has the machine issue exception 6A, AFT request for host cashout, and wait
for the host to take the credits with a long poll 72 transfer. This watches
for that exception and answers it.

It is the other half of aft_push_credits, and the mechanism behind the
card-removal flow where a player's remaining credits return to their
account.

Three timings from the spec decide how this has to be written:

  - the machine reissues 6A every 800ms while the cashout is pending;
  - if the host does not perform or deny the transfer within 8 SECONDS, the
    machine runs its cashout-to-host failure recovery;
  - a long poll 74 resets the 800ms timer, but not the 8-second deadline.

So the answer has to be prompt. Nothing slow runs between seeing the
exception and replying -- the meter sweep happens before the watch starts
and again after the transfer, never in between.

Failure recovery depends on a mode this machine reports in long poll 74. In
SOFT mode a missed deadline is silent: the machine simply cashes out by
whatever other means it has, and nothing looks wrong. In HARD mode it tilts.
Soft is the more dangerous one to test against, because a bug looks like
success.

    python3 examples/host_cashout.py /dev/ttyS0 --address 1
    python3 examples/host_cashout.py /dev/ttyS0 --address 1 --deny

--deny sends a transfer of zero, which section 8.7 defines as instructing
the machine to cash out by its own means. That is a legitimate answer, not
a refusal to participate, and it is the safe way to watch the exception
without taking any money.
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
    AFT_TRANSFER_CODE_PARTIAL_ALLOWED,
    AftTransferType,
    MeterCode,
    describe_aft_transfer_status,
    describe_exception,
)
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

CASHOUT_REQUEST = 0x6A       # AFT request for host cashout
WIN_CASHOUT_REQUEST = 0x6B   # AFT request for host to cash out win
TRANSFER_COMPLETE = 0x69

# Section 8.7: "the amount fields may all be set to 9999999999, with partial
# transfer allowed, to transfer the entire cashout amount."
TAKE_EVERYTHING = 9_999_999_999

# Table C-7, the to-host side. Read before and after so the test shows which
# amount types actually moved, the same way the push does.
VERIFY_METERS = (
    (MeterCode.CURRENT_CREDITS, "current credits"),
    (MeterCode.CURRENT_RESTRICTED_CREDITS, "current RESTRICTED credits"),
    (0x00B8, "cashable transfers to HOST (cents)"),
    (0x00B9, "cashable transfers to HOST (quantity)"),
    (0x00BA, "RESTRICTED transfers to HOST (cents)"),
    (0x00BB, "RESTRICTED transfers to HOST (quantity)"),
    (0x00BC, "NONRESTRICTED transfers to HOST (cents)"),
    (0x00BD, "NONRESTRICTED transfers to HOST (quantity)"),
)


def read_meters(client) -> dict:
    out = {}
    for code, label in VERIFY_METERS:
        try:
            result = client.send_extended_meters([int(code)])
            out[int(code)] = (label, result.meters.get(int(code)))
        except SASError as e:
            out[int(code)] = (label, f"({type(e).__name__})")
    return out


def show_deltas(before: dict, after: dict) -> None:
    print("\nmeters before -> after:")
    for code, (label, old) in before.items():
        new = after.get(code, (label, None))[1]
        delta = f"   delta {new - old:+}" if isinstance(old, int) and isinstance(new, int) else ""
        print(f"  {label:44} {old} -> {new}{delta}")


def answer(client, code: int, *, asset_number: int, deny: bool,
           amount_cents: int | None) -> bool:
    """Respond to a pending cashout. Returns True if money moved."""
    transfer_type = (
        AftTransferType.WIN_AMOUNT_FROM_GAMING_MACHINE_TO_HOST
        if code == WIN_CASHOUT_REQUEST
        else AftTransferType.IN_HOUSE_FROM_GAMING_MACHINE_TO_HOST
    )

    if deny:
        # Section 8.7: "all amounts may be set to zero to instruct the gaming
        # machine to perform the cashout by whatever other means are
        # available". A zero transfer is an answer, not a no-op.
        cashable = restricted = nonrestricted = 0
        transfer_code = AFT_TRANSFER_CODE_FULL_ONLY
        print("  answering with a ZERO transfer: the machine cashes out by its own means")
    elif amount_cents is not None:
        cashable, restricted, nonrestricted = amount_cents, 0, 0
        transfer_code = AFT_TRANSFER_CODE_PARTIAL_ALLOWED
        print(f"  taking {amount_cents / 100:,.2f} cashable")
    else:
        cashable = restricted = nonrestricted = TAKE_EVERYTHING
        transfer_code = AFT_TRANSFER_CODE_PARTIAL_ALLOWED
        print("  taking the whole cashout (9999999999 with partial allowed, section 8.7)")

    try:
        result = client.aft_transfer_funds(
            transfer_code=transfer_code,
            transaction_index=0x00,
            transfer_type=int(transfer_type),
            cashable_amount=cashable,
            restricted_amount=restricted,
            nonrestricted_amount=nonrestricted,
            # Section 8: when restricted amounts move TO the host, the host
            # leaves pool ID and expiration zero -- the machine reports the
            # values currently associated with them in its response.
            pool_id=0,
            expiration=0,
            asset_number=asset_number,
            registration_key=bytes(20),
            transaction_id=f"NIMAOUT{int(time.time())}",
        )
    except SASError as e:
        print(f"  transfer failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False

    category, description = describe_aft_transfer_status(result.transfer_status)
    print(f"  status 0x{result.transfer_status:02X}  {category}: {description}")

    if category == "PENDING":
        for attempt in range(1, 13):
            time.sleep(1.0)
            try:
                final = client.aft_interrogate_transfer(0x00, acknowledge=True)
            except SASError as e:
                print(f"  interrogation failed: {type(e).__name__}: {e}", file=sys.stderr)
                break
            cat, desc = describe_aft_transfer_status(final.transfer_status)
            if cat != "PENDING":
                print(f"  final  0x{final.transfer_status:02X}  {cat}: {desc}")
                result, category = final, cat
                break
        else:
            print("  still pending after 12 interrogations", file=sys.stderr)

    moved = [
        f"{getattr(result, attr, 0) / 100:,.2f} {label}"
        for attr, label in (("cashable_amount", "cashable"),
                            ("restricted_amount", "restricted"),
                            ("nonrestricted_amount", "nonrestricted"))
        if getattr(result, attr, 0)
    ]
    print(f"  amount reported: {' + '.join(moved) if moved else 'nothing'}")
    return category == "SUCCESSFUL" and bool(moved)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--interval", type=float, default=0.2,
                    help="seconds between polls. The machine reissues 0x6A every 800ms and "
                         "gives the host 8 seconds, so this must stay well under both.")
    ap.add_argument("--deny", action="store_true",
                    help="answer with a zero transfer: the machine cashes out by its own "
                         "means. Takes no money, and is the safe way to see the exchange.")
    ap.add_argument("--amount", type=float, default=None,
                    help="take this much cashable instead of the whole cashout")
    ap.add_argument("--once", action="store_true", help="exit after answering one request")
    args = ap.parse_args()

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(
        SASTransport(port, byte_timeout=args.timeout), args.address, timeout=args.timeout
    )

    # --- pre-flight -----------------------------------------------------
    try:
        lock = client.aft_game_lock_and_status_request(lock_code=0xFF)
    except SASError as e:
        print(f"0x74 failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    enabled = bool(lock.host_cashout_status & 0x02)
    hard = bool(lock.host_cashout_status & 0x04)
    from_machine = bool(lock.available_transfers & 0x02)

    print(f"{args.port}, address {args.address}")
    print(f"  host cashout        {'ENABLED' if enabled else 'DISABLED'}")
    print(f"  mode                {'HARD (a missed deadline tilts)' if hard else 'SOFT (a missed deadline is SILENT)'}")
    print(f"  transfer FROM GM    {'available' if from_machine else 'NOT available right now'}")
    print(f"  on the machine      cashable {lock.current_cashable_amount / 100:,.2f}"
          f"  restricted {lock.current_restricted_amount / 100:,.2f}"
          f"  non-restricted {lock.current_non_restricted_amount / 100:,.2f}")

    if not enabled:
        print("\nHost cashout is disabled (0x74 host cashout status bit 1), so the machine")
        print("will never raise 0x6A -- it will cash out by its own means. Nothing to watch.")
        return 1
    if not hard:
        print("\n  Note: in SOFT mode, if this tool is too slow the machine cashes out by")
        print("  its own means and NOTHING looks wrong. Absence of a transfer here is not")
        print("  proof the exchange worked -- check the meters.")

    before = read_meters(client)

    print(f"\nWatching for 0x6A/0x6B. Press CASH OUT on the machine. Ctrl-C to stop.")
    if args.deny:
        print("--deny is set: no money will be taken.\n")
    else:
        print("Money WILL be taken off the machine.\n")

    answered = False
    try:
        while True:
            try:
                code = client.general_poll()
            except SASError:
                time.sleep(args.interval)
                continue

            if code in (CASHOUT_REQUEST, WIN_CASHOUT_REQUEST):
                print(f"[{time.strftime('%H:%M:%S')}] 0x{code:02X} {describe_exception(code)}")
                started = time.monotonic()
                moved = answer(
                    client, code,
                    asset_number=lock.asset_number,
                    deny=args.deny,
                    amount_cents=None if args.amount is None else int(round(args.amount * 100)),
                )
                print(f"  answered in {time.monotonic() - started:.2f}s "
                      f"(the machine allows 8s before its own recovery)")
                answered = answered or moved
                if args.once:
                    break
            elif code == TRANSFER_COMPLETE:
                # Acknowledge, or it reissues every 15 seconds forever (8.8).
                try:
                    client.aft_interrogate_transfer(0x00, acknowledge=True)
                except SASError:
                    pass
            elif code != 0x00:
                print(f"[{time.strftime('%H:%M:%S')}] 0x{code:02X} {describe_exception(code)}")

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")

    show_deltas(before, read_meters(client))
    print("\nThe status byte is what the machine decided; the meter deltas are what")
    print("happened. Check the machine's own credit display too.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
