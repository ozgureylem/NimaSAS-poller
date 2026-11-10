"""§2.3.3's polling floor, enforced where every message passes.

    "The host may not issue general polls or long polls to any single
    gaming machine at a rate faster than once per 200 ms."

Both kinds, counted together. A sweep that interleaves heartbeat general
polls between its long polls pays for both out of one budget, and that is
how this was being broken: a 22-exchange sweep with a heartbeat after every
exchange measured 0.616 s on the lab EGT -- 44 messages, 14 ms apart.

§12.2 relaxes the floor to 40 ms for real time events, and 14 ms breaches
that too. The relaxation is also one there is no reason to take: §12.3
delivers at most one event per poll, so 200 ms already allows five events a
second against a measured play rate near 0.85.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.transport import SASTransport


class _FakePort:
    parity = "M"

    def __init__(self):
        self.written = bytearray()

    def write(self, data):
        self.written.extend(data)
        return len(data)

    def read(self, size=1):
        return b""

    def reset_input_buffer(self):
        pass

    def flush(self):
        pass


def _paced(monkeypatch, interval, clock):
    """A transport whose clock and sleep are ours, so the test measures the
    decision rather than wall time."""
    slept = []
    port = _FakePort()
    t = SASTransport(port, min_poll_interval=interval, wake_byte=None)
    monkeypatch.setattr("saspy.transport.time.monotonic", lambda: clock[0])

    def _sleep(s):
        slept.append(s)
        clock[0] += s

    monkeypatch.setattr("saspy.transport.time.sleep", _sleep)
    return t, slept


def test_the_default_is_the_specifications_own_number():
    assert SASTransport(_FakePort())._min_poll_interval == 0.2


def test_the_first_message_is_not_delayed(monkeypatch):
    clock = [100.0]
    t, slept = _paced(monkeypatch, 0.2, clock)
    t.write_with_wakeup(b"\x81")
    assert slept == []


def test_a_message_too_soon_waits_out_the_remainder(monkeypatch):
    clock = [100.0]
    t, slept = _paced(monkeypatch, 0.2, clock)
    t.write_with_wakeup(b"\x81")
    clock[0] += 0.05           # only 50 ms later
    t.write_with_wakeup(b"\x81")
    assert slept == [pytest.approx(0.15)]


def test_a_message_after_the_interval_is_not_delayed(monkeypatch):
    clock = [100.0]
    t, slept = _paced(monkeypatch, 0.2, clock)
    t.write_with_wakeup(b"\x81")
    clock[0] += 0.25
    t.write_with_wakeup(b"\x81")
    assert slept == []


def test_the_interval_is_measured_from_the_previous_SEND(monkeypatch):
    """§2.3.3: "The polling rate does not include the gaming machine
    response time or the inter-byte delay time." An exchange whose reply
    already took 150 ms waits 50 ms more, not another full interval."""
    clock = [100.0]
    t, slept = _paced(monkeypatch, 0.2, clock)
    t.write_with_wakeup(b"\x01\xA0\x00\x00\x6C\x13")
    clock[0] += 0.15           # the machine's reply took 150 ms
    t.write_with_wakeup(b"\x81")
    assert slept == [pytest.approx(0.05)]


def test_pacing_applies_to_general_polls_too_not_just_long_polls(monkeypatch):
    """The sentence says "general polls OR long polls", and the heartbeat
    general polls are exactly what blew the budget."""
    clock = [100.0]
    t, slept = _paced(monkeypatch, 0.2, clock)
    t.write_with_wakeup(b"\x01\x0F\x00\x00")   # a long poll
    clock[0] += 0.01
    t.write_with_wakeup(b"\x81")               # a general poll, 10 ms later
    assert slept == [pytest.approx(0.19)]


def test_the_rte_relaxation_is_available_but_not_the_default(monkeypatch):
    """§12.2: "the polling rate can be increased to 40 ms in order to better
    approximate real time reporting." Permission, not obligation."""
    clock = [100.0]
    t, slept = _paced(monkeypatch, 0.04, clock)
    t.write_with_wakeup(b"\x81")
    clock[0] += 0.01
    t.write_with_wakeup(b"\x81")
    assert slept == [pytest.approx(0.03)]


