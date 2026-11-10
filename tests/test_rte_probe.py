"""The RTE capability probe, and the one thing it must never do quietly.

Operator, 2026-11-10: "we should add enable RTE during the survey. To see if
machine that's being surveyed is really RTE compatible."

SAS 6.02 has no capability query: §4.4 lets a machine ignore a long poll it
does not implement, so a cabinet that cannot do RTE answers LP 0x0E with
silence. Trying is the only way to know.

That is why enable_real_time_events sits in meter_snapshot's NEVER_PROBE
list, and it stays there. This is a separate, opt-in procedure, and its whole
design rule is that LEAVING RTE ON MUST NOT BE ABLE TO HAPPEN QUIETLY -- a
surveyed floor cabinet goes back to the incumbent CMS, and in RTE it would
feed that CMS Table 12.5 frames where it expects single bytes.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from conftest import requires_lab_file

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from saspy.exceptions import SASError, SASTimeoutError  # noqa: E402
from meter_snapshot import NEVER_PROBE, SAFE_PROBES, probe_rte  # noqa: E402


class Cabinet:
    """A machine that speaks RTE."""

    def __init__(self, *, supports=True, disable_fails=0, events=0,
                 stuck_in_rte=False):
        self.supports = supports
        self.disable_fails = disable_fails
        self.events = events
        self.stuck_in_rte = stuck_in_rte
        self.address = 1
        self.rte = False
        self.calls = []

    def enable_real_time_events(self, enable=True):
        self.calls.append(("enable", enable))
        if not self.supports:
            raise SASTimeoutError("got 0/1 bytes before timeout")
        if not enable and self.disable_fails > 0:
            self.disable_fails -= 1
            raise SASError("no reply")
        self.rte = enable

    def general_poll_rte(self):
        self.calls.append(("poll_rte",))
        if self.events:
            self.events -= 1
            class E:
                code = 0x7E
                fields = None
            return E()
        return None

    def general_poll(self):
        self.calls.append(("poll",))
        # A machine still in RTE answers Table 12.5's address/FF frame, so a
        # one-byte read comes back holding the ADDRESS.
        return self.address if (self.rte or self.stuck_in_rte) else 0x00


def _no_sleep(_):
    return None


def test_enable_real_time_events_is_still_in_NEVER_PROBE():
    """The probe is an opt-in procedure, not an addition to the safe table.
    If it ever lands in SAFE_PROBES it runs on every survey of every
    cabinet, unattended, with no teardown of its own."""
    assert "enable_real_time_events" in NEVER_PROBE
    assert "enable_real_time_events" not in {m for _, m, _, _ in SAFE_PROBES}


def test_a_capable_cabinet_is_reported_supported_and_left_OFF():
    c = Cabinet(events=2)
    out = probe_rte(c, polls=4, sleep=_no_sleep)
    assert out["supported"] is True
    assert out["events_seen"] == 2
    assert out["disabled_confirmed"] is True
    assert c.rte is False
    assert ("enable", False) in c.calls


def test_a_cabinet_that_ignores_LP_0x0E_is_NOT_capable_and_owes_no_teardown():
    """§4.4: a machine ignores a long poll it does not implement. Nothing
    was turned on, so nothing needs turning off -- and the probe must not
    report an unconfirmed teardown for a machine it never touched."""
    c = Cabinet(supports=False)
    out = probe_rte(c, polls=4, sleep=_no_sleep)
    assert out["supported"] is False
    assert out["disabled_confirmed"] is True
    assert ("enable", False) not in c.calls


def test_an_idle_cabinet_still_counts_as_capable():
    """§12.6: a machine in RTE reports no activity by sending NOTHING. A
    survey runs before opening, on an idle floor, so zero events is the
    expected case -- the frame parsing working at all is what proves the
    mode took. Reading silence as 'not capable' would mark every idle
    cabinet unsupported."""
    c = Cabinet(events=0)
    out = probe_rte(c, polls=3, sleep=_no_sleep)
    assert out["supported"] is True
    assert out["events_seen"] == 0


def test_a_failed_disable_is_RETRIED_then_reported_unconfirmed():
    """Three attempts, and if none of them can be verified the result says
    so. This is the outcome that leaves a floor cabinet unreadable by the
    incumbent CMS."""
    c = Cabinet(disable_fails=99)
    out = probe_rte(c, polls=2, sleep=_no_sleep)
    assert out["disabled_confirmed"] is False
    assert out["error"]
    assert sum(1 for k in c.calls if k == ("enable", False)) == 3


def test_a_disable_that_LOOKS_fine_but_left_the_machine_in_RTE_is_caught():
    """The teardown is VERIFIED, not assumed. A machine still in RTE answers
    a one-byte general poll with its own ADDRESS byte -- the same signature
    that produced phantom 'exception 0x01' on address 1 when the poller's
    cycle poll was converted and the heartbeat's was not."""
    c = Cabinet(stuck_in_rte=True)
    out = probe_rte(c, polls=2, sleep=_no_sleep)
    assert out["disabled_confirmed"] is False
    assert "still in RTE" in out["error"]


def test_the_teardown_runs_even_when_polling_raises():
    class Exploding(Cabinet):
        def general_poll_rte(self):
            raise SASError("link died mid-probe")

    c = Exploding()
    out = probe_rte(c, polls=4, sleep=_no_sleep)
    assert ("enable", False) in c.calls, (
        "the disable must run in a finally -- a probe that dies between "
        "enable and teardown is exactly how a cabinet gets left in RTE")
    assert out["disabled_confirmed"] is True


def test_the_disable_is_inside_a_finally_in_the_source():
    """Structural, so a refactor cannot move the teardown onto the happy
    path without this failing. Matched on the AST rather than on text --
    a text search for 'finally' would match this docstring."""
    tree = ast.parse((ROOT / "examples" / "meter_snapshot.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "probe_rte")
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]
    assert tries, "probe_rte has no try/finally at all"
    disables = []
    for t in tries:
        for node in ast.walk(ast.Module(body=t.finalbody, type_ignores=[])):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "enable_real_time_events"):
                disables.append(node)
    assert disables, (
        "no enable_real_time_events() call in a finally block -- the "
        "teardown is on a path an exception can skip")


def test_the_floor_survey_has_a_backstop_teardown_on_every_exit():
    """meter_snapshot's finally does not run if the process is killed --
    the survey's own `timeout` can do that mid-probe. finish() runs on
    every exit path of the survey, so the backstop belongs there."""
    script = requires_lab_file(
        ROOT / "deploy" / "floor-survey.sh").read_text()
    finish = script[script.index("finish() {"):]
    finish = finish[:finish.index("\n}\n")]
    assert "--disable-rte" in finish, (
        "floor-survey.sh's finish() does not force RTE off. A snapshot "
        "killed by the deadline between enable and teardown would leave the "
        "cabinet in RTE, unattended, on a floor")
