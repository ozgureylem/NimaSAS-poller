#!/usr/bin/env python3
"""What an outage cost: credits played while nobody was polling, against
the events the cabinet could still hand back afterwards.

READ-ONLY. Opens the database `mode=ro` and touches no machine.

WHY THIS EXISTS. On 2026-11-08 the lab poller died on its own after about
eighteen hours, on a gateway that stayed up — the last meter reading was
17:15:14 UTC and nobody noticed for 46 minutes. The operator's reading of
it was the right one:

    "This is a perfect example of what may go on when we're live. Let's
     see how much of RTE is lost."

A staged outage is a test. This was not staged, nobody chose when it
started, and the cabinet was doing whatever it does at that hour. That
makes it the closest thing to floor evidence this project has.

THE ARITHMETIC, and every term comes from somewhere different:

    played    = total_coin_in AFTER the gap  -  total_coin_in BEFORE it
                a cumulative meter, which keeps counting with no host
                attached. This is the truth the events are measured
                against.

    recovered = the wagers of game starts the cabinet handed back when
                polling resumed, stored with source 'startup-backlog'.
                §2.2.1's queue is non-volatile, so a cabinet holds what
                it can and surrenders it on the next poll.

    lost      = played - recovered, which is the number §19 is about:
                turnover with no event behind it, so a player's bets that
                never register.

WHAT THE QUEUE DEPTH PREDICTS. §2.2.1 guarantees at least 20 elements and
this cabinet measured ≥67. Under RTE a game is two events, so 67 elements
is about 33 games — roughly 80 seconds at 24 games a minute. Everything
before that is gone by arithmetic, not by misfortune, and no polling rate
on the host side recovers it. A long outage therefore loses almost
everything: the recovery is bounded by the queue, not by the gap.

    python3 examples/outage_loss.py gateway.sqlite3
    python3 examples/outage_loss.py gateway.sqlite3 --min-gap 300
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BUFFER_OVERFLOW = 0x70
GAME_START = 0x7E


def find_gaps(conn, min_gap: float) -> list[dict]:
    """Consecutive meter readings further apart than a cycle should be.

    The meter table is the right place to look rather than the event
    table: events stop arriving for lots of reasons, but a missing METER
    reading means the poller itself was not there.
    """
    rows = conn.execute(
        "SELECT polled_at, total_coin_in, games_played FROM meters_history "
        "WHERE total_coin_in IS NOT NULL ORDER BY id").fetchall()
    out = []
    for (t0, c0, g0), (t1, c1, g1) in zip(rows, rows[1:]):
        gap = _seconds(t0, t1)
        if gap is None or gap < min_gap:
            continue
        out.append({"from": t0, "to": t1, "seconds": round(gap),
                    "coin_in_before": c0, "coin_in_after": c1,
                    "played": (c1 - c0) if None not in (c0, c1) else None,
                    "games_before": g0, "games_after": g1,
                    "games": (g1 - g0) if None not in (g0, g1) else None})
    return out


def _seconds(a: str, b: str) -> float | None:
    import datetime as dt
    try:
        return (dt.datetime.fromisoformat(b) - dt.datetime.fromisoformat(a)
                ).total_seconds()
    except (ValueError, TypeError):
        return None


def recovered_after(conn, gap_end: str, until: str | None = None) -> dict:
    """What the cabinet handed back once polling resumed.

    Scoped to events stored as 'startup-backlog', which is precisely the
    drain the poller does before its first cycle. An event that arrived
    later than that drain belongs to play AFTER the outage and is not
    recovery.

    `until` IS NOT OPTIONAL IN PRACTICE, and the first version of this
    treated it as though it were. With no upper bound every LATER
    restart's drain was counted against every EARLIER gap, so the lab
    database reported 266 game starts recovered from an outage in which
    0 games were played, and losses came out negative. A number that
    cannot happen is the cheapest kind of bug to catch and the easiest to
    publish: it had already been run on real data before anyone looked at
    the sign.

    The bound is the start of the NEXT outage, because the drain belongs
    to the restart that ended this one and nothing else intervenes.
    """
    if until:
        rows = conn.execute(
            "SELECT exception_code, rte_fields FROM machine_events "
            "WHERE source = 'startup-backlog' AND occurred_at >= ? "
            "AND occurred_at < ? ORDER BY id", (gap_end, until)).fetchall()
    else:
        rows = conn.execute(
            "SELECT exception_code, rte_fields FROM machine_events "
            "WHERE source = 'startup-backlog' AND occurred_at >= ? "
            "ORDER BY id", (gap_end,)).fetchall()
    wagered = 0
    starts = overflows = 0
    for code, fields in rows:
        if int(code) == BUFFER_OVERFLOW:
            overflows += 1
            continue
        if int(code) != GAME_START:
            continue
        starts += 1
        try:
            f = json.loads(fields) if fields else {}
        except ValueError:
            continue
        w = f.get("wager_cents")
        if isinstance(w, int):
            wagered += w
    return {"events": len(rows), "game_starts": starts,
            "wagered": wagered, "overflows": overflows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("--all-history", action="store_true",
                    help="ignore LEGACY-CUTOFF and price every outage this "
                         "database remembers. The default stops at the "
                         "cutoff, because outages from a build that no "
                         "longer exists are archaeology.")
    ap.add_argument("--min-gap", type=float, default=180.0, metavar="SECONDS",
                    help="how long a hole in the meter history has to be "
                         "before it counts as an outage (default: 180). A "
                         "cycle is about a minute, so anything under three "
                         "is a slow sweep rather than an absence.")
    args = ap.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"no database at {path}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    cutoff = None
    if not args.all_history:
        try:
            for line in (Path(__file__).resolve().parent.parent
                         / "LEGACY-CUTOFF").read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    cutoff = line
                    break
        except OSError:
            pass

    gaps = [g for g in find_gaps(conn, args.min_gap)
            if cutoff is None or g["from"] >= cutoff]
    print("=" * 72)
    print(f"Outage loss: {path}")
    if cutoff:
        print(f"scoped to the LEGACY CUTOFF {cutoff}; --all-history for the rest")
    print("=" * 72)
    if not gaps:
        print(f"\nNo hole longer than {args.min_gap:.0f}s in the meter "
              f"history. Nothing to measure.")
        return 0

    print(f"\n{len(gaps)} outage(s) of {args.min_gap:.0f}s or more:\n")
    total_played = total_recovered = 0
    for i, g in enumerate(gaps):
        nxt = gaps[i + 1]["from"] if i + 1 < len(gaps) else None
        rec = recovered_after(conn, g["to"], nxt)
        played = g["played"]
        lost = None if played is None else played - rec["wagered"]
        print(f"  {g['from'][:19]} -> {g['to'][:19]}  "
              f"({g['seconds'] / 60:.0f} min)")
        if played is None:
            print("      coin in unreadable at one end; nothing to compare")
            continue
        total_played += played
        total_recovered += rec["wagered"]
        games = f"  ({g['games']:,} games)" if g["games"] is not None else ""
        print(f"      played while nobody polled : {played:>12,} credits{games}")
        print(f"      recovered from the queue   : {rec['wagered']:>12,} credits"
              f"  ({rec['game_starts']} game start(s))")
        # NOBODY PLAYED is a clean result, not a total loss. An outage on an
        # idle cabinet costs nothing, and printing "100% lost" for it buries
        # the ones that cost something.
        if played == 0:
            print("      nothing was played during this outage, so nothing "
                  "was lost")
            if rec["wagered"]:
                print(f"      (the {rec['wagered']:,} credits above are the "
                      f"queue handing back play from BEFORE the gap)")
            print()
            continue
        if lost < 0:
            print(f"      ** recovered MORE than the meters say was played "
                  f"({-lost:,} credits over). The drain is being credited "
                  f"with events from another outage, or the queue held play "
                  f"from before this gap. Not a loss figure; do not quote it.")
            print()
            continue
        print(f"      LOST                       : {lost:>12,} credits"
              f"  ({100.0 * lost / played:.1f}% of it)")
        if rec["overflows"]:
            print(f"      the cabinet reported {rec['overflows']} buffer "
                  f"overflow(s) on reconnect: its own admission that the "
                  f"queue could not hold it all")
        elif played and rec["wagered"] < played:
            print("      NO 0x70 on reconnect, and the figures still do not "
                  "close. Either the machine did not report the overflow, "
                  "or the drain missed events it did hand back -- §2.2.1 "
                  "says it SHOULD report, so this is worth a look.")
        print()

    if total_played:
        print("-" * 72)
        pct = 100.0 * total_recovered / total_played
        print(f"  Across every outage: {total_played:,} credits played, "
              f"{total_recovered:,} recovered, "
              f"{total_played - total_recovered:,} LOST ({100.0 - pct:.1f}%).")
        print("  The recovery is bounded by §2.2.1's queue, not by the length")
        print("  of the gap: this cabinet holds about 33 games' worth, so a")
        print("  long outage loses nearly all of it however fast the host")
        print("  polls when it comes back. That is the number §19 is about --")
        print("  turnover with no event behind it, which is a player's bets")
        print("  that never register.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
