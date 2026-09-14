import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from examples.sql_poll_logger import (
    SCHEMA,
    HistoryConfig,
    PollState,
    backfill_ticket_out_history,
    poll_and_log,
    seed_validation_pool,
)
from saspy.constants import ExceptionCode
from saspy.exceptions import SASTimeoutError
from saspy.models import BasicMeters, EnhancedValidationInfo, PendingCashoutInfo, TicketValidationData


def make_meters(**overrides) -> BasicMeters:
    base = dict(
        total_cancelled_credits=0,
        total_coin_in=0,
        total_coin_out=0,
        total_drop=0,
        total_jackpot=0,
        games_played=0,
    )
    base.update(overrides)
    return BasicMeters(**base)


def make_ticket_out(**overrides) -> EnhancedValidationInfo:
    base = dict(
        validation_type=0x00,
        index_number=0,
        date="09142026",
        time="153045",
        validation_number=1234567890123456,
        amount_cents=4750,
        ticket_number=7,
        validation_system_id=0,
        expiration="00009999",
        pool_id=0,
    )
    base.update(overrides)
    return EnhancedValidationInfo(**base)


EMPTY_TICKET_OUT = EnhancedValidationInfo(
    validation_type=0,
    index_number=0,
    date="00000000",
    time="000000",
    validation_number=0,
    amount_cents=0,
    ticket_number=0,
    validation_system_id=0,
    expiration="00000000",
    pool_id=0,
)


def make_cashout_info(**overrides) -> PendingCashoutInfo:
    base = dict(cashout_type=0x00, amount_cents=1000)
    base.update(overrides)
    return PendingCashoutInfo(**base)


def seed_pool(conn, *numbers, validation_system_id=1):
    for n in numbers:
        conn.execute(
            "INSERT INTO validation_pool (validation_number, validation_system_id, status) VALUES (?, ?, 'available')",
            (n, validation_system_id),
        )
    conn.commit()


def make_ticket_in(**overrides) -> TicketValidationData:
    base = dict(ticket_in_escrow=True, amount_cents=2500, parsing_code=0, validation_data=b"\x00" + b"1" * 9)
    base.update(overrides)
    return TicketValidationData(**base)


class ScriptedClient:
    """Returns scripted sequences per method, one item per call. Methods
    with no script default to the harmless no-op response
    (general_poll -> ExceptionCode.NONE) so existing tests that only care
    about meters don't need to know about ticket capture at all.
    """

    def __init__(
        self,
        meters_script,
        *,
        exception_script=None,
        ticket_script=None,
        ticket_out_script=None,
        cashout_info_script=None,
        validation_number_script=None,
    ):
        self._meters_script = list(meters_script)
        self._exception_script = list(exception_script) if exception_script is not None else None
        self._ticket_script = list(ticket_script) if ticket_script is not None else []
        self._ticket_out_script = list(ticket_out_script) if ticket_out_script is not None else []
        self._cashout_info_script = list(cashout_info_script) if cashout_info_script is not None else []
        self._validation_number_script = list(validation_number_script) if validation_number_script is not None else []
        self.validation_number_calls = []

    def general_poll(self):
        if self._exception_script is None:
            return ExceptionCode.NONE
        item = self._exception_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def send_meters_10_through_15(self):
        item = self._meters_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def send_ticket_validation_data(self):
        item = self._ticket_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def send_enhanced_validation_information(self, function_code=0xFF):
        item = self._ticket_out_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def send_pending_cashout_info(self):
        item = self._cashout_info_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def send_validation_number(self, validation_system_id, validation_number):
        self.validation_number_calls.append((validation_system_id, validation_number))
        item = self._validation_number_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClock:
    """A controllable stand-in for time.monotonic — lets tests exercise
    cadence/burst logic without actually sleeping.
    """

    def __init__(self, start: float = 0.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def history_row_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM meters_history").fetchone()[0]


def current_coin_in(conn: sqlite3.Connection):
    row = conn.execute("SELECT total_coin_in FROM meters_current").fetchone()
    return row[0] if row else None


# --- meters_current: always exactly one row, always the latest value ---


def test_meters_current_stays_a_single_row_across_many_polls():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters(total_coin_in=i) for i in range(4)])
    for _ in range(4):
        poll_and_log(client, conn, state, history, monotonic_fn=clock)
    assert conn.execute("SELECT COUNT(*) FROM meters_current").fetchone()[0] == 1
    assert current_coin_in(conn) == 3


def test_failed_poll_does_not_touch_meters_current():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters(total_coin_in=5), SASTimeoutError("no response")])
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    assert current_coin_in(conn) == 5


# --- poll_errors: unchanged append-only behavior ---


