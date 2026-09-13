#!/usr/bin/env python3
"""Point this at a real serial port with a gaming machine on the other end
and it will general-poll the given address, then try a couple of read-only
long polls and print what comes back. It changes nothing on the machine.

Usage:
    python3 examples/connectivity_check.py /dev/ttyUSB0 --address 1
    python3 examples/connectivity_check.py /dev/ttyUSB0 --address 1 --baud 19200 --cycles 5

Exit code is 0 if at least one general poll got a response, 1 otherwise —
convenient for a lab script that's sweeping several ports/addresses.
"""

from __future__ import annotations

import argparse
import sys

from saspy.client import SASClient
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", help="serial device, e.g. /dev/ttyUSB0 or COM3")
    parser.add_argument("--address", type=int, default=1, help="SAS address to poll (1-127)")
    parser.add_argument("--baud", type=int, default=19200)
    parser.add_argument("--cycles", type=int, default=3, help="general poll attempts before giving up")
    parser.add_argument("--timeout", type=float, default=1.0, help="per-exchange timeout in seconds")
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

    print(
        "\nDone. See saspy/client.py for the full set of implemented long polls, "
        "and README.md for what's covered vs. not."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
