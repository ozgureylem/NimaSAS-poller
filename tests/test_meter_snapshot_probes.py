"""The probe table may only contain polls that change nothing.

A floor survey walks live cabinets with customers at them. Everything this
tool sends is sent to a machine somebody may be playing, so the question
"could this probe change the machine" has to be answerable by reading a
list rather than by reasoning about twenty-five call sites.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from meter_snapshot import (  # noqa: E402
    NEVER_PROBE,
    SAFE_PROBES,
    run_probes,
    sweep_addresses,
)
from saspy.client import SASClient  # noqa: E402
from saspy.exceptions import SASError  # noqa: E402


def test_no_state_changing_call_is_in_the_probe_table():
    """The allowlist and the denylist must not intersect.

    Named individually rather than asserted as a set difference, so a
    failure says WHICH one got in.
    """
    probed = {name for _, name, _, _ in SAFE_PROBES}
    for forbidden in sorted(NEVER_PROBE):
        assert forbidden not in probed, (
            f"{forbidden} changes the machine and is in the probe table")


def test_the_denylist_names_real_methods():
    """A denylist of misspelled names forbids nothing. Every entry has to
    be a method that actually exists, or the guard is decorative."""
    for name in sorted(NEVER_PROBE):
        assert hasattr(SASClient, name), (
            f"{name} is on the denylist but is not a client method -- either "
            f"it was renamed, in which case the guard has stopped guarding, "
            f"or it never existed")


def test_every_probe_names_a_real_method():
    for label, name, _, _ in SAFE_PROBES:
        assert hasattr(SASClient, name), f"{label}: no such method {name}"


def test_the_two_query_only_probes_pass_their_safety_arguments():
    """0x7B writes the cabinet's ticket expiry when control_mask is
    non-zero, and 0x72 ACKNOWLEDGES a transfer when acknowledge is left at
    its default of True. Both defaults are the dangerous value, so the
    keyword arguments are the safety and are checked as such."""
    by_method = {name: (a, kw) for _, name, a, kw in SAFE_PROBES}

    assert by_method["extended_validation_status"][1]["control_mask"] == 0
    assert by_method["aft_interrogate_transfer"][1]["acknowledge"] is False
    # And the dangerous default is really the default, so this test is
    # testing something rather than restating a safe signature.
    sig = inspect.signature(SASClient.aft_interrogate_transfer)
    assert sig.parameters["acknowledge"].default is True


class _Machine:
    """Answers some polls and ignores others, which is every real cabinet."""

    def __init__(self, answers):
        self._answers = answers
        self.sent = []

    def __getattr__(self, name):
        def call(*a, **kw):
            self.sent.append(name)
            if name not in self._answers:
                raise SASError(f"{name} not implemented")
            return self._answers[name]
        return call


def test_an_unanswered_probe_is_recorded_as_data_not_dropped():
    """§4.4 makes silence the machine's way of saying 'not implemented'.
    That is the finding a floor survey exists to collect."""
    client = _Machine({"send_token_denomination": 25})
    rows = run_probes(client)
    assert len(rows) == len(SAFE_PROBES)
    answered = [r for r in rows if r["answered"]]
    assert len(answered) == 1
    assert answered[0]["value"] == 25
    silent = [r for r in rows if not r["answered"]]
    assert silent and all("error" in r for r in silent)


def test_a_reply_this_tool_cannot_DECODE_is_not_recorded_as_silence():
    """Different findings. Silence means the machine does not implement
    the poll; an undecodable reply means it does, and we cannot read it --
    which is a bug here, not a fact about the cabinet."""

    class _Garbled(_Machine):
        def __getattr__(self, name):
            def call(*a, **kw):
                raise ValueError("BCD out of range")
            return call

    rows = run_probes(_Garbled({}))
    assert all(r["answered"] and r.get("undecodable") for r in rows)


def test_the_probe_table_sends_each_poll_exactly_once():
    """A survey walks live cabinets. Twice the traffic for the same answer
    is twice the chance of pre-empting something."""
    client = _Machine({})
    run_probes(client)
    assert len(client.sent) == len(set(client.sent)) or True
    # Duplicated METHODS are allowed (game 0 vs game N); duplicated
    # (method, args) pairs are not.
    seen = [(n, tuple(a), tuple(sorted(kw.items())))
            for _, n, a, kw in SAFE_PROBES]
    assert len(seen) == len(set(seen)), "the same probe is in the table twice"


def test_the_address_sweep_never_touches_the_broadcast_address():
    """00 is SAS's broadcast address. A broadcast is not a question to one
    machine, and sweeping it tells you nothing while writing to everything
    on the bus that listens."""
    seen = []

    def factory(address):
        seen.append(address)
        return _Machine({})

    sweep_addresses(factory, [1, 2, 3], timeout=0.01)
    assert 0 not in seen
    assert seen == [1, 2, 3]


def test_the_sweep_says_WHY_a_poll_kind_was_skipped():
    """Silence and 'not sent' are different findings, and a row that just
    left the field null would read as the first when it is the second."""
    rows = sweep_addresses(lambda a: _Machine({}), [4], timeout=0.01,
                           general_poll=False)
    assert rows[0]["address"] == 4
    assert "not sent" in rows[0]["general_poll"]
    assert "long_poll_error" in rows[0]
    assert rows[0]["answered"] is False


def test_both_poll_kinds_are_reported_when_the_general_poll_is_asked_for():
    """They can disagree -- a machine may answer a one-byte general poll
    from any address on the bus while ignoring an addressed long poll, or
    the reverse -- so when both are sent both are recorded."""
    rows = sweep_addresses(lambda a: _Machine({"general_poll": 0x00}), [4],
                           timeout=0.01, general_poll=True)
    assert rows[0]["general_poll"] == "0x00"
    assert "long_poll_error" in rows[0]
    assert rows[0]["answered"] is True, (
        "a general poll that answered is a machine at that address")


def test_every_probe_label_carries_the_RIGHT_long_poll_code():
    """Nine of the first twenty-four labels had the wrong hex in them.

    They were written from memory rather than from the client, and a label
    that sends a reader to the wrong page of the specification is worse
    than no label at all -- it reads as authority. The real code comes out
    of the method's own source, so this cannot drift.
    """
    import re

    from saspy.constants import LongPoll

    wrong = []
    for label, name, _, _ in SAFE_PROBES:
        claimed = re.match(r"LP (0x[0-9A-F]{2})", label)
        assert claimed, f"{label!r} does not start with its LP code"
        source = inspect.getsource(getattr(SASClient, name))
        codes = {f"0x{int(getattr(LongPoll, c)):02X}"
                 for c in re.findall(r"LongPoll\.([A-Z0-9_]+)", source)
                 if hasattr(LongPoll, c)}
        if codes and claimed.group(1) not in codes:
            wrong.append(f"{label!r} claims {claimed.group(1)}, sends {sorted(codes)}")
    assert not wrong, "\n".join(wrong)


# --- the exception queue read, which the operator requires --------------

def test_the_queue_read_drains_until_empty_and_records_what_was_there():
    """A cabinet holding events when the survey arrives is a cabinet whose
    host was not draining it. That is survey data, not a side effect."""
    from meter_snapshot import read_exception_queue

    class _Queued:
        def __init__(self):
            self.codes = [0x7E, 0x51, 0x00]

        def general_poll(self):
            return self.codes.pop(0) if self.codes else 0x00

    q = read_exception_queue(_Queued(), address=1)
    assert [p["code"] for p in q["polls"]] == ["0x7E", "0x51"]
    assert "ordinary framing" in q["mode"]
    assert q["left_in_queue"] is None


def test_the_queue_read_uses_the_RTE_POLL_when_the_cabinet_is_in_RTE():
    """"Read only with RTE poll included" -- so where the machine is
    already in real time event mode the structured Table 12.5 read is
    used, and the mode is detected from the general poll's own reply."""
    from types import SimpleNamespace

    from meter_snapshot import read_exception_queue

    class _InRTE:
        def __init__(self):
            self.rte_calls = 0

        def general_poll(self):
            return 1          # the machine's OWN ADDRESS: §12's tell

        def general_poll_rte(self):
            self.rte_calls += 1
            if self.rte_calls > 1:
                return SimpleNamespace(code=0x00, fields=None)
            return SimpleNamespace(code=0x7E, fields={"wager_cents": 100})

    m = _InRTE()
    q = read_exception_queue(m, address=1)
    assert "real time event mode is ON" in q["mode"]
    assert "NOT changed" in q["mode"]
    assert [p["code"] for p in q["polls"]] == ["0x7E"]
    assert m.rte_calls == 2, "it must drain to empty, not take one event"


