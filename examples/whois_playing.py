"""What is actually driving this cabinet's meters?

WHY IT EXISTS. On 2026-11-10 the lab gateway's database showed 216,000
games and 28.7 million credits of coin in across eight days, at a steady
1,100 games an hour, on a cabinet the operator said had last been played
on the 9th. The payout ratio over a sampled 24 minutes was 95.2% -- a
textbook RTP, which is not what a misread meter produces.

Three explanations fit that shape, and they need different answers:

  1. The cabinet is playing ITSELF -- a demo, attract or autoplay mode, or
     a stuck button. Play is then CONTINUOUS and FLAT, around the clock.
  2. The gateway is reading a DIFFERENT machine than anyone thinks. The
     meters jump at the moment the cable moved, and machine_epochs records
     the serial it saw either side.
  3. Somebody is playing it. Play is then BURSTY and stops overnight.

The hourly profile tells 1 from 3 at a glance, and the epoch table settles
2. Nothing here opens the serial port, so it is safe to run against a
gateway that is mid-soak -- one port, one master.

    python3 examples/whois_playing.py
    python3 examples/whois_playing.py ~/NimaSAS/gateway.sqlite3 --hours 72

A NOTE ON ORDERING, because it cost an hour. `ORDER BY id LIMIT 40` on
meters_history returns the OLDEST rows. On an --mode append database that
has been running for days, that is the beginning of the run, not the
present, and it reads exactly like a stale file. Everything here is DESC.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys


def _has(conn, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("database", nargs="?", default="gateway.sqlite3",
                   help="the gateway database (default: %(default)s)")
    p.add_argument("--hours", type=int, default=48,
                   help="how many hours of profile to show (default: %(default)s)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    db = args.database
    if not os.path.exists(db):
        print(f"{db}: no such file", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    q = lambda s, *a: conn.execute(s, a).fetchall()

    print(f"=== {os.path.abspath(db)}  ({os.path.getsize(db) / 1e6:.0f} MB)\n")

    print("--- how the poller was started (its own argv) ---")
    if _has(conn, "poller_lifecycle"):
        rows = q("SELECT at, event, argv FROM poller_lifecycle "
                 "ORDER BY id DESC LIMIT 4")
        for at, event, argv_text in rows or []:
            print(f"  {at}  {event}")
            print(f"     {argv_text}")
        if not rows:
            print("  (no lifecycle rows)")
    else:
        print("  (table absent -- database predates it)")
    print()

    print("--- machine identity, and every epoch boundary ---")
    print("  A new epoch with a DIFFERENT serial means the cable moved.")
    if _has(conn, "machine_epochs"):
        rows = q("SELECT id, started_at, ended_at, reason, serial_number, "
                 "asset_number FROM machine_epochs ORDER BY id DESC LIMIT 10")
        for r in rows or []:
            print("  " + " | ".join("-" if x is None else str(x) for x in r))
        if not rows:
            print("  (no epochs recorded)")
    else:
        print("  (table absent)")
    print()

    print("--- the LIVE tail (newest first) ---")
    for r in q("SELECT polled_at, games_played, total_coin_in, total_coin_out "
               "FROM meters_history ORDER BY id DESC LIMIT 8"):
        print("  " + " | ".join(str(x) for x in r))
    print()

    print(f"--- games per hour, last {args.hours} h ---")
    print("  FLAT around the clock = the machine is playing itself.")
    print("  Bursty, with quiet nights = somebody is playing it.")
    rows = q("""
        WITH h AS (
          SELECT substr(polled_at, 1, 13) hr,
                 min(games_played) g0, max(games_played) g1,
                 min(total_coin_in) c0, max(total_coin_in) c1
          FROM meters_history
          WHERE games_played IS NOT NULL
          GROUP BY hr ORDER BY hr DESC LIMIT ?)
        SELECT hr, g1 - g0, c1 - c0 FROM h ORDER BY hr
    """, args.hours)
    peak = max((g or 0) for _, g, _ in rows) if rows else 0
    for hr, games, coin in rows:
        games, coin = games or 0, coin or 0
        bar = "#" * (int(games / peak * 54) if peak else 0)
        print(f"  {hr}Z {games:6d} games {coin:10d} coin_in {bar}")
    if rows:
        tot = sum(g or 0 for _, g, _ in rows)
        live = sum(1 for _, g, _ in rows if (g or 0) > 0)
        print(f"\n  {tot:,} games over {len(rows)} hour(s); "
              f"{live} of them had play.")
        if live == len(rows) and len(rows) > 12:
            print("  EVERY hour had play. A person does not do that.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
