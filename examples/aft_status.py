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
        print("  A machine with AFT off may simply ignore this (section 4.4).\n")

    # --- 0x74 with 0xFF: status without touching the lock ----------------
    try:
        lock = client.aft_game_lock_and_status_request(lock_code=0xFF)
        print("0x74 lock and status (query only, lock untouched)")
        print(f"    asset_number            {lock.asset_number}")
        print(f"    game_lock_status        0x{lock.game_lock_status:02X}")
        print(f"    available_transfers     0x{lock.available_transfers:02X}")
        print(f"    host_cashout_status     0x{lock.host_cashout_status:02X}")
        print(f"    aft_status              0x{lock.aft_status:02X}")
        print(f"    transfer limit          {lock.gaming_machine_transfer_limit / 100:,.2f}")
        print(f"    credits on machine      cashable {lock.current_cashable_amount / 100:,.2f}"
              f"  restricted {lock.current_restricted_amount / 100:,.2f}"
              f"  non-restricted {lock.current_non_restricted_amount / 100:,.2f}")
        print(f"    max_buffer_index        {lock.max_buffer_index}")
        print()
        if lock.gaming_machine_transfer_limit == 0:
            print("  transfer limit is 0: this machine will not accept a host")
            print("  transfer of any size as currently configured.")
        if lock.game_lock_status not in (0xFF, 0x00):
            print(f"  game_lock_status 0x{lock.game_lock_status:02X} is not idle -- resolve that")
            print("  before attempting a transfer.")
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
