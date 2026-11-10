#!/usr/bin/env python3
"""After a RAM clear: did the technician type the serial in correctly?

READ-ONLY. Reads the cabinet's identity (LP 0x54 for SAS version and serial,
LP 0x74 for the asset number) and compares it against what this cabinet is
supposed to report. Moves no money, enables nothing, writes nothing to the
machine.

WHY THIS EXISTS. The serial a gateway sees is NOT read off the chassis -- it
is typed into machine configuration during the RAM clear, exactly like the
asset number (HANDOVER section 5e). A technician can leave it blank or
mistype it, and on this project's own lab machine one did. The symptom
surfaced weeks later as a ticket redemption refused for a serial mismatch,
which looks like a software fault and is not one.

SAS gives a host NO WAY to tell a mistyped serial from a real one. Both are
just numbers in a field. The only check that exists anywhere is a comparison
against the label, made by someone standing at the cabinet while it is
freshly cleared -- which is this tool, and which is why it has to be runnable
over SSH on a gateway with no screen.

    # one cabinet, serial read off its label
    python3 examples/verify_identity.py /dev/ttyS0 --address 1 \
        --expect-serial 20884471 --expect-asset 7

    # or from a roster built during the floor walk, address found by scanning
    python3 examples/verify_identity.py /dev/ttyS0 --scan \
        --roster floor.csv --machine 7

    # append every check, so the clear-out is auditable afterwards
    python3 examples/verify_identity.py /dev/ttyS0 --scan \
        --roster floor.csv --machine 7 --log ~/identity-checks.jsonl

EXIT STATUS IS THE POINT, because this will be run from a script across a
floor: 0 only when everything asked for matched. Anything else is a cabinet
that must not go live until it is re-entered.

    0  everything checked matched
    1  a value came back DIFFERENT from what was expected
    2  bad usage, or the port could not be opened
    3  the field came back blank or a placeholder -- left empty at the clear
    4  the cabinet did not answer at all

ROSTER FORMAT. A CSV with a header, one row per cabinet, built from the
labels during a floor walk. Extra columns are ignored so a spreadsheet export
works unchanged:

    machine,serial,asset,note
    7,20884471,7,bank A left
    8,20884470,8,bank A right
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from saspy.client import SASClient
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

# Imported, never copied. read_identity() already issues both long polls and
# already knows that a missing asset is not the same as asset zero; a second
# implementation here would drift from the poller's, and the one place they
# disagreed would be the one that mattered.
from sql_poll_logger import read_identity

# FROM saspy.constants, NOT survey_store.
#
# survey_store only re-exports it, and survey_store is lab-only apparatus
# (tool_tree.py's own classification) while this tool is product. Importing
# it through there made a product tool depend on a lab one -- invisible
# here, and fatal in the public mirror, where the lab tier is not
# published. Found on 2026-11-10 by running the mirrored test suite.
from saspy.constants import is_placeholder_identifier

OK = 0
MISMATCH = 1
BAD_USAGE = 2
NOT_REPORTED = 3
NO_ANSWER = 4


def is_placeholder(serial) -> bool:
    """Blank, '0', '000000000', 'N/A' and friends are not serial numbers.

    This is the signature of a configuration field left empty at the clear,
    and it is a likelier cause of a cabinet "not reporting a serial" than
    section 4.4's permission to ignore the long poll.

    One line, because saspy.constants already does the comparison. It used
    to spell out `text.lower() in {p.lower() for p in PLACEHOLDER_IDS}`
    here -- which is the very thing is_placeholder_identifier's docstring
    says it exists to stop callers writing at each site.
    """
    return is_placeholder_identifier(serial)


def _norm(value) -> str:
    """Compare serials by value, not by spelling.

    The two sides come from different places: a label is read by eye and
    typed, a SAS field is zero-padded by the cabinet. '20884471' and
    '0020884471' are the same number written by two people, and failing that
    comparison would send a technician to re-clear a correctly configured
    machine -- which is worse than not checking, because it destroys working
    configuration to fix nothing.
    """
    return str(value).strip().lstrip("0") or "0"


def compare(seen_serial, seen_asset, expect_serial=None, expect_asset=None):
    """Pure comparison: returns (exit_code, [lines]). Touches no port."""
    lines: list[str] = []
    code = OK

    if expect_serial is None and expect_asset is None:
        return BAD_USAGE, ["nothing to check -- give --expect-serial, "
                           "--expect-asset, or --roster with --machine."]

    if expect_serial is not None:
        if is_placeholder(seen_serial):
            code = NOT_REPORTED
            lines.append(f"SERIAL BLANK: the cabinet reports {seen_serial!r}.")
            lines.append(f"  The field was left EMPTY at the RAM clear. Enter "
                         f"{expect_serial} and clear again.")
        elif _norm(seen_serial) != _norm(expect_serial):
            code = MISMATCH
            lines.append(f"SERIAL MISMATCH: cabinet says {seen_serial!r}, "
                         f"label says {expect_serial!r}.")
            lines.append("  Either it was mistyped at the clear, or this is "
                         "not the cabinet you think it is. A wrong number "
                         "typed confidently is indistinguishable from a "
                         "swapped machine -- to this tool and to the gateway.")
        else:
            lines.append(f"serial OK: {seen_serial}")

    if expect_asset is not None:
        if seen_asset in (None, 0):
            if code == OK:
                code = NOT_REPORTED
            lines.append(f"ASSET UNSET: the cabinet reports {seen_asset!r}, "
                         "and zero is the field's unset value, not an asset "
                         "number.")
        elif int(seen_asset) != int(expect_asset):
            if code == OK:
                code = MISMATCH
            lines.append(f"ASSET MISMATCH: cabinet says {seen_asset}, roster "
                         f"says {expect_asset}.")
        else:
            lines.append(f"asset OK: {seen_asset}")

    return code, lines


def load_roster(path: Path, machine: str) -> tuple[str | None, int | None]:
    """One row of the floor roster, found by its 'machine' column."""
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError(f"{path} has no rows")
    for field in ("machine", "serial"):
        if field not in rows[0]:
            raise ValueError(f"{path} needs a {field!r} column; it has "
                             f"{', '.join(rows[0])}")
    matched = [r for r in rows
               if str(r["machine"]).strip() == str(machine).strip()]
    if not matched:
        raise ValueError(f"no row for machine {machine!r} in {path}")
    if len(matched) > 1:
        # Two rows for one machine means the roster is wrong, and guessing
        # which one is meant would validate a cabinet against the wrong label.
        raise ValueError(f"{len(matched)} rows for machine {machine!r} in "
                         f"{path} -- the roster must name each machine once")
    row = matched[0]
    asset = (row.get("asset") or "").strip()
    return (row["serial"].strip() or None, int(asset) if asset else None)


def find_address(transport, probe_timeout: float = 0.3) -> int | None:
    """First address 1-8 that answers a general poll.

    Costs one general poll per address tried, and a general poll takes one
    exception out of the section 2.2.1 queue on a machine that is live. On a
    cabinet that has just been cleared there is nothing in that queue to lose.
    """
    for candidate in range(1, 9):
        try:
            SASClient(transport, candidate, timeout=probe_timeout).general_poll()
        except SASError:
            continue
        return candidate
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=None,
                    help="SAS address; omit and pass --scan to find it")
    ap.add_argument("--scan", action="store_true",
                    help="find the address by polling 1-8 first, for a floor "
                         "where nothing guarantees address 1")
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--expect-serial", default=None)
    ap.add_argument("--expect-asset", type=int, default=None)
    ap.add_argument("--roster", type=Path, default=None)
    ap.add_argument("--machine", default=None,
                    help="which row of --roster this cabinet is")
    ap.add_argument("--log", type=Path, default=None,
                    help="append the result as one JSON object per line")
    args = ap.parse_args()

    expect_serial, expect_asset = args.expect_serial, args.expect_asset
    if args.roster:
        if not args.machine:
            print("--roster needs --machine", file=sys.stderr)
            return BAD_USAGE
        try:
            expect_serial, expect_asset = load_roster(args.roster, args.machine)
        except (OSError, ValueError) as e:
            print(f"{e}", file=sys.stderr)
            return BAD_USAGE
    if expect_serial is None and expect_asset is None:
        print("nothing to check against. Give --expect-serial/--expect-asset, "
              "or --roster with --machine.", file=sys.stderr)
        return BAD_USAGE

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"could not open {args.port}: {e}", file=sys.stderr)
        return BAD_USAGE
    transport = SASTransport(port, byte_timeout=args.timeout)

    address = args.address
    if address is None:
        if not args.scan:
            print("give --address, or --scan to find it", file=sys.stderr)
            return BAD_USAGE
        address = find_address(transport)
        if address is None:
            print("nothing answered at any address 1-8 -- that is the cable, "
                  "the wiring or the baud rate, NOT a wrong address.",
                  file=sys.stderr)
            return NO_ANSWER
        print(f"address {address} answered")

    identity = read_identity(SASClient(transport, address, timeout=args.timeout))
    seen_serial = identity.get("serial_number")
    seen_asset = identity.get("asset_number")

    if seen_serial is None and seen_asset is None:
        # Section 4.4 lets a cabinet ignore a long poll it does not
        # implement, so silence is not proof of a bad clear -- but it does
        # mean no ticket file can be bound to this machine by serial.
        print("the cabinet answered a general poll but reported NEITHER a "
              "serial nor an asset number.", file=sys.stderr)
        print("Section 4.4 permits ignoring a long poll, so this need not be "
              "a configuration fault -- but the identity cannot be checked, "
              "and a ticket file cannot be bound to this cabinet by serial.",
              file=sys.stderr)
        return NO_ANSWER

    print(f"\ncabinet at address {address} reports serial {seen_serial!r}, "
          f"asset {seen_asset!r}")
    code, lines = compare(seen_serial, seen_asset, expect_serial, expect_asset)
    print()
    for line in lines:
        print(f"  {line}")
    print()
    print("PASS -- safe to go live."
          if code == OK else
          "FAIL -- do NOT put this cabinet live until it is re-entered.")

    if args.log:
        record = {
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "port": args.port,
            "address": address,
            "machine": args.machine,
            "expected_serial": expect_serial,
            "expected_asset": expect_asset,
            "seen_serial": seen_serial,
            "seen_asset": seen_asset,
            "result": "PASS" if code == OK else "FAIL",
            "exit_code": code,
        }
        with args.log.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
        print(f"recorded in {args.log}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
