"""What AFT would do, before you ask it to do anything.

READ-ONLY. It sends long polls 0x73 and 0x74 with their query-only codes
(0xFF, "report current state, change nothing"), plus 0xA0. It does not
register, does not lock the game, and does not transfer funds.

This exists because "the machine is AFT enabled, shall we push some
credits?" is three questions, and only the last one moves money:

  1. Is a host registered at all? An unregistered host cannot transfer,
     and registering one is a real state change -- it writes an asset
     number and a 20-byte key into the machine.
  2. What does the machine say it will accept? 0x74 reports the transfers
     currently available, its transfer limit, and the credits already on
     it. A transfer the machine was never going to accept is a failed
     exchange to debug, not a feature to demo.
  3. Only then, a transfer.

Worth knowing before step 3: of everything in this library, 0x72 is the
one command that moves money, and on this project it has never run
against hardware. A transfer that half-completes can leave the machine
locked waiting on a host reply, and recovering that can end in a RAM
clear -- which zeroes the meters you just validated.

    python3 examples/aft_status.py /dev/ttyS0 --address 1
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


# Table 8.2b, transcribed from the spec. Bit 3 of AFT status and bit 0 of
# available transfers are the two that decide whether a transfer is even
# worth attempting, and neither is legible as a hex byte.
AFT_STATUS_BITS = (
    "printer available for transaction receipts",
    "transfer to host of less than full available amount allowed",
    "custom ticket data supported",
    "AFT REGISTERED",
    "in-house transfers enabled",
    "bonus transfers enabled",
    "debit transfers enabled",
    "ANY AFT ENABLED",
)
AVAILABLE_TRANSFER_BITS = {
    0: "transfer TO gaming machine OK",
    1: "transfer FROM gaming machine OK",
    2: "transfer to printer OK",
    3: "win amount pending cashout to host",
    4: "bonus award to gaming machine OK",
    7: "Lock After Transfer supported",
}
GAME_LOCK = {0x00: "LOCKED", 0x40: "lock pending", 0xFF: "not locked"}

# Table 8.2b host cashout status. This decides whether removing a card can
# hand the machine's credits to the host at all, so it is worth reading
# rather than printing as hex -- bit 1 is the whole answer.
HOST_CASHOUT_BITS = {
    0: ("cashout to host CONTROLLABLE by host", "cashout to host FORCED by gaming machine"),
    1: ("cashout to host currently ENABLED", "cashout to host currently DISABLED"),
    2: ("host cashout mode HARD", "host cashout mode SOFT (only meaningful if enabled)"),
}


def show_host_cashout(value: int) -> None:
    for bit, (on, off) in HOST_CASHOUT_BITS.items():
        set_ = value >> bit & 1
        print(f"      bit {bit}  {set_}  {on if set_ else off}")


def show_bits(value, names):
    for bit in sorted(names):
        label = names[bit] if isinstance(names, dict) else names[bit]
        on = value >> bit & 1
        print(f"      bit {bit}  {'1' if on else '0'}  {'YES' if on else 'no':3}  {label}")


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
    print(f"{args.port}, address {args.address} -- read-only AFT interrogation\n")

    # --- 0xA0: is AFT even advertised? ----------------------------------
    try:
        features = client.send_enabled_features()
        print("0xA0 enabled features")
        print(f"    aft_bonus_awards        {features.aft_bonus_awards}")
        print(f"    legacy_bonus_awards     {features.legacy_bonus_awards}")
        print(f"    validation_extensions   {features.validation_extensions}")
        print(f"    ticket_redemption       {features.ticket_redemption}")
        print("  Note: aft_bonus_awards is about BONUS awards. It is not a")
        print("  statement that cashable host transfers are enabled -- 0x74 below")
        print("  is what actually answers that.\n")
    except SASError as e:
        print(f"0xA0 failed: {type(e).__name__}: {e}\n")

    # --- 0x73 with 0xFF: read the current registration, change nothing ---
    registered = None
    try:
        reg = client.aft_register_gaming_machine(registration_code=0xFF)
        registered = reg.registration_status
        print("0x73 current registration (query only)")
        print(f"    registration_status     0x{reg.registration_status:02X}")
        print(f"    asset_number            {reg.asset_number}")
        print(f"    pos_id                  {reg.pos_id}")
        key = reg.registration_key
        print(f"    registration_key        {'all zero' if not any(key) else key.hex(' ').upper()}")
        print("  An all-zero key and asset number 0 mean no host has registered.")
        print("  Registering one is a state change: check the spec's Table 8.1b")
        print("  code for this status before deciding anything.\n")
    except SASError as e:
        print(f"0x73 failed: {type(e).__name__}: {e}")
        print("  Per section 8.1 the machine SHOULD answer a read-current-registration")
        print("  poll -- 'must still respond with the most recently received")
        print("  registration key, even if it is not currently registered'. Silence")
        print("  means it does not implement 0x73 at all, which section 4.4 permits")
        print("  by ignoring unsupported long polls. That is consistent with debit")
        print("  transfers being off, since registration exists for debit. Check")
        print("  0x74's AFT status bit 3 below for the registration state instead.\n")

    # --- 0x74 with 0xFF: status without touching the lock ----------------
    try:
        lock = client.aft_game_lock_and_status_request(lock_code=0xFF)
        print("0x74 lock and status (query only, lock untouched)")
        print(f"    asset_number            {lock.asset_number}")
        print(f"    game_lock_status        0x{lock.game_lock_status:02X}"
              f"  ({GAME_LOCK.get(lock.game_lock_status, 'unknown code')})")
        print(f"    aft_status              0x{lock.aft_status:02X}")
        show_bits(lock.aft_status, dict(enumerate(AFT_STATUS_BITS)))
        print(f"    available_transfers     0x{lock.available_transfers:02X}")
        show_bits(lock.available_transfers, AVAILABLE_TRANSFER_BITS)
        print(f"    host_cashout_status     0x{lock.host_cashout_status:02X}")
        show_host_cashout(lock.host_cashout_status)
        print(f"    transfer limit          {lock.gaming_machine_transfer_limit / 100:,.2f}")
        print(f"    credits on machine      cashable {lock.current_cashable_amount / 100:,.2f}"
              f"  restricted {lock.current_restricted_amount / 100:,.2f}"
              f"  non-restricted {lock.current_non_restricted_amount / 100:,.2f}")
        print(f"    max_buffer_index        {lock.max_buffer_index}")
        print()
        if lock.gaming_machine_transfer_limit == 0:
            print("  transfer limit is 0: this machine will not accept a host")
            print("  transfer of any size as currently configured.")

        aft_on = bool(lock.aft_status & 0x80)
        in_house = bool(lock.aft_status & 0x10)
        debit = bool(lock.aft_status & 0x40)
        registered = bool(lock.aft_status & 0x08)
        to_machine = bool(lock.available_transfers & 0x01)

        if not aft_on:
            print("  AFT is disabled on this machine (status bit 7 clear). The spec")
            print("  has both these fields read zero in that case, so nothing else")
            print("  here is meaningful until it is enabled.")
        elif not to_machine:
            # The distinction that matters: configuration vs current state.
            print("  AFT is ENABLED, but available_transfers says no transfer type is")
            print("  accepted RIGHT NOW -- including transfer to the gaming machine.")
            print("  Those bits are current state, not configuration: the spec sets")
            print("  them from 'a state where it might be able to accept' the type.")
            print("  Section 8.3 names the conditions that rule it out, among them")
            print("  door open, OPERATOR MENU, tilt, disabled, waiting for handpay.")
            print("  If the machine is sitting in its accounting or attendant menu,")
            print("  that alone explains this. Return it to normal idle play and")
            print("  re-run before reading anything further into it.")
            if not registered and not debit:
                print()
                print("  Registration is NOT the cause. Per section 8.1 it is required")
                print("  for DEBIT transfers only, and debit is disabled here, so an")
                print("  unregistered machine can still do in-house transfers.")
        elif to_machine:
            print()
            print(f"  Transfer to gaming machine is currently available, in-house"
                  f" {'on' if in_house else 'OFF'}, up to"
                  f" {lock.gaming_machine_transfer_limit / 100:,.2f}.")
            if lock.game_lock_status != 0xFF:
                print(f"  But game_lock_status is 0x{lock.game_lock_status:02X} -- resolve that first.")

        if lock.host_cashout_status & 0x02:
            print()
            print("  Cashout to host is ENABLED. So a cashout from this machine --")
            print("  including the card-removal flow that hands credits back to an")
            print("  AFT account -- will raise exception 0x6A, AFT request for host")
            print("  cashout, and section 8.7 has the machine reissue it every 800ms")
            print("  until the host answers. Note the interval: 800ms for 0x6A/0x6B,")
            print("  versus 15 seconds for 0x69. That is how to tell a cashout request")
            print("  apart from an unacknowledged transfer completion without guessing.")
            if not lock.host_cashout_status & 0x01:
                print("  Bit 0 is clear, so cashout to host is FORCED by the machine")
                print("  rather than controllable by the host.")
            if not lock.host_cashout_status & 0x04:
                print("  Mode is SOFT: if the host does not answer within 8 seconds the")
                print("  machine cashes out by its own means instead of tilting.")
    except SASError as e:
        print(f"0x74 failed: {type(e).__name__}: {e}")
        print("  If 0x73 answered and this did not, report both -- that pairing")
        print("  is itself a finding.")

    print("\n" + "=" * 66)
    print("Nothing above changed machine state. Before any transfer:")
    print("  - do it on a machine with no money in it, and not during a soak:")
    print("    a transfer moves the very meters a soak is measuring;")
    print("  - pick an amount that is unmistakable in the meters (not 1 cent);")
    print("  - read the meters before and after and reconcile both against the")
    print("    machine's own accounting screen, which is how the 0x0F decode was")
    print("    confirmed in the first place.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
