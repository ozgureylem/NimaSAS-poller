"""Draining the handpay queue without losing what it held.

Section 7.8.1 makes LP 0x1B destructive in a way that is easy to miss: the
oldest unreported entry is returned, and marked reported "when the gaming
machine's response is acknowledged". So the act of advancing the queue is
the act of making the previous entry unreadable. A drain that acknowledges
before it has durably recorded loses exactly what it was run to recover.

The EGT Premier made the cost concrete: 250 exception 51s in sixty-four
minutes, not one exception 52, and a handpay record from that morning still
at the head of the queue hours after a technician had physically cleared the
lockup -- because 7.8.1 gates exception 52 on the host having read AND
acknowledged LP 0x1B first, and no host ever had.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))

from saspy import models
from saspy.exceptions import SASTimeoutError
import handpay_queue_drain as drain


def _rec(amount, level=0x80, group=0, partial=0, reset=0):
    return {"progressive_group": group, "level": level, "amount": amount,
            "partial_pay": partial, "reset_id": reset}


# --- the all-zeros stop condition ---------------------------------------

def test_all_zeros_is_the_queue_saying_it_is_done():
    assert drain.is_empty(_rec(0, level=0))


def test_a_zero_amount_with_a_level_set_is_not_an_empty_queue():
    """Only ALL zeros means drained. A handpay of zero with a level byte is
    still a record, and treating it as the end would silently truncate the
    drain one entry early."""
    assert not drain.is_empty(_rec(0, level=0x80))


def test_a_real_record_is_not_empty():
    assert not drain.is_empty(_rec(833809))


# --- record before acknowledge ------------------------------------------

class _FakeClient:
    """Answers LP 0x1B with a queue, but only advances it when a general
    poll arrives in between -- which is what the real machine does."""

    def __init__(self, queue, advance_on_ack=True):
        self.queue = list(queue)
        self.advance_on_ack = advance_on_ack
        self.acked = False
        self.reads = 0

    def send_handpay_information(self):
        self.reads += 1
        if self.acked and self.advance_on_ack and self.queue:
            self.queue.pop(0)
            self.acked = False
        if not self.queue:
            return models.HandpayInformation(0, 0, 0, 0, 0)
        d = self.queue[0]
        return models.HandpayInformation(
            d["progressive_group"], d["level"], d["amount"],
            d["partial_pay"], d["reset_id"])

    def general_poll(self):
        self.acked = True
        return 0x00


def _run(monkeypatch, tmp_path, client, argv_extra=()):
    monkeypatch.setattr(drain, "open_serial_port", lambda *a, **k: object())
    monkeypatch.setattr(drain, "SASTransport", lambda *a, **k: object())
    monkeypatch.setattr(drain, "SASClient", lambda *a, **k: client)
    monkeypatch.setattr(drain.time, "sleep", lambda s: None)
    out = tmp_path / "q.json"
    monkeypatch.setattr(sys, "argv",
                        ["handpay_queue_drain.py", "/dev/null", "--out", str(out), *argv_extra])
    assert drain.main() == 0
    return json.loads(out.read_text())


def test_every_queued_entry_is_recovered_in_order(monkeypatch, tmp_path):
    client = _FakeClient([_rec(833809), _rec(1506), _rec(800)])
    got = _run(monkeypatch, tmp_path, client)
    assert [e["record"]["amount"] for e in got["entries"]] == [833809, 1506, 800]
    assert got["complete"] is True


def test_the_file_is_written_before_each_acknowledgement(monkeypatch, tmp_path):
    """The ordering that matters. If the acknowledging poll goes out first,
    a crash between the two loses a record that can never be read again."""
    order = []
    client = _FakeClient([_rec(833809), _rec(1506)])
    real_poll = client.general_poll
    monkeypatch.setattr(client, "general_poll",
                        lambda: (order.append("ack"), real_poll())[1])
    real_write = drain.write_now
    monkeypatch.setattr(drain, "write_now",
                        lambda p, d: (order.append(f"write:{len(d['entries'])}"),
                                      real_write(p, d))[1])
    _run(monkeypatch, tmp_path, client)
    assert order.index("write:1") < order.index("ack")


def test_a_queue_that_does_not_advance_stops_instead_of_looping(monkeypatch, tmp_path):
    """§3.2: LP 0x1B has no alternate command code, so if the machine does
    not accept the general poll as an acknowledgement it just re-sends the
    same record. Reporting one handpay as thirty-two would be worse than
    reporting none."""
    client = _FakeClient([_rec(833809)], advance_on_ack=False)
    got = _run(monkeypatch, tmp_path, client)
    assert len(got["entries"]) == 1
    assert "not advancing" in got["stopped_because"]
    assert got["complete"] is False


def test_dry_run_reads_one_entry_and_sends_no_acknowledgement(monkeypatch, tmp_path):
    client = _FakeClient([_rec(833809), _rec(1506)])
    got = _run(monkeypatch, tmp_path, client, ["--dry-run"])
    assert len(got["entries"]) == 1
    assert client.acked is False
    assert got["complete"] is False


def test_a_timeout_mid_drain_keeps_what_was_already_recovered(monkeypatch, tmp_path):
    class _Dies(_FakeClient):
        def send_handpay_information(self):
            if self.reads >= 1:
                raise SASTimeoutError("got 0/24 bytes before timeout")
            return super().send_handpay_information()

    client = _Dies([_rec(833809), _rec(1506)])
    got = _run(monkeypatch, tmp_path, client)
    assert [e["record"]["amount"] for e in got["entries"]] == [833809]
    assert "real failure" in got["stopped_because"]


def test_an_empty_queue_from_the_first_read_is_reported_as_drained(monkeypatch, tmp_path):
    got = _run(monkeypatch, tmp_path, _FakeClient([]))
    assert got["entries"] == []
    assert got["complete"] is True
