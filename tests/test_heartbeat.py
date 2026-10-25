"""The meter sweep must not leave the exception queue unattended.

Measured on an EGT Premier: 73 long-poll exchanges taking 6.07s, and with
the cycle's sleep a gap of 7.12s between general polls (observed as 7.122
and 7.072). The machine's queue is about 35 deep and a single player in
turbo mode generates ~2.4 exceptions/sec, so that gap spends roughly half
the queue every cycle -- for no reason but ordering. Exception 0x70, buffer
overflow, is what the machine reports once it is gone.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))

from saspy.exceptions import SASTimeoutError
from sql_poll_logger import _make_heartbeat


class _Recorder:
    def __init__(self, codes):
        self._codes = list(codes)
        self.polls = 0

    def general_poll(self):
        self.polls += 1
        return self._codes.pop(0) if self._codes else 0x00


def test_heartbeat_collects_exceptions_without_dispatching_them():
    """Codes are collected, not acted on: issuing a ticket or cashout long
    poll from inside the long poll loop would nest one sweep in another.
    One sweep of latency is well inside the 30s section 15.12 allows.
    """
    client = _Recorder([0x67, 0x00, 0x3D])
    seen: list = []
    beat = _make_heartbeat(client, conn=None, now="now", seen=seen)

    for _ in range(3):
        beat()

    assert client.polls == 3
    assert seen == [0x67, 0x3D], "0x00 is not an event and must not be collected"


def test_a_failed_heartbeat_does_not_abort_the_sweep():
    """Draining is best-effort. The cycle's own general poll has its own
    retry path; a heartbeat that times out must not take the meter row with
    it, or the fix would trade one failure mode for another.
    """
    class Failing:
        def general_poll(self):
            raise SASTimeoutError("no response")

    seen: list = []
    _make_heartbeat(Failing(), conn=None, now="now", seen=seen)()  # must not raise
    assert seen == []


def test_the_sweep_interleaves_general_polls():
    """The behaviour the measurement called for: a general poll every
    heartbeat_every exchanges, instead of one per cycle.

    Uses the suite's existing fake machine, which answers every poll, so the
    count reflects a real sweep's exchange count rather than how far a stub
    gets before aborting.
    """
    from sql_poll_logger import _poll_all_meters
    from tests.test_sql_poll_logger import ScriptedClient, make_meters

    beats = []
    client = ScriptedClient(meters_script=[make_meters()])
    _poll_all_meters(
        client,
        heartbeat=lambda: beats.append(1),
        heartbeat_every=8,
    )

    assert len(beats) >= 5, (
        f"only {len(beats)} heartbeats across a full sweep -- the queue is still "
        f"left unattended for most of it"
    )


def test_heartbeat_frequency_follows_heartbeat_every():
    """Smaller interval, more heartbeats -- so the gap can be tuned against a
    machine's measured queue depth rather than hard-coded."""
    from sql_poll_logger import _poll_all_meters
    from tests.test_sql_poll_logger import ScriptedClient, make_meters

    counts = {}
    for every in (4, 16):
        beats = []
        _poll_all_meters(
            ScriptedClient(meters_script=[make_meters()]),
            heartbeat=lambda: beats.append(1),
            heartbeat_every=every,
        )
        counts[every] = len(beats)

    assert counts[4] > counts[16], counts


def test_backfill_stops_once_the_buffer_is_clearly_absent():
    """A machine with no printer has no ticket-out buffer, so it ignores
    every index (4.4). That was 31 timeouts at every startup on the first EGT
    Premier -- about 31 seconds of it, and 31 poll_errors rows that then
    dominated an 18-hour soak report's failure count and hid a real cluster
    elsewhere. If indices 1 and 2 are silent, 3 through 31 will be too.
    """
    import sqlite3

    from sql_poll_logger import SCHEMA, backfill_ticket_out_history

    class NoBuffer:
        def __init__(self):
            self.calls = 0

        def send_enhanced_validation_information(self, function_code):
            self.calls += 1
            raise SASTimeoutError("no response")

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    client = NoBuffer()

    found = backfill_ticket_out_history(client, conn, now_fn=lambda: "2026-10-24T01:33:00+00:00")

    assert found == 0
    assert client.calls == 2, f"stopped after {client.calls} indices, not 31"
    rows = conn.execute("SELECT COUNT(*) FROM poll_errors").fetchone()[0]
    assert rows == 1, "one row recording the absence, not one per index"


