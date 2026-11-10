#!/usr/bin/env python3
"""Who is on this cable, and at which SAS address.

Read-only. Sends a general poll to each address in the range, and for every
one that answers, asks who it is. Writes nothing, changes nothing, and never
enables or disables anything on a machine.

WHY THIS EXISTS. A multi-terminal cabinet -- a roulette or sic-bo with a
shared wheel and N betting stations -- may put every station on one SAS bus
at consecutive addresses, or may give each its own port with every one
configured as address 1. Nothing in a SAS answer says which, and the
difference decides whether surveying the machine is one visit or N.

The 2026-10-27 floor survey hit exactly this. The gateway was cabled to one
station of an eight-station zoom roulette, polled address 1, and got a
cabinet reporting asset 8. Seven stations were never surveyed and nothing in
the data said they existed.

WHAT THE RANGE COSTS. Each address that does NOT answer costs one timeout,
so a 1-32 scan on a quiet bus is 32 x --timeout seconds. Start narrow. SAS
addresses are 01-7F; 00 is the broadcast address and is not scanned.

    python3 examples/address_scan.py /dev/ttyS0
    python3 examples/address_scan.py /dev/ttyS0 --range 1-8
    python3 examples/address_scan.py /dev/ttyS0 --range 1-32 --timeout 0.2
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


def parse_range(spec: str) -> list[int]:
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        lo, hi = int(lo), int(hi)
    else:
        lo = hi = int(spec)
    if not (1 <= lo <= hi <= 0x7F):
        raise argparse.ArgumentTypeError(
            "SAS addresses run 01-7F; 00 is the broadcast address and is not scanned")
    return list(range(lo, hi + 1))


def identify(client) -> dict:
    """Who answered. Every field is optional: §4.4 lets a machine ignore a
    long poll it does not implement, and a blank here is not evidence that
    nothing is there -- the general poll already proved otherwise."""
    out: dict = {}
    try:
        v = client.send_sas_version_and_serial()
        out["serial"] = v.serial_number.strip()
        out["sas"] = v.sas_version.strip()
    except SASError:
        pass
    try:
        out["asset"] = client.aft_game_lock_and_status_request(
            lock_code=0xFF).asset_number
    except SASError:
        pass
    try:
        cfg = client.send_game_n_configuration(0)
        out["game_id"] = cfg.game_id
        out["denom"] = cfg.denomination
        out["max_bet"] = cfg.max_bet
    except SASError:
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--range", default="1-8", type=parse_range,
                    help="addresses to try (default: %(default)s)")
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=0.3,
                    help="seconds to wait per address. Every address that does "
                         "not answer costs this much (default: %(default)s)")
    ap.add_argument("--first-only", action="store_true",
                    help="print ONLY the first address that answers, as a bare "
                         "number on stdout, and stop scanning there. For the "
                         "headless floor survey, which has no operator to read "
                         "a table and no keyboard to type an address with. "
                         "Everything else goes to stderr so stdout stays "
                         "capturable; exit 1 if nothing answered.")
    args = ap.parse_args()
    # With --first-only, stdout is reserved for the answer. Narration that
    # would otherwise be printed goes to stderr, where the survey log picks
    # it up, instead of being swallowed by a command substitution.
    out = sys.stderr if args.first_only else sys.stdout

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    transport = SASTransport(port, byte_timeout=args.timeout)
    addresses = args.range
    print(f"{args.port} @ {args.baud}, addresses "
          f"{addresses[0]}-{addresses[-1]} ({len(addresses)}), "
          f"{args.timeout}s each\n", file=out)

    found = []
    for address in addresses:
        client = SASClient(transport, address, timeout=args.timeout)
        try:
            client.general_poll()
        except SASError:
            print(f"  {address:>3}  .", file=out)
            continue
        if args.first_only:
            # The bare number, and nothing else, on stdout.
            print(address)
            print(f"  {address:>3}  ANSWERED (stopping here, --first-only)",
                  file=out)
            return 0
        who = identify(client)
        found.append((address, who))
        bits = []
        if who.get("serial") is not None:
            bits.append(f"serial {who['serial'] or '(empty)'}")
        if who.get("asset") is not None:
            bits.append(f"asset {who['asset']}")
        if who.get("sas"):
            bits.append(f"SAS {who['sas']}")
        if who.get("game_id"):
            bits.append(f"game {who['game_id']}")
        print(f"  {address:>3}  ANSWERED   {'  '.join(bits) or '(told us nothing)'}",
              file=out)

    print(file=out)
    if not found:
        print("Nothing answered. Either no machine is on this cable, or its "
              "address is outside the range scanned, or the wiring/baud is "
              "wrong. A wrong address is the usual cause.", file=out)
        return 1

    print(f"{len(found)} address(es) answered on this one cable.", file=out)
    if len(found) == 1:
        print("  One station per cable. A multi-station cabinet then needs the "
              "cable moved once per station, and each survey is its own visit.",
              file=out)
    else:
        print("  Several stations share this bus, so one cable can survey them "
              "all. Run the survey once per address:", file=out)
        for address, _who in found:
            print(f"    python3 examples/meter_snapshot.py {args.port} "
                  f"--address {address}", file=out)

    serials = {w.get("serial") for _a, w in found if w.get("serial")}
    if len(found) > 1 and len(serials) == 1:
        print("\n  NOTE: they all report the SAME serial number. On a shared-wheel "
              "cabinet that is expected -- the stations are one machine -- but it "
              "means the serial cannot tell them apart. Use the address.",
              file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
