#!/usr/bin/env python3
"""Point this at a real serial port with a gaming machine on the other end
and it will general-poll the given address, then try a couple of read-only
long polls and print what comes back. By default it changes nothing on the
machine.

``--test-shutdown`` is the one exception, and it is opt-in for a reason:
it sends LP 0x01 (Shutdown / lock out play) to the address, immediately
followed by LP 0x02 (Startup) to re-enable it — a real state change on
real hardware, not a read. It prompts for an explicit "yes" before
sending anything (skip that with --yes for a scripted/unattended run),
and always attempts the Startup call afterward regardless of whether the
Shutdown itself looked like it succeeded, so a flaky exchange doesn't
leave the machine disabled. See SASClient.send_shutdown()'s docstring
(§7.4.1) for what "disabled" actually means timing-wise on an active
machine, and MANUAL.md/README.md for why this project implements only
these two of SAS's enable/disable long polls.

Usage:
    python3 examples/connectivity_check.py /dev/ttyUSB0 --address 1
    python3 examples/connectivity_check.py /dev/ttyUSB0 --address 1 --baud 19200 --cycles 5
    python3 examples/connectivity_check.py /dev/ttyUSB0 --address 1 --test-shutdown

Exit code is 0 if at least one general poll got a response and (when
--test-shutdown was given) the machine was confirmed re-enabled
afterward; 1 otherwise — convenient for a lab script that's sweeping
several ports/addresses, and for catching a machine left disabled.
"""

from __future__ import annotations

import argparse
import sys

from saspy.client import SASClient
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport


def test_shutdown_startup(client: SASClient, address: int, *, skip_confirmation: bool) -> bool:
    """Sends Shutdown (0x01) then Startup (0x02) to ``address`` and reports
    what happened. Returns True only if the machine was confirmed
    re-enabled by the end -- Startup is always attempted, even if
    Shutdown itself failed or timed out, so a flaky exchange can't leave
    the machine sitting disabled without at least one attempt to undo it.
    """
    print("\n" + "=" * 70)
    print(f"--test-shutdown: will send Shutdown (0x01) then Startup (0x02) to address {address}.")
    print("This is a REAL state change on the machine, not a read. If it's live,")
    print("this locks out play -- an active game finishes its current cycle first")
    print("(§7.4.1) before disabling; an idle machine disables immediately except")
    print("for cash-out and change/attendant.")
    print("=" * 70)
    if not skip_confirmation:
        answer = input(f"\nType 'yes' to send this to address {address}: ")
        if answer.strip().lower() != "yes":
            print("Skipped -- nothing sent.")
            return True  # declining is not a failure

    shutdown_ok = False
    print(f"\nSending Shutdown (0x01) to address {address}...", end=" ")
    try:
        client.send_shutdown()
        print("ACKed.")
        shutdown_ok = True
    except SASError as e:
        print(f"FAILED ({e}) -- the machine's disable state is now uncertain.")

    print(f"Sending Startup (0x02) to re-enable address {address}...", end=" ")
    try:
        client.send_startup()
        print("ACKed -- machine confirmed re-enabled.")
        if not shutdown_ok:
            print("(Shutdown above failed, so this was likely a no-op -- sent anyway as a safety net.)")
        return True
    except SASError as e:
        print(f"FAILED ({e})")
        print(
            "\n*** MACHINE MAY STILL BE DISABLED -- Startup did not get an ACK. ***\n"
            "*** Re-run with --test-shutdown, or re-enable it at the machine. ***"
        )
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", help="serial device, e.g. /dev/ttyUSB0 or COM3")
    parser.add_argument("--address", type=int, default=1, help="SAS address to poll (1-127)")
    parser.add_argument("--baud", type=int, default=19200)
    parser.add_argument("--cycles", type=int, default=3, help="general poll attempts before giving up")
    parser.add_argument("--timeout", type=float, default=1.0, help="per-exchange timeout in seconds")
    parser.add_argument(
        "--test-shutdown",
        action="store_true",
        help="also send LP 0x01 (Shutdown) then LP 0x02 (Startup) to the address -- a real state "
        "change on the machine, unlike every other check this script does. Prompts for "
        "confirmation unless --yes is also given.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the --test-shutdown confirmation prompt (for a scripted/unattended run)",
    )
    args = parser.parse_args()

    print(f"Opening {args.port} at {args.baud} baud...")
    serial_port = open_serial_port(args.port, baudrate=args.baud)
    transport = SASTransport(serial_port, byte_timeout=args.timeout)
    client = SASClient(transport, args.address, timeout=args.timeout)

    got_response = False
    for attempt in range(1, args.cycles + 1):
        print(f"General poll #{attempt} to address {args.address}...", end=" ")
        try:
            exception_code = client.general_poll()
        except SASError as e:
            print(f"no response ({e})")
            continue
        print(f"got exception code 0x{exception_code:02X}")
        got_response = True
        break

    if not got_response:
        print(
            f"\nNo response from address {args.address} after {args.cycles} attempts. "
            "Check wiring, baud rate, and that the address is right — "
            "a wrong address is the single most common cause of silence here."
        )
        return 1

    print("\nAddress is live. Trying a couple of read-only long polls (no state changed):\n")

    def try_poll(label: str, fn):
        print(f"  {label}...", end=" ")
        try:
            result = fn()
            print("OK ->", result)
        except SASError as e:
            print(f"failed ({e})")
        except Exception as e:  # noqa: BLE001 - want to see anything unexpected in a lab run
            print(f"unexpected error: {e!r}")

    try_poll("Send Gaming Machine ID (0x1F)", client.send_gaming_machine_id)
    try_poll("Send Meters 10-15 (0x0F)", client.send_meters_10_through_15)
    try_poll("Send Enabled Features (0xA0)", client.send_enabled_features)

    shutdown_test_ok = True
    if args.test_shutdown:
        shutdown_test_ok = test_shutdown_startup(client, args.address, skip_confirmation=args.yes)

    print(
        "\nDone. See saspy/client.py for the full set of implemented long polls, "
        "and README.md for what's covered vs. not."
    )
    return 0 if shutdown_test_ok else 1


if __name__ == "__main__":
    sys.exit(main())
