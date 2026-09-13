#!/usr/bin/env python3
"""Run this once when a new gateway is attached to a new EGM.

It scans candidate serial ports, finds which SAS address (if any)
responds to a general poll, confirms the find by querying the machine's
SAS version and serial number (long poll 0x54) and its game ID (long
poll 0x1F), and writes a gateway config file a poller can load with
``saspy.config.connect_from_config()``.

This tool is a convenience, not a requirement — a gateway.ini can always
be hand-written (see saspy/config.py's module docstring for the format).

Usage:
    python3 examples/commission_gateway.py
    python3 examples/commission_gateway.py --output /etc/nimasas/gateway.ini
    python3 examples/commission_gateway.py --ports /dev/ttyUSB0 /dev/ttyS0
    python3 examples/commission_gateway.py --address-range 1-8 --yes
"""

from __future__ import annotations

import argparse
import sys

from saspy.client import SASClient
from saspy.config import DEFAULT_BAUDRATE, GatewayConfig, save_config
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport


def discover_ports() -> list[str]:
    from serial.tools import list_ports

    return [p.device for p in list_ports.comports()]


def parse_address_range(spec: str) -> range:
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return range(int(lo), int(hi) + 1)
    return range(int(spec), int(spec) + 1)


def scan_port(port: str, baudrate: int, addresses: range, poll_timeout: float) -> list[int]:
    """Return the SAS addresses on ``port`` that answered a general poll."""
    print(f"  Opening {port} at {baudrate} baud...")
    try:
        serial_port = open_serial_port(port, baudrate=baudrate)
    except Exception as e:  # noqa: BLE001 - report and move on to the next port
        print(f"    could not open: {e}")
        return []

    transport = SASTransport(serial_port, byte_timeout=poll_timeout)
    live = []
    for address in addresses:
        client = SASClient(transport, address, timeout=poll_timeout)
        try:
            client.general_poll()
        except SASError:
            continue
        print(f"    address {address}: responded")
        live.append(address)
    return live


def describe_machine(port: str, address: int, baudrate: int, timeout: float) -> tuple[str, str, str]:
    """Returns (sas_version, serial_number, game_id) — any of which may be
    empty if that particular poll failed or isn't supported.
    """
    serial_port = open_serial_port(port, baudrate=baudrate)
    transport = SASTransport(serial_port, byte_timeout=timeout)
    client = SASClient(transport, address, timeout=timeout)

    sas_version = serial_number = game_id = ""
    try:
        info = client.send_sas_version_and_serial()
        sas_version, serial_number = info.sas_version, info.serial_number
    except SASError as e:
        print(f"  (couldn't read SAS version/serial: {e})")

    try:
        machine_info = client.send_gaming_machine_id()
        game_id = machine_info.game_id
    except SASError as e:
        print(f"  (couldn't read gaming machine ID: {e})")

    return sas_version, serial_number, game_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", default="gateway.ini", help="config file to write (default: gateway.ini)")
    parser.add_argument("--ports", nargs="+", help="specific ports to scan (default: auto-discover)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUDRATE, help="SAS is fixed at 19200; override only for non-standard setups")
    parser.add_argument("--address-range", default="1-32", help="SAS addresses to try per port, e.g. 1-32 or 5")
    parser.add_argument("--poll-timeout", type=float, default=0.3, help="seconds to wait per address before moving on")
    parser.add_argument("--yes", "-y", action="store_true", help="write the config without an interactive confirmation")
    args = parser.parse_args()

    ports = args.ports or discover_ports()
    if not ports:
        print("No serial ports found. Pass --ports explicitly if your ports aren't auto-detected.")
        return 1

    addresses = parse_address_range(args.address_range)
    print(f"Scanning {len(ports)} port(s) x {len(addresses)} address(es)...\n")

    found: list[tuple[str, int]] = []
    for port in ports:
        print(f"{port}:")
        for address in scan_port(port, args.baud, addresses, args.poll_timeout):
            found.append((port, address))

    if not found:
        print("\nNothing responded. Check wiring, baud rate, and that an address is actually "
              "configured on the machine — a wrong address is the most common cause of silence.")
        return 1

    if len(found) > 1:
        print(f"\n{len(found)} live (port, address) pairs found — that's unusual for a 1:1 "
              "gateway/EGM setup, so pick the right one carefully:")
        for i, (port, address) in enumerate(found):
            print(f"  [{i}] {port} @ address {address}")
        if args.yes:
            print("\n--yes was given but there's more than one candidate — refusing to guess. "
                  "Re-run with --ports/--address-range narrowed to just the one you want.")
            return 1
        choice = input("Choose one by number: ").strip()
        try:
            port, address = found[int(choice)]
        except (ValueError, IndexError):
            print("Not a valid choice.")
            return 1
    else:
        port, address = found[0]

    print(f"\nUsing {port} @ address {address}. Querying machine details...")
    sas_version, serial_number, game_id = describe_machine(port, address, args.baud, args.poll_timeout)

    config = GatewayConfig(
        port=port,
        address=address,
        baudrate=args.baud,
        timeout=1.0,
        sas_version=sas_version,
        serial_number=serial_number,
        game_id=game_id,
    )

    print(f"\n  port:           {config.port}")
    print(f"  address:        {config.address}")
    print(f"  baudrate:       {config.baudrate}")
    print(f"  SAS version:    {config.sas_version or '(unknown)'}")
    print(f"  serial number:  {config.serial_number or '(unknown)'}")
    print(f"  game ID:        {config.game_id or '(unknown)'}")

    if not args.yes:
        confirm = input(f"\nWrite this to {args.output}? [y/N] ").strip().lower()
        if confirm != "y":
            print("Not written.")
            return 1

    save_config(config, args.output)
    print(f"Written to {args.output}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