def test_poll_and_log_writes_poll_error_on_failure_and_does_not_raise():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([SASTimeoutError("no response")])
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    rows = conn.execute("SELECT poll_name, error_type, message FROM poll_errors").fetchall()
    assert rows == [("send_meters_10_through_15", "SASTimeoutError", "no response")]
    assert history_row_count(conn) == 0


def test_one_failure_does_not_stop_subsequent_polls():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([SASTimeoutError("no response"), make_meters(total_coin_in=1)])
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    assert conn.execute("SELECT COUNT(*) FROM poll_errors").fetchone()[0] == 1
    assert current_coin_in(conn) == 1


# --- ring mode: cadence, cap, and burst behavior ---


def test_ring_mode_writes_history_on_first_poll():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring", interval=60.0)
    client = ScriptedClient([make_meters(total_coin_in=10)])
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    assert history_row_count(conn) == 1


def test_ring_mode_skips_history_write_before_interval_elapses():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring", interval=60.0)
    client = ScriptedClient([make_meters(total_coin_in=10), make_meters(total_coin_in=20)])
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    clock.advance(5)  # well under the 60s cadence
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    assert history_row_count(conn) == 1
    assert current_coin_in(conn) == 20  # meters_current still updates every cycle


def test_ring_mode_writes_history_again_once_interval_elapses():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring", interval=60.0)
    client = ScriptedClient([make_meters(total_coin_in=10), make_meters(total_coin_in=20)])
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    clock.advance(61)
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    assert history_row_count(conn) == 2


def test_ring_mode_caps_history_row_count_and_keeps_most_recent():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring", cap=3, interval=0.0)
    client = ScriptedClient([make_meters(total_coin_in=i) for i in range(5)])
    for _ in range(5):
        poll_and_log(client, conn, state, history, monotonic_fn=clock)
    assert history_row_count(conn) == 3
    remaining = [
        r[0] for r in conn.execute("SELECT total_coin_in FROM meters_history ORDER BY id").fetchall()
    ]
    assert remaining == [2, 3, 4]


def test_ring_mode_meter_decrease_triggers_burst_writes():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring", interval=60.0, burst_count=2)
    client = ScriptedClient(
        [
            make_meters(total_coin_in=100),  # baseline (first poll always writes)
            make_meters(total_coin_in=50),  # decrease -> burst starts, writes despite cadence
            make_meters(total_coin_in=60),  # still within burst, writes
            make_meters(total_coin_in=70),  # burst exhausted, cadence not elapsed -> skipped
        ]
    )
    for _ in range(4):
        poll_and_log(client, conn, state, history, monotonic_fn=clock)
        clock.advance(1)  # far less than the 60s interval
    assert history_row_count(conn) == 3


def test_failed_poll_sets_burst_for_next_good_poll_in_ring_mode():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring", interval=60.0, burst_count=1)
    client = ScriptedClient(
        [
            make_meters(total_coin_in=10),  # baseline write
            SASTimeoutError("no response"),  # failure -> logged, primes a burst
            make_meters(total_coin_in=20),  # burst active -> writes despite cadence
        ]
    )
    for _ in range(3):
        poll_and_log(client, conn, state, history, monotonic_fn=clock)
        clock.advance(1)
    assert conn.execute("SELECT COUNT(*) FROM poll_errors").fetchone()[0] == 1
    assert history_row_count(conn) == 2


def test_explicit_anomaly_signal_forces_history_write():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring", interval=60.0)
    client = ScriptedClient([make_meters(total_coin_in=1), make_meters(total_coin_in=2)])
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    clock.advance(1)  # well under the interval
    poll_and_log(client, conn, state, history, monotonic_fn=clock, anomaly=True)
    assert history_row_count(conn) == 2


# --- append mode: unbounded, every poll, cap ignored ---


def test_append_mode_writes_every_poll_and_ignores_cap():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="append", cap=2)
    client = ScriptedClient([make_meters(total_coin_in=i) for i in range(5)])
    for _ in range(5):
        poll_and_log(client, conn, state, history, monotonic_fn=clock)
    assert history_row_count(conn) == 5


def test_history_config_rejects_invalid_mode():
    with pytest.raises(ValueError):
        HistoryConfig(mode="unbounded")


# --- general poll failure does not block meters ----------------------------


def test_general_poll_failure_is_logged_and_meters_still_polled():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters(total_coin_in=42)],
        exception_script=[SASTimeoutError("no response")],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    errors = conn.execute("SELECT poll_name, error_type FROM poll_errors").fetchall()
    assert errors == [("general_poll", "SASTimeoutError")]
    assert current_coin_in(conn) == 42


# --- ticket-in capture (exception 0x67) -------------------------------------


def test_poll_and_log_captures_ticket_in_on_exception():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.TICKET_INSERTED],
        ticket_script=[make_ticket_in(amount_cents=2500)],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    rows = conn.execute("SELECT amount_cents FROM ticket_in_events").fetchall()
    assert rows == [(2500,)]


