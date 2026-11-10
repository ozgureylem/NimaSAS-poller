"""Poll fast only while draining, only if the machine said it can.

The operator's instruction, 2026-11-09: "Normal polling will be 300ms and
under drain priority; it will be set to max_polling_rate. But only during
drain priority, it will be set back to 300ms as soon as drain priority is
off."

WHY IT IS WORTH DOING. One poll drains exactly one event, so the drain
ceiling is 1 / poll_interval. At 300 ms that is 3.3 events/s against the
11.51/s a button burst produced on the lab cabinet -- a race that cannot
be won at any queue depth, because the queue only sets how many seconds
pass before loss starts. At 40 ms it is 25/s, which EXCEEDS the burst, so
the queue drains back to empty and nothing is lost.

This project's own note of 2026-11-02 said 40 ms "buys latency, not
retention; retention is the queue's job". Half right, and handover §31
records the correction.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from sql_poll_logger import (  # noqa: E402
    SAS_FAST_POLL_INTERVAL_S, PollState, _apply_drain_poll_rate,
    _set_poll_interval,
)


class _Transport:
    def __init__(self, interval=0.3):
        self._min_poll_interval = interval


class _Client:
    def __init__(self, interval=0.3):
        self.transport = _Transport(interval)


def _state(supported, normal=0.3):
    s = PollState()
    s.fast_poll_supported = supported
    s.normal_poll_interval = normal
    return s


def test_a_machine_that_ADVERTISES_40ms_is_polled_fast_while_draining():
    c, st = _Client(), _state(True)
    _apply_drain_poll_rate(c, st, "now", want_fast=True)
    assert c.transport._min_poll_interval == SAS_FAST_POLL_INTERVAL_S
    assert st.fast_poll_active is True


def test_it_goes_BACK_the_moment_draining_ends():
    """The operator's own words: "set back to 300ms as soon as drain
    priority is off"."""
    c, st = _Client(), _state(True)
    _apply_drain_poll_rate(c, st, "now", want_fast=True)
    _apply_drain_poll_rate(c, st, "now", want_fast=False)
    assert c.transport._min_poll_interval == 0.3
    assert st.fast_poll_active is False


def test_a_machine_that_HAS_NOT_SAID_SO_is_never_polled_fast():
    """Table 7.14e bit 0 is the machine's own claim. Not advertising it is
    not a performance hint to override -- the floor has WMS and IGT
    cabinets of unknown vintage."""
    c, st = _Client(), _state(False)
    _apply_drain_poll_rate(c, st, "now", want_fast=True)
    assert c.transport._min_poll_interval == 0.3
    assert st.fast_poll_active is False


def test_an_UNANSWERED_capability_is_not_a_yes(capsys):
    """None means LP 0xA0 was never read -- an interrogation that failed,
    or a machine that ignored it under §4.4. Silence is not consent."""
    c, st = _Client(), _state(None)
    _apply_drain_poll_rate(c, st, "now", want_fast=True)
    assert c.transport._min_poll_interval == 0.3
    assert "NOT used" in capsys.readouterr().out


def test_the_warning_about_an_unsupported_machine_is_said_ONCE(capsys):
    """A standing condition is one record, not an event stream (§8). The
    pool-age alert wrote one row per cycle and buried the real failures."""
    c, st = _Client(), _state(None)
    for _ in range(5):
        _apply_drain_poll_rate(c, st, "now", want_fast=True)
    assert capsys.readouterr().out.count("NOT used") == 1


def test_it_NEVER_goes_below_section_12_2s_floor():
    """This project polled at 14 ms once and recorded it as a bug —
    "14x over §2.3.3". 40 ms is named by §12.2; below it is not a knob."""
    c, st = _Client(), _state(True)
    _apply_drain_poll_rate(c, st, "now", want_fast=True, fast_interval=0.001)
    assert c.transport._min_poll_interval == SAS_FAST_POLL_INTERVAL_S


