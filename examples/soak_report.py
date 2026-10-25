"""Read a soak's database and say whether the link held.

Read-only, and it touches no serial port -- run it while the soak is still
going, as often as you like.

A soak answers a different question from the probes in this repo. Those ask
"does this exchange work"; a soak asks "does it keep working", and the
failure mode that matters is not a low success rate but a BURSTY one. On
this project's first machine a bare general poll measured 0/20, then 13/20,
then 30/30 across runs minutes apart, so an overall percentage averaged over
hours would have described none of those. This therefore buckets failures by
hour and by minute, and reports the worst minute as well as the mean: fifty
failures evenly spread is a tuning problem, fifty in one minute is an outage
and would look identical in a single percentage.

    python3 examples/soak_report.py soak.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from collections import Counter
from datetime import datetime

# Columns that are NOT cumulative, and so may legitimately fall.
#
# The monotonicity check below assumed every meter column only ever rises.
# On its first real soak it flagged five decreases and all five were correct
# behaviour: current credits is a BALANCE that falls on every wager, the
# "games since" counters reset to zero by definition when the door closes or
# power cycles, the stacker levels drop when the cashbox is emptied, and the
# selected game number is an identifier rather than a meter at all. A check
# wrong five times out of five is worse than no check -- it teaches the
# reader to skim past the one that matters.
#
# Matched as substrings so both the single-meter (sm_) and Table C-7 (c7_)
# copies of the same quantity are covered, since this tool reads several
# meters by two routes deliberately.
NOT_CUMULATIVE = (
    "current_credits",
    "current_restricted_credits",
    "games_since",
    "selected_game_number",
    "currently_in_stacker",
    "hopper_level",
)


def is_cumulative(column: str) -> bool:
    return not any(fragment in column for fragment in NOT_CUMULATIVE)


# The meters worth eyeballing against the machine's own accounting screen,
# which is the only check here that does not come from inside the database.
SCREEN_METERS = (
    ("total_coin_in", "Total Bet (Coin In)"),
    ("total_coin_out", "Total Won"),
    ("total_drop", "Total In"),
    ("total_cancelled_credits", "Total Cancelled Credits"),
)


def parse_ts(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def table_exists(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)
    ).fetchone()
    return row is not None


def columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="the poller's --interval, used to estimate expected cycles (default 1.0)")
    args = ap.parse_args()

    # "unable to open database file" is sqlite's message for a path that does
    # not exist AND for one it cannot read, which sends people looking for a
    # permissions problem when the poller simply never ran.
    if not Path(args.db).exists():
        print(f"{args.db} does not exist.", file=sys.stderr)
        print("\nNothing has written it yet. Run the poller first, e.g.:", file=sys.stderr)
        print(f"  python3 examples/sql_poll_logger.py gateway.ini --db {args.db} "
              f"--cycles 3 --interval 1.0", file=sys.stderr)
        print("\nand if gateway.ini does not exist either:", file=sys.stderr)
        print("  printf '[connection]\\nport = /dev/ttyS0\\naddress = 1\\n' > gateway.ini",
              file=sys.stderr)
        return 2
    try:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    except sqlite3.Error as e:
        print(f"Could not open {args.db}: {e}", file=sys.stderr)
        return 2

    if not table_exists(conn, "meters_history"):
        print(f"{args.db} has no meters_history table -- is it a soak database?", file=sys.stderr)
        return 2

    # --- span -----------------------------------------------------------
    row = conn.execute(
        "SELECT MIN(polled_at), MAX(polled_at), COUNT(*) FROM meters_history"
    ).fetchone()
    first, last, snapshots = row
    t0, t1 = parse_ts(first), parse_ts(last)
    span = (t1 - t0).total_seconds() if t0 and t1 else 0.0

    print(f"=== {args.db} ===")
    print(f"first snapshot   {first}")
    print(f"last snapshot    {last}")
    if span:
        print(f"span             {span / 3600:.2f} h ({span:.0f}s)")
    print(f"meter snapshots  {snapshots}")

    # --- did the loop ever stall? ----------------------------------------
    # Snapshots are written on a schedule, so an unusually long gap between
    # two of them is the loop wedged or the process dead, which no count of
    # poll errors would show.
    stamps = [parse_ts(r[0]) for r in conn.execute(
        "SELECT polled_at FROM meters_history ORDER BY polled_at")]
    stamps = [s for s in stamps if s]
    gaps = [(stamps[i + 1] - stamps[i]).total_seconds() for i in range(len(stamps) - 1)]
    if gaps:
        median = sorted(gaps)[len(gaps) // 2]
        worst = max(gaps)
        worst_at = stamps[gaps.index(worst) + 1]
        print(f"snapshot gap     median {median:.1f}s, worst {worst:.1f}s", end="")
        if worst > max(median * 10, 60):
            print(f"  <- the loop stalled, ending {worst_at:%Y-%m-%d %H:%M:%S}")
            print(f"                 cross-reference that minute against the errors below; "
                  f"a stall\n                 of {worst / median:.0f} cycles is a run of "
                  f"aborted cycles, not one slow one")
        else:
            print()

    # --- failures, and crucially how they are distributed ----------------
    print()
    if not table_exists(conn, "poll_errors"):
        print("no poll_errors table")
    else:
        total = conn.execute("SELECT COUNT(*) FROM poll_errors").fetchone()[0]
        print(f"poll errors      {total}")
        if total:
            print("\n  by poll:")
            for name, n in conn.execute(
                "SELECT poll_name, COUNT(*) FROM poll_errors GROUP BY poll_name "
                "ORDER BY COUNT(*) DESC LIMIT 12"):
                print(f"    {name:34} {n}")
            print("\n  by error type:")
            for etype, n in conn.execute(
                "SELECT error_type, COUNT(*) FROM poll_errors GROUP BY error_type "
                "ORDER BY COUNT(*) DESC"):
                print(f"    {etype:34} {n}")

            by_minute = Counter()
            by_hour = Counter()
            for (occurred,) in conn.execute("SELECT occurred_at FROM poll_errors"):
                ts = parse_ts(occurred)
                if ts:
                    by_minute[ts.strftime("%Y-%m-%d %H:%M")] += 1
                    by_hour[ts.strftime("%Y-%m-%d %H")] += 1

            print("\n  per hour:")
            for hour in sorted(by_hour):
                n = by_hour[hour]
                print(f"    {hour}  {n:5}  {'#' * min(n, 50)}")

            if by_minute:
                worst_minute, worst_n = by_minute.most_common(1)[0]
                mean_per_min = total / max(span / 60, 1)
                print(f"\n  worst minute   {worst_minute}  {worst_n} failures")
                print(f"  mean per min   {mean_per_min:.2f}")
                if worst_n > max(mean_per_min * 10, 10):
                    print("  => BURSTY. The failures are clustered, not a steady rate,")
                    print("     so an overall percentage describes none of them. Look at")
                    print("     what happened at that minute before tuning anything.")
                else:
                    print("  => Spread fairly evenly; a rate, not an outage.")

    # --- what was the machine doing? -------------------------------------
    # The point of this section: an 18-hour soak showed 81 core-meter timeouts
    # and a 116-second stall with nothing in the database to explain either.
    # The explanation was a technician with the slot door open for most of the
    # run -- which the machine had reported over the wire as exceptions 0x11,
    # 0x12, 0x82 and 0x83, and which the poller used to discard. A failure log
    # without the machine's own state beside it cannot be read.
    # Two separate reasons a run is not a clean stability measurement, kept
    # apart because they invalidate it differently.
    #
    # The first version of this listed only the slot door, the menus, options
    # changed and the meter reset -- and then printed "nobody was at the
    # machine" on a run whose own event table showed a belly door opened three
    # times and AC power lost. It contradicted the data three lines above it.
    # The lesson is the same one this file already records about the
    # monotonicity check: a verdict that confidently states the opposite of
    # its own evidence is worse than printing nothing. Every door, every
    # access panel and every attendant action from Appendix A is here now.
    #
    # Note on reading them: on the EGT Premier the BILL VALIDATOR door is
    # reported as 0x1D/0x1E, which Appendix A names "belly door". The
    # machine's own log said "Bill Validator door was just opened" for the
    # same event. Expect that kind of mapping per model.
    ATTENDED = {
        0x11: "slot door opened", 0x12: "slot door closed",
        0x13: "drop door opened", 0x14: "drop door closed",
        0x15: "card cage opened", 0x16: "card cage closed",
        0x19: "cashbox door opened", 0x1A: "cashbox door closed",
        0x1B: "cashbox removed", 0x1C: "cashbox installed",
        0x1D: "belly door opened (bill validator door on some models)",
        0x1E: "belly door closed (bill validator door on some models)",
        0x3A: "memory error reset (operator used self test switch)",
        0x3C: "operator changed options",
        0x7A: "soft meters reset to zero",
        0x7B: "bill validator totals reset by an attendant",
        0x82: "attendant/meter menu entered", 0x83: "attendant/meter menu exited",
        0x84: "operator menu entered", 0x85: "operator menu exited",
        0x86: "out of service (by attendant)",
        0x98: "power off card cage access", 0x99: "power off slot door access",
        0x9A: "power off cashbox door access", 0x9B: "power off drop door access",
    }
    # Not someone at the machine -- the machine not running. Equally fatal to a
    # stability number, for a different reason: the link cannot be measured
    # across an interval when one end was off.
    INTERRUPTED = {
        0x17: "AC power was APPLIED to the machine",
        0x18: "AC power was LOST from the machine",
        0x31: "CMOS RAM error (data recovered from EEPROM)",
        0x32: "CMOS RAM error (no data recovered)",
        0x33: "CMOS RAM error (bad device)",
        0x70: "exception buffer overflow",
    }
    print()
    if not table_exists(conn, "machine_events"):
        print("no machine_events table -- this database predates exception recording,")
        print("so a failure cluster here cannot be correlated with what the machine")
        print("was doing. Re-soak to get that.")
    else:
        total_events = conn.execute("SELECT COUNT(*) FROM machine_events").fetchone()[0]
        print(f"machine events   {total_events}")
        if total_events:
            print("\n  most frequent:")
            for code, desc, n in conn.execute(
                "SELECT exception_code, description, COUNT(*) FROM machine_events "
                "GROUP BY exception_code ORDER BY COUNT(*) DESC LIMIT 10"):
                print(f"    0x{code:02X}  x{n:<6} {desc}")

            def listed(codes, heading, explanation):
                rows = list(conn.execute(
                    "SELECT occurred_at, exception_code, description FROM machine_events "
                    f"WHERE exception_code IN ({','.join(str(c) for c in codes)}) "
                    "ORDER BY occurred_at"))
                if not rows:
                    return False
                print(f"\n  {heading} ({len(rows)} event(s)):")
                for occurred, code, _desc in rows[:25]:
                    print(f"    {occurred}  0x{code:02X}  {codes[code]}")
                if len(rows) > 25:
                    print(f"    ... {len(rows) - 25} more")
                for line in explanation:
                    print(f"  {line}")
                return True

            touched = listed(
                ATTENDED, "SOMEONE WAS AT THE MACHINE",
                ["=> Failures overlapping these are not link failures. A machine with a",
                 "   door open or sitting in an attendant menu is being serviced, and",
                 "   section 8.3 names exactly those states as ones where it cannot act",
                 "   normally."])
            down = listed(
                INTERRUPTED, "THE MACHINE WAS INTERRUPTED",
                ["=> A link cannot be measured across an interval when one end was off.",
                 "   Failures bracketing a power event are the outage, not the link, and",
                 "   the stall above is almost certainly the same event."])

            if touched or down:
                print("\n  => Treat this run as a robustness test rather than a stability")
                print("     measurement, and re-soak undisturbed if stability is the")
                print("     number you need.")
            else:
                print("\n  No door, access-panel, attendant or power events: the machine")
                print("  was undisturbed and running throughout. Failures here are the")
                print("  link's own, which is what a stability measurement needs.")

    # --- did any meter go backwards? -------------------------------------
    # Meters are cumulative, so a decrease means a RAM clear, a decode fault,
    # or a meter read from the wrong machine. All three matter; none would
    # show up as a poll error.
    print()
    all_cols = [c for c in columns(conn, "meters_history") if c not in ("id", "polled_at")]
    meter_cols = [c for c in all_cols if is_cumulative(c)]
    skipped = [c for c in all_cols if not is_cumulative(c)]
    regressions = []
    cleared = []          # fell from non-zero straight to zero
    populated = 0         # columns that ever held a non-zero value
    for col in meter_cols:
        rows = [r[0] for r in conn.execute(
            f'SELECT "{col}" FROM meters_history ORDER BY polled_at') if r[0] is not None]
        if any(v > 0 for v in rows):
            populated += 1
        for i in range(len(rows) - 1):
            if rows[i + 1] < rows[i]:
                regressions.append((col, rows[i], rows[i + 1]))
                if rows[i] > 0 and rows[i + 1] == 0:
                    cleared.append(col)
                break
    if regressions:
        # One cumulative meter falling is a defect to chase. Nearly all of them
        # falling to zero together is a RAM clear, and listing it as 150
        # separate defects buries that under its own evidence.
        mass_clear = populated and len(cleared) >= max(5, populated * 0.8)
        if mass_clear:
            print(f"RAM CLEAR: {len(cleared)} of {populated} populated cumulative "
                  "meters went to zero together.")
            print("  => Not a fault and not a decode problem -- cumulative meters")
            print("     cannot fall, only be cleared. Everything before that point")
            print("     belongs to the machine's previous life and does not")
            print("     reconcile against anything after it. Split this database at")
            print("     the clear, or start a fresh one; a soak that straddles a RAM")
            print("     clear measures two different machines.")
            others = [r for r in regressions if r[0] not in cleared]
            if others:
                print(f"\n  {len(others)} meter(s) also fell WITHOUT reaching zero,")
                print("  which a RAM clear does not explain:")
                for col, was, now in others[:10]:
                    print(f"    {col:38} {was} -> {now}")
        else:
            print(f"meters that decreased ({len(regressions)}):")
            for col, before, after in regressions[:15]:
                print(f"    {col:38} {before} -> {after}")
            print("  => A cumulative meter must never fall. RAM clear, decode fault,")
            print("     or the wrong machine. Resolve before trusting this data.")
    else:
        print(f"meter monotonicity  OK across {len(meter_cols)} cumulative columns")
    if skipped:
        print(f"  ({len(skipped)} columns excluded as not cumulative -- balances, "
              f"\"since\" counters,")
        print("   stacker levels and the selected game number all fall legitimately)")

    # --- the one check from outside the database -------------------------
    if table_exists(conn, "meters_current"):
        cur = conn.execute("SELECT * FROM meters_current WHERE id = 1").fetchone()
        if cur:
            names = columns(conn, "meters_current")
            values = dict(zip(names, cur))
            print("\nCompare these against the machine's Machine Accounting screen:")
            for col, label in SCREEN_METERS:
                v = values.get(col)
                if v is not None:
                    print(f"    {label:28} {v / 100:>15,.2f}   (raw {v})")
            gp = values.get("games_played")
            if gp is not None:
                print(f"    {'Total Games Played':28} {gp:>15,}")
            print("  An exact match is the only validation here that does not come")
            print("  from inside this database.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
