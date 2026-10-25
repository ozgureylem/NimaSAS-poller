"""Write one machine's complete readable state to a committable JSON file.

Read-only. Meter polls, identity polls and one query-only 0x74; nothing changes
machine state.

The point is a file that can be committed, so the gateway's view of a machine
travels with the repo and anyone -- or anything -- reading the repo later is
reading real figures rather than figures quoted from memory into a chat. Each
snapshot is a separate file, so `git log` on the folder is the machine's
history and `git diff` between two of them is exactly what moved.

    python3 examples/meter_snapshot.py /dev/ttyS0 --address 1
    python3 examples/meter_snapshot.py /dev/ttyS0 --address 1 --note "after 3.00 promo push"

WHERE THESE BELONG. A snapshot carries the machine's asset number, serial
number, and its complete accounting history -- coin in, coin out, drop,
cashless totals. That is operator business data. Commit it to the PRIVATE
repository. Do not mirror captures/ into a public mirror: a public repo is
indexed and forked, and a file removed later stays in the history, so the
decision is effectively one-way. This tool writes the folder's own README
saying so the first time it runs, but nothing here can enforce it.

Values are RAW, exactly as the machine reported them, in accounting units --
not divided, not rounded, not relabelled. A snapshot is evidence; interpreting
it is a separate job and belongs in the tools that interpret
(accounting_summary.py, meter_crosscheck.py). Where a meter did not answer,
the value is null and the code is listed under "unsupported", which is a fact
about the machine worth recording rather than a gap to paper over.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.client import SASClient
from saspy.constants import LongPoll, MeterCode
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

CHUNK = 12  # LP 0x6F's own per-request limit (§7.21)

README = """# captures/

Machine state snapshots written by `examples/meter_snapshot.py`.

One JSON file per snapshot, named by UTC timestamp and asset number. Values are
raw, in accounting units, exactly as the machine reported them.

**These are private.** A snapshot carries the machine's asset number, serial
number and complete accounting history. Keep this folder in the private
repository only. Do not copy it into a public mirror -- a public repo is indexed
and forked, and deleting a file later does not remove it from the history.

`git diff` between two snapshots shows what moved between them. `INDEX.md` lists
them with their headline figures.
"""


def git_commit() -> str | None:
    """Which revision of this gateway took the reading."""
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent.parent),
             "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def read_meters(client) -> tuple[dict[str, int | None], list[str]]:
    """Every Table C-7 code this client knows, via LP 0x6F/0xAF in chunks.

    §7.21b makes 0x6F self-describing per meter: a code the machine does not
    implement comes back with size 0, which is 'absent', not 'failed'. Those
    become null here and are named separately. Alternating 0x6F/0xAF is what
    lets consecutive meter polls be distinguished from an implied NACK (§3.2,
    and the spec's own 6.00 note on long poll AF).
    """
    codes = list(MeterCode)
    values: dict[str, int | None] = {}
    unsupported: list[str] = []
    for i in range(0, len(codes), CHUNK):
        chunk = codes[i:i + CHUNK]
        alternate = (i // CHUNK) % 2 == 1
        poll = (client.send_extended_meters_alternate if alternate
                else client.send_extended_meters)
        try:
            result = poll([int(c) for c in chunk])
        except SASError as e:
            print(f"  chunk {i // CHUNK + 1} failed ({type(e).__name__}: {e});"
                  " its meters are recorded as null", file=sys.stderr)
            for code in chunk:
                values[f"{int(code):04X}"] = None
            continue
        for code in chunk:
            got = result.meters.get(int(code))
            values[f"{int(code):04X}"] = got
            if got is None:
                unsupported.append(f"{int(code):04X}")
    return values, unsupported


def try_call(label: str, fn, *args):
    """Identity polls are optional per §4.4; record silence as silence."""
    try:
        return fn(*args)
    except SASError as e:
        print(f"  {label}: no answer ({type(e).__name__})", file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--dir", default="captures",
                    help="where to write (default: captures/, relative to cwd)")
    ap.add_argument("--note", default=None,
                    help="what this snapshot is of -- 'before the soak', 'after a "
                         "3.00 promo push'. Recorded in the file.")
    args = ap.parse_args()

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(
        SASTransport(port, byte_timeout=args.timeout), args.address, timeout=args.timeout)

    taken_at = dt.datetime.now(dt.timezone.utc)
    print(f"Reading {args.port} address {args.address} ...", file=sys.stderr)

    version = try_call("LP 0x54 version/serial", client.send_sas_version_and_serial)
    machine = try_call("LP 0x1F machine id", client.send_gaming_machine_id)
    lock = try_call("LP 0x74 lock/status", client.aft_game_lock_and_status_request, 0xFF)
    games = try_call("LP 0x51 games implemented",
                     client.send_meter, LongPoll.SEND_TOTAL_GAMES_IMPLEMENTED)

    meters, unsupported = read_meters(client)

    asset = lock.asset_number if lock is not None else None
    snapshot = {
        "taken_at_utc": taken_at.isoformat(timespec="seconds"),
        "note": args.note,
        "gateway": {
            "port": args.port,
            "sas_address": args.address,
            "baud": args.baud,
            "repo_commit": git_commit(),
        },
        "identity": {
            "asset_number": asset,
            "serial_number": version.serial_number.strip() if version else None,
            "sas_version": version.sas_version.strip() if version else None,
            "game_id": machine.game_id.strip() if machine else None,
            "additional_id": machine.additional_id.strip() if machine else None,
            "denomination_code": machine.denomination if machine else None,
            "max_bet": machine.max_bet if machine else None,
            "games_implemented": games,
        },
        # From 0x74, the only native source for the nonrestricted balance --
        # Table C-7 stops at 000C and 001B. In cents.
        "balances_cents": None if lock is None else {
            "cashable": lock.current_cashable_amount,
            "restricted": lock.current_restricted_amount,
            "nonrestricted": lock.current_non_restricted_amount,
        },
        "meters_raw": meters,
        "unsupported_meter_codes": unsupported,
    }

    out_dir = Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    readme = out_dir / "README.md"
    if not readme.exists():
        readme.write_text(README)
        print(f"wrote {readme}", file=sys.stderr)

    stamp = taken_at.strftime("%Y-%m-%dT%H-%M-%SZ")
    name = f"{stamp}-asset{asset}.json" if asset is not None else f"{stamp}.json"
    path = out_dir / name
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")

    print(f"\nwrote {path}")
    print(f"  {sum(1 for v in meters.values() if v is not None)} of {len(meters)}"
          f" meters answered; {len(unsupported)} not implemented")
    if lock is not None:
        total = (lock.current_cashable_amount + lock.current_restricted_amount
                 + lock.current_non_restricted_amount)
        print(f"  on the machine now: {total / 100:,.2f}"
              f"  ({lock.current_cashable_amount / 100:,.2f} cashable"
              f" + {lock.current_restricted_amount / 100:,.2f} restricted"
              f" + {lock.current_non_restricted_amount / 100:,.2f} nonrestricted)")
    print("\nCommit it to the PRIVATE repo:")
    print(f"  git add {path} {readme}")
    print('  git commit -m "capture: snapshot of asset '
          f'{asset if asset is not None else "?"}"')
    print("  git push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