def test_poll_and_log_ignores_ticket_in_exception_when_nothing_in_escrow():
    """A rare race: the exception fired, but by the time we read LP 70 the
    ticket is no longer in escrow (already returned to the player, e.g.).
    Must not create a phantom row.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.TICKET_INSERTED],
        ticket_script=[make_ticket_in(ticket_in_escrow=False, amount_cents=0)],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    assert conn.execute("SELECT COUNT(*) FROM ticket_in_events").fetchone()[0] == 0


def test_ticket_in_capture_failure_logs_error_without_stopping_meters():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters(total_coin_in=9)],
        exception_script=[ExceptionCode.TICKET_INSERTED],
        ticket_script=[SASTimeoutError("no response")],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    errors = conn.execute("SELECT poll_name FROM poll_errors").fetchall()
    assert errors == [("send_ticket_validation_data",)]
    assert current_coin_in(conn) == 9


# --- ticket-out drain (exception 0x3D/0x3E) ---------------------------------


def test_poll_and_log_drains_ticket_out_on_cash_out_ticket_printed():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.CASH_OUT_TICKET_PRINTED],
        ticket_out_script=[make_ticket_out(validation_number=111), EMPTY_TICKET_OUT],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    rows = conn.execute("SELECT validation_number FROM ticket_out_history").fetchall()
    assert rows == [(111,)]


def test_drain_continues_past_multiple_unread_records():
    """Draining must keep going (function code 0x00, FIFO) until it sees
    an empty record — not stop after the first one — so a burst of
    several tickets printed between poll cycles is fully captured.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.HANDPAY_VALIDATED],
        ticket_out_script=[
            make_ticket_out(validation_number=1),
            make_ticket_out(validation_number=2),
            make_ticket_out(validation_number=3),
            EMPTY_TICKET_OUT,
        ],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    rows = conn.execute("SELECT validation_number FROM ticket_out_history ORDER BY validation_number").fetchall()
    assert rows == [(1,), (2,), (3,)]


def test_ticket_out_drain_failure_logs_error_without_stopping_meters():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters(total_coin_in=3)],
        exception_script=[ExceptionCode.CASH_OUT_TICKET_PRINTED],
        ticket_out_script=[SASTimeoutError("no response")],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    errors = conn.execute("SELECT poll_name FROM poll_errors").fetchall()
    assert errors == [("send_enhanced_validation_information(drain)",)]
    assert current_coin_in(conn) == 3


# --- ticket-out backfill (startup) ------------------------------------------


def test_backfill_reads_all_31_indices_and_skips_empty_slots():
    conn = make_db()
    script = [EMPTY_TICKET_OUT] * 31
    script[4] = make_ticket_out(validation_number=222, index_number=5)
    client = ScriptedClient([], ticket_out_script=script)
    found = backfill_ticket_out_history(client, conn)
    assert found == 1
    rows = conn.execute("SELECT validation_number FROM ticket_out_history").fetchall()
    assert rows == [(222,)]


def test_backfill_is_idempotent_via_dedup():
    conn = make_db()
    record = make_ticket_out(validation_number=333)
    client1 = ScriptedClient([], ticket_out_script=[record] + [EMPTY_TICKET_OUT] * 30)
    backfill_ticket_out_history(client1, conn)
    client2 = ScriptedClient([], ticket_out_script=[record] + [EMPTY_TICKET_OUT] * 30)
    backfill_ticket_out_history(client2, conn)
    assert conn.execute("SELECT COUNT(*) FROM ticket_out_history").fetchone()[0] == 1


def test_backfill_continues_past_a_read_failure():
    conn = make_db()
    script = [EMPTY_TICKET_OUT] * 31
    script[0] = SASTimeoutError("no response")
    script[10] = make_ticket_out(validation_number=444, index_number=11)
    client = ScriptedClient([], ticket_out_script=script)
    found = backfill_ticket_out_history(client, conn)
    assert found == 1
    assert conn.execute("SELECT COUNT(*) FROM poll_errors").fetchone()[0] == 1


def test_ticket_out_dedup_across_backfill_and_live_drain():
    """The same physical ticket can be seen by both the non-destructive
    backfill and the destructive live drain — must not double-count.
    """
    conn = make_db()
    record = make_ticket_out(validation_number=555, date="09142026", time="120000")
    backfill_client = ScriptedClient([], ticket_out_script=[record] + [EMPTY_TICKET_OUT] * 30)
    backfill_ticket_out_history(backfill_client, conn)

    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    live_client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.CASH_OUT_TICKET_PRINTED],
        ticket_out_script=[record, EMPTY_TICKET_OUT],
    )
    poll_and_log(live_client, conn, state, history, monotonic_fn=clock)
    assert conn.execute("SELECT COUNT(*) FROM ticket_out_history").fetchone()[0] == 1


