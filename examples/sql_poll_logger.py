#!/usr/bin/env python3
"""Poll a gateway on a fixed interval and log what comes back to SQLite.

This is a worked, runnable example of the "local SQL layer on top of
SASClient" pattern described in MANUAL.md — a starting point for
prototyping gateway architecture, not a production drain/sync service.
Every poll failure is logged rather than raised, so one bad exchange
never stops the loop — which is the behavior you want during stress
testing, where the failures are the interesting part.

Usage:
    python3 examples/sql_poll_logger.py gateway.ini
    python3 examples/sql_poll_logger.py gateway.ini --db gateway.sqlite3 --interval 5
    python3 examples/sql_poll_logger.py gateway.ini --cycles 100   # stop after 100 cycles

``gateway.ini`` is the file commission_gateway.py writes (see MANUAL.md),
or one you hand-wrote in the same format.
"""

from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys
import time

from saspy.config import connect_from_config
from saspy.exceptions import SASError

SCHEMA = """
CREATE TABLE IF NOT EXISTS meter_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    polled_at TEXT NOT NULL,
    total_cancelled_credits INTEGER,
    total_coin_in INTEGER,
    total_coin_out INTEGER,
    total_drop INTEGER,
    total_jackpot INTEGER,
    games_played INTEGER,
    synced_at TEXT
);

CREATE TABLE IF NOT EXISTS poll_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    poll_name TEXT NOT NULL,
    error_type TEXT NOT NULL,
    message TEXT
);
"""
# `synced_at` starts NULL on every row and is deliberately never touched by
# this script. It's here so a future drain/sync process has an obvious,
# cheap way to find "rows nobody has acknowledged yet" (WHERE synced_at IS
# NULL) without this local logger needing to know anything about how or
# where syncing happens — see MANUAL.md's SQL guide, last step.


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def poll_and_log(client, conn: sqlite3.Connection) -> None:
    now = utc_now()
    try:
        meters = client.send_meters_10_through_15()
    except SASError as e:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
            (now, "send_meters_10_through_15", type(e).__name__, str(e)),
        )
        conn.commit()
        print(f"[{now}] poll failed: {type(e).__name__}: {e}")
        return

    conn.execute(
        "INSERT INTO meter_snapshots "
        "(polled_at, total_cancelled_credits, total_coin_in, total_coin_out, "
        " total_drop, total_jackpot, games_played) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            now,
            meters.total_cancelled_credits,
            meters.total_coin_in,
            meters.total_coin_out,
            meters.total_drop,
            meters.total_jackpot,
            meters.games_played,
        ),
    )
    conn.commit()
    print(f"[{now}] coin_in={meters.total_coin_in} coin_out={meters.total_coin_out} games_played={meters.games_played}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", help="gateway .ini file (see saspy/config.py or MANUAL.md)")
    parser.add_argument("--db", default="gateway.sqlite3", help="SQLite file to write to")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between poll cycles")
    parser.add_argument("--cycles", type=int, default=0, help="stop after N cycles (default: run until Ctrl-C)")
    args = parser.parse_args()

    client = connect_from_config(args.config)
    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)
    conn.commit()

    print(f"Polling {args.config} every {args.interval}s, writing to {args.db}.")
    print("Ctrl-C to stop." if args.cycles == 0 else f"Will stop after {args.cycles} cycle(s).")

    cycle = 0
    try:
        while args.cycles == 0 or cycle < args.cycles:
            cycle += 1
            poll_and_log(client, conn)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