def test_zero_disables_pacing_entirely(monkeypatch):
    """For tests, replay and simulated ports -- never for a real machine."""
    clock = [100.0]
    t, slept = _paced(monkeypatch, 0.0, clock)
    for _ in range(5):
        t.write_with_wakeup(b"\x81")
    assert slept == []


def test_an_empty_frame_neither_paces_nor_counts(monkeypatch):
    clock = [100.0]
    t, slept = _paced(monkeypatch, 0.2, clock)
    t.write_with_wakeup(b"")
    t.write_with_wakeup(b"\x81")
    assert slept == [], "a frame that sends nothing is not a poll"


# --- what the floor costs a retry loop ----------------------------------

def test_a_spent_budget_names_the_preemption_not_a_bare_timeout():
    """§2.3.3's floor makes each §12.4 retry cost a poll slot, so a long
    poll being pre-empted repeatedly now runs out of time before it runs
    out of retries. The error has to say that. "got 0/2 bytes before
    timeout" names neither the pre-emption nor the events collected, and a
    misleading error is how a morning gets spent on a working link."""
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    from test_rte import ADDRESS, FakeSerial, _event
    from saspy.bcd import encode_bcd
    from saspy.client import SASClient
    from saspy.constants import LongPoll
    from saspy.exceptions import SASError

    many = _event(0x7F, encode_bcd(0, 4)) * 30
    client = SASClient(
        SASTransport(FakeSerial(many), wake_byte=None, min_poll_interval=0.2),
        ADDRESS, timeout=1.0)
    client._rte_active = True

    with pytest.raises(SASError, match="section 12.3"):
        client.send_meter(LongPoll.SEND_CURRENT_CREDITS)
    assert len(client.drain_rte_events()) >= 1, "collected events are never lost"


def test_the_first_attempt_is_never_cut_short_by_the_budget_check():
    """The give-up only applies once something has actually pre-empted. A
    poll issued with a timeout shorter than the poll interval must still go
    out on the wire, not be refused before it is sent."""
    port = _FakePort()
    t = SASTransport(port, wake_byte=None, min_poll_interval=0.2)
    with pytest.raises(Exception):      # nothing answers; a timeout is fine
        t.exchange_rte_aware(b"\x01\x1A\x12\x34", timeout=0.01,
                             data_len_for=lambda c: 0, on_event=lambda raw: None,
                             response_length=6)
    assert bytes(port.written) == b"\x01\x1A\x12\x34", (
        "the first poll must reach the wire even on a budget too short to retry")


# --- §12.4's reissue must not be rationed by §2.3.3's pacing ----------------


class _AlwaysPreempts:
    """A cabinet with a bottomless event queue: every long poll comes back as
    a Table 12.5 event instead of its own answer."""

    parity = "M"

    def __init__(self):
        self.polls = 0
        self._pending = bytearray()

    def write(self, data):
        # NOT a poll counter: the transport sends the wakeup byte and the
        # frame as separate writes, so counting writes double-counts. One
        # attempt is one reset_input_buffer, which the loop does exactly once.
        if not self._pending:
            # address, FF, code, (no data), CRC -- §12.3's pre-emption shape.
            self._pending += bytes([0x01, 0xFF, 0x7E, 0x00, 0x00])
        return len(data)

    def read(self, size=1):
        if not self._pending:
            return b""
        out, self._pending = bytes(self._pending[:size]), self._pending[size:]
        return out

    def reset_input_buffer(self):
        self.polls += 1
        self._pending = bytearray()

    def flush(self):
        pass