# --- gateway-local cashout validation (exception 0x57) ----------------------


def test_cashout_request_assigns_next_available_pool_number():
    conn = make_db()
    seed_pool(conn, 111, 222)
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.SYSTEM_VALIDATION_REQUEST],
        cashout_info_script=[make_cashout_info(amount_cents=2500)],
        validation_number_script=[0x00],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    assert client.validation_number_calls == [(1, 111)]  # lowest available number used first
    row = conn.execute(
        "SELECT status, assigned_amount_cents FROM validation_pool WHERE validation_number = 111"
    ).fetchone()
    assert row == ("assigned", 2500)
    remaining = conn.execute(
        "SELECT status FROM validation_pool WHERE validation_number = 222"
    ).fetchone()
    assert remaining == ("available",)


def test_cashout_request_with_empty_pool_logs_pool_exhausted_and_leaves_machine_unanswered():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.SYSTEM_VALIDATION_REQUEST],
        cashout_info_script=[make_cashout_info(amount_cents=500)],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    errors = conn.execute("SELECT poll_name, error_type FROM poll_errors").fetchall()
    assert errors == [("validation_pool", "PoolExhausted")]
    assert client.validation_number_calls == []  # never even tried to answer


def test_cashout_request_ignores_race_where_machine_no_longer_waiting():
    conn = make_db()
    seed_pool(conn, 111)
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.SYSTEM_VALIDATION_REQUEST],
        cashout_info_script=[make_cashout_info(cashout_type=0x80)],  # "not waiting for system validation"
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    assert client.validation_number_calls == []
    row = conn.execute("SELECT status FROM validation_pool WHERE validation_number = 111").fetchone()
    assert row == ("available",)


def test_cashout_request_rejected_number_stays_available_for_reuse():
    conn = make_db()
    seed_pool(conn, 111)
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.SYSTEM_VALIDATION_REQUEST],
        cashout_info_script=[make_cashout_info(amount_cents=750)],
        validation_number_script=[0x81],  # improper validation rejected
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    row = conn.execute("SELECT status, assigned_at FROM validation_pool WHERE validation_number = 111").fetchone()
    assert row == ("available", None)


def test_cashout_info_read_failure_logs_error_without_stopping_meters():
    conn = make_db()
    seed_pool(conn, 111)
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters(total_coin_in=6)],
        exception_script=[ExceptionCode.SYSTEM_VALIDATION_REQUEST],
        cashout_info_script=[SASTimeoutError("no response")],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    errors = conn.execute("SELECT poll_name FROM poll_errors").fetchall()
    assert errors == [("send_pending_cashout_info",)]
    assert current_coin_in(conn) == 6


def test_send_validation_number_failure_leaves_number_available():
    conn = make_db()
    seed_pool(conn, 111)
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.SYSTEM_VALIDATION_REQUEST],
        cashout_info_script=[make_cashout_info()],
        validation_number_script=[SASTimeoutError("no response")],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    errors = conn.execute("SELECT poll_name FROM poll_errors").fetchall()
    assert errors == [("send_validation_number",)]
    row = conn.execute("SELECT status FROM validation_pool WHERE validation_number = 111").fetchone()
    assert row == ("available",)


# --- validation pool seeding -------------------------------------------------


def test_seed_validation_pool_adds_requested_count():
    conn = make_db()
    counter = iter(range(1, 100))
    added = seed_validation_pool(conn, 5, random_fn=lambda bits: next(counter))
    assert added == 5
    assert conn.execute("SELECT COUNT(*) FROM validation_pool WHERE status = 'available'").fetchone()[0] == 5


def test_seed_validation_pool_only_tops_up_the_shortfall():
    conn = make_db()
    seed_pool(conn, 1, 2, 3)
    counter = iter(range(1000, 1100))
    added = seed_validation_pool(conn, 5, random_fn=lambda bits: next(counter))
    assert added == 2
    assert conn.execute("SELECT COUNT(*) FROM validation_pool").fetchone()[0] == 5


def test_seed_validation_pool_retries_on_collision():
    conn = make_db()
    seed_pool(conn, 42)
    sequence = iter([42, 42, 43])  # first two candidates collide with the existing row and each other
    added = seed_validation_pool(conn, 2, random_fn=lambda bits: next(sequence))
    assert added == 1
    numbers = {r[0] for r in conn.execute("SELECT validation_number FROM validation_pool").fetchall()}
    assert numbers == {42, 43}


def test_seed_validation_pool_zero_is_a_no_op():
    conn = make_db()
    assert seed_validation_pool(conn, 0) == 0
    assert conn.execute("SELECT COUNT(*) FROM validation_pool").fetchone()[0] == 0
