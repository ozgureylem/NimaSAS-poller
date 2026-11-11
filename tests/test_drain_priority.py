"""Drain priority: spend poll slots on collecting, not on meters, under load.

WHY IT EXISTS. Measured on the lab cabinet 2026-11-08 under a deliberate
button burst: one sweep made 170 exchanges in 60 seconds, of which only 97
were general polls. **A long poll that is not pre-empted drains nothing**,
so 43% of the pacing budget did no draining at all -- 1.6 events/s
collected against a 3.3/s allowance, while the cabinet produced 11.51/s.
Six game starts were lost and the cabinet raised 24 buffer overflows live.

It cannot beat the protocol: one poll drains one event and §2.3.3 caps any
conforming host at 5/s. It changes how close we get to that ceiling.

The operator authorised the trade in those terms: "suspending meter sweep
is something I can exchange with accurate customer result reading."
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from sql_poll_logger import (  # noqa: E402
    DRAIN_ENTER_FRACTION, DRAIN_LEAVE_FRACTION, PollState, _drain_sleep,
    _should_drain,
)


def _state(rate: float, capacity: float = 3.3, draining: bool = False):
    s = PollState()
    s.last_event_rate, s.last_capacity, s.draining = rate, capacity, draining
    return s


# --- the decision -------------------------------------------------------

def test_a_quiet_machine_never_defers_the_sweep():
    assert _should_drain(_state(0.9)) is False          # 27%, the idle case


def test_a_burst_defers_it():
    assert _should_drain(_state(11.51)) is True         # 345%, the measured burst


def test_the_thresholds_have_HYSTERESIS():
    """A single threshold oscillates: one busy cycle defers the sweep, the
    quiet cycle that follows resumes it, and the sweep never finishes while
    play sits near the line."""
    assert DRAIN_LEAVE_FRACTION < DRAIN_ENTER_FRACTION
    between = _state(3.3 * (DRAIN_ENTER_FRACTION + DRAIN_LEAVE_FRACTION) / 2)
    assert _should_drain(between) is False, "must not ENTER in the dead band"
    between.draining = True
    assert _should_drain(between) is True, "must not LEAVE in the dead band"


def test_an_unknown_capacity_changes_nothing():
    """No pacing floor configured means no budget to compare against.
    Guessing would either defer every sweep or never defer one."""
    assert _should_drain(_state(99.0, capacity=0.0)) is False
    assert _should_drain(_state(99.0, capacity=0.0, draining=True)) is True


# --- the wait -----------------------------------------------------------

class _Flood:
    """Answers every general poll with an event, for ever."""
    def __init__(self):
        self.polls = 0

    def general_poll(self):
        self.polls += 1
        return 0x71


class _Quiet:
    def __init__(self):
        self.polls = 0

    def general_poll(self):
        self.polls += 1
        return 0x00


def test_not_draining_means_an_ordinary_SLEEP(monkeypatch):
    slept = []
    client = _Flood()
    n = _drain_sleep(client, None, _state(0.5), 2.0, rte=False,
                     sleep_fn=slept.append)
    assert slept == [2.0] and client.polls == 0 and n == 0


def test_draining_spends_the_WAIT_ON_GENERAL_POLLS(tmp_path):
    """The inter-cycle gap is the largest uninterrupted block in the loop,
    and during a burst it is the worst possible moment to stop collecting."""
    import sqlite3
    from sql_poll_logger import SCHEMA
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)

    clock = [0.0]

    def mono():
        clock[0] += 0.3          # the §2.3.3 pacing floor this run used
        return clock[0]

    client = _Flood()
    state = _state(11.51, draining=True)
    n = _drain_sleep(client, conn, state, 2.0, rte=False,
                     monotonic_fn=mono, sleep_fn=lambda _s: None)

    assert n > 0, "drain mode collected nothing from a flooding machine"
    assert client.polls == n
    stored = conn.execute("SELECT count(*) FROM machine_events").fetchone()[0]
    assert stored == n, "every drained event must reach the database"


def test_a_QUIET_machine_stops_polling_and_waits():
    """§2.3.3's floor is a minimum interval, not a target. Polling a silent
    cabinet at full rate spends the budget for nothing."""
    slept = []
    clock = [0.0]

    def mono():
        clock[0] += 0.3
        return clock[0]

    client = _Quiet()
    n = _drain_sleep(client, None, _state(11.51, draining=True), 2.0,
                     rte=False, monotonic_fn=mono, sleep_fn=slept.append)
    assert n == 0
    assert client.polls == 1, "stopped after the first empty answer"
    assert slept, "the rest of the interval must still be waited out"


def test_a_DEAD_LINK_is_the_cycles_problem_not_this_ones():
    """Best-effort, exactly like the heartbeat: a drain that raises would
    take the whole run with it -- which is how today's NameError ended two
    soaks."""
    from saspy.exceptions import SASTimeoutError

    class Dead:
        def general_poll(self):
            raise SASTimeoutError("no response")

    slept = []
    n = _drain_sleep(Dead(), None, _state(11.51, draining=True), 2.0,
                     rte=False, monotonic_fn=lambda: 0.0,
                     sleep_fn=slept.append)
    assert n == 0 and slept


# --- what must never be deferred ----------------------------------------

def test_the_GROUPED_meters_are_never_deferred():
    """Coin in, coin out and games played anchor every reconciliation
    window. A burst may leave that window with a COARSER meter sample; it
    must not leave it with none."""
    src = (ROOT / "examples" / "sql_poll_logger.py").read_text()
    assert "full_sweep=full_meter_sweep and not draining," in src
    assert "table_c7_sweep=c7_due and not draining," in src
    assert "validation_meters_sweep=validation_meters_sweep and not draining," in src
    # GROUPED_METER_POLL_COUNT polls have no `and not draining` anywhere:
    # _poll_all_meters sends them unconditionally.
    assert "GROUPED_METER_POLL_COUNT = 8" in src


def test_it_is_ON_by_default_and_can_be_turned_off():
    """Changed 2026-11-11, at the operator's instruction: "drain priority
    must be on by default (sas poller, not only during soak). It really
    helps losing less of rte."

    It was off because it changes what goes on the wire, and this project
    makes that kind of decision explicit at start time. The measurements
    settled it the other way. What drain priority defers is 39 single
    meters, 13 Table C-7 chunks and 12 validation polls -- every one of
    them a CUMULATIVE counter that reads the same a minute later. What it
    buys is poll slots during the only moments events are at risk, and a
    lost real time event is gone for good: no meter carries a player's
    turnover (HANDOVER §19, A14). An asymmetric trade, measured, so the
    default follows the measurement.

    Tested through the real parser rather than by matching source text.
    The previous version grepped for `action="store_true"` and called it
    a "cheap structural check"; build_parser() exists now, so the check
    can be the actual behaviour.
    """
    import sql_poll_logger as m
    parse = lambda *a: m.build_parser().parse_args(["gateway.ini", *a])
    assert parse().drain_priority is True, "the DEFAULT must be on"
    assert parse("--drain-priority").drain_priority is True, \
        "the explicit flag must still work, so a unit file can say so"
    assert parse("--no-drain-priority").drain_priority is False, \
        "measuring what the deferral is worth needs a way to turn it off"
    assert m.DRAIN_ENTER_FRACTION == 0.50


# --- reacting INSIDE the sweep, not a cycle later -----------------------
#
# The first hardware run, 2026-11-08T23:51, measured 74% of the budget and
# then ran the full 56.9-second sweep anyway, because the decision came
# from the PREVIOUS cycle. Drain mode engaged 59 seconds later, after the
# burst was over. A 59-second reaction to a ten-second event is no
# reaction at all.


def test_the_probe_waits_for_a_usable_SAMPLE():
    """A rate over a fraction of a second is noise: one event in the first
    50 ms reads as 20/s and would abandon every sweep on this cabinet."""
    from sql_poll_logger import _live_drain_probe
    clock = [0.0]
    st = _state(0.0)
    st.events_seen_live = 50
    probe = _live_drain_probe(st, 0.0, monotonic_fn=lambda: clock[0])
    clock[0] = 0.05
    assert probe() is False, "judged a burst from 50 ms of data"


def test_the_probe_fires_MID_SWEEP_on_a_real_burst():
    from sql_poll_logger import _live_drain_probe
    clock = [0.0]
    st = _state(0.0)
    probe = _live_drain_probe(st, 0.0, monotonic_fn=lambda: clock[0])
    clock[0] = 10.0
    st.events_seen_live = 115           # 11.5/s, the measured burst
    assert probe() is True


def test_the_probe_is_quiet_on_an_ordinary_run():
    from sql_poll_logger import _live_drain_probe
    clock = [0.0]
    st = _state(0.0)
    probe = _live_drain_probe(st, 0.0, monotonic_fn=lambda: clock[0])
    clock[0] = 10.0
    st.events_seen_live = 9             # 0.9/s, autoplay
    assert probe() is False


def test_the_optional_sweeps_CHECK_the_probe():
    """Structural, because the cost of this being wrong is a sweep that
    runs to the end while the queue overflows."""
    src = (ROOT / "examples" / "sql_poll_logger.py").read_text()
    assert src.count("if give_up_optional():") >= 2
    assert "give_up_optional=(" in src, "poll_and_log must pass a live probe"


def test_the_default_is_to_FINISH_the_sweep():
    """Without --drain-priority nothing changes, including this."""
    from sql_poll_logger import _never_give_up
    assert _never_give_up() is False


def test_the_long_poll_COUNT_follows_the_decision_not_the_flags():
    """Third time on this line. 2026-11-08: the cycle line read "60lp" for
    a cycle that made 8, because `draining` was missing from the count
    while the call site had it. 8 long polls plus 8 heartbeats at the
    0.3 s floor is 4.8 s, and the cycle took 4.796 s -- the arithmetic was
    the only thing that said so.
    """
    src = (ROOT / "examples" / "sql_poll_logger.py").read_text()
    start = src.index("meter_poll_count = (")
    block = src[start:src.index("\n    #", start + 1)]
    for term in ("last_accepted_bill_poll and not draining",
                 "full_meter_sweep and not draining",
                 "c7_due and not draining",
                 "validation_meters_sweep and not draining"):
        assert term in block, f"meter_poll_count ignores draining for {term}"


def test_the_probe_reads_the_LIVE_counter_not_the_FILED_one():
    """The reason --drain-priority did nothing on its first burst.

    events_this_cycle is incremented in _dispatch_exception. The heartbeat
    QUEUES most codes into `seen` and dispatches them after the sweep, so
    mid-sweep that counter is still near zero however hard the cabinet is
    flooding. The probe read it and therefore could never fire -- measured
    2026-11-09T00:18, where three sweeps in a row were pre-empted 24 times
    each and no probe fired once.
    """
    from sql_poll_logger import _live_drain_probe
    st = _state(0.0)
    st.events_this_cycle = 115      # filed, but not yet -- this is the trap
    st.events_seen_live = 0
    probe = _live_drain_probe(st, 0.0, monotonic_fn=lambda: 10.0)
    assert probe() is False, "reading the filed counter again"
    st.events_seen_live = 115
    assert probe() is True


def test_a_FAILED_sweep_still_updates_the_rate():
    """A pre-empted sweep is the strongest evidence of a burst there is,
    and it was the one evidence the decision could not see: the failure
    path returns before the headroom block that records the rate, so
    _should_drain kept reading the last SUCCESSFUL cycle's 25%."""
    src = (ROOT / "examples" / "sql_poll_logger.py").read_text()
    handler = src[src.index("except _MeterPollFailure as failure:"):]
    handler = handler[:handler.index("        return")]
    assert "state.last_capacity" in handler
    assert "state.last_event_rate" in handler


def test_every_optional_sweep_has_a_probe_including_C7():
    """The first burst died inside send_extended_meters(chunk 5/13) -- the
    one optional loop with no probe in it."""
    src = (ROOT / "examples" / "sql_poll_logger.py").read_text()
    assert src.count("if give_up_optional():") >= 3, (
        "single meters, Table C-7 chunks and validation meters must each "
        "be able to give up mid-loop")
