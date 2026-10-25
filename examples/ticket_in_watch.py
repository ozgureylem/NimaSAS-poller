"""Watch a ticket being inserted, and read what the machine says about it.

READ-ONLY, and deliberately so. It never sends long poll 0x71 with a transfer
code that authorises anything -- the only 0x71 it sends is the FF status query
(section 15.12b), which asks "how did the current redemption cycle end?" and
authorises nothing. If this machine is waiting on a host to approve the ticket,
this tool will let it time out rather than approve it.

That matters because the two interesting outcomes look identical until the
meters are read:

  SYSTEM validation -- the machine asks a host whether the ticket is good.
  Nothing here answers, so the ticket is returned and no meter moves. This is
  the safe case and the common one on a machine configured for a host.

  STANDALONE / self-validation -- the machine decides for itself from the
  ticket's own validation number. It can then credit the full face value with
  no host involved at all, and the credit is real as far as every meter is
  concerned: 002C/0028 and the Voucher In lines move, and a meter cannot be
  un-rung without an RAM clear.

So: take a snapshot before running this. If the machine self-validates, the
lab machine's accounting baseline changes permanently, and every reconciliation
done against it afterwards has to account for a ticket that came from somewhere
else entirely.

    python3 examples/ticket_in_watch.py /dev/ttyS0 --address 1
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.client import SASClient
from saspy.constants import GENERAL_POLL_EXCEPTIONS
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

TICKET_INSERTED = 0x67
TICKET_TRANSFER_COMPLETE = 0x68

# Table 15.11b parsing codes seen in the field; anything else prints raw.
PARSING = {0x00: "standard validation number (18 digits)"}

# Table 15.12b machine status for the redemption cycle.
REDEEM_STATUS = {
    0x00: "ticket redeemed",
    0x01: "ticket rejected -- not a valid ticket",
    0x02: "ticket rejected -- ticket expired",
    0x03: "ticket rejected -- validation number not in system",
    0x80: "not in a redemption cycle / no ticket",
    0xFF: "ticket in escrow, waiting for the host to authorise or deny",
}

# Meters that a redeemed ticket must move. Read before and after so the
# question "did it actually credit?" is answered by the machine, not by the
# absence of an error.
WATCH_METERS = [
    (0x15, "total ticket in (credits)"),
    (0x28, "total cashable ticket in (credits)"),
    (0x29, "total regular cashable ticket in (credits)"),
    (0x2A, "total restricted promotional ticket in (credits)"),
    (0x2B, "total nonrestricted promotional ticket in (credits)"),
    (0x11, "cashable ticket in (quantity)"),
    (0x0C, "current credits"),
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


def read_meters(client) -> dict[int, int | None]:
    codes = [c for c, _ in WATCH_METERS]
    try:
        return {c: client.send_extended_meters(codes).meters.get(c) for c in codes}
    except SASError as e:
        print(f"  (could not read meters: {type(e).__name__}: {e})", file=sys.stderr)
        return {c: None for c in codes}


def show_meter_change(before: dict, after: dict) -> bool:
    moved = False
    for code, label in WATCH_METERS:
        was, now = before.get(code), after.get(code)
        if was is None or now is None or was == now:
            continue
        moved = True
        print(f"    {code:04X}  {label:<44} {was:>12,} -> {now:>12,}"
              f"  {now - was:+,}")
    return moved


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--interval", type=float, default=0.3,
                    help="seconds between general polls (section 2.3.3 allows "
                         "0.2-5.0)")
    args = ap.parse_args()

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(
        SASTransport(port, byte_timeout=args.timeout), args.address, timeout=args.timeout)

    print(f"\n{args.port}, address {args.address}")
    print("\nREAD-ONLY: nothing here authorises a ticket. If the machine is")
    print("waiting on a host, the ticket will be returned.")
    print("\nMachine configuration (LP 0xA0):")
    enabled = ticket_preflight(client)

    print("\nTicket meters before:")
    before = read_meters(client)
    for code, label in WATCH_METERS:
        value = before.get(code)
        print(f"    {code:04X}  {label:<44} {'n/a' if value is None else f'{value:,}'}")

    print("\nInsert a ticket. Ctrl-C to stop.\n")

    try:
        while True:
            try:
                code = client.general_poll()
            except SASError as e:
                print(f"  (poll failed: {type(e).__name__}: {e})", file=sys.stderr)
                time.sleep(args.interval)
                continue

            if code:
                stamp = time.strftime("%H:%M:%S")
                name = GENERAL_POLL_EXCEPTIONS.get(code, "unknown exception")
                print(f"[{stamp}] 0x{code:02X} {name}")

                if code == TICKET_INSERTED:
                    try:
                        data = client.send_ticket_validation_data()
                    except SASError as e:
                        print(f"    LP 0x70 did not answer: {type(e).__name__}: {e}")
                    else:
                        print(f"    in escrow        {data.ticket_in_escrow}")
                        print(f"    amount           {data.amount_cents:,} "
                              f"({data.amount_cents / 100:,.2f})")
                        print(f"    parsing code     0x{data.parsing_code:02X}"
                              f"  ({PARSING.get(data.parsing_code, 'see Table 15.11b')})")
                        print(f"    validation data  {data.validation_data.hex()}")
                        print("    -> not authorising it. Section 15.12b's FF status")
                        print("       query is the only 0x71 this tool sends.")

                if code == TICKET_TRANSFER_COMPLETE:
                    try:
                        result = client.redeem_ticket_status()
                    except SASError as e:
                        print(f"    LP 0x71/FF did not answer: {type(e).__name__}: {e}")
                    else:
                        print(f"    machine status   0x{result.machine_status:02X}"
                              f"  ({REDEEM_STATUS.get(result.machine_status, 'see Table 15.12b')})")
                        print(f"    amount           {result.amount_cents:,}")
                    print("\n  meters now:")
                    after = read_meters(client)
                    if show_meter_change(before, after):
                        print("\n  The ticket CREDITED. This machine self-validates --")
                        print("  no host authorised anything. Those meters are permanent.")
                    else:
                        print("    nothing moved -- the ticket did not credit.")
                    before = after
                    print()

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.\n")

    print("Ticket meters at exit:")
    after = read_meters(client)
    if not show_meter_change(before, after):
        print("    unchanged since the last report.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