def test_backfill_keeps_going_past_a_single_timeout():
    """Only a RUN of silence means the feature is absent. One timeout among
    answering indices is a transient and must not truncate the scan, or a
    blip would silently skip real records further up the buffer.
    """
    import sqlite3

    from sql_poll_logger import SCHEMA, backfill_ticket_out_history

    class Flaky:
        def __init__(self):
            self.calls = 0

        def send_enhanced_validation_information(self, function_code):
            self.calls += 1
            if function_code == 1:
                raise SASTimeoutError("transient")
            raise _Empty()

    class _Empty(Exception):
        pass

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    client = Flaky()
    try:
        backfill_ticket_out_history(client, conn, now_fn=lambda: "2026-10-24T01:33:00+00:00")
    except Exception:
        pass

    assert client.calls >= 2, "a single timeout must not end the scan"


def test_every_exception_is_recorded_not_just_the_actionable_ones():
    """The lesson from an 18-hour soak.

    It showed 81 core-meter timeouts and a 116-second stall, and nothing in
    the database explained either. The explanation was in the machine's own
    event log, read off its screen afterwards: a technician had the slot door
    open for most of the run and entered the attendant menu twice. The poller
    had been told all of it over the wire -- exceptions 0x11, 0x12, 0x82,
    0x83 -- and discarded it because it only acted on tickets and cashouts.

    A failure log without the machine's own state beside it cannot be read.
    """
    import sqlite3

    from sql_poll_logger import SCHEMA, _dispatch_exception

    class Nop:
        def __getattr__(self, name):
            return lambda *a, **k: None

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)

    for code in (0x11, 0x82, 0x83, 0x12):
        _dispatch_exception(Nop(), conn, "2026-10-24T10:52:00+00:00", code)

    rows = conn.execute(
        "SELECT exception_code, description FROM machine_events ORDER BY id"
    ).fetchall()
    assert [r[0] for r in rows] == [0x11, 0x82, 0x83, 0x12]
    assert rows[0][1] == "slot door was opened"
    assert rows[1][1] == "display meters or attendant menu has been entered"


def test_no_activity_is_not_recorded_as_an_event():
    """0x00 is the absence of an event. Recording it would put a row on every
    poll and bury the ones that mean something."""
    import sqlite3

    from saspy.constants import ExceptionCode
    from sql_poll_logger import SCHEMA, _dispatch_exception

    class Nop:
        def __getattr__(self, name):
            return lambda *a, **k: None

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    _dispatch_exception(Nop(), conn, "2026-10-24T10:52:00+00:00", ExceptionCode.NONE)

    assert conn.execute("SELECT COUNT(*) FROM machine_events").fetchone()[0] == 0


def test_heartbeat_sourced_events_are_distinguishable():
    """Knowing an exception was drained mid-sweep rather than at the top of a
    cycle is how the interleaving can be shown to be doing anything."""
    import sqlite3

    from sql_poll_logger import SCHEMA, _dispatch_exception

    class Nop:
        def __getattr__(self, name):
            return lambda *a, **k: None

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    _dispatch_exception(Nop(), conn, "t", 0x11, source="heartbeat")
    _dispatch_exception(Nop(), conn, "t", 0x12, source="cycle")

    sources = [r[0] for r in conn.execute("SELECT source FROM machine_events ORDER BY id")]
    assert sources == ["heartbeat", "cycle"]


def test_the_attendance_check_covers_every_door_not_just_the_slot_door():
    """The blind spot a 24-hour soak found.

    The report printed "0x1D x3 belly door was opened", "0x18 AC power was
    lost" and then, three lines later, "nobody was at the machine" -- because
    the check only knew the slot door, the menus, options-changed and the
    meter reset. It contradicted its own evidence, which is worse than
    printing nothing at all.

    On the EGT Premier the BILL VALIDATOR door reports as 0x1D/0x1E, which
    Appendix A names "belly door" -- so somebody checking the stacker shows
    up under a name that does not mention the bill validator. Covering only
    the obvious doors is not enough.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "soak_report", str(Path(__file__).resolve().parent.parent / "examples" / "soak_report.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Read the sets out of the source, since they are local to main().
    src = (Path(__file__).resolve().parent.parent / "examples" / "soak_report.py").read_text()
    for code in (0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x19, 0x1A, 0x1B, 0x1C,
                 0x1D, 0x1E, 0x3A, 0x3C, 0x7A, 0x7B, 0x82, 0x83, 0x84, 0x85, 0x86,
                 0x98, 0x99, 0x9A, 0x9B):
        assert f"0x{code:02X}:" in src, f"0x{code:02X} missing from the attendance set"


def test_power_events_are_reported_separately_from_attendance():
    """AC power lost is not somebody at the machine -- it is the machine not
    running. Both ruin a stability number, for different reasons, and a
    report that conflates them explains an outage as a flaky link.
    """
    src = (Path(__file__).resolve().parent.parent / "examples" / "soak_report.py").read_text()
    assert "INTERRUPTED" in src
    for code in (0x17, 0x18):
        assert f"0x{code:02X}:" in src
    assert "one end was off" in src