def _preempt_run(monkeypatch, *, interval, timeout, max_requeue=6):
    """Run a poll against a cabinet that pre-empts forever, with a clock we
    control, and report how many times it actually went out on the wire."""
    clock = [0.0]
    port = _AlwaysPreempts()
    t = SASTransport(port, min_poll_interval=interval, wake_byte=None)
    monkeypatch.setattr("saspy.transport.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("saspy.transport.time.sleep",
                        lambda s: clock.__setitem__(0, clock[0] + s))
    # Each read advances the clock a little, as a real exchange would.
    real_read = t._read_exact

    def _read(n, deadline):
        clock[0] += 0.001
        return real_read(n, deadline)

    monkeypatch.setattr(t, "_read_exact", _read)
    events = []
    with pytest.raises(Exception):
        t.exchange_rte_aware(b"\x01\x1A\x12\x34", timeout=timeout,
                             data_len_for=lambda c: 0,
                             on_event=events.append,
                             response_length=6, max_requeue=max_requeue)
    return port.polls, events


def test_pacing_no_longer_rations_the_reissues(monkeypatch):
    """The defect that cost a week. `timeout` used to be ONE deadline for the
    whole sequence, so how many reissues fitted depended on how fast they
    could be sent -- which pacing governs. Measured on the lab EGT:

        14 ms pacing, 1.0 s budget  -> ~24 reissues -> worked
        300 ms pacing, 1.0 s budget -> ~3 reissues  -> ~94 timeouts/hour
        300 ms pacing, 3.0 s budget -> ~9 reissues  -> zero

    Nothing about link health changed between those rows. Now each reissue
    gets its own timeout, so the count is max_requeue and pacing does not
    enter into it.
    """
    fast, _ = _preempt_run(monkeypatch, interval=0.014, timeout=1.0)
    slow, _ = _preempt_run(monkeypatch, interval=0.300, timeout=1.0)
    assert fast == slow == 6, (
        f"pacing still rations reissues: {fast} fast vs {slow} slow")


def test_the_timeout_no_longer_buys_more_reissues(monkeypatch):
    """The corollary: raising the exchange timeout was the right experiment
    and the wrong lever. It must now change nothing about backlog draining,
    so it can go back to being purely about link health."""
    short, _ = _preempt_run(monkeypatch, interval=0.3, timeout=1.0)
    long, _ = _preempt_run(monkeypatch, interval=0.3, timeout=3.0)
    assert short == long == 6


def test_every_reissue_still_collects_its_event(monkeypatch):
    """The reissues are not wasted work -- each one carries a real event off
    the machine, which is why a bounded number of them is a backlog being
    drained rather than a fault."""
    polls, events = _preempt_run(monkeypatch, interval=0.3, timeout=1.0)
    assert len(events) == polls == 6


def test_a_wall_clock_backstop_stops_one_machine_holding_the_port(monkeypatch):
    """max_requeue x timeout could otherwise be one cabinet starving a whole
    sweep on a floor."""
    clock = [0.0]
    port = _AlwaysPreempts()
    t = SASTransport(port, min_poll_interval=0.3, wake_byte=None)
    monkeypatch.setattr("saspy.transport.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("saspy.transport.time.sleep",
                        lambda s: clock.__setitem__(0, clock[0] + s))
    with pytest.raises(Exception, match="may not hold the port"):
        t.exchange_rte_aware(b"\x01\x1A\x12\x34", timeout=1.0,
                             data_len_for=lambda c: 0, on_event=lambda e: None,
                             response_length=6, max_requeue=1000,
                             max_requeue_seconds=2.0)
    assert port.polls < 1000, "the backstop must fire before max_requeue does"


# --- §12.4: a NACKed poll is requeued, not failed ------------------------
#
# Acceptance row P7, and the operator's reason for it: "some customers know
# that pressing change and cashout repeatedly somehow effects the sas
# system, I have seen so many customers doing that." It is not folklore. A
# few seconds of it on 2026-11-06 revived the §12.3 pre-emption class that
# had been extinct since 2026-11-01.

