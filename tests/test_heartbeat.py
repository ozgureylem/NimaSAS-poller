"""The meter sweep must not leave the exception queue unattended.

Measured on an EGT Premier: 73 long-poll exchanges taking 6.07s, and with
the cycle's sleep a gap of 7.12s between general polls (observed as 7.122
and 7.072). The machine's queue is about 35 deep and a single player in
turbo mode generates ~2.4 exceptions/sec, so that gap spends roughly half
the queue every cycle -- for no reason but ordering. Exception 0x70, buffer
overflow, is what the machine reports once it is gone.
"""

from __future__ import annotations

from types import SimpleNamespace

import sys
from pathlib import Path

from conftest import requires_lab_file

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))

from saspy.exceptions import SASTimeoutError
from sql_poll_logger import (
    HEARTBEAT_DRAIN_CAP,
    _make_credit_sampler,
    _make_heartbeat,
)


class _Recorder:
    def __init__(self, codes):
        self._codes = list(codes)
        self.polls = 0

    def general_poll(self):
        self.polls += 1
        return self._codes.pop(0) if self._codes else 0x00

    def drain_rte_events(self):
        return []


def test_heartbeat_defers_ordinary_exceptions():
    """Most codes are collected, not acted on: issuing a cashout or a config
    re-read from inside the long poll loop would nest one sweep in another,
    and nothing in this group has a deadline a sweep can miss.

    This test used to include 0x67 and justified the deferral with "one sweep
    of latency is well inside the 30s section 15.12 allows". That reasoning
    was wrong twice over -- see the next test.
    """
    client = _Recorder([0x3D, 0x00, 0x11])
    seen: list = []
    beat = _make_heartbeat(client, conn=None, now="now", seen=seen)

    for _ in range(3):
        beat()

    # Five polls, not three, since 2026-11-06: a heartbeat now drains to
    # EMPTY rather than taking one poll and returning. The 0x00 in the middle
    # of the list ends the first heartbeat; the second drains 0x11 then its
    # own 0x00; the third finds nothing. That is the point -- the queue, not
    # the schedule, decides when a heartbeat stops.
    assert client.polls == 5
    # (code, rte_fields, source). Ordinary polling carries no payload, so the
    # second element is None; under RTE it is the decoded Table 12.5 data. The
    # third is which path found it, since one list now carries both.
    assert seen == [(0x3D, None, "heartbeat"), (0x11, None, "heartbeat")], \
        "0x00 is not an event and must not be collected"


def test_a_ticket_or_validation_request_is_dispatched_where_it_is_FOUND():
    """The deferral misses the deadline, and the lab proved it.

    A 0x67 collected by a heartbeat waited for the whole sweep: 5.4s on an
    ordinary cycle, 16.8s on a Table C-7 cycle, and 69.4s during the startup
    interrogation measured on 2026-11-02. §15.12 allows thirty seconds, so the
    machine had returned the ticket long before LP 0x70 was read -- which is
    exactly what four ticket_in_events rows that day said: "exception 0x67
    fired but LP 0x70 reported no ticket in escrow".

    §15.7 gives a system validation request only TEN seconds, so 0x57 is
    worse: a C-7 cycle alone overruns it, and an unanswered 0x57 becomes a
    handpay.
    """
    import sqlite3
    from sql_poll_logger import SCHEMA

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)

    class _Quiet(_Recorder):
        def send_ticket_validation_data(self):
            raise SASTimeoutError("no answer -- not what this test measures")

        def send_pending_cashout_info(self):
            raise SASTimeoutError("no answer -- not what this test measures")

        def send_enhanced_validation_information(self, *a, **kw):
            raise SASTimeoutError("no answer -- not what this test measures")

    for code in (0x67, 0x57):
        seen: list = []
        client = _Quiet([code])
        beat = _make_heartbeat(client, conn=conn, now="2026-11-02T00:00:00+00:00",
                               seen=seen)
        beat()
        assert seen == [], (
            f"0x{code:02X} must be acted on where it is found, not queued "
            "until the sweep ends")
        row = conn.execute(
            "SELECT exception_code FROM machine_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None and row[0] == code, (
            f"0x{code:02X} must still be recorded")


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
        "soak_report", str(requires_lab_file(
            Path(__file__).resolve().parent.parent / "examples" / "soak_report.py"))
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Read the sets out of the source, since they are local to main().
    src = requires_lab_file(
        Path(__file__).resolve().parent.parent / "examples"
        / "soak_report.py").read_text()
    for code in (0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x19, 0x1A, 0x1B, 0x1C,
                 0x1D, 0x1E, 0x3A, 0x3C, 0x7A, 0x7B, 0x82, 0x83, 0x84, 0x85, 0x86,
                 0x98, 0x99, 0x9A, 0x9B):
        assert f"0x{code:02X}:" in src, f"0x{code:02X} missing from the attendance set"


