"""The two features that let the poller act on money by itself.

They exist for one reason: an unattended overnight soak stops dead at the
first jackpot lockup, and a soak that stops at 1am measures nothing after
1am. They are also the only code in this project that would be a
cash-dispensing defect on a floor, so what is tested here is mostly the
refusals -- the gate, the ceilings, and the cases where NOT acting is the
correct behaviour.

The one that matters most: a FAILED credit read must never be treated as
zero credits. A cabinet that stops answering LP 0x74 reads as broke, and a
top-up loop that believes it would push every cycle until morning.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))

from saspy.exceptions import SASTimeoutError
from sql_poll_logger import (
    LAB_AUTO_AFT_MAX_PUSHES,
    LAB_AUTO_AFT_MAX_TOTAL_CENTS,
    LAB_AUTO_AFT_MIN_INTERVAL_S,
    LAB_AUTO_HANDPAY_MIN_SECONDS,
    SCHEMA,
    LabAutoConfig,
    PollState,
    _capture_handpay,
    _lab_auto_aft_credit,
    _lab_auto_handpay_reset,
    migrate_epoch_columns,
)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    migrate_epoch_columns(conn)
    return conn


def _actions(conn):
    return conn.execute(
        "SELECT action, detail, outcome FROM lab_auto_actions ORDER BY id").fetchall()


class _Handpay:
    def __init__(self, code=0x00, raises=False):
        self.code, self.raises, self.calls = code, raises, 0

    def reset_handpay(self):
        self.calls += 1
        if self.raises:
            raise SASTimeoutError("no answer")
        return self.code


class _Aft:
    """A cabinet with `on_machine` cents, answering LP 0x74 and 0x72."""

    def __init__(self, on_machine=0, status=0x00, lock_raises=False,
                 transfer_raises=False):
        self.on_machine = on_machine
        self.status = status
        self.lock_raises = lock_raises
        self.transfer_raises = transfer_raises
        self.transfers = []

    def aft_game_lock_and_status_request(self, lock_code=0xFF):
        if self.lock_raises:
            raise SASTimeoutError("no answer")
        return SimpleNamespace(
            asset_number=246,
            current_cashable_amount=self.on_machine,
            current_restricted_amount=0,
            current_non_restricted_amount=0)

    def aft_transfer_funds(self, **kw):
        if self.transfer_raises:
            raise SASTimeoutError("no answer")
        self.transfers.append(kw)
        return SimpleNamespace(transfer_status=self.status)

    def aft_interrogate_transfer(self, index, acknowledge=False):
        return SimpleNamespace(transfer_status=0x00)


# --- the handpay clock --------------------------------------------------

def test_a_real_handpay_starts_the_clock_and_all_zeros_does_not():
    """§7.8.1: a second 0x1B read of an already-reported entry answers all
    zeros. If that restarted the clock, the 15-second 0x51 reissue would push
    the deadline out forever and the auto-reset would never fire."""
    conn = _db()
    state = PollState()
    real = SimpleNamespace(amount=10000, partial_pay=0, level=0,
                           progressive_group=0, reset_id=0x00)
    zeros = SimpleNamespace(amount=0, partial_pay=0, level=0,
                            progressive_group=0, reset_id=0x00)

    _capture_handpay(SimpleNamespace(send_handpay_information=lambda: real),
                     conn, "now", state, monotonic_fn=lambda: 100.0)
    assert state.handpay_pending_since == 100.0

    _capture_handpay(SimpleNamespace(send_handpay_information=lambda: zeros),
                     conn, "now", state, monotonic_fn=lambda: 200.0)
    assert state.handpay_pending_since == 100.0, \
        "an all-zeros re-read must not push the deadline out"


def test_without_state_capture_handpay_still_works():
    """It is called from paths that carry no state."""
    conn = _db()
    info = SimpleNamespace(amount=500, partial_pay=0, level=0,
                           progressive_group=0, reset_id=0x00)
    _capture_handpay(SimpleNamespace(send_handpay_information=lambda: info),
                     conn, "now", None)
    assert conn.execute("SELECT count(*) FROM handpay_queue").fetchone()[0] == 1


# --- the auto reset -----------------------------------------------------

def test_disabled_by_default():
    conn, client = _db(), _Handpay()
    state = PollState()
    state.handpay_pending_since = 0.0
    assert _lab_auto_handpay_reset(client, conn, "now", state, after_seconds=0,
                                   monotonic_fn=lambda: 99999.0) is False
    assert client.calls == 0


def test_it_waits_the_full_threshold():
    conn, client = _db(), _Handpay()
    state = PollState()
    state.handpay_pending_since = 100.0
    assert _lab_auto_handpay_reset(client, conn, "now", state,
                                   after_seconds=180,
                                   monotonic_fn=lambda: 279.0) is False
    assert client.calls == 0, "179 seconds is not 180"


def test_it_resets_once_the_threshold_passes_and_records_it():
    conn, client = _db(), _Handpay(code=0x00)
    state = PollState()
    state.handpay_pending_since = 100.0
    assert _lab_auto_handpay_reset(client, conn, "now", state,
                                   after_seconds=180,
                                   monotonic_fn=lambda: 281.0) is True
    assert client.calls == 1
    assert state.handpay_pending_since is None, "the lockup is gone; stop the clock"
    action, detail, outcome = _actions(conn)[0]
    assert action == "handpay_reset"
    assert "181s" in detail
    assert "0x00" in outcome


def test_no_answer_is_recorded_as_the_feature_being_off_not_retried():
    """§7.9: a machine not configured for remote handpay reset must IGNORE
    LP 0x94. Silence means the feature is off, which is a different fact
    from code 0x01, and nagging it every cycle helps nobody."""
    conn, client = _db(), _Handpay(raises=True)
    state = PollState()
    state.handpay_pending_since = 100.0
    _lab_auto_handpay_reset(client, conn, "now", state, after_seconds=180,
                            monotonic_fn=lambda: 300.0)
    assert state.handpay_pending_since is None
    assert "NO ANSWER" in _actions(conn)[0][2]
    assert "§7.9" in _actions(conn)[0][2]

    # and it does not try again on the next cycle
    _lab_auto_handpay_reset(client, conn, "now", state, after_seconds=180,
                            monotonic_fn=lambda: 600.0)
    assert client.calls == 1


@pytest.mark.parametrize("code", [0x01, 0x02])
def test_a_declined_reset_stops_the_clock_rather_than_nagging(code):
    conn, client = _db(), _Handpay(code=code)
    state = PollState()
    state.handpay_pending_since = 100.0
    _lab_auto_handpay_reset(client, conn, "now", state, after_seconds=180,
                            monotonic_fn=lambda: 300.0)
    assert state.handpay_pending_since is None
    assert client.calls == 1


# --- the auto credit ----------------------------------------------------

def test_a_failed_credit_read_is_never_treated_as_zero_credits():
    """THE one that matters. A cabinet that stops answering LP 0x74 reads as
    broke. Pushing on a failed read turns this into a money printer."""
    conn, client = _db(), _Aft(lock_raises=True)
    state = PollState()
    assert _lab_auto_aft_credit(client, conn, "now", state,
                                amount_cents=5000, floor_cents=100) is False
    assert client.transfers == []
    assert state.aft_pushes == 0
    assert _actions(conn) == []
    assert conn.execute(
        "SELECT count(*) FROM poll_errors WHERE poll_name LIKE 'aft_game_lock%'"
    ).fetchone()[0] == 1


def test_it_does_not_push_while_the_machine_still_has_credits():
    conn, client = _db(), _Aft(on_machine=5000)
    state = PollState()
    assert _lab_auto_aft_credit(client, conn, "now", state,
                                amount_cents=5000, floor_cents=100) is False
    assert client.transfers == []


def test_it_pushes_when_the_balance_is_at_or_below_the_floor():
    conn, client = _db(), _Aft(on_machine=100)
    state = PollState()
    assert _lab_auto_aft_credit(client, conn, "now", state, amount_cents=5000,
                                floor_cents=100, monotonic_fn=lambda: 10.0) is True
    assert len(client.transfers) == 1
    assert client.transfers[0]["cashable_amount"] == 5000
    assert client.transfers[0]["asset_number"] == 246
    assert state.aft_cents_pushed == 5000
    assert "aft_credit" == _actions(conn)[0][0]


def test_the_minimum_interval_holds():
    conn, client = _db(), _Aft(on_machine=0)
    state = PollState()
    state.aft_pushes = 1
    state.aft_last_push_mono = 1000.0
    assert _lab_auto_aft_credit(
        client, conn, "now", state, amount_cents=5000, floor_cents=100,
        monotonic_fn=lambda: 1000.0 + LAB_AUTO_AFT_MIN_INTERVAL_S - 1) is False
    assert client.transfers == []


def test_the_push_count_ceiling_holds():
    conn, client = _db(), _Aft(on_machine=0)
    state = PollState()
    state.aft_pushes = LAB_AUTO_AFT_MAX_PUSHES
    assert _lab_auto_aft_credit(client, conn, "now", state, amount_cents=5000,
                                floor_cents=100) is False
    assert client.transfers == []


def test_the_total_amount_ceiling_holds():
    conn, client = _db(), _Aft(on_machine=0)
    state = PollState()
    state.aft_cents_pushed = LAB_AUTO_AFT_MAX_TOTAL_CENTS
    assert _lab_auto_aft_credit(client, conn, "now", state, amount_cents=5000,
                                floor_cents=100) is False
    assert client.transfers == []


def test_a_failed_transfer_is_not_retried_and_counts_against_the_cap():
    """An exchange that fails AFTER the machine acted looks identical to one
    that failed before. A fresh transaction ID risks paying twice."""
    conn, client = _db(), _Aft(on_machine=0, transfer_raises=True)
    state = PollState()
    assert _lab_auto_aft_credit(client, conn, "now", state, amount_cents=5000,
                                floor_cents=100, monotonic_fn=lambda: 5.0) is False
    assert state.aft_pushes == 1, "a failed attempt still spends a push"
    assert state.aft_cents_pushed == 0
    outcome = _actions(conn)[0][2]
    assert "FAILED" in outcome and "may or may not have" in outcome


def test_pending_is_acknowledged_because_0x69_reissues_forever():
    """§8.8: an unacknowledged completion makes the machine reissue 0x69
    every fifteen seconds, as a priority exception, indefinitely."""
    conn = _db()
    client = _Aft(on_machine=0, status=0x40)   # PENDING
    acked = []
    client.aft_interrogate_transfer = lambda i, acknowledge=False: (
        acked.append(acknowledge) or SimpleNamespace(transfer_status=0x00))
    state = PollState()
    _lab_auto_aft_credit(client, conn, "now", state, amount_cents=5000,
                         floor_cents=100, monotonic_fn=lambda: 5.0,
                         sleep_fn=lambda s: None)
    assert acked and all(acked), "the interrogation must acknowledge"
    assert "0x00" in _actions(conn)[0][2]


def test_disabled_when_the_amount_is_zero():
    conn, client = _db(), _Aft(on_machine=0)
    assert _lab_auto_aft_credit(client, conn, "now", PollState(),
                                amount_cents=0, floor_cents=100) is False
    assert client.transfers == []


# --- the config ---------------------------------------------------------

def test_lab_auto_config_is_off_by_default():
    assert LabAutoConfig().any_enabled() is False


def test_the_minimum_threshold_is_at_least_a_minute():
    assert LAB_AUTO_HANDPAY_MIN_SECONDS >= 60.0