def test_exhausting_the_budget_raises_SASPreempted_not_a_plain_timeout(monkeypatch):
    """A pre-emption and a timeout are opposite facts and had the same type.

    Every pre-emption COLLECTED A REAL EVENT: the exchange did useful work
    and the queue is one shorter. A timeout is silence -- nothing learned,
    probably a cable. Recording both as SASTimeoutError is why 1,025 rows of
    the first kind sat in poll_errors looking like the second, and why the
    §12.3 diagnosis took a week."""
    from saspy.exceptions import SASPreempted
    clock = [0.0]
    port = _AlwaysPreempts()
    t = SASTransport(port, min_poll_interval=0.3, wake_byte=None)
    monkeypatch.setattr("saspy.transport.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("saspy.transport.time.sleep",
                        lambda s: clock.__setitem__(0, clock[0] + s))
    with pytest.raises(SASPreempted) as caught:
        t.exchange_rte_aware(b"\x01\x1A\x12\x34", timeout=1.0,
                             data_len_for=lambda c: 0, on_event=lambda e: None,
                             response_length=6, max_requeue=4)
    assert caught.value.events_collected == 4


def test_SASPreempted_is_still_a_timeout_for_existing_handlers(monkeypatch):
    """Every `except SASTimeoutError` in the tree must keep working, or a
    taxonomy fix becomes an outage."""
    from saspy.exceptions import SASPreempted
    from saspy.exceptions import SASTimeoutError as T
    assert issubclass(SASPreempted, T)


def test_a_DEAD_LINK_is_still_a_plain_timeout_not_a_preemption():
    """The distinction only helps if it stays true in both directions. A
    cabinet that answers nothing has collected no events and must not be
    reported as a backlog being drained."""
    from saspy.exceptions import SASPreempted, SASTimeoutError

    class _Silent:
        def read(self, size=1):
            return b""

        def write(self, data):
            return len(data)

        def reset_input_buffer(self):
            pass

        def flush(self):
            pass

    t = SASTransport(_Silent(), min_poll_interval=0.0, wake_byte=None)
    with pytest.raises(SASTimeoutError) as caught:
        t.exchange_rte_aware(b"\x01\x1A\x12\x34", timeout=0.01,
                             data_len_for=lambda c: 0, on_event=lambda e: None,
                             response_length=6, max_requeue=4)
    assert not isinstance(caught.value, SASPreempted)


def test_the_poller_requeues_a_preempted_poll_instead_of_failing_it():
    """§12.4: "the host considers the long poll NACKed and REINSERTS THE
    LONG POLL INTO ITS TRANSMIT QUEUE." Not "gives up on it", which is what
    the sweep did until 2026-11-06 -- a customer who generated more events
    than the reissue budget simply cost the cycle its meter read, and the
    database recorded a fault.

    The source is checked rather than the behaviour because the sweep is a
    straight line of calls with no seam to inject at; a behavioural test
    would need the whole poll loop. The three things that must be true are
    each distinctive enough to assert.
    """
    import re
    root = Path(__file__).resolve().parent.parent
    poller = (root / "examples" / "sql_poll_logger.py").read_text()
    block = poller[poller.index("    def poll(name, fn, *args):"):]
    block = block[:block.index("    def poll_optional(")]

    # It must catch the new type SEPARATELY from SASError, or the generic
    # handler below would swallow it and nothing would be requeued.
    assert "except SASPreempted as first:" in block
    assert block.index("except SASPreempted") < block.index("except SASError")

    # A heartbeat BEFORE the retry: a general poll drains one event for one
    # byte where reissuing this long poll drains one for seventy.
    first_retry = block.index("except SASPreempted as first:")
    assert "beat()" in block[first_retry:block.index("except SASPreempted as second")]

    # And a second pre-emption is a real failure, not an infinite requeue.
    assert "except SASPreempted as second:" in block
    assert re.search(r"raise _MeterPollFailure\(name, second\)", block)