def test_power_events_are_reported_separately_from_attendance():
    """AC power lost is not somebody at the machine -- it is the machine not
    running. Both ruin a stability number, for different reasons, and a
    report that conflates them explains an outage as a flaky link.
    """
    src = requires_lab_file(
        Path(__file__).resolve().parent.parent / "examples"
        / "soak_report.py").read_text()
    assert "INTERRUPTED" in src
    for code in (0x17, 0x18):
        assert f"0x{code:02X}:" in src
    assert "one end was off" in src


def test_the_heartbeat_uses_the_rte_poll_when_rte_is_on():
    """The bug this pins, found on an idle cabinet within a minute of the
    first --rte run.

    ``general_poll()`` returns ``response[0]``. In the ordinary mode that
    byte is the exception code. Under real time event reporting the reply is
    Table 12.5's "address, FF, code, data..., CRC", so the first byte is the
    gaming machine's ADDRESS -- and on address 1 every heartbeat reported a
    phantom "exception 0x01, unknown/unassigned". Ten or eleven of them per
    cycle, on a machine doing nothing.

    The phantom was the visible half. The damaging half is that reading one
    byte of a multi-byte frame leaves the rest in the buffer for the next
    read to misframe, which is why the same run also produced a burst of
    interleaved "events" whose codes were partly frame fragments.

    The cycle's own general poll had been converted to the RTE path; this
    one had not. A test that only checked the cycle's poll would have passed.
    """
    class _RTERecorder:
        def __init__(self):
            self.plain_polls = 0
            self.rte_polls = 0

        def drain_rte_events(self):
            return []

        def general_poll(self):
            self.plain_polls += 1
            return 0x01  # the address byte, which is what the bug returned

        def general_poll_rte(self):
            # An exhausting queue, because a real one exhausts. This used to
            # return the same event on every call, which was harmless while a
            # heartbeat took exactly one poll and becomes an infinite event
            # source once it drains to empty -- the fixture would then be
            # modelling a machine that can never be caught up with.
            self.rte_polls += 1
            if self.rte_polls > 1:
                return SimpleNamespace(code=0x00, fields=None)
            return SimpleNamespace(code=0x7E, fields={"wager_cents": 500})

    client = _RTERecorder()
    seen: list = []
    beat = _make_heartbeat(client, conn=None, now="now", seen=seen, rte=True)
    beat()

    assert client.plain_polls == 0, "the non-RTE poll must not be used under RTE"
    # Two: the event, then the 0x00 that says the queue is empty.
    assert client.rte_polls == 2
    assert seen == [(0x7E, {"wager_cents": 500}, "rte-heartbeat")], \
        "the heartbeat must carry the event's decoded fields, not just a code"


def test_an_rte_heartbeat_treats_silence_as_idle():
    """12.6: a machine in this mode reports no activity by sending nothing.
    general_poll_rte() returns None for that, and None is not an event."""
    class _Silent:
        def general_poll_rte(self):
            return None

        def drain_rte_events(self):
            return []

    seen: list = []
    _make_heartbeat(_Silent(), conn=None, now="now", seen=seen, rte=True)()
    assert seen == []


def test_an_rte_heartbeat_drops_an_explicit_code_00():
    """The lab cabinet sends an explicit code-00 event where 12.6 says it
    should send nothing at all. Both shapes mean idle and neither is an
    exception worth a row."""
    class _ExplicitIdle:
        def general_poll_rte(self):
            return SimpleNamespace(code=0x00, fields={})

        def drain_rte_events(self):
            return []

    seen: list = []
    _make_heartbeat(_ExplicitIdle(), conn=None, now="now", seen=seen, rte=True)()
    assert seen == []