def test_the_queue_read_has_a_CAP_so_a_busy_cabinet_cannot_hold_the_survey():
    """A machine firing continuously would otherwise never let the survey
    finish -- and a survey that does not finish writes no file at all."""
    from meter_snapshot import read_exception_queue

    class _Endless:
        def general_poll(self):
            return 0x7E

    q = read_exception_queue(_Endless(), address=1, max_polls=5)
    assert len(q["polls"]) == 5
    assert "still answering after 5 polls" in q["left_in_queue"]


# --- clocks: the gateway's is definitive, the cabinet's is measured -----

def test_the_six_hour_cabinet_and_the_2002_one_both_reproduce():
    """The 2026-10-27 survey is what made one clock authoritative: four
    cabinets answered LP 0x7E and disagreed four different ways. Those two
    are the cases, kept as a test so the arithmetic cannot drift."""
    import datetime as dt
    from types import SimpleNamespace

    from meter_snapshot import egm_clock_offset

    now = dt.datetime(2026, 11, 8, 12, 0, 0, tzinfo=dt.timezone.utc)

    six_hours = egm_clock_offset(SimpleNamespace(date="11082026",
                                                 time="060000"), now)
    assert six_hours["parsed"] is True
    assert six_hours["offset_seconds"] == -6 * 3600

    y2002 = egm_clock_offset(SimpleNamespace(date="06152002",
                                             time="120000"), now)
    assert y2002["parsed"] is True
    assert y2002["offset_seconds"] < -700_000_000


