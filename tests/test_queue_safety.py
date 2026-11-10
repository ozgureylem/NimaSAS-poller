"""The queue-safety budget, and the measure it replaces.

The operator's standard, which these tests encode:

    "Unless the CPU is exhausted or the gateway is rebooting, EGM's buffer
    cannot be overflowing even with 400ms polling. Exception overflows are
    only acceptable if only the gateway lost connection (reboot, faulty
    cable). I won't ever accept buffer overflow because gateway is busy."

The mistake this tool exists to stop repeating is comparing events-per-cycle
against queue depth. That assumes the queue is drained once per cycle; §12.3
drains it on every message, and the lab database shows 95% of game starts
arriving mid-cycle as `rte-interleaved`. So the only quantity that can
overflow a queue is the GAP between consecutive host messages, and the
budget is depth / event-rate.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from queue_safety import (  # noqa: E402
    MEASURED_LAB_QUEUE,
    OUTAGE_GAP_SECONDS,
    SPEC_MIN_QUEUE,
    cycle_gaps,
    event_rate,
    overflows,
)

SCRIPT = ROOT / "examples" / "queue_safety.py"

GAME_START, GAME_END, OVERFLOW = 0x7E, 0x7F, 0x70


def _db(tmp_path, cycles, events, name="q.sqlite3"):
    """``cycles`` are polled_at strings; ``events`` are (at, code, source)."""
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meters_history (id INTEGER PRIMARY KEY, "
                 "polled_at TEXT, games_played INTEGER)")
    conn.execute("CREATE TABLE machine_events (id INTEGER PRIMARY KEY, "
                 "occurred_at TEXT, exception_code INTEGER, description TEXT, "
                 "source TEXT, rte_fields TEXT)")
    for i, at in enumerate(cycles):
        conn.execute("INSERT INTO meters_history (polled_at, games_played) "
                     "VALUES (?,?)", (at, i))
    for at, code, source in events:
        conn.execute("INSERT INTO machine_events (occurred_at, exception_code, "
                     "description, source, rte_fields) VALUES (?,?,?,?,NULL)",
                     (at, code, "x", source))
    conn.commit()
    return path, conn


def _steady(minutes=30, per_minute=2, source="rte"):
    """One game per minute: a start and an end, at a sane cycle cadence."""
    cycles = [f"2026-11-05T21:{m:02d}:30" for m in range(minutes)]
    events = []
    for m in range(minutes):
        events.append((f"2026-11-05T21:{m:02d}:00", GAME_START, source))
        events.append((f"2026-11-05T21:{m:02d}:10", GAME_END, source))
    return cycles, events


def _run(db, *args):
    return subprocess.run([sys.executable, str(SCRIPT), str(db), *args],
                          capture_output=True, text=True, timeout=60)


# --- the arithmetic ------------------------------------------------------

def test_events_per_game_is_measured_not_assumed(tmp_path):
    """Two per game on this cabinet, because it sends no reel stops. The
    whole point of measuring it is that a five-reel cabinet sends about
    eight and a rate proved safe here would not transfer."""
    cycles, events = _steady()
    path, conn = _db(tmp_path, cycles, events)
    rate = event_rate(conn, None, None)
    assert rate["per_game"] == 2.0
    assert rate["games"] == 30


def test_the_startup_backlog_is_excluded_from_the_rate(tmp_path):
    """A backlog drained at startup was generated while the gateway was down.
    Counting it as throughput inflates the event rate, which SHRINKS the
    safety budget and would fail a clean run."""
    cycles, events = _steady()
    events += [(f"2026-11-05T21:00:01", GAME_START, "startup-backlog")] * 500
    path, conn = _db(tmp_path, cycles, events)
    assert event_rate(conn, None, None)["per_game"] == 2.0


def test_the_budget_is_depth_over_rate(tmp_path):
    """One game a minute is 2 events in 60s = 0.0333/s. A 20-deep queue
    therefore tolerates 600s of silence, a 67-deep one 2010s. The arithmetic
    is the whole claim, so it is asserted rather than eyeballed."""
    cycles, events = _steady()
    path, _ = _db(tmp_path, cycles, events)
    out = _run(path)
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"{SPEC_MIN_QUEUE} elements" in out.stdout
    assert f"{MEASURED_LAB_QUEUE} elements" in out.stdout
    # 0.0333 events/s -> ~600s at 20 deep. Allow for the sample's own edges.
    import re
    got = dict(re.findall(r"(\d+) elements ->\s+([\d.]+) s", out.stdout))
    assert 500 < float(got["20"]) < 700, got
    assert 1800 < float(got["67"]) < 2300, got


# --- what the tool must catch -------------------------------------------

def _dense(minutes, games_per_minute, hole_at=None, hole_seconds=0,
           collect_during_hole=False):
    """Cycles every 10 s and ``games_per_minute`` games, with an optional hole.

    ``collect_during_hole`` is the distinction the tool turns on. A STALLED
    poller collects nothing while it is stalled, so its events are absent
    from the hole as well as its cycle rows. A poller merely running a long
    Table C-7 sweep keeps collecting throughout -- §12.3 gives it ~146
    chances inside one cycle -- so its events ARE in the hole and only the
    cycle row is missing.

    Real datetime arithmetic rather than f-string minute formatting, because
    the local rate is measured over a half hour either side of a gap and a
    fixture spanning ten minutes cannot produce a realistic rate at that
    scale.
    """
    from datetime import datetime, timedelta
    t0 = datetime(2026, 11, 5, 21, 0, 0)
    cycles, events = [], []
    for s_ in range(0, minutes * 60, 10):
        if hole_at is not None and hole_at <= s_ < hole_at + hole_seconds:
            continue
        cycles.append((t0 + timedelta(seconds=s_)).isoformat())
    step = 60.0 / games_per_minute
    for i in range(int(minutes * games_per_minute)):
        off = i * step
        in_hole = (hole_at is not None
                   and hole_at <= off < hole_at + hole_seconds)
        if in_hole and not collect_during_hole:
            continue
        at = t0 + timedelta(seconds=off)
        events.append((at.isoformat(), GAME_START, "rte"))
        events.append(((at + timedelta(seconds=1)).isoformat(), GAME_END, "rte"))
    return cycles, events


def test_a_long_stall_WHILE_CONNECTED_fails(tmp_path):
    """The case the operator's standard forbids: the gateway was up, stopped
    sending, and the cabinet queued events it could not unload.

    30 games a minute is 1 event/second, so a 90-second stall queues about 90
    events -- far over a 20-deep cabinet. Nothing is collected during it,
    which is what a stall looks like in the database."""
    cycles, events = _dense(40, 30, hole_at=1200, hole_seconds=90)
    path, _ = _db(tmp_path, cycles, events)
    out = _run(path, "--queue-depth", "20")
    assert out.returncode == 1, out.stdout
    assert "OVER 20" in out.stdout
    assert "operator's standard does not allow" in out.stdout
    # The quiet stretch found must be the stall itself (~90s), not the whole
    # cycle gap: a couple of events land at the hole's edges, and the tool
    # correctly measures the silence BETWEEN arrivals rather than the gap.
    import re
    quiet = float(re.search(r"(\d+\.\d)s of silence", out.stdout).group(1))
    assert 85 <= quiet <= 100, quiet


def test_a_LONG_CYCLE_that_kept_draining_is_fine(tmp_path):
    """The reading that made this tool cry wolf, and the reason for the
    drained-inside check.

    A full Table C-7 sweep takes 46-58 s and sends ~146 messages, every one a
    chance for the cabinet to unload an event. Its cycle row is 58 s from the
    previous one, but the queue never builds. Scoring the whole cycle gap at
    the event rate read 47 elements on the lab database when the true depth
    was 0.33 -- out by the number of messages in a sweep.

    Same hole as the test above, same rate; the only difference is that
    events kept arriving during it. The verdicts must differ."""
    cycles, events = _dense(40, 30, hole_at=1200, hole_seconds=90,
                            collect_during_hole=True)
    path, _ = _db(tmp_path, cycles, events)
    out = _run(path, "--queue-depth", "20")
    assert out.returncode == 0, out.stdout
    assert "OK   20-deep" in out.stdout
    assert "drained" in out.stdout


def test_the_same_stall_on_an_IDLE_cabinet_is_fine(tmp_path):
    """The reason the rate is taken locally rather than globally. An
    identical 90-second stall with the machine barely playing queues almost
    nothing, and a tool judging by gap length alone would condemn it."""
    cycles, events = _dense(40, 1, hole_at=1200, hole_seconds=90)
    path, _ = _db(tmp_path, cycles, events)
    out = _run(path, "--queue-depth", "20")
    assert out.returncode == 0, out.stdout
    assert "OK   20-deep" in out.stdout


def test_both_discarded_readings_are_printed_so_they_cannot_return(tmp_path):
    """This tool gave two wrong answers before this one: the whole-run mean
    rate (too permissive, it averages over the idle hours) and the whole
    cycle gap at the local rate (too pessimistic, it ignores the messages
    sent inside a sweep). Both are printed beside the real figure, with the
    reason each is wrong."""
    cycles, events = _dense(40, 30, hole_at=1200, hole_seconds=90)
    path, _ = _db(tmp_path, cycles, events)
    out = _run(path, "--queue-depth", "20")
    assert "too permissive" in out.stdout
    assert "too pessimistic" in out.stdout
    assert "BUSIEST HOUR" in out.stdout
    assert "THE DEPTH EACH GAP ACTUALLY REACHED" in out.stdout


def test_an_outage_length_gap_is_not_counted_against_the_poll_rate(tmp_path):
    """A gap longer than the outage threshold is the gateway being down or
    the cable being out, which is the one acceptable cause. Counting it as a
    polling failure would condemn every run that spans a reboot."""
    cycles, events = _steady()
    cycles.insert(10, "2026-11-05T23:59:00")  # hours later
    path, _ = _db(tmp_path, cycles, events)
    out = _run(path)
    assert out.returncode == 0, out.stdout
    assert "treated as the gateway being DOWN" in out.stdout
    assert "excluded below" in out.stdout


def test_a_startup_backlog_overflow_is_ALLOWED(tmp_path):
    """§2.2.1 says the cabinet reports a queue it overran. One reported after
    an outage is the audit trail working, not a defect."""
    cycles, events = _steady()
    events.append(("2026-11-05T21:00:05", OVERFLOW, "startup-backlog"))
    path, _ = _db(tmp_path, cycles, events)
    out = _run(path)
    assert out.returncode == 0, out.stdout
    assert "ALLOWED" in out.stdout
    assert "the audit trail working" in out.stdout


def test_an_overflow_while_connected_is_NOT_ALLOWED(tmp_path):
    """The 61 overflows of 2026-10-30 were this: source `rte`, gateway
    connected throughout. It must fail, and it must say the fix is a named
    cause rather than a faster poll."""
    cycles, events = _steady()
    events.append(("2026-11-05T21:05:05", OVERFLOW, "rte"))
    path, _ = _db(tmp_path, cycles, events)
    out = _run(path)
    assert out.returncode == 1, out.stdout
    assert "NOT ALLOWED" in out.stdout
    assert "gateway defect" in out.stdout
    assert "not a faster poll rate" in out.stdout


def test_a_reel_stop_cabinet_can_be_asked_about_without_owning_one(tmp_path):
    """The honest limit of the lab result. This EGT sends 2 events a game; a
    cabinet sending 8 has four times the rate and a quarter of the budget.
    The operator's 400ms claim is safe here and needs re-checking there, so
    the tool must be able to answer it before anyone buys the cabinet."""
    cycles, events = _steady()
    path, _ = _db(tmp_path, cycles, events)
    base = _run(path, "--queue-depth", "20")
    worse = _run(path, "--queue-depth", "20", "--events-per-game", "8")
    import re
    def budget(out):
        return float(re.search(r"20 elements ->\s+([\d.]+) s", out.stdout).group(1))
    assert budget(worse) < budget(base) / 3.5, (budget(base), budget(worse))
    assert "ASSUMING 8.0 events/game" in worse.stdout


# --- the discipline every detector on this project owes -----------------

def test_an_idle_cabinet_cannot_clear_anything(tmp_path):
    """No events means no rate means no budget. A tool that returns OK on an
    idle machine has certified nothing, which is the mistake recorded in
    HANDOVER section 8 about reading a clean sweep on an idle cabinet."""
    cycles, _ = _steady()
    path, _ = _db(tmp_path, cycles, [])
    out = _run(path)
    assert out.returncode == 3
    assert "cannot overflow a queue" in out.stdout


def test_it_never_opens_the_serial_port(tmp_path):
    """It is meant to run beside a live soak."""
    source = SCRIPT.read_text()
    for forbidden in ("open_serial_port", "SASTransport", "SASClient"):
        assert forbidden not in source, forbidden


def test_it_opens_the_database_read_only(tmp_path):
    assert "mode=ro" in SCRIPT.read_text()


def test_the_cycle_gap_caveat_is_stated_in_the_output(tmp_path):
    """meters_history is one row per CYCLE, and a cycle sends many messages,
    so every gap here is an upper bound on the real message gap. Saying so
    matters: the number is conservative, and a reader who thinks it is exact
    will mistrust the tool when it disagrees with a message-level trace."""
    cycles, events = _steady()
    path, _ = _db(tmp_path, cycles, events)
    out = _run(path)
    assert "UPPER bound" in out.stdout
    assert "cannot clear one that was unsafe" in out.stdout


def test_it_refuses_to_call_a_stall_when_the_stamps_are_cycle_granular(tmp_path):
    """The third bug this tool had, and the worst, because it produced a
    confident false alarm on a healthy overnight soak.

    sql_poll_logger.py calls drain_rte_events() ONCE PER CYCLE and stamps
    every event it returns with the cycle's own `now`. So ~47 interleaved
    events share one timestamp and none can fall strictly inside a gap
    between cycle rows -- which made the drained-inside test report NOTHING
    COLLECTED for every cycle of a run that was fine.

    The tool must notice that the database cannot answer and say so, rather
    than report the bound as a finding."""
    from datetime import datetime, timedelta
    t0 = datetime(2026, 11, 5, 21, 0, 0)
    cycles, events = [], []
    for c in range(40):
        at = t0 + timedelta(seconds=c * 58)
        cycles.append(at.isoformat())
        # Every event of this cycle carries the cycle's stamp, as the poller
        # really does it.
        for _ in range(24):
            events.append((at.isoformat(), GAME_START, "rte-interleaved"))
            events.append((at.isoformat(), GAME_END, "rte-interleaved"))
    path, _ = _db(tmp_path, cycles, events)
    out = _run(path, "--queue-depth", "20")
    assert "CANNOT ANSWER THE QUESTION EXACTLY" in out.stdout
    assert "cycle-granular" in out.stdout.lower()
    assert "cannot tell (bound)" in out.stdout
    assert "NOT PROVEN" in out.stdout
    assert "Do not treat this as a stall" in out.stdout
    # Exit 3 (nothing to conclude), never 1 (something needs a cause).
    assert out.returncode == 3, out.stdout


def test_a_database_with_real_arrival_times_still_gets_a_verdict(tmp_path):
    """The gate must not disable the check for a poller that does stamp each
    arrival. One event per distinct timestamp means the stamps are usable."""
    cycles, events = _dense(40, 30, hole_at=1200, hole_seconds=90)
    path, _ = _db(tmp_path, cycles, events)
    out = _run(path, "--queue-depth", "20")
    assert "CANNOT ANSWER THE QUESTION EXACTLY" not in out.stdout
    assert out.returncode == 1, out.stdout