def test_interleaved_events_are_recorded_before_the_heartbeats_own(monkeypatch):
    """Arrival order, which the 41-hour soak lost.

    Under RTE an event can arrive in place of a LONG poll's reply (12.3), and
    the client keeps it. Those used to be written only at the end of the
    cycle -- after every heartbeat code -- so an event that arrived DURING
    the long poll just before a heartbeat was stored AFTER the heartbeat's
    own event, and row ids ran backwards against arrival.

    It cost ids 2883/2884 on 2026-10-29: total_coin_in 281,750 stored before
    281,725. Since 12.5.3's total_coin_in only ever rises, rte_reconcile read
    that inversion as a 50-credit jump against a 25-credit wager and reported
    300 credits "unaccounted for" over twelve gaps. Nothing was missing.
    """
    class _Both:
        def __init__(self):
            self.drained = False

        def drain_rte_events(self):
            self.drained = True
            return [SimpleNamespace(code=0x7E, fields={"total_coin_in": 281725})]

        def general_poll_rte(self):
            assert self.drained, "the buffer must be drained BEFORE this poll"
            # One event then an empty queue: a heartbeat drains to empty, so
            # a fixture that answers forever is a machine nobody can catch up
            # with rather than a test of ordering.
            self.polls = getattr(self, "polls", 0) + 1
            if self.polls > 1:
                return SimpleNamespace(code=0x00, fields=None)
            return SimpleNamespace(code=0x7E, fields={"total_coin_in": 281750})

    seen: list = []
    _make_heartbeat(_Both(), conn=None, now="now", seen=seen, rte=True)()

    assert [f["total_coin_in"] for _, f, _ in seen] == [281725, 281750], \
        "the earlier arrival must be stored first, so ids follow arrival"
    assert [src for _, _, src in seen] == ["rte-interleaved", "rte-heartbeat"], \
        "each source keeps its own label even though one list carries both"


def test_a_failing_drain_does_not_abort_the_heartbeat():
    """Best-effort, like the poll itself: a drain that raises must not cost
    the sweep its heartbeat."""
    class _BadDrain:
        def drain_rte_events(self):
            raise SASTimeoutError("no")

        def general_poll_rte(self):
            self.polls = getattr(self, "polls", 0) + 1
            if self.polls > 1:
                return SimpleNamespace(code=0x00, fields=None)
            return SimpleNamespace(code=0x7F, fields={"game_win": 0})

    seen: list = []
    _make_heartbeat(_BadDrain(), conn=None, now="now", seen=seen, rte=True)()
    assert seen == [(0x7F, {"game_win": 0}, "rte-heartbeat")]


# --- draining to empty, 2026-11-06 ---------------------------------------

def test_a_heartbeat_drains_the_whole_queue_not_one_event():
    """THE CHANGE THAT MAKES A LONG SWEEP SAFE ON A SPEC-FLOOR CABINET.

    A heartbeat used to take exactly one general poll, so a sweep alternated
    long poll / general poll blindly: one event drained per two slots, while
    events can arrive faster. Under a burst the queue falls further behind
    with every pair.

    The inequality that decides whether anything is lost is

        sweep duration  <  queue depth / event rate

    and at 300 ms, 60 long polls is 18 s of sweep. Against this cabinet's
    measured 67-deep queue and a 3.2 events/s burst the limit is 20.9 s, so
    it survived with 14% to spare. Against §2.2.1's guaranteed 20 the limit
    is 6.2 s and the same sweep is three times over.

    Draining to empty removes the sweep's duration from that inequality
    altogether: the queue can only grow by what arrives between two checks.
    """
    class _Queue:
        def __init__(self, codes):
            self._codes = list(codes)
            self.polls = 0

        def drain_rte_events(self):
            return []

        def general_poll_rte(self):
            self.polls += 1
            code = self._codes.pop(0) if self._codes else 0x00
            return SimpleNamespace(code=code, fields=None)

    seen = []
    client = _Queue([0x7E, 0x7F, 0x71, 0x72, 0x66])
    beat = _make_heartbeat(client, conn=None, now="now", seen=seen, rte=True)
    beat()
    assert [c for c, _, _ in seen] == [0x7E, 0x7F, 0x71, 0x72, 0x66], (
        "one heartbeat must empty the queue, not take one event from it")
    assert client.polls == 6, "five events then the 0x00 that ends it"


def test_the_drain_is_capped_so_a_busy_machine_cannot_hold_the_sweep():
    """The cap is not optional. A machine firing events continuously would
    otherwise hold the sweep forever, and a meter row that never gets
    written is worse than a late one.

    Nothing is dropped when it fires: the sweep resumes and the next
    heartbeat carries on draining."""
    from examples.sql_poll_logger import HEARTBEAT_DRAIN_CAP

    class _Endless:
        def __init__(self):
            self.polls = 0

        def drain_rte_events(self):
            return []

        def general_poll_rte(self):
            self.polls += 1
            return SimpleNamespace(code=0x7E, fields=None)

    seen = []
    client = _Endless()
    beat = _make_heartbeat(client, conn=None, now="now", seen=seen, rte=True)
    beat()
    assert client.polls == HEARTBEAT_DRAIN_CAP
    assert beat.capped == 1, (
        "hitting the cap must be recorded: it is the signal that events are "
        "arriving faster than one heartbeat can clear")