def test_a_SLOWER_requested_rate_is_honoured():
    """Clamped at the floor, not pinned to it: someone testing 100 ms on a
    cabinet they do not trust should get 100 ms."""
    c, st = _Client(), _state(True)
    _apply_drain_poll_rate(c, st, "now", want_fast=True, fast_interval=0.1)
    assert c.transport._min_poll_interval == 0.1


def test_flipping_to_the_state_it_is_already_in_does_nothing(capsys):
    c, st = _Client(), _state(True)
    _apply_drain_poll_rate(c, st, "now", want_fast=True)
    capsys.readouterr()
    _apply_drain_poll_rate(c, st, "now", want_fast=True)
    assert capsys.readouterr().out == ""


def test_a_client_with_no_transport_is_survived():
    class Bare:
        pass
    st = _state(True)
    _apply_drain_poll_rate(Bare(), st, "now", want_fast=True)
    assert st.fast_poll_active is False


def test_the_teardown_restores_the_rate():
    """A run that dies mid-burst must not hand the cabinet to the next tool
    still being polled at 40 ms -- and must not send its own last messages
    at that rate either, which is why the restore is BEFORE the RTE
    teardown's long poll."""
    src = (ROOT / "examples" / "sql_poll_logger.py").read_text()
    tail = src[src.index("    finally:"):]
    restore = tail.index("state.fast_poll_active and state.normal_poll_interval")
    rte = tail.index("if state.rte_enabled or rte_on:")
    assert restore < rte, "the rate must be restored before the RTE teardown"


def test_fast_poll_without_drain_priority_is_REFUSED():
    """It only changes the rate WHILE draining; without drain priority
    nothing ever drains, so the flag would be a silent no-op."""
    src = (ROOT / "examples" / "sql_poll_logger.py").read_text()
    assert "--drain-fast-poll does nothing without --drain-priority" in src


def test_the_capability_is_read_from_the_MACHINE_not_assumed():
    src = (ROOT / "examples" / "sql_poll_logger.py").read_text()
    assert 'getattr(f, "max_polling_rate_40ms", False)' in src


# --- everything fires at once -------------------------------------------
#
# The operator, 2026-11-09, reading a table of mine: "Isn't it supposed to
# start faster polling immediately?" It was not. The probe abandoned the
# optional polls mid-sweep but left the poll RATE alone until the next
# cycle, because the rate flipped at the top of a cycle from
# state.draining -- which the probe did not set. The whole latency the
# probe exists to remove, reintroduced one layer down.


def test_the_probe_flips_the_RATE_as_well_as_the_sweep():
    from sql_poll_logger import _live_drain_probe
    c, st = _Client(), _state(True)
    st.last_capacity = 3.3
    fired = []
    probe = _live_drain_probe(
        st, 0.0, monotonic_fn=lambda: 10.0,
        on_fire=lambda: (fired.append(1), _apply_drain_poll_rate(
            c, st, "now", want_fast=True)))
    st.events_seen_live = 115                      # 11.5/s
    assert probe() is True
    assert fired, "the probe abandoned the sweep but not the poll rate"
    assert c.transport._min_poll_interval == SAS_FAST_POLL_INTERVAL_S


def test_the_probe_also_makes_the_INTER_CYCLE_GAP_drain():
    """Same lag, one layer down again: _drain_sleep reads state.draining,
    which the probe did not set, so the gap after a burst-shortened cycle
    was still a sleep."""
    from sql_poll_logger import _live_drain_probe
    st = _state(True)
    st.last_capacity = 3.3
    st.events_seen_live = 115
    probe = _live_drain_probe(st, 0.0, monotonic_fn=lambda: 10.0)
    assert st.draining is False
    probe()
    assert st.draining is True


def test_the_probe_fires_its_callback_ONCE():
    from sql_poll_logger import _live_drain_probe
    st = _state(True)
    st.last_capacity = 3.3
    st.events_seen_live = 115
    fired = []
    probe = _live_drain_probe(st, 0.0, monotonic_fn=lambda: 10.0,
                              on_fire=lambda: fired.append(1))
    for _ in range(6):
        probe()
    assert len(fired) == 1


# --- a machine that never said -------------------------------------------
#
# Operator, 2026-11-09: "If a cabinet doesn't advertise; we can still try
# 100ms during drain priority."


