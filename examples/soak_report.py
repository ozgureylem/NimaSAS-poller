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
from collections import Counter
from datetime import datetime

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
        print(f"snapshot gap     median {median:.1f}s, worst {worst:.1f}s", end="")
        print("  <- investigate: the loop stalled" if worst > max(median * 10, 60) else "")

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

    # --- did any meter go backwards? -------------------------------------
    # Meters are cumulative, so a decrease means a RAM clear, a decode fault,
    # or a meter read from the wrong machine. All three matter; none would
    # show up as a poll error.
    print()
    meter_cols = [c for c in columns(conn, "meters_history")
                  if c not in ("id", "polled_at")]
    regressions = []
    for col in meter_cols:
        rows = [r[0] for r in conn.execute(
            f'SELECT "{col}" FROM meters_history ORDER BY polled_at') if r[0] is not None]
        for i in range(len(rows) - 1):
            if rows[i + 1] < rows[i]:
                regressions.append((col, rows[i], rows[i + 1]))
                break
    if regressions:
        print(f"meters that decreased ({len(regressions)}):")
        for col, before, after in regressions[:15]:
            print(f"    {col:38} {before} -> {after}")
        print("  => A cumulative meter must never fall. RAM clear, decode fault,")
        print("     or the wrong machine. Resolve before trusting this data.")
    else:
        print(f"meter monotonicity  OK across {len(meter_cols)} columns")

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