def test_the_cap_is_at_least_a_whole_spec_floor_queue():
    """Below §2.2.1's twenty, a heartbeat could give up with the guaranteed
    queue still full, which is the one depth every cabinet promises."""
    from examples.sql_poll_logger import HEARTBEAT_DRAIN_CAP
    assert HEARTBEAT_DRAIN_CAP >= 20


# --- the credit sampler: it must cost nothing under a burst -------------

class _Burst:
    """A machine whose queue never empties: every general poll yields an
    event, so the heartbeat runs to HEARTBEAT_DRAIN_CAP and never returns
    on ExceptionCode.NONE. That is a button burst."""

    def __init__(self):
        self.rte_polls = 0
        self.meter_polls = 0

    def general_poll_rte(self):
        self.rte_polls += 1
        return SimpleNamespace(code=0x7E, fields={"wager_cents": 100})

    def drain_rte_events(self):
        return []

    def send_meter(self, poll):
        self.meter_polls += 1
        return 12345


class _Quiet(_Burst):
    """A machine with nothing queued: the first poll says so."""

    def general_poll_rte(self):
        self.rte_polls += 1
        return SimpleNamespace(code=0x00, fields=None)


def _conn():
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE credit_samples (id INTEGER PRIMARY KEY, "
              "sampled_at TEXT, credits INTEGER, heartbeats INTEGER)")
    return c


def test_the_credit_sampler_STANDS_DOWN_under_a_button_burst():
    """The operator's constraint, and the whole reason this fires on queue
    state rather than on a timer:

        "The cost is important. Button bursts shouldn't make the events
        lost. All our aim is gathering correct data without jeopardizing
        the correct polling."

    A burst never lets the heartbeat reach ExceptionCode.NONE, so the
    sampler is never called. It gets out of the way exactly when event
    collection matters most, with no threshold to tune.
    """
    client, conn = _Burst(), _conn()
    sampler = _make_credit_sampler(client, conn, every=1)
    beat = _make_heartbeat(client, conn=None, now="now", seen=[], rte=True,
                           credit_sampler=sampler)
    for _ in range(5):
        beat()

    assert client.meter_polls == 0, (
        "a credit poll went out while the queue was backed up -- that slot "
        "could have carried an event")
    assert sampler.taken == 0
    assert conn.execute("SELECT count(*) FROM credit_samples").fetchone()[0] == 0
    # And it really was a burst, not a fixture that did nothing.
    assert client.rte_polls == 5 * HEARTBEAT_DRAIN_CAP


