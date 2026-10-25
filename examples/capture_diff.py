"""What moved between two snapshots, by name, with the identities re-checked.

Offline. Reads two JSON files written by meter_snapshot.py; touches no hardware.

`git diff` between two snapshots already shows which numbers changed, but it
shows them as four-digit hex codes and raw accounting units, which is not a
form anyone can read a test result out of. This names each meter, prints the
delta in currency, and separates what a test MOVED from what merely differs.

It also re-runs the identity checks on both ends, because the interesting case
is not a meter that changed -- it is an identity that held in the first
snapshot and stopped holding in the second. That is how the Total Out identity
was caught: 0004 matched the screen's Total Out until an attendant first paid
a win, then silently did not.

    python3 examples/capture_diff.py                    # the two most recent
    python3 examples/capture_diff.py BEFORE.json AFTER.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.constants import MeterCode

# (label, native code, component codes). Checked on BOTH snapshots so an
# identity that stops holding is visible as a change of state, not just as a
# number that moved.
IDENTITIES = [
    ("0022 total won        = 0001 + 0002", 0x22, (0x01, 0x02)),
    ("0023 total hand paid  = 0003 + 0002", 0x23, (0x03, 0x02)),
    ("0017 cashless in      = 002E+002F+0030+0031", 0x17, (0x2E, 0x2F, 0x30, 0x31)),
    ("0018 cashless out     = 0032+0033+0034", 0x18, (0x32, 0x33, 0x34)),
    ("0024 total drop       = 000B + 0017", 0x24, (0x0B, 0x17)),
    ("0004 cancelled credits= 0003 + 0018", 0x04, (0x03, 0x18)),
]

# (label, in, out, played, which balance of 0x74's three is this credit type).
# The balance matters: credit sitting ON the machine has not left by any route,
# so it is part of the shortfall and moves it every time money is pushed or
# spent. Netting it off leaves the amount that is genuinely unattributed, which
# is the figure that should stay still unless something new happened.
LEDGERS = [
    ("restricted  (screen 'Non-cashable')", 0x2F, 0x33, 0x19, "restricted"),
    ("nonrestricted (screen 'Promotional')", 0x30, 0x34, 0x1A, "nonrestricted"),
]


def is_count(code_hex: str) -> bool:
    """Whether this meter counts THINGS rather than money.

    Table C-7 mixes them freely and the wire says nothing about which is
    which, so dividing everything by 100 turns 665,755 games played into
    "6,657.55" -- a plausible-looking currency figure that is wrong by two
    decimal places and reads as money. The enum's own names carry the
    distinction, so take it from there rather than keeping a hand-list that
    drifts out of date as codes are added.
    """
    try:
        name = MeterCode(int(code_hex, 16)).name
    except ValueError:
        return False
    return (name.endswith("_QUANTITY") or "NUMBER_OF" in name
            or name.startswith("GAMES_"))


def is_percent(code_hex: str) -> bool:
    try:
        return MeterCode(int(code_hex, 16)) is MeterCode.WEIGHTED_AVERAGE_THEORETICAL_PAYBACK_PERCENTAGE
    except ValueError:
        return False


RAW = False   # set from --raw; currency formatting is a display choice only


def fmt(code_hex: str, value: int | None) -> str:
    if value is None:
        return "n/a"
    if is_count(code_hex) or RAW:
        return f"{value:,}"
    if is_percent(code_hex):
        return f"{value / 100:,.2f}%"
    return f"{value / 100:,.2f}"


def fmt_delta(code_hex: str, delta: int) -> str:
    if is_count(code_hex) or RAW:
        return f"{delta:+,}"
    return f"{delta / 100:+,.2f}"


def name_of(code_hex: str) -> str:
    try:
        return MeterCode(int(code_hex, 16)).name.lower().replace("_", " ")
    except ValueError:
        return "unassigned code"


def money(value: int | None) -> str:
    return "n/a" if value is None else f"{value / 100:,.2f}"


def get(snapshot: dict, code: int) -> int | None:
    return snapshot["meters_raw"].get(f"{code:04X}")


def check(snapshot: dict, native: int, parts: tuple[int, ...]) -> str:
    """HOLDS / BROKEN / incomplete -- the three states an identity can be in."""
    native_value = get(snapshot, native)
    part_values = [get(snapshot, c) for c in parts]
    if native_value is None or any(v is None for v in part_values):
        return "incomplete"
    return "HOLDS" if native_value == sum(part_values) else "BROKEN"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # Optional, because the overwhelmingly common case is "the last two", and
    # typing ISO-8601 filenames by hand is both tedious and a trap: a shell
    # reads <before>.json as a redirection, not as a placeholder, and answers
    # with "No such file or directory" about a file nobody meant to name.
    ap.add_argument("before", nargs="?",
                    help="older snapshot. Omit BOTH to use the two most recent "
                         "in --dir.")
    ap.add_argument("after", nargs="?", help="newer snapshot")
    ap.add_argument("--dir", default="captures",
                    help="where to look when no files are named (default: captures/)")
    ap.add_argument("--all", action="store_true",
                    help="also list meters that did not change")
    ap.add_argument("--raw", action="store_true",
                    help="show every meter in raw accounting units rather than as "
                         "currency. Counts and the payback percentage are unaffected: "
                         "they are already raw.")
    args = ap.parse_args()

    global RAW
    RAW = args.raw

    if args.before and args.after:
        before_path, after_path = Path(args.before), Path(args.after)
    elif args.before or args.after:
        print("Name both snapshots or neither.", file=sys.stderr)
        return 2
    else:
        # Snapshot names start with an ISO-8601 UTC timestamp, so sorting them
        # as text sorts them in time.
        found = sorted(Path(args.dir).glob("*.json"))
        if len(found) < 2:
            print(f"Need two snapshots in {args.dir}/ to compare; found "
                  f"{len(found)}. Name them explicitly, or point --dir elsewhere.",
                  file=sys.stderr)
            return 2
        before_path, after_path = found[-2], found[-1]
        print(f"\nComparing the two most recent snapshots in {args.dir}/:")
        print(f"  {before_path.name}\n  {after_path.name}")

    try:
        before = json.loads(before_path.read_text())
        after = json.loads(after_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"Could not read both snapshots: {e}", file=sys.stderr)
        return 2

    print(f"\nBEFORE  {before['taken_at_utc']}  {before.get('note') or '(no note)'}")
    print(f"AFTER   {after['taken_at_utc']}  {after.get('note') or '(no note)'}")

    if before["identity"].get("asset_number") != after["identity"].get("asset_number"):
        print("\n!! These are different machines -- asset "
              f"{before['identity'].get('asset_number')} vs "
              f"{after['identity'].get('asset_number')}. Nothing below is a delta.")

    if after["taken_at_utc"] < before["taken_at_utc"]:
        print("\n!! The 'after' snapshot is OLDER than the 'before' one. Every delta"
              " below has its sign inverted; check the order of the arguments.")

    print("\n--- meters that moved -------------------------------------------\n")
    codes = sorted(set(before["meters_raw"]) | set(after["meters_raw"]))
    moved = 0
    for code in codes:
        was, now = before["meters_raw"].get(code), after["meters_raw"].get(code)
        if was == now and not args.all:
            continue
        if was == now:
            print(f"    {code}  {name_of(code):<52} {fmt(code, now):>14}")
            continue
        moved += 1
        if was is None or now is None:
            # A meter that starts or stops answering is a change in the
            # MACHINE, not in its counters, and is worth saying out loud.
            print(f"  {code}  {name_of(code):<52} {fmt(code, was):>12} ->"
                  f" {fmt(code, now):>12}   (support changed)")
            continue
        delta = now - was
        print(f"  {code}  {name_of(code):<52} {fmt(code, was):>12} ->"
              f" {fmt(code, now):>12}  {fmt_delta(code, delta):>12}")
    if not moved:
        print("  nothing moved.")

    print("\n--- identities, before and after --------------------------------\n")
    for label, native, parts in IDENTITIES:
        was, now = check(before, native, parts), check(after, native, parts)
        flag = ""
        if was == "HOLDS" and now == "BROKEN":
            flag = "   <<< STOPPED HOLDING"
        elif was == "BROKEN" and now == "HOLDS":
            flag = "   <<< started holding"
        print(f"  {label:<44} {was:>10} -> {now:<10}{flag}")

    print("\n--- promotional ledgers (raw; in = out + played + on machine) ---\n")
    for label, in_code, out_code, played_code, balance_key in LEDGERS:
        line = []
        for snapshot in (before, after):
            got_in, got_out = get(snapshot, in_code), get(snapshot, out_code)
            played = get(snapshot, played_code)
            balances = snapshot.get("balances_cents") or {}
            on_machine = balances.get(balance_key)
            if None in (got_in, got_out, played, on_machine):
                line.append("incomplete")
                continue
            short = got_in - got_out - played - on_machine
            line.append("closes" if short == 0 else f"unattributed {short:,}")
        print(f"  {label:<44} {line[0]:>22} -> {line[1]}")
    print()
    print("  Raw accounting units, not currency: a ledger is held against itself,")
    print("  and dividing by 100 adds an assumption it does not need.")
    print()
    print("  Credit still ON the machine is netted off here -- it has not left by")
    print("  any route yet, and without that the figure moves every time money is")
    print("  pushed or spent, which tells you nothing.")
    print()
    print("  What remains is genuinely unattributed, and it should STAY STILL. If it")
    print("  is the same in both snapshots, this test added nothing to it. If it grew,")
    print("  this test is what grew it -- most often an attendant payment, which")
    print("  leaves through 0003, one meter covering every credit type with nothing")
    print("  in SAS attributing it to one.")

    for label, snapshot in (("BEFORE", before), ("AFTER", after)):
        balances = snapshot.get("balances_cents")
        if balances:
            total = sum(balances.values())
            print(f"\n  on the machine, {label}: {total / 100:,.2f}"
                  f"  ({balances['cashable'] / 100:,.2f} cashable"
                  f" + {balances['restricted'] / 100:,.2f} restricted"
                  f" + {balances['nonrestricted'] / 100:,.2f} nonrestricted)")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