def test_an_UNADVERTISED_machine_gets_the_fallback_rate():
    c, st = _Client(), _state(None)
    _apply_drain_poll_rate(c, st, "now", want_fast=True, fallback_interval=0.1)
    assert c.transport._min_poll_interval == 0.1
    assert st.fast_poll_active is True


def test_the_fallback_still_respects_the_floor():
    c, st = _Client(), _state(False)
    _apply_drain_poll_rate(c, st, "now", want_fast=True, fallback_interval=0.001)
    assert c.transport._min_poll_interval == SAS_FAST_POLL_INTERVAL_S


def test_fallback_ZERO_means_a_silent_machine_stays_slow():
    c, st = _Client(), _state(None)
    _apply_drain_poll_rate(c, st, "now", want_fast=True, fallback_interval=0.0)
    assert c.transport._min_poll_interval == 0.3
    assert st.fast_poll_active is False


def test_the_fallback_is_announced_ONCE_and_says_how_to_turn_it_off(capsys):
    c, st = _Client(), _state(None)
    for _ in range(4):
        _apply_drain_poll_rate(c, st, "now", want_fast=True, fallback_interval=0.1)
        _apply_drain_poll_rate(c, st, "now", want_fast=False, fallback_interval=0.1)
    out = capsys.readouterr().out
    assert out.count("NOT advertised") == 1
    assert "--drain-fast-poll-fallback 0" in out


def test_an_advertised_machine_is_NOT_given_the_fallback():
    c, st = _Client(), _state(True)
    _apply_drain_poll_rate(c, st, "now", want_fast=True,
                           fast_interval=0.04, fallback_interval=0.1)
    assert c.transport._min_poll_interval == 0.04


def test_the_decision_uses_the_BASELINE_budget_not_the_current_one():
    """Fast polling moved the DENOMINATOR, and oscillated against itself.

    last_capacity is 1/current interval, so switching to 40 ms multiplies
    it by 7.5 -- the same event rate then reads as a seventh of the budget,
    drain mode turns off, 300 ms is restored, and it looks busy again.
    Measured on hardware 2026-11-09T18:46: "ON: 3.3 events/s against a
    3.3/s budget", then "OFF: 2.59 events/s against a 25.0/s budget", over
    and over. The hysteresis cannot help -- it guards the numerator.
    """
    from sql_poll_logger import _should_drain
    st = PollState()
    st.baseline_capacity = 3.3          # what 300 ms could collect
    st.last_capacity = 25.0             # what 40 ms is collecting NOW
    st.last_event_rate = 2.17           # 66% of baseline, 9% of current
    st.draining = True
    assert _should_drain(st) is True, (
        "drain mode turned itself off because IT had sped the poller up")


def test_without_a_baseline_it_falls_back_to_the_current_capacity():
    from sql_poll_logger import _should_drain
    st = PollState()
    st.baseline_capacity = 0.0
    st.last_capacity = 3.3
    st.last_event_rate = 2.17
    assert _should_drain(st) is True


def test_a_PRE_EMPTED_SWEEP_enters_drain_mode_immediately():
    """Operator, 2026-11-09: "it moved almost 30 seconds after button
    burst."

    The burst hit send_meters_10_through_15 -- one of the 8 GROUPED polls,
    which run first and carry no give_up_optional() probe because they are
    never deferred. The cycle died 20.4s into that poll's reissues, before
    any probe point existed, so drain mode waited for the next cycle.

    SASPreempted after the full retry budget is not ambiguous: 24 real
    time events arrived in place of one reply. Acting on it costs nothing
    if the burst is over -- the next cycle measures a quiet machine and
    leaves again.
    """
    src = (ROOT / "examples" / "sql_poll_logger.py").read_text()
    handler = src[src.index("except _MeterPollFailure as failure:"):]
    handler = handler[:handler.index("        return")]
    assert "isinstance(failure.original, SASPreempted)" in handler
    assert "state.draining = True" in handler
    assert "_apply_drain_poll_rate(" in handler