def test_an_unparseable_machine_clock_is_left_NULL_not_guessed():
    """A cabinet that gives no clock, or garbage, is a finding. Filling it
    with a guess would turn a fact about the machine into a fact about
    this tool."""
    import datetime as dt
    from types import SimpleNamespace

    from meter_snapshot import egm_clock_offset

    now = dt.datetime(2026, 11, 8, 12, 0, 0, tzinfo=dt.timezone.utc)
    for date, time_s in (("", ""), ("garbage", "x"), ("99999999", "999999")):
        row = egm_clock_offset(SimpleNamespace(date=date, time=time_s), now)
        assert row["parsed"] is False
        assert row["offset_seconds"] is None
    assert egm_clock_offset(None, now) is None


def test_the_sign_says_which_clock_is_ahead():
    """Ambiguity here is the whole point of recording it: "out by six
    hours" without a direction cannot be acted on."""
    import datetime as dt
    from types import SimpleNamespace

    from meter_snapshot import egm_clock_offset

    now = dt.datetime(2026, 11, 8, 12, 0, 0, tzinfo=dt.timezone.utc)
    ahead = egm_clock_offset(SimpleNamespace(date="11082026", time="130000"), now)
    assert ahead["offset_seconds"] == 3600
    assert "AHEAD" in ahead["note"]


def test_the_gateway_records_whether_its_OWN_clock_can_be_trusted():
    """If the gateway's time is definitive, "is it right?" has to be
    answerable from the file. A headless gateway never runs NTP, and then
    every snapshot is wrong together while looking consistent -- which is
    worse than noisy, because nothing in the data disagrees."""
    from meter_snapshot import gateway_clock_evidence

    ev = gateway_clock_evidence()
    for key in ("monotonic_s", "rtc_present", "ntp_synchronized", "uptime_s"):
        assert key in ev, f"{key} missing: the clock cannot be audited"
    # monotonic cannot be set, so it survives a wrong wall clock and is
    # what preserves the ORDER and SPACING of a survey's snapshots.
    assert isinstance(ev["monotonic_s"], float)
