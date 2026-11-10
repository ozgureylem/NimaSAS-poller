"""What an outage cost, measured from the database alone.

Built 2026-11-08 after the lab poller died on its own and nobody noticed
for 46 minutes. The operator: "This is a perfect example of what may go
on when we're live. Let's see how much of RTE is lost."
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from outage_loss import find_gaps, recovered_after  # noqa: E402


def _db(tmp_path, meters, events=()):
    conn = sqlite3.connect(tmp_path / "o.sqlite3")
    conn.execute("CREATE TABLE meters_history (id INTEGER PRIMARY KEY, "
                 "polled_at TEXT, total_coin_in INTEGER, games_played INTEGER)")
    conn.execute("CREATE TABLE machine_events (id INTEGER PRIMARY KEY, "
                 "occurred_at TEXT, exception_code INTEGER, description TEXT, "
                 "source TEXT, rte_fields TEXT)")
    for i, m in enumerate(meters):
        conn.execute("INSERT INTO meters_history VALUES (?,?,?,?)", (i + 1, *m))
    for i, e in enumerate(events):
        conn.execute("INSERT INTO machine_events VALUES (?,?,?,?,?,?)", (i + 1, *e))
    conn.commit()
    return conn


def test_the_meter_delta_is_what_the_events_are_measured_against():
    """total_coin_in keeps counting with no host attached, which is the
    whole reason it can price an outage: it is the one number the gap
    cannot affect."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        conn = _db(Path(d), [
            ("2026-11-08T17:00:00+00:00", 1_000_000, 10_000),
            ("2026-11-08T17:01:00+00:00", 1_002_400, 10_020),
            ("2026-11-08T18:01:00+00:00", 1_112_400, 11_120),   # the hole
        ])
        gaps = find_gaps(conn, min_gap=180)
        assert len(gaps) == 1
        assert gaps[0]["played"] == 110_000
        assert gaps[0]["games"] == 1_100
        assert gaps[0]["seconds"] == 3600


def test_an_ordinary_cycle_is_not_an_outage():
    """A cycle is about a minute and the sweep can stretch it. Calling
    that an outage would price every normal gap as a loss."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        conn = _db(Path(d), [
            ("2026-11-08T17:00:00+00:00", 1_000_000, 10_000),
            ("2026-11-08T17:01:30+00:00", 1_002_400, 10_020),
            ("2026-11-08T17:02:50+00:00", 1_004_800, 10_040),
        ])
        assert find_gaps(conn, min_gap=180) == []


def test_only_the_STARTUP_DRAIN_counts_as_recovery():
    """An event that arrived after the drain belongs to play AFTER the
    outage. Counting it would credit the queue with events it never
    held, and make a long outage look survivable."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        conn = _db(Path(d), [("2026-11-08T18:01:00+00:00", 1, 1)], events=[
            ("2026-11-08T18:01:05+00:00", 0x7E, "S", "startup-backlog",
             json.dumps({"wager_cents": 100})),
            ("2026-11-08T18:02:05+00:00", 0x7E, "S", "rte",
             json.dumps({"wager_cents": 100})),
        ])
        rec = recovered_after(conn, "2026-11-08T18:01:00+00:00")
        assert rec["game_starts"] == 1
        assert rec["wagered"] == 100


def test_the_overflow_the_cabinet_reports_is_counted_separately():
    """0x70 is the machine's own admission that its queue could not hold
    it all — §2.2.1 requires it. It is not a game start and must not be
    summed as one."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        conn = _db(Path(d), [("2026-11-08T18:01:00+00:00", 1, 1)], events=[
            ("2026-11-08T18:01:05+00:00", 0x70, "overflow", "startup-backlog", None),
            ("2026-11-08T18:01:05+00:00", 0x7E, "S", "startup-backlog",
             json.dumps({"wager_cents": 250})),
        ])
        rec = recovered_after(conn, "2026-11-08T18:01:00+00:00")
        assert rec["overflows"] == 1
        assert rec["game_starts"] == 1
        assert rec["wagered"] == 250


def test_a_LONG_outage_loses_nearly_everything_whatever_the_host_does():
    """The point §19 rests on, and it is arithmetic rather than bad luck.

    §2.2.1 guarantees 20 elements and this cabinet measured ≥67. Under
    RTE a game is two events, so the cabinet can hold about 33 games
    however long the gap is -- a 46-minute outage at 24 games a minute
    therefore loses about 97% of it, and no polling rate on the host
    side changes that by a credit.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        events = [("2026-11-08T18:01:05+00:00", 0x7E, "S", "startup-backlog",
                   json.dumps({"wager_cents": 100})) for _ in range(33)]
        conn = _db(Path(d), [
            ("2026-11-08T17:15:00+00:00", 1_000_000, 10_000),
            ("2026-11-08T18:01:00+00:00", 1_110_000, 11_100),
        ], events=events)
        gap = find_gaps(conn, min_gap=180)[0]
        rec = recovered_after(conn, gap["to"])
        lost = gap["played"] - rec["wagered"]
        assert gap["played"] == 110_000
        assert rec["wagered"] == 3_300
        assert lost == 106_700
        assert lost / gap["played"] > 0.95


def test_a_LATER_restarts_drain_is_not_credited_to_an_EARLIER_outage():
    """The bug the lab database exposed on 2026-11-08, after this tool had
    already been run on real data.

    recovered_after() had no upper bound, so every later restart's drain
    counted against every earlier gap. The output claimed 266 game starts
    recovered from an outage in which 0 games were played, and losses came
    out NEGATIVE -- a number that cannot happen, printed in a report.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        conn = _db(Path(d), [("2026-11-08T18:01:00+00:00", 1, 1)], events=[
            ("2026-11-08T18:01:05+00:00", 0x7E, "S", "startup-backlog",
             json.dumps({"wager_cents": 100})),
            # a DIFFERENT restart, hours later
            ("2026-11-08T23:30:05+00:00", 0x7E, "S", "startup-backlog",
             json.dumps({"wager_cents": 9_999})),
        ])
        bounded = recovered_after(conn, "2026-11-08T18:01:00+00:00",
                                  until="2026-11-08T23:00:00+00:00")
        assert bounded["wagered"] == 100
        assert bounded["game_starts"] == 1

        unbounded = recovered_after(conn, "2026-11-08T18:01:00+00:00")
        assert unbounded["wagered"] == 10_099, (
            "without a bound it swallows the later drain -- which is "
            "exactly what shipped")


def test_an_outage_with_NO_PLAY_is_not_a_total_loss():
    """An outage on an idle cabinet costs nothing. Reporting it as "100%
    lost" buries the ones that cost something, which is what the first
    run did for six of its fourteen gaps."""
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        conn = _db(Path(d), [
            ("2026-11-08T17:00:00+00:00", 1_000_000, 10_000),
            ("2026-11-08T18:01:00+00:00", 1_000_000, 10_000),   # nothing played
        ])
        conn.commit()
        out = subprocess.run(
            [sys.executable, str(ROOT / "examples" / "outage_loss.py"),
             str(Path(d) / "o.sqlite3"), "--all-history"],
            capture_output=True, text=True, timeout=60)
        assert "nothing was played during this outage" in out.stdout, out.stdout
        assert "100.0% of it" not in out.stdout