def test_the_credit_sampler_fires_on_an_EMPTY_queue():
    """The one moment a poll provably cannot displace an event: there was
    no event for that slot to carry."""
    client, conn = _Quiet(), _conn()
    sampler = _make_credit_sampler(client, conn, every=1)
    beat = _make_heartbeat(client, conn=None, now="now", seen=[], rte=True,
                           credit_sampler=sampler)
    for _ in range(3):
        beat()

    assert client.meter_polls == 3
    assert sampler.taken == 3
    rows = conn.execute("SELECT credits, heartbeats FROM credit_samples "
                        "ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [12345, 12345, 12345]
    assert [r[1] for r in rows] == [1, 2, 3]


def test_every_N_means_every_N():
    client, conn = _Quiet(), _conn()
    sampler = _make_credit_sampler(client, conn, every=4)
    beat = _make_heartbeat(client, conn=None, now="now", seen=[], rte=True,
                           credit_sampler=sampler)
    for _ in range(12):
        beat()
    assert sampler.taken == 3
    assert client.meter_polls == 3


def test_a_preempted_credit_poll_is_SILENT_not_a_fault():
    """12.3 lets the machine answer this long poll with an event that
    arrived after the heartbeat read empty. The event is collected by the
    normal pre-emption path; only the reading is missed. Recording that as
    a fault would make a working design look broken."""
    from saspy.exceptions import SASPreempted

    class _Preempts(_Quiet):
        def send_meter(self, poll):
            self.meter_polls += 1
            raise SASPreempted("an event took the slot")

    client, conn = _Preempts(), _conn()
    sampler = _make_credit_sampler(client, conn, every=1)
    beat = _make_heartbeat(client, conn=None, now="now", seen=[], rte=True,
                           credit_sampler=sampler)
    beat()
    assert sampler.preempted == 1
    assert sampler.taken == 0
    assert sampler.failed == 0, "a pre-emption is not a failure"
    assert conn.execute("SELECT count(*) FROM credit_samples").fetchone()[0] == 0


def test_a_failed_credit_poll_never_aborts_the_sweep():
    class _Broken(_Quiet):
        def send_meter(self, poll):
            self.meter_polls += 1
            raise SASTimeoutError("no reply")

    client, conn = _Broken(), _conn()
    sampler = _make_credit_sampler(client, conn, every=1)
    beat = _make_heartbeat(client, conn=None, now="now", seen=[], rte=True,
                           credit_sampler=sampler)
    beat()        # must not raise
    assert sampler.failed == 1
    assert sampler.taken == 0


def test_zero_means_off_and_off_is_the_default():
    """A knob that moves money or wire traffic defaults to off. The poller
    builds no sampler at all at 0, so there is no code path to get wrong."""
    import inspect

    from sql_poll_logger import poll_and_log

    assert inspect.signature(poll_and_log).parameters[
        "credit_poll_every"].default == 0

    client, conn = _Quiet(), _conn()
    sampler = _make_credit_sampler(client, conn, every=0)
    sampler()
    sampler()
    assert client.meter_polls == 0
    assert sampler.taken == 0


def test_the_sampler_is_not_wired_into_an_INTERROGATION_heartbeat():
    """An interrogation is already ~150 long polls and the one thing it must
    not do is get longer. Matched against what RUNS, per tests/guards.py --
    the comment above the wiring says the same thing and a naive search
    would find that instead."""
    from guards import python_code_only

    src = python_code_only(
        (Path(__file__).resolve().parent.parent
         / "examples" / "sql_poll_logger.py").read_text())
    # python_code_only() tokenises, so it joins with spaces: match the
    # bare name, not the call punctuation. (Caught by this very test.)
    # Exactly one place builds a sampler, and exactly one heartbeat takes it.
    assert src.count("_make_credit_sampler") == 2, (
        "one definition and one call site: a second call site means an "
        "interrogation started sampling too")
    assert src.count("credit_sampler = credit_sampler") == 1


# --- §12.4's requeue path, which no test executed until 2026-11-08 -------


def test_a_PRE_EMPTED_long_poll_is_requeued_and_the_message_can_be_PRINTED():
    """The path that ended two soaks.

    `poll()` catches SASPreempted, beats, PRINTS a line naming the poll,
    and sends it again. That print read `f"[{now}] ..."` with no `now` in
    scope, so the first real customer-style button burst raised
    NameError, escaped the _MeterPollFailure handler, reached main()'s
    catch-all and ended the run -- on 2026-11-08 at 17:16 and again at
    22:51 when it was reproduced deliberately.

    Every test passed throughout, because no test had ever driven a
    pre-emption. This one does, and it asserts the recovery AND that the
    message formats, which is the half that was broken.
    """
    from sql_poll_logger import SASPreempted, _poll_all_meters
    from tests.test_sql_poll_logger import ScriptedClient, make_meters

    class PreemptOnce:
        """Answers everything, but pre-empts the FIRST meter poll once."""

        def __init__(self):
            self._inner = ScriptedClient(meters_script=[make_meters(), make_meters()])
            self._fired = False

        def send_meters_10_through_15(self):
            if not self._fired:
                self._fired = True
                raise SASPreempted("pre-empted", events_collected=24, elapsed=1.0)
            return self._inner.send_meters_10_through_15()

        def __getattr__(self, item):
            return getattr(self._inner, item)

    client = PreemptOnce()
    values = _poll_all_meters(
        client,
        now="2026-11-08T22:51:21+00:00",
        heartbeat=lambda: None,
        heartbeat_every=8,
    )
    assert client._fired, "the pre-emption never happened -- the test proves nothing"
    assert values, "the sweep must recover and return meters after a requeue"


def test_the_requeue_message_formats_without_a_now(capsys):
    """`now` defaults to empty, so an embedding caller that omits it gets a
    tidy line rather than a crash. The crash is the thing being prevented."""
    from sql_poll_logger import SASPreempted, _poll_all_meters
    from tests.test_sql_poll_logger import ScriptedClient, make_meters

    class PreemptOnce:
        def __init__(self):
            self._inner = ScriptedClient(meters_script=[make_meters(), make_meters()])
            self._fired = False

        def send_meters_10_through_15(self):
            if not self._fired:
                self._fired = True
                raise SASPreempted("pre-empted", events_collected=24, elapsed=1.0)
            return self._inner.send_meters_10_through_15()

        def __getattr__(self, item):
            return getattr(self._inner, item)

    _poll_all_meters(PreemptOnce(), heartbeat=lambda: None)   # no now= at all
    assert "was pre-empted" in capsys.readouterr().out
