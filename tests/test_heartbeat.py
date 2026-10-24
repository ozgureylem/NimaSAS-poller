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
