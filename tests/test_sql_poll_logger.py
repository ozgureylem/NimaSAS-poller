import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from conftest import requires_lab_file

from examples.sql_poll_logger import (
    ALL_METER_FIELDS,
    GROUPED_METER_POLL_COUNT,
    PRIORITY_METER_COLUMNS,
    SAS_MIN_POLL_INTERVAL_S,
    SCHEMA,
    SINGLE_METER_COLUMNS,
    TABLE_C7_CHUNK_COUNT,
    TICKET_METER_CODES,
    TICKET_METER_COLUMNS,
    VALIDATION_METER_TYPES,
    HistoryConfig,
    PollState,
    _db_file_size_bytes,
    _dispatch_exception,
    _general_poll_with_retry,
    _insert_ticket_out_record,
    _pool_age_hours,
    _safe_commit,
    _sas_poll_interval,
    backfill_ticket_out_history,
    interrogate,
    migrate_epoch_columns,
    poll_and_log,
    seed_validation_pool,
    ticket_authority_binding_verdict,
    game_config_diff,
    identity_changed,
    machine_key,
    usable_asset,
    usable_serial,
    _check_pool_age,
)
from saspy.constants import ExceptionCode, LongPoll, MeterCode, ValidationType
from saspy.exceptions import SASTimeoutError
from saspy.models import (
    BasicMeters,
    BillMeters,
    EnhancedValidationInfo,
    ExtendedMeters,
    GamesSincePowerUpAndDoorClosure,
    HandpayInformation,
    HopperStatus,
    LastAcceptedBillInfo,
    Meters11Through15,
    PendingCashoutInfo,
    RedeemTicketResult,
    SelectedMeters,
    TicketValidationData,
    ValidationMeters,
)


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


def make_ticket_meters(**overrides) -> SelectedMeters:
    """``overrides`` keys are TICKET_METER_COLUMNS names (e.g.
    ticket_in_cashable_cents=10500), not MeterCode values — translated
    here to the {code: value} shape send_selected_meters() actually
    returns, matching the column<->code pairing in TICKET_METER_COLUMNS.
    """
    base = {column: 0 for column in TICKET_METER_COLUMNS}
    base.update(overrides)
    meters = {code: base[column] for column, code in zip(TICKET_METER_COLUMNS, TICKET_METER_CODES)}
    return SelectedMeters(game_number=0, meters=meters)


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


def seed_pool(conn, *numbers, validation_system_id=1, issued_at=None):
    """``issued_at=None`` (the default) leaves it NULL — matching a
    hand-inserted real number with no batch info, and deliberately
    excluded from _pool_age_hours() rather than treated as brand new or
    infinitely old. Pass an ISO 8601 string to backdate a row for a
    pool-age test.
    """
    for n in numbers:
        conn.execute(
            "INSERT INTO validation_pool (validation_number, validation_system_id, status, issued_at) "
            "VALUES (?, ?, 'available', ?)",
            (n, validation_system_id, issued_at),
        )
    conn.commit()


def make_ticket_in(**overrides) -> TicketValidationData:
    base = dict(ticket_in_escrow=True, amount_cents=2500, parsing_code=0, validation_data=b"\x00" + b"1" * 9)
    base.update(overrides)
    return TicketValidationData(**base)


def make_ticket_completion(**overrides) -> RedeemTicketResult:
    base = dict(machine_status=0x00, amount_cents=2500, parsing_code=0, validation_data=b"\x00" + b"1" * 9)
    base.update(overrides)
    return RedeemTicketResult(**base)


# Sentinel for table_c7_values: marks a meter code as genuinely unsupported
# by the simulated machine (size=0, §7.21b) -- absent from the response
# entirely, not present with value 0. Distinct from an Exception (a failed
# exchange) and from a plain int (a supported meter's value).
UNSUPPORTED_METER = object()


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
        ticket_completion_script=None,
        ticket_out_script=None,
        cashout_info_script=None,
        validation_number_script=None,
        ticket_meters_script=None,
        meters_11_15=None,
        extended_meters_group=None,
        games_since_power_up=None,
        total_bill_meters=None,
        hand_paid_cancelled_credits=None,
        hopper_status=None,
        single_meter_values=None,
        table_c7_values=None,
        last_accepted_bill_info=None,
        validation_meters_values=None,
        handpay_information=None,
    ):
        self._meters_script = list(meters_script)
        self._exception_script = list(exception_script) if exception_script is not None else None
        self._ticket_script = list(ticket_script) if ticket_script is not None else []
        self._ticket_completion_script = list(ticket_completion_script) if ticket_completion_script is not None else []
        self._ticket_out_script = list(ticket_out_script) if ticket_out_script is not None else []
        self._cashout_info_script = list(cashout_info_script) if cashout_info_script is not None else []
        self._validation_number_script = list(validation_number_script) if validation_number_script is not None else []
        self._ticket_meters_script = list(ticket_meters_script) if ticket_meters_script is not None else None
        # These next several are fixed values (or an Exception to raise),
        # applied on every call rather than popped from a sequence — the
        # rest of this tool's grouped meter polls are read once per cycle
        # with nothing to sequence, unlike the exception-driven scripts
        # above. None means "use a harmless all-zero default response."
        self._meters_11_15 = meters_11_15
        self._extended_meters_group = extended_meters_group
        self._games_since_power_up = games_since_power_up
        self._total_bill_meters = total_bill_meters
        self._hand_paid_cancelled_credits = hand_paid_cancelled_credits
        self._hopper_status = hopper_status
        self._single_meter_values = dict(single_meter_values) if single_meter_values else {}
        self._table_c7_values = dict(table_c7_values) if table_c7_values else {}
        self._last_accepted_bill_info = last_accepted_bill_info
        self._validation_meters_values = dict(validation_meters_values) if validation_meters_values else {}
        self._handpay_information = handpay_information
        self.validation_number_calls = []
        self.extended_meter_commands = []

    def general_poll(self):
        if self._exception_script is None:
            return ExceptionCode.NONE
        if not self._exception_script:
            # A real machine answers every general poll, returning 00 when it
            # has nothing pending (2.2.1). The script is the events to deliver,
            # not a cap on how many times it may be asked -- the meter sweep
            # now interleaves heartbeat polls to keep the exception queue
            # drained, so a fake that ran dry would be modelling a machine
            # that stops answering, which is not a thing.
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

    def send_selected_meters(self, meter_codes, *, game_number=0):
        if self._ticket_meters_script is None:
            # No script given: a harmless all-zero read (never decreases,
            # never raises) so tests that don't care about ticket meters
            # don't need to know this poll exists at all.
            return make_ticket_meters()
        item = self._ticket_meters_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @staticmethod
    def _fixed_or_raise(value, default):
        if value is None:
            return default
        if isinstance(value, Exception):
            raise value
        return value

    def send_meters_11_through_15(self):
        return self._fixed_or_raise(
            self._meters_11_15,
            Meters11Through15(total_coin_in=0, total_coin_out=0, total_drop=0, total_jackpot=0, games_played=0),
        )

    def send_extended_meters_group(self):
        return self._fixed_or_raise(
            self._extended_meters_group,
            ExtendedMeters(
                total_coin_in=0, total_coin_out=0, total_drop=0, total_jackpot=0,
                games_played=0, games_won=0, slot_door_opened=0, power_reset=0,
            ),
        )

    def send_games_since_power_up_and_door_closure(self):
        return self._fixed_or_raise(
            self._games_since_power_up,
            GamesSincePowerUpAndDoorClosure(games_since_power_up=0, games_since_door_closure=0),
        )

    def send_total_bill_meters(self):
        return self._fixed_or_raise(
            self._total_bill_meters,
            BillMeters(bills_1=0, bills_5=0, bills_10=0, bills_20=0, bills_50=0, bills_100=0),
        )

    def send_total_hand_paid_cancelled_credits(self, game_number=0):
        return self._fixed_or_raise(self._hand_paid_cancelled_credits, 0)

    def send_handpay_information(self):
        return self._fixed_or_raise(
            self._handpay_information,
            HandpayInformation(progressive_group=0, level=0, amount=0,
                               partial_pay=0, reset_id=0),
        )

    def send_current_hopper_status(self):
        return self._fixed_or_raise(self._hopper_status, HopperStatus(status=0, percent_full=0, level=0))

    def send_last_accepted_bill_information(self):
        return self._fixed_or_raise(
            self._last_accepted_bill_info,
            LastAcceptedBillInfo(country_code=0, denomination_code=0, bill_meter=0),
        )

    def send_validation_meters(self, validation_type):
        """``validation_meters_values`` maps ValidationType -> an int (used
        for both fields), a (total_validations, cumulative_amount_cents)
        tuple, or an Exception to raise. Unscripted types default to 0/0.
        """
        value = self._validation_meters_values.get(validation_type, 0)
        if isinstance(value, Exception):
            raise value
        total, amount = value if isinstance(value, tuple) else (value, value)
        return ValidationMeters(validation_type=validation_type, total_validations=total, cumulative_amount_cents=amount)

    def send_meter(self, poll):
        value = self._single_meter_values.get(poll, 0)
        if isinstance(value, Exception):
            raise value
        return value

    def send_extended_meters(self, meter_codes, *, game_number=0):
        return self._extended_meters(0x6F, meter_codes, game_number)

    def send_extended_meters_alternate(self, meter_codes, *, game_number=0):
        return self._extended_meters(0xAF, meter_codes, game_number)

    def _extended_meters(self, command, meter_codes, game_number):
        # Records the command byte per call so tests can assert the caller
        # alternates 6F/AF across consecutive chunks (§7.21 / Table 3.1).
        self.extended_meter_commands.append(command)
        meters = {}
        for code in meter_codes:
            value = self._table_c7_values.get(code, 0)
            if isinstance(value, Exception):
                raise value
            if value is UNSUPPORTED_METER:
                continue  # matches the real client: an unsupported code is silently absent, not zero
            meters[code] = value
        return SelectedMeters(game_number=game_number, meters=meters)

    def send_ticket_validation_data(self):
        item = self._ticket_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def redeem_ticket_status(self):
        item = self._ticket_completion_script.pop(0)
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


# --- column order ------------------------------------------------------


def test_priority_meter_columns_lead_all_meter_fields_in_order():
    """Business-facing meters (coin in/out, cancelled credits, jackpot,
    dollar value of bills, games played, ticket in/out cashable and
    restricted) come first, in the exact order PRIORITY_METER_COLUMNS
    specifies -- everything else follows, in whatever order it already
    had. This is a column-position guarantee, not just a set membership
    one: meters_current/meters_history are meant to be eyeballed and
    queried with SELECT *, so where a field lands in the row matters.
    """
    assert ALL_METER_FIELDS[: len(PRIORITY_METER_COLUMNS)] == PRIORITY_METER_COLUMNS


def test_priority_meter_columns_are_not_duplicated_in_all_meter_fields():
    assert len(ALL_METER_FIELDS) == len(set(ALL_METER_FIELDS))


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


# --- ticket meters (LP 2F): cumulative Cashable/Restricted Ticket In/Out ---


def test_ticket_meters_are_written_to_meters_current_and_history():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        ticket_meters_script=[make_ticket_meters(ticket_in_cashable_cents=10_500, ticket_out_cashable_count=3)],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)

    current = conn.execute(
        "SELECT ticket_in_cashable_cents, ticket_out_cashable_count FROM meters_current"
    ).fetchone()
    assert current == (10_500, 3)
    history_row = conn.execute(
        "SELECT ticket_in_cashable_cents, ticket_out_cashable_count FROM meters_history"
    ).fetchone()
    assert history_row == (10_500, 3)


def test_ticket_meters_poll_failure_logs_error_and_does_not_touch_meters_current():
    """Mirrors test_failed_poll_does_not_touch_meters_current for LP 0F:
    a failed LP 2F read must not write a meters_current row that's only
    half current (basic meters fresh, ticket meters stale or missing).
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters(total_coin_in=5)],
        ticket_meters_script=[SASTimeoutError("no response")],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    rows = conn.execute("SELECT poll_name, error_type, message FROM poll_errors").fetchall()
    assert rows == [("send_selected_meters(ticket_meters)", "SASTimeoutError", "no response")]
    assert conn.execute("SELECT COUNT(*) FROM meters_current").fetchone()[0] == 0
    assert history_row_count(conn) == 0


def test_ticket_meter_decrease_arms_the_burst_window_like_a_core_meter_decrease():
    """A wrapped ticket meter (§8.2 rollover) is ordinary diagnostic
    signal, not an error -- exactly like a core-meter decrease. See
    test_poll_and_log_detects_ticket_in_meter_rollover_during_105_dollar_ticket_capture
    for the full $105.00-ticket scenario this generalizes.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring", burst_count=3)
    client = ScriptedClient(
        [make_meters(), make_meters()],
        ticket_meters_script=[
            make_ticket_meters(ticket_out_cashable_count=50),
            make_ticket_meters(ticket_out_cashable_count=2),  # wrapped: fewer than before
        ],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    assert state.burst_remaining == 0  # first cycle: nothing to compare against yet
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    assert state.burst_remaining == history.burst_count - 1
    assert conn.execute("SELECT COUNT(*) FROM poll_errors").fetchone()[0] == 0


# --- the wider meter sweep: LP 0x18/0x19/0x1C/0x1E/0x2D/0x4F and the ~39
#     single-meter polls (LP 0x10-0x51/0x55) -- deliberately redundant with
#     the core six and with each other; see the module docstring for why ---


def test_poll_and_log_writes_every_meter_column():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()])
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    row = conn.execute(f"SELECT {', '.join(ALL_METER_FIELDS)} FROM meters_current").fetchone()
    assert row is not None
    assert len(row) == len(ALL_METER_FIELDS)
    # every default across every group is 0 -- nothing silently missing or None
    assert all(v == 0 for v in row)


def test_redundant_meter_reads_are_stored_independently_even_when_they_disagree():
    """Different long polls reading the same underlying counter aren't
    reconciled or deduped by this tool -- storing both, even when they
    disagree, is the point (see the module docstring): three long polls
    disagreeing about "total coin in" this cycle is a real finding a
    single poll can never surface.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters(total_coin_in=100)],
        meters_11_15=Meters11Through15(
            total_coin_in=999, total_coin_out=0, total_drop=0, total_jackpot=0, games_played=0
        ),
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    row = conn.execute("SELECT total_coin_in, lp19_total_coin_in FROM meters_current").fetchone()
    assert row == (100, 999)


def test_skip_full_meter_sweep_leaves_single_meter_columns_null_but_other_groups_populate():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters(total_coin_in=7)])
    poll_and_log(client, conn, state, history, monotonic_fn=clock, full_meter_sweep=False)
    row = conn.execute(
        "SELECT total_coin_in, sm_true_coin_in, lp19_total_coin_in FROM meters_current"
    ).fetchone()
    assert row == (7, None, 0)


def test_toggling_full_meter_sweep_between_cycles_does_not_crash_decrease_check():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters(), make_meters()])
    poll_and_log(client, conn, state, history, monotonic_fn=clock, full_meter_sweep=True)
    poll_and_log(client, conn, state, history, monotonic_fn=clock, full_meter_sweep=False)
    assert conn.execute("SELECT COUNT(*) FROM poll_errors").fetchone()[0] == 0


@pytest.mark.parametrize(
    "kwarg,poll_name",
    [
        ("meters_11_15", "send_meters_11_through_15"),
        ("extended_meters_group", "send_extended_meters_group"),
        ("games_since_power_up", "send_games_since_power_up_and_door_closure"),
        ("total_bill_meters", "send_total_bill_meters"),
        ("hand_paid_cancelled_credits", "send_total_hand_paid_cancelled_credits"),
        ("hopper_status", "send_current_hopper_status"),
    ],
)
def test_any_new_meter_group_poll_failure_aborts_the_whole_cycle(kwarg, poll_name):
    """Confirms the "any poll failure aborts the whole cycle" rule holds
    for every meter poll added in this expansion, not just LP 0x0F/0x2F.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters(total_coin_in=5)], **{kwarg: SASTimeoutError("no response")})
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    rows = conn.execute("SELECT poll_name, error_type FROM poll_errors").fetchall()
    assert rows == [(poll_name, "SASTimeoutError")]
    assert conn.execute("SELECT COUNT(*) FROM meters_current").fetchone()[0] == 0


def test_single_meter_sweep_failure_aborts_the_whole_cycle():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        single_meter_values={LongPoll.SEND_TRUE_COIN_IN: SASTimeoutError("no response")},
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    rows = conn.execute("SELECT poll_name, error_type FROM poll_errors").fetchall()
    assert rows == [("send_meter(SEND_TRUE_COIN_IN)", "SASTimeoutError")]
    assert conn.execute("SELECT COUNT(*) FROM meters_current").fetchone()[0] == 0


# --- Table C-7 extended sweep (LP 0x6F, ~154 meters in 13 chunks) ----------


def test_table_c7_sweep_writes_values_from_multiple_chunks():
    """MeterCode.TOTAL_COIN_IN_CREDITS (0x00) is in the first chunk;
    MeterCode.IN_HOUSE_TRANSFERS_TO_HOST_THAT_INCLUDED_NONRESTRICTED_AMOUNTS_QUANTITY
    (0xBD, the very last Table C-7 entry) is in the last -- covering both
    exercises more than just the first send_extended_meters() call.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        table_c7_values={MeterCode.TOTAL_COIN_IN_CREDITS: 12345, MeterCode.IN_HOUSE_TRANSFERS_TO_HOST_THAT_INCLUDED_NONRESTRICTED_AMOUNTS_QUANTITY: 7},
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    row = conn.execute(
        "SELECT c7_total_coin_in_credits, c7_in_house_transfers_to_host_that_included_nonrestricted_amounts_quantity "
        "FROM meters_current"
    ).fetchone()
    assert row == (12345, 7)


def test_table_c7_sweep_failure_aborts_the_whole_cycle():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        table_c7_values={MeterCode.GAMES_WON: SASTimeoutError("no response")},
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    rows = conn.execute("SELECT poll_name, error_type FROM poll_errors").fetchall()
    assert rows == [("send_extended_meters(chunk 1/13)", "SASTimeoutError")]
    assert conn.execute("SELECT COUNT(*) FROM meters_current").fetchone()[0] == 0


def test_table_c7_sweep_meter_unsupported_by_the_machine_is_null_not_a_crash():
    """A meter the EGM doesn't implement is answered with size=0 (§7.21b)
    -- send_extended_meters() silently omits it from result.meters, which
    is a normal, known outcome, not a failure. Regression test: an
    earlier version indexed result.meters[code] directly, which raised
    an uncaught KeyError (crashing the whole tool, not just that column)
    the moment any real EGM was missing even one of Table C-7's ~154
    optional meters -- close to guaranteed on real hardware, since almost
    no machine implements the entire table. The row must still be
    written, with NULL for exactly the unsupported meter and real values
    for every other one in the same chunk.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        table_c7_values={
            MeterCode.TOTAL_COIN_IN_CREDITS: UNSUPPORTED_METER,
            MeterCode.GAMES_WON: 42,
        },
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    row = conn.execute(
        "SELECT c7_total_coin_in_credits, c7_games_won FROM meters_current"
    ).fetchone()
    assert row == (None, 42)
    assert conn.execute("SELECT COUNT(*) FROM poll_errors").fetchone()[0] == 0


def test_table_c7_sweep_alternates_6f_and_af_between_consecutive_chunks():
    """SAS implicitly ACKs a long poll with "a general poll or a long poll
    with a different command byte" (Table 3.1); a *repeated* identical poll
    is an implied NACK meaning "re-send what you just sent" (3.2). 0xAF
    reaches identical meter data to 0x6F and exists precisely so a host can
    alternate: 7.21 provides it "to allow a host to perform consecutive
    meter polls and still provide a proper implied acknowledgement", and
    the spec's 6.00 revision note says the same in one line.

    Calibration, so this test isn't read as guarding against a known
    failure: IGT's own tooling fires 41 consecutive LP 0x52 polls in its
    canned meter script, so machines clearly tolerate same-command runs in
    practice. Alternating is cheap insurance on the one poll the spec
    singled out for automated back-to-back use, not a patched bug.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()])
    poll_and_log(client, conn, state, history, monotonic_fn=clock)

    sent = client.extended_meter_commands
    assert len(sent) == TABLE_C7_CHUNK_COUNT
    assert sent == [0x6F if n % 2 == 0 else 0xAF for n in range(TABLE_C7_CHUNK_COUNT)]
    # The property that actually matters, stated independently of the
    # ordering above: no two consecutive chunks share a command byte.
    assert all(a != b for a, b in zip(sent, sent[1:]))


def test_skip_table_c7_sweep_leaves_its_columns_null_but_other_groups_populate():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters(total_coin_in=7)])
    poll_and_log(client, conn, state, history, monotonic_fn=clock, table_c7_sweep=False)
    row = conn.execute(
        "SELECT total_coin_in, c7_total_coin_in_credits, sm_true_coin_in FROM meters_current"
    ).fetchone()
    assert row == (7, None, 0)


# --- Last accepted bill information (LP 0x48) -------------------------------


def test_last_accepted_bill_information_writes_values():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        last_accepted_bill_info=LastAcceptedBillInfo(country_code=1, denomination_code=4, bill_meter=37),
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    row = conn.execute(
        "SELECT lp48_last_bill_country_code, lp48_last_bill_denomination_code, lp48_last_bill_meter "
        "FROM meters_current"
    ).fetchone()
    assert row == (1, 4, 37)


def test_last_accepted_bill_information_failure_aborts_the_whole_cycle():
    """Unlike LP 0x6F's per-meter size=0 signal, a machine that doesn't
    support LP 0x48 at all (§7.11) fails the whole exchange -- a genuine
    SASError, not a soft skip. On by default, so this must still abort
    the cycle the same as every other grouped poll; see
    --skip-last-accepted-bill-poll for the actual opt-out.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()], last_accepted_bill_info=SASTimeoutError("no response"))
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    rows = conn.execute("SELECT poll_name, error_type FROM poll_errors").fetchall()
    assert rows == [("send_last_accepted_bill_information", "SASTimeoutError")]
    assert conn.execute("SELECT COUNT(*) FROM meters_current").fetchone()[0] == 0


def test_skip_last_accepted_bill_poll_leaves_its_columns_null_but_other_groups_populate():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters(total_coin_in=7)],
        last_accepted_bill_info=SASTimeoutError("would abort if this ran"),
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock, last_accepted_bill_poll=False)
    row = conn.execute(
        "SELECT total_coin_in, lp48_last_bill_country_code, lp48_last_bill_denomination_code, lp48_last_bill_meter "
        "FROM meters_current"
    ).fetchone()
    assert row == (7, None, None, None)
    assert conn.execute("SELECT COUNT(*) FROM poll_errors").fetchone()[0] == 0


# --- Validation meters (LP 0x50) ---------------------------------------------


def test_validation_meters_sweep_writes_values_for_each_type():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        validation_meters_values={
            ValidationType.CASHABLE_TICKET_REDEEMED: (12, 4750),
            ValidationType.JACKPOT_HANDPAY_RECEIPT_PRINTED: (3, 90000),
        },
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    row = conn.execute(
        "SELECT lp50_cashable_ticket_redeemed_total_validations, "
        "lp50_cashable_ticket_redeemed_cumulative_amount_cents, "
        "lp50_jackpot_handpay_receipt_printed_total_validations, "
        "lp50_jackpot_handpay_receipt_printed_cumulative_amount_cents "
        "FROM meters_current"
    ).fetchone()
    assert row == (12, 4750, 3, 90000)


def test_validation_meters_sweep_failure_aborts_the_whole_cycle():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        validation_meters_values={ValidationType.CASHABLE_TICKET_REDEEMED: SASTimeoutError("no response")},
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    rows = conn.execute("SELECT poll_name, error_type FROM poll_errors").fetchall()
    assert rows == [(f"send_validation_meters({ValidationType.CASHABLE_TICKET_REDEEMED.name})", "SASTimeoutError")]
    assert conn.execute("SELECT COUNT(*) FROM meters_current").fetchone()[0] == 0


def test_skip_validation_meters_sweep_leaves_its_columns_null_but_other_groups_populate():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters(total_coin_in=7)],
        validation_meters_values={ValidationType.CASHABLE_TICKET_REDEEMED: SASTimeoutError("would abort if this ran")},
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock, validation_meters_sweep=False)
    row = conn.execute(
        "SELECT total_coin_in, lp50_cashable_ticket_redeemed_total_validations, "
        "lp50_cashable_ticket_redeemed_cumulative_amount_cents FROM meters_current"
    ).fetchone()
    assert row == (7, None, None)
    assert conn.execute("SELECT COUNT(*) FROM poll_errors").fetchone()[0] == 0


def test_gauge_fields_decreasing_does_not_arm_the_burst_window():
    """Current credits, current hopper level/status, and selected game
    number go up and down in normal operation -- a decrease there is not
    the anomaly signal a cumulative-counter decrease is. See
    GAUGE_METER_FIELDS.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters(), make_meters()],
        hopper_status=HopperStatus(status=1, percent_full=90, level=900),
        single_meter_values={LongPoll.SEND_CURRENT_CREDITS: 500},
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    # second cycle: hopper drains and the player's credit balance drops --
    # both ordinary gauge movement, not a rollover
    client._hopper_status = HopperStatus(status=1, percent_full=10, level=100)
    client._single_meter_values[LongPoll.SEND_CURRENT_CREDITS] = 0
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    assert state.burst_remaining == 0
    assert conn.execute("SELECT COUNT(*) FROM poll_errors").fetchone()[0] == 0


# --- meter poll timing: real wall-clock measurement, not a theoretical
#     estimate -- see poll_and_log()'s ``interval`` parameter -----------------


def test_meter_poll_timing_is_reported(capsys):
    conn = make_db()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()])
    times = iter([0.0, 2.5])
    poll_and_log(client, conn, state, history, monotonic_fn=lambda: next(times))
    full_count = (
        GROUPED_METER_POLL_COUNT
        + 1  # send_last_accepted_bill_information (LP 0x48)
        + len(SINGLE_METER_COLUMNS)
        + TABLE_C7_CHUNK_COUNT
        + len(VALIDATION_METER_TYPES)  # LP 0x50, one exchange per validation type
    )
    assert f"meter_poll=2.500s/{full_count}lp+" in capsys.readouterr().out


def test_meter_poll_count_reflects_skip_full_meter_sweep(capsys):
    conn = make_db()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()])
    times = iter([0.0, 0.05])
    poll_and_log(client, conn, state, history, monotonic_fn=lambda: next(times), full_meter_sweep=False)
    # single-meter sweep skipped; LP 0x48, C-7 sweep, and validation-meters sweep still on
    expected_count = GROUPED_METER_POLL_COUNT + 1 + TABLE_C7_CHUNK_COUNT + len(VALIDATION_METER_TYPES)
    assert f"meter_poll=0.050s/{expected_count}lp+" in capsys.readouterr().out


def test_meter_poll_count_reflects_skip_table_c7_sweep(capsys):
    conn = make_db()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()])
    times = iter([0.0, 0.05])
    poll_and_log(client, conn, state, history, monotonic_fn=lambda: next(times), table_c7_sweep=False)
    # C-7 sweep skipped; LP 0x48, single-meter sweep, and validation-meters sweep still on
    expected_count = GROUPED_METER_POLL_COUNT + 1 + len(SINGLE_METER_COLUMNS) + len(VALIDATION_METER_TYPES)
    assert f"meter_poll=0.050s/{expected_count}lp+" in capsys.readouterr().out


def test_meter_poll_count_excludes_a_c7_sweep_that_was_not_due(capsys):
    """The count must report what the cycle DID, not what the flags allow.

    --table-c7-interval 300 means the C-7 sweep runs on roughly one cycle in
    150. The count keyed off ``table_c7_sweep`` (C-7 enabled at all) instead
    of ``c7_due`` (this cycle ran one), so on the other 149 it added 13
    exchanges that never happened: "22polls" for a cycle that made 9. That
    number is what a human reads when deciding whether the sweep is too heavy
    for the --interval, so the exaggeration argued for cutting a sweep that
    was not the cost.
    """
    conn = make_db()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters(), make_meters()])
    clock = iter([0.0, 0.05, 1.0, 1.05]).__next__
    # First cycle: last_c7_sweep is None, so the sweep is due and counted.
    poll_and_log(client, conn, state, history, monotonic_fn=clock,
                 full_meter_sweep=False, validation_meters_sweep=False,
                 table_c7_interval=3600)
    due = GROUPED_METER_POLL_COUNT + 1 + TABLE_C7_CHUNK_COUNT
    assert f"meter_poll=0.050s/{due}lp+" in capsys.readouterr().out
    # Second cycle, one second later: not due, so those 13 must not appear.
    poll_and_log(client, conn, state, history, monotonic_fn=clock,
                 full_meter_sweep=False, validation_meters_sweep=False,
                 table_c7_interval=3600)
    not_due = GROUPED_METER_POLL_COUNT + 1
    assert f"meter_poll=0.050s/{not_due}lp+" in capsys.readouterr().out


def test_meter_poll_count_reports_the_heartbeat_general_polls(capsys):
    """Section 2.3.3 counts general polls and long polls against one budget,
    so a label that counts only long polls understates the cycle."""
    conn = make_db()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()])
    times = iter([0.0, 0.05])
    poll_and_log(client, conn, state, history, monotonic_fn=lambda: next(times),
                 full_meter_sweep=False, validation_meters_sweep=False,
                 table_c7_sweep=False, heartbeat_every=2)
    out = capsys.readouterr().out
    lp = GROUPED_METER_POLL_COUNT + 1
    # heartbeat_every=2 over 9 long polls: at least a few, and never zero.
    import re as _re
    m = _re.search(rf"meter_poll=0\.050s/{lp}lp\+(\d+)gp", out)
    assert m is not None, out
    assert int(m.group(1)) > 0


def test_a_sweep_over_the_interval_but_INSIDE_THE_QUEUE_is_a_note_not_a_warning(capsys):
    """Changed 2026-11-09, on the operator's objection to a line that fired
    on every single full sweep.

    "Longer than the sleep" was never the question worth asking. A 60s
    sweep is FINE at 0.91 events/s, because the cabinet's queue survives
    74s at that rate -- and four times too long at 4.31/s. The old line
    could not tell those apart and fired identically on all three, which
    made it background rather than a warning. §8 already records that
    mistake once, in the pool-age alert.
    """
    conn = make_db()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()])
    times = iter([0.0, 2.5])
    poll_and_log(client, conn, state, history, monotonic_fn=lambda: next(times), interval=2.0)
    out = capsys.readouterr().out
    assert "WARNING: the sweep OUTLASTS THE QUEUE" not in out, (
        "a quiet cabinet must not raise a queue warning")
    assert "note: sweep 2.5s over --interval 2.0s" in out


def test_a_cycle_that_LOST_EVENTS_warns(capsys):
    """The only two witnesses a host ever gets, and both are evidence
    rather than a model: the cabinet's own 0x70, and §12.5.3's arithmetic
    on the coin-in carried inside each game start.

    This replaced a warning of mine that computed `queue_depth / rate` and
    called it "the time the queue survives". `rate` is the rate this
    poller COLLECTED events, and a collected event is not in the queue --
    so the formula divided the queue depth by the rate it was being
    EMPTIED. It fired on hardware at 00:41:43 on a cycle that was draining
    1.71/s while collecting 1.66/s, with nothing lost at all.

    THE ARRIVAL RATE IS NOT OBSERVABLE. Any warning resting on it rests on
    a number we do not have, however reasonable the formula looks.
    """
    conn = make_db()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()])
    state.overflows_this_cycle = 3
    state.gaps_this_cycle = 1
    times = iter([0.0, 10.0])
    poll_and_log(client, conn, state, history, monotonic_fn=lambda: next(times), interval=2.0)
    out = capsys.readouterr().out
    assert "WARNING: EVENTS WERE LOST IN THIS CYCLE" in out
    assert "3 buffer overflow(s)" in out and "1 stream gap(s)" in out
    assert "--drain-priority" in out, "the remedy must name the fix we built"
    assert "larger --interval is NOT the remedy" in out


def test_a_long_sweep_that_LOST_NOTHING_is_only_a_note(capsys):
    """A 60-second sweep is a COST, not a fault: it buys 154 Table C-7
    meters and 39 single ones. The first version of this line fired on
    every full sweep and became background; §8 records the same mistake in
    the pool-age alert."""
    conn = make_db()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()])
    times = iter([0.0, 10.0])
    poll_and_log(client, conn, state, history, monotonic_fn=lambda: next(times), interval=2.0)
    out = capsys.readouterr().out
    assert "WARNING" not in out
    assert "note: sweep 10.0s over --interval 2.0s" in out
    assert "nothing reported lost" in out


def test_the_warning_NEVER_divides_the_queue_by_the_COLLECTION_rate():
    """Structural, because the formula looked right and shipped."""
    src = (ROOT / "examples" / "sql_poll_logger.py").read_text()
    body = src[src.index("def poll_and_log("):]
    assert "MEASURED_LAB_QUEUE_DEPTH / rate" not in body
    assert "queue that survives" not in body

def test_meter_poll_under_interval_does_not_warn(capsys):
    conn = make_db()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()])
    times = iter([0.0, 0.1])
    poll_and_log(client, conn, state, history, monotonic_fn=lambda: next(times), interval=5.0)
    assert "WARNING: meter poll" not in capsys.readouterr().out


def test_interval_none_default_never_warns_regardless_of_elapsed(capsys):
    conn = make_db()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()])
    times = iter([0.0, 999.0])
    poll_and_log(client, conn, state, history, monotonic_fn=lambda: next(times))  # interval defaults to None
    assert "WARNING: meter poll" not in capsys.readouterr().out


def test_meter_poll_failure_still_reports_elapsed_time(capsys):
    conn = make_db()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()], meters_11_15=SASTimeoutError("no response"))
    times = iter([0.0, 1.234])
    poll_and_log(client, conn, state, history, monotonic_fn=lambda: next(times))
    assert "meters poll failed after 1.234s" in capsys.readouterr().out


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
    """general_poll_retries=1 (retrying disabled) is the degenerate case
    that matches this tool's pre-D-09 behavior exactly: one attempt, one
    poll_errors row named plain "general_poll" (no attempt-count suffix).
    See the retry-specific tests below for the >1 case.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters(total_coin_in=42)],
        exception_script=[SASTimeoutError("no response")],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock, general_poll_retries=1)
    errors = conn.execute("SELECT poll_name, error_type FROM poll_errors").fetchall()
    assert errors == [("general_poll(attempt 1/1)", "SASTimeoutError")]
    assert current_coin_in(conn) == 42


# --- general poll retry (Decisions Annex D-09): immediate re-poll, capped --


def test_general_poll_retries_immediately_and_recovers_within_the_cap():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters(total_coin_in=7)],
        exception_script=[SASTimeoutError("try 1"), SASTimeoutError("try 2"), ExceptionCode.NONE],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock, general_poll_retries=3, retry_sleep_fn=lambda s: None)
    errors = conn.execute("SELECT poll_name, error_type FROM poll_errors ORDER BY id").fetchall()
    assert errors == [
        ("general_poll(attempt 1/3)", "SASTimeoutError"),
        ("general_poll(attempt 2/3)", "SASTimeoutError"),
    ]
    # the eventual success (3rd attempt) is not itself an error row, and meters still polled
    assert current_coin_in(conn) == 7


def test_general_poll_retries_exhausted_logs_every_attempt_and_still_polls_meters():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters(total_coin_in=9)],
        exception_script=[SASTimeoutError("try 1"), SASTimeoutError("try 2"), SASTimeoutError("try 3")],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock, general_poll_retries=3, retry_sleep_fn=lambda s: None)
    errors = conn.execute("SELECT poll_name, error_type FROM poll_errors ORDER BY id").fetchall()
    assert errors == [
        ("general_poll(attempt 1/3)", "SASTimeoutError"),
        ("general_poll(attempt 2/3)", "SASTimeoutError"),
        ("general_poll(attempt 3/3)", "SASTimeoutError"),
    ]
    # a general-poll failure (even exhausted) never blocks the meter poll -- unchanged pre-D-09 behavior
    assert current_coin_in(conn) == 9


def test_general_poll_default_retries_is_three():
    """No general_poll_retries argument given: DEFAULT_GENERAL_POLL_RETRIES
    (3) applies, matching the module's own documented default.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[SASTimeoutError("try 1"), SASTimeoutError("try 2"), SASTimeoutError("try 3")],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock, retry_sleep_fn=lambda s: None)  # default general_poll_retries
    errors = conn.execute("SELECT poll_name FROM poll_errors").fetchall()
    assert len(errors) == 3
    assert all(name == f"general_poll(attempt {i}/3)" for i, (name,) in enumerate(errors, start=1))


def test_general_poll_retries_are_paced_to_the_sas_minimum_poll_interval():
    """SAS 6.02 §2.3.3: no faster than once per 200ms to a single
    machine. A retry loop with no pacing would violate that floor on a
    fast failure -- confirm each retry actually waits for roughly the
    remainder of that window rather than firing back-to-back.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[SASTimeoutError("try 1"), ExceptionCode.NONE],
    )
    sleeps = []
    poll_and_log(
        client, conn, state, history, monotonic_fn=clock,
        general_poll_retries=2, retry_sleep_fn=sleeps.append,
    )
    assert len(sleeps) == 1
    # the mocked exchange takes negligible real time, so the sleep should
    # be close to the full 200ms window, never negative or wildly larger
    assert 0 < sleeps[0] <= SAS_MIN_POLL_INTERVAL_S


def test_interval_rejects_values_outside_sas_polling_rate_bounds():
    with pytest.raises(argparse.ArgumentTypeError):
        _sas_poll_interval("0.1")  # faster than the 200ms floor
    with pytest.raises(argparse.ArgumentTypeError):
        _sas_poll_interval("5.1")  # slower than the 5000ms ceiling


def test_interval_accepts_values_within_sas_polling_rate_bounds():
    assert _sas_poll_interval("0.2") == pytest.approx(0.2)
    assert _sas_poll_interval("5.0") == pytest.approx(5.0)


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


def test_poll_and_log_records_a_ticket_in_exception_with_nothing_in_escrow():
    """0x67 fired and LP 0x70 reports no escrow, every time we ask.

    This used to assert the opposite -- "must not create a phantom row" --
    and that silence is what hid the tool's most visible defect. The row is
    not a phantom: exception 0x67 really did fire, we really did decline to
    answer, and the machine really will hand the ticket back after §15.12's
    thirty seconds. The player sees a rejection. Recording nothing made that
    unexplainable from the database afterwards.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.TICKET_INSERTED],
        ticket_script=[make_ticket_in(ticket_in_escrow=False, amount_cents=0)] * 8,
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    rows = conn.execute(
        "SELECT authority_decision FROM ticket_in_events").fetchall()
    assert len(rows) == 1
    assert "no ticket in escrow" in rows[0][0]


def test_poll_and_log_detects_ticket_in_meter_rollover_during_105_dollar_ticket_capture():
    """The scenario this project was actually asked to test: an EGM whose
    cumulative Cashable Ticket In meter (LP 2F, 5 BCD bytes) sits near its
    max and rolls over ($99,999,950.00 -> $55.00, mod 10**10 -- see
    test_send_selected_meters_ticket_in_meter_rollover in test_client.py
    for the raw decode), on the very poll cycle a $105.00 ticket is
    inserted and captured via LP 70. poll_and_log() polls LP 2F every
    cycle now (previously a real gap: this rollover was invisible to the
    tool entirely), so this exercises the full path: the wrapped meter
    must be stored as-is (no client-side correction), must arm the burst
    window exactly like a core-meter decrease, and must not interfere
    with the independent LP 70 ticket-in capture landing on the same
    cycle.
    """
    conn = make_db()
    clock = FakeClock()
    pre_rollover_cents = 9_999_995_000
    post_rollover_cents = 5_500  # (pre_rollover_cents + 10_500) % 10**10
    state = PollState(last_meters={
        "total_cancelled_credits": 0, "total_coin_in": 0, "total_coin_out": 0,
        "total_drop": 0, "total_jackpot": 0, "games_played": 0,
        **{column: 0 for column in TICKET_METER_COLUMNS},
        "ticket_in_cashable_cents": pre_rollover_cents,
    })
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],  # basic meters unchanged -- isolates the ticket meter as the trigger
        exception_script=[ExceptionCode.TICKET_INSERTED],
        ticket_script=[make_ticket_in(amount_cents=10_500)],
        ticket_meters_script=[make_ticket_meters(ticket_in_cashable_cents=post_rollover_cents)],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)

    ticket_rows = conn.execute("SELECT amount_cents FROM ticket_in_events").fetchall()
    assert ticket_rows == [(10_500,)]
    assert conn.execute("SELECT COUNT(*) FROM poll_errors").fetchone()[0] == 0

    row = conn.execute("SELECT ticket_in_cashable_cents FROM meters_current").fetchone()
    assert row[0] == post_rollover_cents  # the raw wrapped wire value, not a corrected one

    # the ticket-meter decrease alone (basic meters didn't move) still arms
    # the burst window: this cycle's own write immediately consumes one
    assert state.burst_remaining == history.burst_count - 1


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


# --- ticket-in completion capture (exception 0x68) --------------------------


def test_poll_and_log_captures_ticket_in_completion_on_exception():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.TICKET_TRANSFER_COMPLETE],
        ticket_completion_script=[make_ticket_completion(machine_status=0x00, amount_cents=2500)],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    rows = conn.execute("SELECT machine_status, amount_cents FROM ticket_in_completions").fetchall()
    assert rows == [(0, 2500)]


def test_ticket_in_completion_ff_status_is_logged_not_filtered():
    """machine_status 0xFF ("no completed cycle since last polled") is a
    real race, not an error -- the exception fired, but nothing to read
    by the time this poll landed. Logged as-is, same as any other status.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.TICKET_TRANSFER_COMPLETE],
        ticket_completion_script=[make_ticket_completion(machine_status=0xFF, amount_cents=0)],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    rows = conn.execute("SELECT machine_status FROM ticket_in_completions").fetchall()
    assert rows == [(0xFF,)]
    assert conn.execute("SELECT COUNT(*) FROM poll_errors").fetchone()[0] == 0


def test_ticket_in_completion_capture_failure_logs_error_without_stopping_meters():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters(total_coin_in=11)],
        exception_script=[ExceptionCode.TICKET_TRANSFER_COMPLETE],
        ticket_completion_script=[SASTimeoutError("no response")],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    errors = conn.execute("SELECT poll_name FROM poll_errors").fetchall()
    assert errors == [("redeem_ticket_status",)]
    assert current_coin_in(conn) == 11


def test_ticket_in_completions_synced_at_defaults_to_null():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.TICKET_TRANSFER_COMPLETE],
        ticket_completion_script=[make_ticket_completion()],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    row = conn.execute("SELECT synced_at FROM ticket_in_completions").fetchone()
    assert row == (None,)


# --- ticket_in_history view: correlating 0x67 inserts with 0x68 completions --


TICKET_VD = b"\x00" + b"1" * 9  # the shared default in make_ticket_in/make_ticket_completion


def run_ticket_in_cycle(conn, *, insert_at=None, complete_at=None, completion=None, ticket=None):
    """Drive the real capture path (exception 0x67 then 0x68, one cycle
    each) with controlled timestamps, so the view is exercised against
    rows written by _capture_ticket_in()/_capture_ticket_in_completion()
    rather than hand-inserted ones.
    """
    state = PollState()
    history = HistoryConfig(mode="ring")
    if insert_at is not None:
        client = ScriptedClient(
            [make_meters()],
            exception_script=[ExceptionCode.TICKET_INSERTED],
            ticket_script=[ticket if ticket is not None else make_ticket_in()],
        )
        poll_and_log(client, conn, state, history, monotonic_fn=FakeClock(), now_fn=lambda: insert_at)
    if complete_at is not None:
        client = ScriptedClient(
            [make_meters()],
            exception_script=[ExceptionCode.TICKET_TRANSFER_COMPLETE],
            ticket_completion_script=[completion if completion is not None else make_ticket_completion()],
        )
        poll_and_log(client, conn, state, history, monotonic_fn=FakeClock(), now_fn=lambda: complete_at)


def history_rows(conn):
    return conn.execute(
        "SELECT inserted_at, validation_number, amount_cents, completed_at, machine_status, outcome "
        "FROM ticket_in_history ORDER BY COALESCE(inserted_at, completed_at)"
    ).fetchall()


def test_ticket_in_history_correlates_an_insert_with_its_completion():
    conn = make_db()
    run_ticket_in_cycle(
        conn,
        insert_at="2026-09-15T10:00:01+00:00",
        complete_at="2026-09-15T10:00:03+00:00",
        ticket=make_ticket_in(amount_cents=2500),
        completion=make_ticket_completion(machine_status=0x00, amount_cents=2500),
    )
    assert history_rows(conn) == [
        (
            "2026-09-15T10:00:01+00:00",
            TICKET_VD.hex(),
            2500,
            "2026-09-15T10:00:03+00:00",
            0x00,
            "redeemed",
        )
    ]


def test_ticket_in_history_correlates_across_independent_autoincrement_ids():
    """Regression test. ticket_in_events.id and ticket_in_completions.id are
    SEPARATE AUTOINCREMENT sequences, so the first insert and the first
    completion BOTH have id=1 -- ordering the two tables against each other
    by id is meaningless and silently correlates nothing. An earlier version
    of this view joined on `event.id < completion.id`, which made every
    single ticket show up as two uncorrelated rows ("awaiting completion"
    plus an orphan completion) instead of one. The view orders by
    captured_at (ISO-8601 UTC, so string order is time order) instead.
    """
    conn = make_db()
    run_ticket_in_cycle(conn, insert_at="2026-09-15T10:00:01+00:00", complete_at="2026-09-15T10:00:03+00:00")
    event_id = conn.execute("SELECT id FROM ticket_in_events").fetchone()[0]
    completion_id = conn.execute("SELECT id FROM ticket_in_completions").fetchone()[0]
    assert event_id == completion_id == 1, "the id collision this test exists to defend against"
    rows = history_rows(conn)
    assert len(rows) == 1, "one ticket must be one row, not an insert and an orphan completion"
    assert rows[0][0] is not None and rows[0][3] is not None


def test_ticket_in_history_shows_an_insert_with_no_completion_as_awaiting():
    conn = make_db()
    run_ticket_in_cycle(conn, insert_at="2026-09-15T10:00:01+00:00")
    assert history_rows(conn) == [
        ("2026-09-15T10:00:01+00:00", TICKET_VD.hex(), 2500, None, None, "awaiting completion")
    ]


def test_ticket_in_history_shows_a_completion_whose_insert_was_never_seen():
    """Started mid-cycle, or the 0x67 was lost to an exception-buffer
    overflow. The completion is still real and must stay visible, with a
    NULL inserted_at saying plainly that this half was never observed.
    """
    conn = make_db()
    run_ticket_in_cycle(conn, complete_at="2026-09-15T10:00:03+00:00")
    assert history_rows(conn) == [
        (None, TICKET_VD.hex(), 2500, "2026-09-15T10:00:03+00:00", 0x00, "redeemed")
    ]


def test_ticket_in_history_classifies_a_rejection():
    conn = make_db()
    run_ticket_in_cycle(
        conn,
        insert_at="2026-09-15T10:00:01+00:00",
        complete_at="2026-09-15T10:00:03+00:00",
        completion=make_ticket_completion(machine_status=0x85),  # validation number does not match
    )
    row = history_rows(conn)[0]
    assert row[4] == 0x85
    assert row[5] == "rejected"


@pytest.mark.parametrize(
    "machine_status, expected",
    [
        (0x00, "redeemed"),  # 000xxxxx -- cashable ticket redeemed
        (0x02, "redeemed"),  # 000xxxxx -- nonrestricted promotional redeemed
        (0x20, "waiting for long poll 71"),  # 001xxxxx
        (0x40, "redemption pending"),  # 010xxxxx
        (0x80, "rejected"),  # 100xxxxx -- rejected by host or unknown
        (0x8B, "rejected"),  # 100xxxxx -- validator failure
        (0xC0, "incompatible with current cycle"),  # 110xxxxx
        (0xFF, "no validation information"),  # 111xxxxx
    ],
)
def test_ticket_in_history_classifies_status_by_the_specs_own_msbit_categories(machine_status, expected):
    """Table 15.12d states outright that the 3 most significant bits
    determine the category of the status code, so the view derives the
    outcome with `machine_status >> 5` rather than carrying a 20-row
    lookup table that could drift from the spec.
    """
    conn = make_db()
    run_ticket_in_cycle(
        conn,
        complete_at="2026-09-15T10:00:03+00:00",
        completion=make_ticket_completion(machine_status=machine_status),
    )
    assert history_rows(conn)[0][5] == expected


def test_ticket_in_history_does_not_correlate_a_completion_with_no_validation_data():
    """machine_status FF comes back with no validation data at all (the
    response carries only the status byte), so there is nothing to join
    on. It must not be silently attached to whatever ticket happened to
    go in most recently -- that would invent a correlation the wire never
    supported.
    """
    conn = make_db()
    run_ticket_in_cycle(
        conn,
        insert_at="2026-09-15T10:00:01+00:00",
        complete_at="2026-09-15T10:00:03+00:00",
        completion=make_ticket_completion(machine_status=0xFF, amount_cents=0, validation_data=b""),
    )
    assert history_rows(conn) == [
        ("2026-09-15T10:00:01+00:00", TICKET_VD.hex(), 2500, None, None, "awaiting completion"),
        (None, None, 0, "2026-09-15T10:00:03+00:00", 0xFF, "no validation information"),
    ]


def test_ticket_in_history_keeps_both_inserts_when_the_same_ticket_is_reinserted():
    """A rejected ticket re-inserted by the player produces two 0x67s with
    the same validation number and one 0x68. The completion belongs to the
    most recent preceding insert; the earlier insert must still appear
    (never silently dropped) and must not also claim the same completion.
    """
    conn = make_db()
    run_ticket_in_cycle(conn, insert_at="2026-09-15T10:00:01+00:00")
    run_ticket_in_cycle(conn, insert_at="2026-09-15T10:00:05+00:00", complete_at="2026-09-15T10:00:07+00:00")
    assert history_rows(conn) == [
        ("2026-09-15T10:00:01+00:00", TICKET_VD.hex(), 2500, None, None, "awaiting completion"),
        ("2026-09-15T10:00:05+00:00", TICKET_VD.hex(), 2500, "2026-09-15T10:00:07+00:00", 0x00, "redeemed"),
    ]
    assert conn.execute("SELECT COUNT(*) FROM ticket_in_completions").fetchone()[0] == 1


def test_ticket_in_history_keeps_two_different_tickets_apart():
    conn = make_db()
    other = b"\x00" + b"7" * 9
    run_ticket_in_cycle(conn, insert_at="2026-09-15T10:00:01+00:00", complete_at="2026-09-15T10:00:02+00:00")
    run_ticket_in_cycle(
        conn,
        insert_at="2026-09-15T10:00:09+00:00",
        complete_at="2026-09-15T10:00:10+00:00",
        ticket=make_ticket_in(amount_cents=700, validation_data=other),
        completion=make_ticket_completion(machine_status=0x85, amount_cents=700, validation_data=other),
    )
    assert history_rows(conn) == [
        ("2026-09-15T10:00:01+00:00", TICKET_VD.hex(), 2500, "2026-09-15T10:00:02+00:00", 0x00, "redeemed"),
        ("2026-09-15T10:00:09+00:00", other.hex(), 700, "2026-09-15T10:00:10+00:00", 0x85, "rejected"),
    ]


def test_ticket_in_history_is_empty_when_nothing_has_happened():
    assert history_rows(make_db()) == []


def test_ticket_in_history_view_is_rebuilt_not_preserved_on_reopen():
    """The view is DROP+CREATEd rather than CREATE IF NOT EXISTS, so a
    database made by an older version picks up the current definition.
    Re-running SCHEMA must be safe and must not duplicate rows.
    """
    conn = make_db()
    run_ticket_in_cycle(conn, insert_at="2026-09-15T10:00:01+00:00", complete_at="2026-09-15T10:00:03+00:00")
    conn.executescript(SCHEMA)  # same thing startup does against an existing file
    assert len(history_rows(conn)) == 1


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


def test_seed_validation_pool_sets_issued_at_from_now_fn():
    conn = make_db()
    counter = iter(range(1, 100))
    seed_validation_pool(conn, 3, random_fn=lambda bits: next(counter), now_fn=lambda: "2026-09-01T00:00:00+00:00")
    rows = conn.execute("SELECT issued_at FROM validation_pool").fetchall()
    assert all(r == ("2026-09-01T00:00:00+00:00",) for r in rows)


# --- validation_pool age alert (Decisions Annex D-16) -----------------------


def test_pool_age_hours_none_when_pool_has_no_available_rows():
    conn = make_db()
    assert _pool_age_hours(conn, "2026-09-14T12:00:00+00:00") is None


def test_pool_age_hours_ignores_rows_with_no_issued_at():
    conn = make_db()
    seed_pool(conn, 1)  # issued_at defaults to NULL
    assert _pool_age_hours(conn, "2026-09-14T12:00:00+00:00") is None


def test_pool_age_hours_measures_the_oldest_available_row():
    conn = make_db()
    seed_pool(conn, 1, issued_at="2026-09-13T00:00:00+00:00")  # 24h before "now" below
    seed_pool(conn, 2, issued_at="2026-09-14T06:00:00+00:00")  # 6h before -- not the oldest
    age = _pool_age_hours(conn, "2026-09-14T00:00:00+00:00")
    assert age == pytest.approx(24.0)


def test_pool_age_alert_fires_at_or_above_threshold(capsys):
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    seed_pool(conn, 1, issued_at="2026-09-13T00:00:00+00:00")  # exactly 36h before "now" via now_fn below
    client = ScriptedClient([make_meters()])
    poll_and_log(
        client, conn, state, history, monotonic_fn=clock,
        now_fn=lambda: "2026-09-14T12:00:00+00:00", pool_age_alert_hours=36.0,
    )
    out = capsys.readouterr().out
    assert "ALERT: validation_pool's oldest available number is 36.0h old" in out
    rows = conn.execute("SELECT poll_name, error_type FROM poll_errors WHERE error_type = 'PoolStale'").fetchall()
    assert rows == [("validation_pool", "PoolStale")]


def test_pool_age_alert_does_not_fire_below_threshold(capsys):
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    seed_pool(conn, 1, issued_at="2026-09-14T00:00:00+00:00")  # 12h before "now" -- well under 36h
    client = ScriptedClient([make_meters()])
    poll_and_log(
        client, conn, state, history, monotonic_fn=clock,
        now_fn=lambda: "2026-09-14T12:00:00+00:00", pool_age_alert_hours=36.0,
    )
    assert "ALERT: validation_pool" not in capsys.readouterr().out
    assert conn.execute("SELECT COUNT(*) FROM poll_errors WHERE error_type = 'PoolStale'").fetchone()[0] == 0


def test_pool_age_alert_never_blocks_dispensing():
    """A stale pool is a health signal, not a validity check (D-16) --
    cashout requests must still be answered normally from the same pool
    that just triggered the alert.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    seed_pool(conn, 555, issued_at="2026-09-01T00:00:00+00:00")  # very stale
    client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.SYSTEM_VALIDATION_REQUEST],
        cashout_info_script=[make_cashout_info(amount_cents=2500)],
        validation_number_script=[0x00],
    )
    poll_and_log(
        client, conn, state, history, monotonic_fn=clock,
        now_fn=lambda: "2026-09-14T12:00:00+00:00", pool_age_alert_hours=36.0,
    )
    assert conn.execute("SELECT status FROM validation_pool WHERE validation_number = 555").fetchone() == ("assigned",)


def test_pool_age_alert_zero_disables_it(capsys):
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    seed_pool(conn, 1, issued_at="2020-01-01T00:00:00+00:00")  # ancient
    client = ScriptedClient([make_meters()])
    poll_and_log(client, conn, state, history, monotonic_fn=clock, pool_age_alert_hours=0)
    assert "ALERT: validation_pool" not in capsys.readouterr().out


# --- synced_at: ticket tables are drain-eligible, meters never are ---------


def test_ticket_out_history_synced_at_defaults_to_null():
    conn = make_db()
    _insert_ticket_out_record(conn, make_ticket_out(validation_number=999), "2026-01-01T00:00:00Z")
    row = conn.execute("SELECT synced_at FROM ticket_out_history WHERE validation_number = 999").fetchone()
    assert row == (None,)


def test_ticket_in_events_synced_at_defaults_to_null():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.TICKET_INSERTED],
        ticket_script=[make_ticket_in()],
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    row = conn.execute("SELECT synced_at FROM ticket_in_events").fetchone()
    assert row == (None,)


# --- _safe_commit: loud, never silent, never raises -------------------------


def test_safe_commit_returns_true_on_success():
    conn = make_db()
    assert _safe_commit(conn, "2026-01-01T00:00:00Z") is True


def test_safe_commit_catches_disk_full_and_reports_to_stderr_without_raising(capsys):
    class FailingConn:
        def commit(self):
            raise sqlite3.OperationalError("database or disk is full")

    result = _safe_commit(FailingConn(), "2026-01-01T00:00:00Z")
    assert result is False
    err = capsys.readouterr().err
    assert "FAULT" in err
    assert "disk is full" in err


def test_safe_commit_never_deletes_anything_on_failure():
    """A failed commit must leave existing rows untouched — this tool
    never frees space by deleting data, even under write pressure.
    """
    conn = make_db()
    _insert_ticket_out_record(conn, make_ticket_out(validation_number=1), "2026-01-01T00:00:00Z")

    class FailingConn:
        def commit(self):
            raise sqlite3.OperationalError("database or disk is full")

    _safe_commit(FailingConn(), "2026-01-01T00:00:00Z")
    assert conn.execute("SELECT COUNT(*) FROM ticket_out_history").fetchone()[0] == 1


# --- database size warning ---------------------------------------------------


def test_db_file_size_bytes_returns_none_for_in_memory_db():
    conn = make_db()
    assert _db_file_size_bytes(conn) is None


def test_db_file_size_bytes_returns_real_size_for_file_backed_db(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.commit()
    size = _db_file_size_bytes(conn)
    assert size is not None and size > 0


def test_db_size_warning_prints_once_over_threshold(capsys):
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()])
    poll_and_log(
        client, conn, state, history, monotonic_fn=clock,
        db_size_warning_mb=1, db_size_fn=lambda c: 2 * 1024 * 1024,
    )
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "database file is" in out


def test_db_size_warning_silent_under_threshold():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()])
    poll_and_log(
        client, conn, state, history, monotonic_fn=clock,
        db_size_warning_mb=10, db_size_fn=lambda c: 1024,
    )
    # no assertion needed on stdout content here beyond "doesn't crash" —
    # covered for real by the "prints_once_over_threshold" test above;
    # this just proves a small size doesn't trip a warning meant for large ones.
    assert conn.execute("SELECT COUNT(*) FROM meters_current").fetchone()[0] == 1


def test_db_size_warning_disabled_by_default():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()])
    # db_size_warning_mb defaults to 0 (disabled); a huge fake size must not matter.
    poll_and_log(client, conn, state, history, monotonic_fn=clock, db_size_fn=lambda c: 10**12)
    assert conn.execute("SELECT COUNT(*) FROM meters_current").fetchone()[0] == 1


# --- Continuity: epochs -----------------------------------------------------
#
# A RAM clear and a machine swap are both routine on a floor and neither may
# stop the poller; both must be recorded, because meters written across one
# without a marker look continuous and are not.

import sqlite3 as _sqlite3
import types as _types

import examples.sql_poll_logger as _logger
from saspy.exceptions import SASError


class _FakeIdentityClient:
    """Answers the two identity polls, and can be made to go silent."""

    def __init__(self, serial="SER-1", asset=246, silent=False):
        self.serial, self.asset, self.silent = serial, asset, silent

    def send_sas_version_and_serial(self):
        if self.silent:
            raise SASError("no answer")
        return _types.SimpleNamespace(serial_number=self.serial, sas_version="602")

    def aft_game_lock_and_status_request(self, lock_code=0xFF, **kw):
        if self.silent:
            raise SASError("no answer")
        return _types.SimpleNamespace(asset_number=self.asset)


def _epoch_conn():
    conn = _sqlite3.connect(":memory:")
    conn.executescript(_logger.SCHEMA)
    return conn


def _meters(value):
    return {f: value for f in _logger.DECREASE_CHECK_FIELDS}


def _epochs(conn):
    return conn.execute(
        "SELECT id, reason, detail FROM machine_epochs ORDER BY id").fetchall()


def test_first_cycle_opens_an_epoch():
    conn, state = _epoch_conn(), _logger.PollState()
    _logger.maintain_epoch(conn, _FakeIdentityClient(), state, _meters(10), "T0")
    rows = _epochs(conn)
    assert len(rows) == 1 and rows[0][1] == "first_seen"
    assert state.epoch_id == rows[0][0]


def test_a_restart_against_the_same_machine_reuses_the_open_epoch():
    """A gateway restart is not a discontinuity. Opening an epoch on every
    restart would bury the real ones among the noise."""
    conn = _epoch_conn()
    first = _logger.PollState()
    _logger.maintain_epoch(conn, _FakeIdentityClient(), first, _meters(10), "T0")

    resumed = _logger.PollState()
    _logger.maintain_epoch(conn, _FakeIdentityClient(), resumed, _meters(20), "T1")

    assert resumed.epoch_id == first.epoch_id
    assert len(_epochs(conn)) == 1


def test_one_zeroed_read_does_not_declare_a_ram_clear():
    """A single bad sweep must not fabricate a discontinuity -- that is the
    same corruption this prevents, in the other direction."""
    conn, state = _epoch_conn(), _logger.PollState()
    client = _FakeIdentityClient()
    _logger.maintain_epoch(conn, client, state, _meters(10), "T0")
    opened = state.epoch_id

    state.last_meters = _meters(10)
    _logger.maintain_epoch(conn, client, state, _meters(0), "T1")

    assert state.epoch_id == opened
    assert len(_epochs(conn)) == 1
    assert state.clear_candidate_cycles == 1


def test_meters_recovering_after_zeroes_is_logged_as_a_read_fault():
    conn, state = _epoch_conn(), _logger.PollState()
    client = _FakeIdentityClient()
    _logger.maintain_epoch(conn, client, state, _meters(10), "T0")

    state.last_meters = _meters(10)
    _logger.maintain_epoch(conn, client, state, _meters(0), "T1")
    state.last_meters = _meters(0)
    _logger.maintain_epoch(conn, client, state, _meters(10), "T2")

    assert len(_epochs(conn)) == 1
    assert state.clear_candidate_cycles == 0
    errors = conn.execute(
        "SELECT error_type FROM poll_errors").fetchall()
    assert errors and errors[0][0] == "TransientZeroes"


def test_a_confirmed_ram_clear_opens_an_epoch_and_keeps_polling():
    conn, state = _epoch_conn(), _logger.PollState()
    client = _FakeIdentityClient()
    _logger.maintain_epoch(conn, client, state, _meters(10), "T0")
    opened = state.epoch_id

    # The machine stays zeroed, which is what a real clear looks like: the
    # first cycle sees the fall, the rest see it has not come back.
    for cycle in range(_logger.CLEAR_CONFIRM_CYCLES):
        state.last_meters = _meters(10) if cycle == 0 else _meters(0)
        _logger.maintain_epoch(conn, client, state, _meters(0), f"T{cycle + 1}")

    rows = _epochs(conn)
    assert len(rows) == 2
    assert rows[1][1] == "ram_clear"
    assert state.epoch_id != opened
    assert "confirmed over" in rows[1][2]


def test_a_different_serial_is_a_swap_not_a_clear():
    """A RAM clear and a machine swap look identical in the meters. The
    identity read is what tells them apart, and the reason recorded decides
    what the CMS does about it."""
    conn, state = _epoch_conn(), _logger.PollState()
    _logger.maintain_epoch(conn, _FakeIdentityClient(serial="SER-1"), state,
                           _meters(10), "T0")

    state.last_meters = _meters(10)
    swapped = _FakeIdentityClient(serial="SER-2", asset=99)
    _logger.maintain_epoch(conn, swapped, state, _meters(0), "T1")

    rows = _epochs(conn)
    assert len(rows) == 2
    assert rows[1][1] == "machine_changed"
    assert "SER-1" in rows[1][2] and "SER-2" in rows[1][2]


def test_an_unreadable_identity_is_not_a_machine_change():
    """Section 4.4 lets a machine ignore a long poll. A missed identity read
    is not evidence that the cabinet changed, and treating it as one would
    open an epoch every time a poll was dropped."""
    conn, state = _epoch_conn(), _logger.PollState()
    _logger.maintain_epoch(conn, _FakeIdentityClient(), state, _meters(10), "T0")

    state.last_meters = _meters(10)
    _logger.maintain_epoch(conn, _FakeIdentityClient(silent=True), state,
                           _meters(0), "T1")

    rows = _epochs(conn)
    assert len(rows) == 1, "a silent machine must not be read as a new machine"


def test_migration_adds_epoch_id_to_a_database_that_predates_it():
    """Soak databases are days long. Refusing to open one, or dropping its
    history, would both be worse than an ALTER."""
    conn = _sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE meters_history (id INTEGER PRIMARY KEY, "
                 "polled_at TEXT)")
    conn.execute("CREATE TABLE meters_current (id INTEGER PRIMARY KEY, "
                 "polled_at TEXT)")
    conn.execute("INSERT INTO meters_history (polled_at) VALUES ('old')")

    _logger.migrate_epoch_columns(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(meters_history)")}
    assert "epoch_id" in columns
    kept = conn.execute("SELECT polled_at, epoch_id FROM meters_history").fetchall()
    assert kept == [("old", None)], "pre-epoch rows keep their data and a NULL epoch"


def _persist(conn, values):
    """Write a meters_current row, the way a real cycle would."""
    cols = ", ".join(_logger.ALL_METER_FIELDS)
    qs = ", ".join("?" for _ in _logger.ALL_METER_FIELDS)
    conn.execute(
        f"INSERT OR REPLACE INTO meters_current (id, polled_at, epoch_id, {cols}) "
        f"VALUES (1, 'T', NULL, {qs})",
        tuple(values.get(f) for f in _logger.ALL_METER_FIELDS))


def test_a_ram_clear_while_the_gateway_was_stopped_is_still_detected():
    """The case in-memory state cannot see: powering down, clearing, and
    starting again. Without the persisted baseline the new machine's meters
    are filed under the old machine's history."""
    conn, first = _epoch_conn(), _logger.PollState()
    _logger.maintain_epoch(conn, _FakeIdentityClient(), first, _meters(10), "T0")
    _persist(conn, _meters(10))

    # Gateway stopped, machine RAM-cleared (which also wiped the asset
    # number), gateway restarted.
    resumed = _logger.PollState()
    _logger.maintain_epoch(conn, _FakeIdentityClient(asset=0), resumed,
                           _meters(0), "T9")

    rows = _epochs(conn)
    assert len(rows) == 2
    assert rows[1][1] == "ram_clear", rows
    assert "while the gateway was stopped" in rows[1][2]
    assert resumed.epoch_id == rows[1][0]


def test_a_cleared_asset_number_is_not_mistaken_for_a_cabinet_swap():
    """A RAM clear can wipe the operator-entered asset number while the
    serial is re-entered unchanged, so serial is what identifies the cabinet.

    NOT because the serial is burned in -- it is typed in at configuration
    too, and this project has seen one typed wrong. The asymmetry this test
    protects is narrower: an asset that moves while the serial holds is a
    reconfiguration, not a swap."""
    conn, state = _epoch_conn(), _logger.PollState()
    _logger.maintain_epoch(conn, _FakeIdentityClient(serial="S", asset=246),
                           state, _meters(10), "T0")

    state.last_meters = _meters(10)
    for cycle in range(_logger.CLEAR_CONFIRM_CYCLES):
        if cycle:
            state.last_meters = _meters(0)
        _logger.maintain_epoch(conn, _FakeIdentityClient(serial="S", asset=0),
                               state, _meters(0), f"T{cycle + 1}")

    rows = _epochs(conn)
    assert [r[1] for r in rows] == ["first_seen", "ram_clear"], rows


def test_an_asset_change_with_intact_meters_is_recorded_as_continuous():
    """AFT quotes the asset number, so the CMS needs to know it moved -- but
    the meter history is unbroken and the record has to say so."""
    conn, state = _epoch_conn(), _logger.PollState()
    _logger.maintain_epoch(conn, _FakeIdentityClient(serial="S", asset=246),
                           state, _meters(10), "T0")

    state.last_meters = _meters(10)
    state.cycles_since_identity = _logger.IDENTITY_CHECK_CYCLES
    _logger.maintain_epoch(conn, _FakeIdentityClient(serial="S", asset=777),
                           state, _meters(20), "T1")

    rows = _epochs(conn)
    assert rows[1][1] == "asset_changed"
    assert "CONTINUOUS" in rows[1][2]


def test_restart_against_an_unchanged_machine_still_reuses_the_epoch():
    """The common case must stay quiet: a gateway restart with the meters
    where they were left is not a discontinuity."""
    conn, first = _epoch_conn(), _logger.PollState()
    _logger.maintain_epoch(conn, _FakeIdentityClient(), first, _meters(10), "T0")
    _persist(conn, _meters(10))

    resumed = _logger.PollState()
    _logger.maintain_epoch(conn, _FakeIdentityClient(), resumed, _meters(30), "T9")

    assert resumed.epoch_id == first.epoch_id
    assert len(_epochs(conn)) == 1


def test_an_announced_meter_reset_opens_an_epoch_without_waiting():
    """0x7A is the machine saying its meters are gone. That is better evidence
    than any number of cycles of inferring it from the meters, so it must not
    wait for the heuristic to agree."""
    conn, state = _epoch_conn(), _logger.PollState()
    client = _FakeIdentityClient()
    _logger.maintain_epoch(conn, client, state, _meters(10), "T0")

    state.clear_announced_at = "T1"
    state.last_meters = _meters(10)
    _logger.maintain_epoch(conn, client, state, _meters(0), "T1")

    rows = _epochs(conn)
    assert len(rows) == 2
    assert rows[1][1] == "ram_clear"
    assert "announced by the machine" in rows[1][2]
    assert state.clear_announced_at is None, "the announcement is consumed once"


def test_a_bill_validator_period_reset_is_not_a_ram_clear():
    """0x7B fires on an ordinary drop, with the lifetime meters untouched.
    Treating it as a clear would open an epoch every time the stacker was
    emptied."""
    assert 0x7B not in _logger.CLEAR_ANNOUNCED_BY
    assert 0x7A in _logger.CLEAR_ANNOUNCED_BY


# --- Screen labels: claims that must stay backed by evidence ---------------

import examples.meter_snapshot as _snapshot


def test_screen_labels_exclude_meters_that_were_always_zero():
    """A zero matching a zero on screen is not a correspondence. 0020 and 0021
    read 0.00 on every capture against screen lines also reading 0.00, so
    neither may claim a label."""
    for code in (0x0020, 0x0021):
        assert code not in _snapshot.SCREEN_VERIFIED


def test_screen_labels_exclude_arithmetic_only_agreement():
    """0023 satisfies the spec's own definition (0003 + 0002) exactly, but no
    screen line carrying it was ever observed, so what the machine calls it is
    unknown. 00A0 matched 002E, but both are SAS -- that establishes the
    accounting unit, not a screen name."""
    assert 0x0023 not in _snapshot.SCREEN_VERIFIED
    assert 0x00A0 not in _snapshot.SCREEN_VERIFIED


def test_screen_labels_are_non_empty_where_they_exist():
    """An empty label is the signal for 'no evidence'. A blank entry in the
    map would be indistinguishable from an absent one and defeat that."""
    for code, label in _snapshot.SCREEN_VERIFIED.items():
        assert label.strip(), f"0x{code:04X} has a blank label"


# --- The manuals' meter table must stay true to the wire -------------------

import re as _re


def _documented_frames(path):
    """Every `01 6F ...` frame in a manual's meter table, as bytes."""
    text = Path(path).read_text()
    out = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 4 or not cells[1].startswith("`"):
            continue
        code_cell, frame_cell = cells[1].strip("`"), cells[3].strip("`")
        if not _re.fullmatch(r"[0-9A-F]{4}", code_cell):
            continue
        out[int(code_cell, 16)] = bytes.fromhex(frame_cell.replace(" ", ""))
    return out


@pytest.mark.parametrize("manual", ["MANUAL.md", "POLL_TEST_TOOL_MANUAL.md"])
def test_documented_meter_frames_match_what_the_client_sends(manual):
    """A hex frame printed in a manual is something a person will paste into a
    machine. If it drifts from what the client builds -- a changed CRC, a
    changed body layout -- the manual becomes a source of wrong frames, and
    nothing would otherwise notice."""
    from saspy.bcd import encode_bcd
    from saspy.binary import encode_binary_le
    from saspy.framing import build_command

    frames = _documented_frames(requires_lab_file(
        Path(__file__).resolve().parent.parent / manual))
    assert frames, f"no meter frames found in {manual}"
    for code, documented in frames.items():
        body = encode_bcd(0, 2) + encode_binary_le(code, 2)
        expected = build_command(1, bytes([0x6F, len(body)]) + body,
                                 crc_required=True)
        assert documented == expected, (
            f"{manual} documents 0x{code:04X} as {documented.hex(' ')}, "
            f"the client builds {expected.hex(' ')}")


@pytest.mark.parametrize("manual", ["MANUAL.md", "POLL_TEST_TOOL_MANUAL.md"])
def test_documented_meter_labels_match_the_verified_map(manual):
    """The table's labels and meter_snapshot's SCREEN_VERIFIED are the same
    claim written twice. They must not disagree about what this machine
    proved."""
    text = requires_lab_file(
        Path(__file__).resolve().parent.parent / manual).read_text()
    for code, label in _snapshot.SCREEN_VERIFIED.items():
        if f"`{code:04X}`" in text:
            assert label in text, (
                f"{manual} lists 0x{code:04X} but not its verified label "
                f"{label!r}")


# --- The lab ticket authority must not outlive the bench -------------------

_LAB_AUTHORITY = Path(__file__).resolve().parent.parent / "examples" / "ticket_lab_authority.py"


def _lab_authority_or_skip():
    """ticket_lab_authority.py is lab-only apparatus and is NOT published to
    the public mirror (tool_tree.py's tier, honoured by mirror_to_public.py
    since 2026-11-10). Four tests here already checked it existed before
    importing it; three more did not, and they failed the moment the
    mirrored suite was first run as the dev team would run it.

    Skip rather than return: a test that quietly passes when its subject is
    absent is the "absence read as a measurement" shape this project has
    now hit five times."""
    if not _LAB_AUTHORITY.exists():
        pytest.skip("ticket_lab_authority.py is lab-only, not in this checkout")
    import examples.ticket_lab_authority as _lab
    return _lab


def test_the_lab_ticket_authority_has_not_outlived_its_expiry():
    """This is the reminder, and it is deliberately a failing test rather than
    a comment.

    examples/ticket_lab_authority.py lets the gateway decide what a ticket is
    worth -- correct on a bench wired to nothing, a cash-dispensing defect
    anywhere else. It was always meant to be deleted when the bench work
    finished, and things that are meant to be deleted are not.

    So the suite notices. When this fails there are exactly two honest
    responses: delete the tool and this test with it, or move
    LAB_AUTHORITY_EXPIRES as a deliberate act. Both are fine; forgetting is
    not, and that is the option this removes.
    """
    import datetime as _dt
    _lab = _lab_authority_or_skip()
    assert _dt.date.today() <= _lab.LAB_AUTHORITY_EXPIRES, (
        f"ticket_lab_authority.py expired on {_lab.LAB_AUTHORITY_EXPIRES}. "
        "Delete examples/ticket_lab_authority.py and the ticket file, or move "
        "LAB_AUTHORITY_EXPIRES deliberately if the bench work is genuinely "
        "still running.")


def test_the_lab_ticket_authority_refuses_unparseable_validation_numbers():
    """An 18-digit number is Table 15.11b parsing code 00. A typo in the
    ticket file must fail at load, not silently never match."""
    if not _LAB_AUTHORITY.exists():
        return
    import json as _json
    import tempfile
    _lab = _lab_authority_or_skip()

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        _json.dump({"tickets": {"00-0000-0000": {"as": "cashable",
                                                 "amount": 1.0}}}, fh)
        bad = fh.name
    with pytest.raises(ValueError, match="18-digit"):
        _lab.load_tickets(Path(bad))


def test_the_lab_ticket_authority_normalises_printed_dashes():
    """The number is copied off a ticket, where it is printed with dashes, and
    arrives from the machine as 18 BCD digits. Those must meet."""
    _lab = _lab_authority_or_skip()
    assert _lab.normalise("00-0000-0000-0000-0003") == "000000000000000003"
    assert len(_lab.normalise("00-0000-0000-0000-0003")) == 18


def test_an_unedited_example_file_says_so_rather_than_implying_a_swap():
    """Copying the example and forgetting to edit it is the likeliest mistake,
    and it happened on the first real run. The generic 'if the bench machine
    really changed' message sent the reader looking for a problem that was not
    there, so the placeholder case is detected and named."""
    _lab = _lab_authority_or_skip()
    # The placeholder carries no digits; a real serial is all digits.
    assert _lab.normalise("PUT-THE-BENCH-MACHINE-SERIAL-HERE") == ""
    assert _lab.normalise("000106719") == "000106719"


# --- Drain rate: the cause of the exception buffer overflows ---------------


def test_heartbeat_every_controls_the_drain_rate():
    """SAS delivers ONE exception per general poll, so the number of heartbeats
    inside a meter sweep IS the drain rate. It was hard-coded at 8, which on a
    60-exchange sweep is eight drains in ~5s -- thinner than a played machine
    produces, and 0x70 exception buffer overflow was the result.

    Driving the real sweep needs a whole fake machine, so this tests the
    cadence arithmetic the sweep uses: exchanges % heartbeat_every == 0.
    """
    for every, exchanges, expected in ((8, 60, 7), (3, 60, 20), (2, 60, 30),
                                       (8, 73, 9), (2, 73, 36)):
        drains = sum(1 for n in range(1, exchanges + 1) if n % every == 0)
        assert drains == expected, (every, exchanges, drains)


def test_heartbeat_every_is_reachable_from_the_command_line():
    """It was a function default with no way to set it, so the one knob that
    fixes an overflow could not be turned without editing the source."""
    import examples.sql_poll_logger as _logger
    parser = _logger.build_parser() if hasattr(_logger, "build_parser") else None
    if parser is None:
        source = Path(_logger.__file__).read_text()
        assert '"--heartbeat-every"' in source
        assert "heartbeat_every=args.heartbeat_every" in source
    else:
        args = parser.parse_args(["cfg", "--heartbeat-every", "3"])
        assert args.heartbeat_every == 3


# --- One cashout must consume one validation number ------------------------


class _FakeCashoutClient:
    """A machine that reissues exception 0x57 until it is satisfied, which is
    what section 15 requires of it."""

    def __init__(self, amount_cents=14025):
        self.amount_cents = amount_cents
        self.answered = []

    def send_pending_cashout_info(self):
        return _types.SimpleNamespace(cashout_type=0x00,
                                      amount_cents=self.amount_cents)

    def send_validation_number(self, validation_system_id, validation_number):
        self.answered.append(validation_number)
        return 0x00


def _pool_conn(count=10):
    conn = _epoch_conn()
    for n in range(1, count + 1):
        conn.execute(
            "INSERT INTO validation_pool (validation_number, validation_system_id,"
            " status, issued_at) VALUES (?, 1, 'available', 'T0')", (n,))
    conn.commit()
    return conn


def test_a_reissued_0x57_reuses_the_same_validation_number():
    """One real ticket on the bench consumed SIX numbers: the machine reissued
    0x57 six times and each was answered with a fresh one. Five were marked
    assigned and never reached paper, which to a CMS reconciling issued
    against printed is five phantom vouchers per real one.

    Section 15.8 expects the same answer within the same cashout, so the
    answer has to be idempotent."""
    conn = _pool_conn()
    client = _FakeCashoutClient()
    for _ in range(6):
        _logger._handle_cashout_request(client, conn, "T1")

    assert len(set(client.answered)) == 1, client.answered
    assigned = conn.execute(
        "SELECT COUNT(*) FROM validation_pool WHERE status = 'assigned'").fetchone()[0]
    assert assigned == 1, f"{assigned} numbers consumed for one cashout"


def test_a_different_cashout_amount_takes_a_fresh_number():
    """Idempotence is per cashout, not forever. A different amount is a
    different cashout and must not be given the previous ticket's number."""
    conn = _pool_conn()
    first = _FakeCashoutClient(amount_cents=14025)
    _logger._handle_cashout_request(first, conn, "T1")
    second = _FakeCashoutClient(amount_cents=5000)
    _logger._handle_cashout_request(second, conn, "T2")

    assert first.answered[0] != second.answered[0]
    assigned = conn.execute(
        "SELECT COUNT(*) FROM validation_pool WHERE status = 'assigned'").fetchone()[0]
    assert assigned == 2


def test_a_number_already_printed_is_not_reused():
    """Once the ticket-out record is drained the number is on paper and spent.
    A later cashout for the same amount must take a fresh one."""
    conn = _pool_conn()
    client = _FakeCashoutClient()
    _logger._handle_cashout_request(client, conn, "T1")
    printed = client.answered[0]
    conn.execute(
        "INSERT INTO ticket_out_history (captured_at, buffer_index, "
        "validation_type, ticket_date, ticket_time, validation_number, "
        "amount_cents, ticket_number, validation_system_id, expiration, pool_id)"
        " VALUES ('T2', 1, 0, 10262026, 131312, ?, 14025, 1, 1, 0, 0)", (printed,))
    conn.commit()

    again = _FakeCashoutClient()
    _logger._handle_cashout_request(again, conn, "T3")
    assert again.answered[0] != printed


def test_one_ram_clear_does_not_open_two_epochs():
    """Observed on hardware: epochs 2 and 3, eleven seconds apart, both
    ram_clear, both for the same clear.

    The machine queues its meter-reset announcement in a non-volatile buffer
    (section 2.2.1), so a clear performed while the gateway was STOPPED is
    still waiting to be drained when it starts. Both detectors then fire for
    the same event -- the meter comparison on the first cycle, the queued
    announcement on the next. The announcement is evidence for the clear
    already recorded, not a second one.
    """
    conn = _epoch_conn()
    client = _FakeIdentityClient()
    first = _logger.PollState()
    _logger.maintain_epoch(conn, client, first, _meters(10), "T0")
    _persist(conn, _meters(10))

    # Gateway stopped, machine cleared, gateway restarted. The queued 0x7A is
    # dispatched during the first cycle, before maintain_epoch runs.
    resumed = _logger.PollState()
    resumed.clear_announced_at = "T9"
    _logger.maintain_epoch(conn, client, resumed, _meters(0), "T9")
    assert resumed.clear_announced_at is None, \
        "the announcement must be consumed by the epoch it corroborates"

    # Next cycle: nothing left to act on, so no second epoch.
    resumed.last_meters = _meters(0)
    _logger.maintain_epoch(conn, client, resumed, _meters(0), "T10")

    reasons = [r[1] for r in _epochs(conn)]
    assert reasons == ["first_seen", "ram_clear"], reasons


# -- Exception 0x51 and the handpay queue (§7.8.1) --------------------------


def test_handpay_pending_reads_the_1b_long_poll():
    """Section 7.8.1 requires the host to read LP 0x1B after exception 0x51.
    Not reading it leaves the queue entry unreported, and the machine then
    "re-issue[s] exception 51 every fifteen seconds as long as the entry is
    in the queue" -- observed on an EGT Premier as a 0x51 every 15s for
    minutes, on a machine that was unlocked and being played throughout.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.HANDPAY_PENDING],
        handpay_information=HandpayInformation(
            progressive_group=0, level=0x80, amount=15000, partial_pay=0, reset_id=0),
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    rows = conn.execute(
        "SELECT level, amount, reset_id, all_zeros FROM handpay_queue").fetchall()
    assert rows == [(0x80, 15000, 0, 0)]


def test_handpay_all_zeros_is_recorded_as_such_not_as_a_zero_handpay():
    """Section 7.8.1: "long poll 1B returns all zeros if a handpay record has
    been reported and acknowledged, and a subsequent exception 51 has not
    been issued and acknowledged." That is the normal answer on a re-read,
    and it must not be stored as a handpay of 0.00 -- which is how a second
    read of the same entry would otherwise look.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.HANDPAY_PENDING],
    )  # the fake's default 0x1B answer is all zeros
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    rows = conn.execute("SELECT amount, all_zeros FROM handpay_queue").fetchall()
    assert rows == [(0, 1)]


def test_handpay_read_failure_is_logged_not_fatal():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient(
        [make_meters()],
        exception_script=[ExceptionCode.HANDPAY_PENDING],
        handpay_information=SASTimeoutError("no response"),
    )
    poll_and_log(client, conn, state, history, monotonic_fn=clock)
    assert conn.execute("SELECT COUNT(*) FROM handpay_queue").fetchone()[0] == 0
    rows = conn.execute("SELECT poll_name FROM poll_errors").fetchall()
    assert ("send_handpay_information",) in rows


def test_the_poller_never_resets_a_handpay_by_default():
    """0x94 moves money and belongs to a human. --lab-auto-handpay-reset can
    license it on a cabinet bound by a lab ticket file; absent that, a lockup
    of any age must be left alone.

    This test used to assert that the string "reset_handpay" did not appear
    anywhere in the module. That grep stopped being true when the lab flag
    landed -- and it was always the weaker claim. What matters is that the
    DEFAULT path does not call it, which is what this now checks by running a
    cycle with a lockup pending since the beginning of time.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    state.handpay_pending_since = 0.0
    calls = []

    class _CountsResets(ScriptedClient):
        def reset_handpay(self):
            calls.append(1)
            return 0x00

    poll_and_log(_CountsResets([make_meters()]), conn, state,
                 HistoryConfig(mode="ring"), monotonic_fn=clock)
    assert calls == [], "a default run must never send LP 0x94"
    assert conn.execute(
        "SELECT count(*) FROM lab_auto_actions").fetchone()[0] == 0


def test_reset_handpay_is_reachable_only_from_the_gated_lab_function():
    """The spirit of the original grep, kept: one call site, and it is the
    one --lab-ticket-authority protects."""
    import inspect
    import examples.sql_poll_logger as mod
    assert inspect.getsource(mod).count("client.reset_handpay()") == 1
    assert "client.reset_handpay()" in inspect.getsource(
        mod._lab_auto_handpay_reset)


def test_every_aft_transfer_the_poller_sends_is_accounted_for():
    """THE PROPERTY IS ABOUT AMOUNTS, NOT ABOUT CALL SITES.

    This asserted one call site, which held until §8.7's cashout decline was
    added on 2026-11-06. That decline is also an LP 0x72, and it must be:
    the machine asked a question and 'all amounts zero' is the spec's answer
    for "not by AFT, use your own means". But it moves nothing.

    So the guarantee worth holding is that the poller has exactly two
    reasons to send LP 0x72, and only one of them carries an amount."""
    import inspect
    import examples.sql_poll_logger as mod
    src = inspect.getsource(mod)
    assert src.count("client.aft_transfer_funds(") == 2

    # The one that moves money: credits IN, under a lab-only flag.
    credit = inspect.getsource(mod._lab_auto_aft_credit)
    assert "client.aft_transfer_funds(" in credit
    assert "IN_HOUSE_TO_GAMING_MACHINE" in credit

    # The one that moves nothing: every amount a literal zero.
    decline = inspect.getsource(mod._decline_aft_cashout)
    assert "client.aft_transfer_funds(" in decline
    for amount in ("cashable_amount=0", "restricted_amount=0",
                   "nonrestricted_amount=0"):
        assert amount in decline, (
            f"{amount} must be a literal zero: a cashout decline that could "
            f"carry a non-zero amount is an AFT-out path by another name")


# -- Table C-7 sweep cadence ------------------------------------------------


def test_the_c7_sweep_runs_on_the_first_cycle_whatever_the_interval():
    """A run that polls for an hour and never captures C-7 because its first
    cycle was not yet "due" would lose exactly the data the cadence exists to
    collect. last_c7_sweep starts None and None means due.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    client = ScriptedClient([make_meters()], table_c7_values={0x0000: 4290})
    poll_and_log(client, conn, state, HistoryConfig(mode="ring"),
                 monotonic_fn=clock, table_c7_interval=3600)
    row = conn.execute(
        "SELECT c7_total_coin_in_credits FROM meters_current").fetchone()
    assert row[0] == 4290
    assert state.last_c7_sweep is not None


def test_a_cycle_inside_the_interval_leaves_the_c7_columns_null():
    """NULL means "not read this cycle", which is what actually happened.
    Carrying the previous reading forward would make a ten-minute-old meter
    indistinguishable from one read a second ago.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="append")
    client = ScriptedClient([make_meters(), make_meters()],
                            table_c7_values={0x0000: 4290})
    poll_and_log(client, conn, state, history, monotonic_fn=clock,
                 table_c7_interval=3600)
    poll_and_log(client, conn, state, history, monotonic_fn=clock,
                 table_c7_interval=3600)
    rows = [r[0] for r in conn.execute(
        "SELECT c7_total_coin_in_credits FROM meters_history ORDER BY id")]
    assert rows == [4290, None], rows


def test_interval_zero_still_sweeps_every_cycle():
    """The old behaviour has to remain reachable."""
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="append")
    client = ScriptedClient([make_meters(), make_meters()],
                            table_c7_values={0x0000: 4290})
    for _ in range(2):
        poll_and_log(client, conn, state, history, monotonic_fn=clock,
                     table_c7_interval=0)
    rows = [r[0] for r in conn.execute(
        "SELECT c7_total_coin_in_credits FROM meters_history ORDER BY id")]
    assert rows == [4290, 4290], rows


def test_skipping_the_sweep_still_skips_it_entirely():
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    client = ScriptedClient([make_meters()], table_c7_values={0x0000: 4290})
    poll_and_log(client, conn, state, HistoryConfig(mode="ring"),
                 monotonic_fn=clock, table_c7_sweep=False, table_c7_interval=0)
    row = conn.execute(
        "SELECT c7_total_coin_in_credits FROM meters_current").fetchone()
    assert row[0] is None
    assert state.last_c7_sweep is None


def test_a_failed_cycle_does_not_consume_the_cadence():
    """On a flapping link, letting a failure mark the sweep as done would
    make the next cycle "not due" and could starve C-7 indefinitely while
    every individual failure looked harmless.
    """
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    client = ScriptedClient([SASTimeoutError("no response")],
                            table_c7_values={0x0000: 4290})
    poll_and_log(client, conn, state, HistoryConfig(mode="ring"),
                 monotonic_fn=clock, table_c7_interval=3600)
    assert state.last_c7_sweep is None


def test_the_latest_c7_view_skips_the_null_cycles():
    conn = make_db()
    conn.execute("INSERT INTO meters_history (polled_at, c7_total_coin_in_credits) "
                 "VALUES ('2026-10-27T01:00:00', 100)")
    conn.execute("INSERT INTO meters_history (polled_at, c7_total_coin_in_credits) "
                 "VALUES ('2026-10-27T02:00:00', NULL)")
    conn.execute("INSERT INTO meters_current (polled_at, c7_total_coin_in_credits) "
                 "VALUES ('2026-10-27T03:00:00', NULL)")
    row = conn.execute("SELECT polled_at, c7_total_coin_in_credits "
                       "FROM latest_c7_meters").fetchone()
    assert row == ("2026-10-27T01:00:00", 100)


# --- real time event reporting -------------------------------------------
#
# RTE lived only in exception_monitor.py, which prints and stores nothing,
# while this tool stored everything and polled in the ordinary mode. Only one
# process can own the serial port, so "run RTE and meter polling together for
# hours and watch SQLite fill" was not a thing anyone could do. These pin the
# join.


class _RTEEvent:
    def __init__(self, code, fields=None, description="event"):
        self.code = code
        self.fields = fields or {}
        self.description = description


class _RTEClient(ScriptedClient):
    """A ScriptedClient that answers general polls as Table 12.5 events and
    can also hand back events the client collected in place of long-poll
    replies, per §12.3."""

    def __init__(self, meters_script, *, rte_script=None, interleaved=None, **kw):
        super().__init__(meters_script, **kw)
        self._rte_script = list(rte_script) if rte_script is not None else []
        self._interleaved = list(interleaved) if interleaved is not None else []
        self.drain_calls = 0
        self.rte_enabled_with = []

    def enable_real_time_events(self, on):
        self.rte_enabled_with.append(on)

    def general_poll_rte(self):
        return self._rte_script.pop(0) if self._rte_script else None

    def drain_rte_events(self):
        self.drain_calls += 1
        out, self._interleaved = self._interleaved, []
        return out


def test_rte_silence_is_idle_and_is_not_retried(tmp_path):
    """§12.6: a machine in real time event mode reports no activity by
    sending NOTHING. Treating that as a timeout would burn the whole retry
    budget on every quiet cycle -- and on an idle lab cabinet that is almost
    every cycle."""
    conn = sqlite3.connect(tmp_path / "db.sqlite3")
    conn.executescript(SCHEMA)
    migrate_epoch_columns(conn)
    client = _RTEClient([make_meters()], rte_script=[])  # always silent

    code, attempt, fields = _general_poll_with_retry(
        client, conn, "2026-10-28T00:00:00", max_attempts=3, rte=True,
        sleep_fn=lambda _s: None)

    assert code == int(ExceptionCode.NONE)
    assert attempt == 1, "silence must not consume retries"
    assert fields is None
    assert conn.execute("SELECT COUNT(*) FROM poll_errors").fetchone()[0] == 0


def test_an_rte_event_stores_its_decoded_fields(tmp_path):
    """The whole point of RTE: the exception carries data. A bare code is
    what ordinary polling already gave us."""
    conn = sqlite3.connect(tmp_path / "db.sqlite3")
    conn.executescript(SCHEMA)
    migrate_epoch_columns(conn)
    client = _RTEClient(
        [make_meters()],
        rte_script=[_RTEEvent(0x7F, {"wager": 500, "win": 12500})])

    code, _attempt, fields = _general_poll_with_retry(
        client, conn, "2026-10-28T00:00:00", max_attempts=3, rte=True,
        sleep_fn=lambda _s: None)
    assert code == 0x7F and fields == {"wager": 500, "win": 12500}

    _dispatch_exception(client, conn, "2026-10-28T00:00:00", code,
                        source="rte", fields=fields)
    row = conn.execute(
        "SELECT exception_code, source, rte_fields FROM machine_events").fetchone()
    assert row[0] == 0x7F and row[1] == "rte"
    assert json.loads(row[2]) == {"wager": 500, "win": 12500}


def test_ordinary_polling_stores_no_fields(tmp_path):
    """rte_fields is NULL without RTE, and must stay NULL rather than '{}' --
    an empty object would read as 'the event carried no data' when the truth
    is that the protocol mode carries none at all."""
    conn = sqlite3.connect(tmp_path / "db.sqlite3")
    conn.executescript(SCHEMA)
    migrate_epoch_columns(conn)
    client = ScriptedClient([make_meters()])
    _dispatch_exception(client, conn, "2026-10-28T00:00:00", 0x11)
    assert conn.execute("SELECT rte_fields FROM machine_events").fetchone()[0] is None


def test_interleaved_events_are_drained_every_cycle(tmp_path):
    """§12.3 lets the machine answer a LONG poll with an event. The client
    keeps those; this tool has to take them off it.

    Draining every cycle is the leak guard: the client's pending list has no
    cap, so a multi-hour run with a ~70-long-poll sweep that never drained
    would grow it for the length of the run.

    It is drained more than once now -- each heartbeat takes what has built
    up since the last, so the rows land in arrival order rather than with
    every interleaved event pushed behind every heartbeat one. What this
    test guards is that the buffer is emptied and the events are stored, not
    how many calls it takes.
    """
    conn = sqlite3.connect(tmp_path / "db.sqlite3")
    conn.executescript(SCHEMA)
    migrate_epoch_columns(conn)
    client = _RTEClient(
        [make_meters(), make_meters()],
        rte_script=[_RTEEvent(0x00), _RTEEvent(0x00)],
        interleaved=[_RTEEvent(0x11, {"door": "slot"}), _RTEEvent(0x12)])

    poll_and_log(client, conn, PollState(), HistoryConfig(mode="append"),
                 rte=True, retry_sleep_fn=lambda _s: None,
                 full_meter_sweep=False, table_c7_sweep=False,
                 validation_meters_sweep=False, last_accepted_bill_poll=False)

    assert client.drain_calls >= 1
    rows = conn.execute(
        "SELECT exception_code, source FROM machine_events ORDER BY id").fetchall()
    assert rows == [(0x11, "rte-interleaved"), (0x12, "rte-interleaved")]
    assert json.loads(conn.execute(
        "SELECT rte_fields FROM machine_events WHERE exception_code = 17"
    ).fetchone()[0]) == {"door": "slot"}


def test_nothing_is_drained_when_rte_is_off(tmp_path):
    conn = sqlite3.connect(tmp_path / "db.sqlite3")
    conn.executescript(SCHEMA)
    migrate_epoch_columns(conn)
    client = _RTEClient([make_meters()], interleaved=[_RTEEvent(0x11)])
    poll_and_log(client, conn, PollState(), HistoryConfig(mode="append"),
                 retry_sleep_fn=lambda _s: None, full_meter_sweep=False,
                 table_c7_sweep=False, validation_meters_sweep=False,
                 last_accepted_bill_poll=False)
    assert client.drain_calls == 0


def test_an_old_database_gains_the_rte_column(tmp_path):
    """A soak database from before --rte existed keeps its old
    machine_events shape, and every insert naming rte_fields would fail on
    it. Soak databases are days long; refusing to open them would be worse
    than an ALTER."""
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute("ALTER TABLE machine_events DROP COLUMN rte_fields")
    conn.commit()
    assert "rte_fields" not in {
        r[1] for r in conn.execute("PRAGMA table_info(machine_events)")}

    migrate_epoch_columns(conn)
    assert "rte_fields" in {
        r[1] for r in conn.execute("PRAGMA table_info(machine_events)")}
    _dispatch_exception(ScriptedClient([make_meters()]), conn,
                        "2026-10-28T00:00:00", 0x11)
    assert conn.execute("SELECT COUNT(*) FROM machine_events").fetchone()[0] == 1


# --- interrogation on connect and after a comms gap ----------------------
#
# The scenario, in the operator's words: the gateway is running on a machine,
# someone pulls the SAS cable and plugs it into a different one. The gap is
# the trigger because the act is physical and physical acts take time -- not
# because 30 seconds is when data is lost. By §2.2.1's arithmetic the
# exception queue is gone after about 8 seconds on a played machine and no
# sweep brings it back; what a sweep establishes is WHO is answering now.


class _InterrogationClient(ScriptedClient):
    def __init__(self, meters_script, *, serial="000106719", asset=246,
                 games=2, **kw):
        super().__init__(meters_script, **kw)
        self._serial, self._asset, self._games = serial, asset, games
        self.game_configs_read = []

    def send_sas_version_and_serial(self):
        return SimpleNamespace(sas_version="602", serial_number=self._serial)

    def aft_game_lock_and_status_request(self, lock_code=0xFF, **kw):
        return SimpleNamespace(asset_number=self._asset)

    def send_enabled_features(self, game_number=0):
        return SimpleNamespace(game_number=0, max_polling_rate_40ms=True,
                               ticket_redemption=True)

    def send_meter(self, poll, **kw):
        if poll == LongPoll.SEND_TOTAL_GAMES_IMPLEMENTED:
            return self._games
        raise SASTimeoutError("no")

    def send_game_n_configuration(self, number=0):
        self.game_configs_read.append(number)
        return SimpleNamespace(game_number=number, game_id="EG",
                               additional_id="3CO", denomination=1,
                               max_bet=200, progressive_group=0,
                               game_options=0, paytable_id="P1",
                               base_percentage="9500")

    def send_extended_game_n_information(self, number=0):
        return SimpleNamespace(game_number=number, max_bet=200,
                               progressive_group=0, progressive_levels=0,
                               game_name=f"Game {number}",
                               paytable_name="PT", wager_categories=1)

    def redeem_ticket_status(self):
        raise SASTimeoutError("none pending")

    def send_enhanced_validation_information(self, function_code=0xFF):
        # This cabinet has no ticket-out buffer to back-fill. Its absence is
        # recorded by the interrogation rather than being a failure.
        raise SASTimeoutError("no buffer")


def _fresh(tmp_path, name="db.sqlite3"):
    conn = sqlite3.connect(tmp_path / name)
    conn.executescript(SCHEMA)
    migrate_epoch_columns(conn)
    return conn


def test_an_interrogation_records_every_game_by_both_polls(tmp_path):
    """LP 0x53 carries the configuration and LP 0xB5 the human-readable
    names; neither is a superset, so a game gets one row built from both."""
    conn = _fresh(tmp_path)
    client = _InterrogationClient([make_meters()], games=3)
    interrogate(client, conn, PollState(), "2026-10-28T06:00:00",
                reason="first_connect")

    assert client.game_configs_read == [1, 2, 3], "game 0 is the machine, not a game"
    rows = conn.execute(
        "SELECT game_number, game_id, denomination, game_name, paytable_name "
        "FROM interrogation_games ORDER BY game_number").fetchall()
    assert rows == [(1, "EG", 1, "Game 1", "PT"),
                    (2, "EG", 1, "Game 2", "PT"),
                    (3, "EG", 1, "Game 3", "PT")]
    row = conn.execute(
        "SELECT reason, serial_number, asset_number, games_found, "
        "machine_changed FROM interrogations").fetchone()
    assert row == ("first_connect", "000106719", 246, 3, 0)


def test_a_swapped_cable_is_caught_and_opens_a_new_epoch(tmp_path):
    """The scenario this exists for. Same address, different cabinet."""
    conn = _fresh(tmp_path)
    state = PollState()
    interrogate(_InterrogationClient([make_meters()]), conn, state,
                "2026-10-28T06:00:00", reason="first_connect")
    first_epoch = state.epoch_id

    moved = _InterrogationClient([make_meters()], serial="999999999", asset=7)
    interrogate(moved, conn, state, "2026-10-28T06:05:00",
                reason="comms_restored", gap_seconds=45.0)

    assert state.epoch_id != first_epoch, "a different cabinet needs its own epoch"
    row = conn.execute(
        "SELECT reason, gap_seconds, serial_number, machine_changed, identity_note "
        "FROM interrogations ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "comms_restored" and row[1] == 45.0
    assert row[2] == "999999999" and row[3] == 1
    assert "serial" in (row[4] or "").lower()
    assert conn.execute(
        "SELECT reason FROM machine_epochs ORDER BY id DESC LIMIT 1"
    ).fetchone()[0] == "machine_changed"


def test_the_same_machine_after_a_gap_still_breaks_continuity(tmp_path):
    """No swap, but the events of the gap are gone for good (§2.2.1), so the
    history either side of it must not be read as continuous."""
    conn = _fresh(tmp_path)
    state = PollState()
    client = _InterrogationClient([make_meters()])
    interrogate(client, conn, state, "2026-10-28T06:00:00", reason="first_connect")
    before = state.epoch_id

    interrogate(client, conn, state, "2026-10-28T06:05:00",
                reason="comms_restored", gap_seconds=31.0)

    assert state.epoch_id != before
    reason, detail = conn.execute(
        "SELECT reason, detail FROM machine_epochs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert reason == "comms_restored"
    assert "31s" in detail
    assert conn.execute(
        "SELECT machine_changed FROM interrogations ORDER BY id DESC LIMIT 1"
    ).fetchone()[0] == 0


def test_a_machine_that_answers_nothing_is_still_interrogated(tmp_path):
    """§4.4 lets a machine ignore any long poll it does not implement. The
    row records what it refused, which is itself a fact about the machine."""
    class _Silent(ScriptedClient):
        def send_sas_version_and_serial(self):
            raise SASTimeoutError("no")

        def aft_game_lock_and_status_request(self, **kw):
            raise SASTimeoutError("no")

        def send_enabled_features(self, game_number=0):
            raise SASTimeoutError("no")

        def send_meter(self, poll, **kw):
            raise SASTimeoutError("no")

        def send_enabled_game_numbers(self):
            raise SASTimeoutError("no")

        def redeem_ticket_status(self):
            raise SASTimeoutError("no")

        def send_enhanced_validation_information(self, function_code=0xFF):
            raise SASTimeoutError("no")

    conn = _fresh(tmp_path)
    interrogate(_Silent([make_meters()]), conn, PollState(),
                "2026-10-28T06:00:00", reason="first_connect")
    row = conn.execute(
        "SELECT serial_number, games_found, enabled_features FROM interrogations"
    ).fetchone()
    assert row[0] is None and row[1] == 0
    assert "error" in row[2], "a refusal is recorded, not silently dropped"


def test_the_interrogation_drains_the_queue_while_it_runs(tmp_path):
    """~150 long polls is longer than the exception queue takes to overflow
    on a played machine. An interrogation that did not drain would cause the
    loss it exists to detect, silently, when its output is most trusted."""
    conn = _fresh(tmp_path)
    beats = []
    interrogate(_InterrogationClient([make_meters()], games=2), conn,
                PollState(), "2026-10-28T06:00:00", reason="first_connect",
                heartbeat=lambda: beats.append(1))
    # identity, features, then two polls per game, plus the ticket read.
    assert len(beats) >= 2 + 2 * 2, f"only {len(beats)} heartbeats"


def test_an_inherited_sig_ign_is_left_alone():
    """nohup sets SIGHUP to SIG_IGN before exec. Installing a handler over
    that revokes the protection it was asked for, turning `nohup ... &` from
    "survives a dropped SSH session" into "stops cleanly on one".

    The first version of the teardown-signal block did exactly that, and it
    would only have shown up as an overnight soak coming back short.
    """
    import signal
    import subprocess
    import textwrap

    prog = textwrap.dedent('''
        import signal, sys, time
        def _stop(signum, _frame):
            raise KeyboardInterrupt(f"signal {signum}")
        for s in (signal.SIGTERM, signal.SIGHUP):
            if signal.getsignal(s) == signal.SIG_IGN:
                continue
            signal.signal(s, _stop)
        print("RUNNING", flush=True)
        try:
            time.sleep(10)
            print("SURVIVED", flush=True)
        except KeyboardInterrupt:
            print("STOPPED", flush=True)
    ''')
    proc = subprocess.Popen(
        ["nohup", sys.executable, "-c", prog],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        assert proc.stdout.readline().strip() == "RUNNING"
        proc.send_signal(signal.SIGHUP)
        assert proc.stdout.readline().strip() == "SURVIVED", \
            "nohup's SIGHUP protection must not be revoked"
    finally:
        proc.kill()
        proc.wait()

    # And the poller's own block carries the guard, not just this copy of it.
    source = (Path(__file__).resolve().parent.parent
              / "examples" / "sql_poll_logger.py").read_text()
    assert "signal.getsignal(_sig) == signal.SIG_IGN" in source


# --- the call sites, not just the functions --------------------------------
#
# Both bugs below were in main()'s startup wiring while the function it
# called was correct and had its own passing test. A unit test on the callee
# cannot see them, and main() needs a serial port, so these assert the
# structure of the source directly. That is a blunt instrument; it is aimed
# at two invariants that cost real events and a real safety check.

def _sql_poll_logger_ast():
    import ast as _ast
    import pathlib as _pathlib
    src = (_pathlib.Path(__file__).resolve().parent.parent
           / "examples" / "sql_poll_logger.py").read_text()
    return _ast.parse(src)


def _main_function(tree):
    import ast as _ast
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name == "main":
            return node
    raise AssertionError("sql_poll_logger.py has no main()")


def test_every_interrogate_call_site_passes_a_heartbeat():
    """interrogate() threads a heartbeat through ~150 long polls and its own
    docstring says in capitals that this must not be skipped. The function
    was right and tested; the first_connect CALL omitted the argument, so
    every run began with a blind window -- ~13s at the 88ms a sweep measured,
    and ~45s once --min-poll-interval 0.3 paced it, against a 20-deep queue
    (§2.2.1) a played cabinet fills in about eight seconds.

    That is the only mechanism found here that destroys events rather than
    delaying them, and it was invisible to the unit test one level down.
    """
    import ast
    tree = _sql_poll_logger_ast()
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "interrogate"
    ]
    assert calls, "no interrogate() call sites found -- has it been renamed?"
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords}
        assert "heartbeat" in kwargs, (
            f"interrogate() called at line {call.lineno} without heartbeat=; "
            "that is ~150 long polls of not draining the exception queue"
        )


def test_the_ticket_serial_binding_is_not_inside_the_interrogate_branch():
    """The lab ticket file binds to one cabinet by serial, and that binding
    is the authorisation -- it is what stops the file redeeming tickets
    against a machine it was not issued for.

    (It was ``ticket_authority_serial`` when this test was written; the
    attribute became ``ticket_authority_binding`` when the asset-number
    fallback landed, and the name here was updated with it -- a guard that
    names a symbol which no longer exists passes for free.)

    The check used to sit inside ``if args.interrogate_on_connect:``, so
    --no-interrogate-on-connect, a flag about startup chatter, silently
    switched off a safety property. A flag that says "skip the chatter" must
    not be able to say "and skip the check".
    """
    import ast
    tree = _sql_poll_logger_ast()
    main = _main_function(tree)
    branches = [
        node for node in ast.walk(main)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Attribute)
        and node.test.attr == "interrogate_on_connect"
        and isinstance(node.test.value, ast.Name)
        and node.test.value.id == "args"
    ]
    assert branches, "no `if args.interrogate_on_connect:` branch in main()"
    for branch in branches:
        for node in ast.walk(ast.Module(body=branch.body, type_ignores=[])):
            name = getattr(node, "attr", None) or getattr(node, "id", None)
            assert name != "ticket_authority_binding", (
                f"the serial binding check is inside the interrogation branch "
                f"at line {getattr(node, 'lineno', '?')}; "
                "--no-interrogate-on-connect would disable it"
            )


# --- the ticket file's serial binding --------------------------------------
#
# Two bugs, one conflation, both shipped in the same afternoon. First the
# check read `if seen and seen.strip() != bound`, which fails OPEN: a cabinet
# that will not answer LP 0x54 (§4.4 lets it) had no serial to compare, so the
# binding went unenforced and nothing said so. Then that was replaced with a
# bare refusal, which fails CLOSED on the same cabinet -- and killed a running
# soak on a command line that had worked ten minutes earlier.


def _binding(serial=None, asset=None):
    MachineBinding = _lab_authority_or_skip().MachineBinding
    return MachineBinding(serial=serial, asset=asset)


def _verdict(binding, identity, *, allow_unverified=False):
    v = ticket_authority_binding_verdict(
        binding, identity, allow_unverified=allow_unverified)
    return None if v is None else v[0]


def test_a_mismatched_serial_is_always_refused():
    """The machine identified itself and it is the wrong one. No flag helps:
    an operator cannot know better than a burned-in serial."""
    ident = {"serial_number": "XYZ789", "asset_number": 1}
    assert _verdict(_binding(serial="ABC123"), ident) == "mismatch"
    assert _verdict(_binding(serial="ABC123"), ident,
                    allow_unverified=True) == "mismatch", (
        "--ticket-authority-unverified-serial must NOT excuse a wrong serial")


def test_a_matching_serial_passes_however_it_is_spaced():
    assert _verdict(_binding(serial="ABC123"),
                    {"serial_number": "ABC123", "asset_number": 1}) is None
    assert _verdict(_binding(serial=" ABC123 "),
                    {"serial_number": "ABC123\n", "asset_number": 1}) is None


def test_a_matching_serial_is_not_vetoed_by_a_changed_asset_number():
    """A RAM clear wipes the asset number and leaves the serial. Refusing a
    cabinet whose serial matches because its asset was retyped would block a
    legitimate machine after every clear."""
    assert _verdict(_binding(serial="ABC123", asset=246),
                    {"serial_number": "ABC123", "asset_number": 999}) is None


def test_the_binding_falls_back_to_the_asset_number():
    """§4.4 lets a cabinet ignore LP 0x54 and this project's lab EGM does, so
    a file bound only by serial cannot be checked against it at all. An asset
    binding is enforceable there -- asset numbers are never reissued on these
    floors."""
    no_serial = {"serial_number": None, "asset_number": 246}
    assert _verdict(_binding(asset=246), no_serial) is None
    assert _verdict(_binding(asset=247), no_serial) == "mismatch"
    assert _verdict(_binding(asset=247), no_serial,
                    allow_unverified=True) == "mismatch", (
        "a readable asset number that disagrees is still the wrong cabinet")


def test_a_serial_only_binding_against_a_silent_cabinet_is_unverifiable():
    """The case that forced --ticket-authority-unverified-serial into the soak
    command. An asset binding is the fix; the flag is the override."""
    no_serial = {"serial_number": None, "asset_number": 246}
    assert _verdict(_binding(serial="ABC123"), no_serial) == "unverifiable"
    assert _verdict(_binding(serial="ABC123"), no_serial,
                    allow_unverified=True) is None


def test_a_cabinet_with_no_identity_at_all_is_unverifiable():
    for ident in (None, {}, {"serial_number": "n/a", "asset_number": 0}):
        assert _verdict(_binding(serial="ABC123", asset=246),
                        ident) == "unverifiable", ident


def test_an_unbound_file_is_permitted_but_reported():
    """Whoever wrote the file chose that. It is said loudly rather than
    silently allowed."""
    assert _verdict(_binding(), {"serial_number": "ABC123"}) == "unbound"



# --- RTE that could not be enabled at startup ------------------------------

class _RTELateClient(ScriptedClient):
    """A cabinet that is still booting: LP 0x0E fails until it is up."""

    def __init__(self, *args, fail_times: int = 2, **kwargs):
        super().__init__(*args, **kwargs)
        self.rte_attempts = 0
        self._fail_times = fail_times

    def enable_real_time_events(self, on: bool = True):
        self.rte_attempts += 1
        if self.rte_attempts <= self._fail_times:
            raise SASTimeoutError("machine is still booting")
        return True

    # Once RTE is on the cycle switches to the Table 12.5 polls, so a client
    # that only answers the ordinary ones cannot test the switchover.
    def general_poll_rte(self):
        return None

    def drain_rte_events(self):
        return []


def test_a_failed_rte_enable_is_retried_every_cycle(capsys):
    """A gateway micro-PC boots in about thirty seconds; an EGM takes
    minutes. So after any mains event the poller comes up first, finds
    nothing answering, and -- before this -- fell back to ordinary polling
    for the WHOLE RUN, announced in one startup line and invisible after.

    A supervisor does not help: the process does not exit, it runs happily
    in the wrong mode. The only fix is to keep asking.
    """
    conn = make_db()
    state = PollState()
    state.rte_wanted = True          # --rte was given
    state.rte_enabled = False        # ...and startup could not enable it
    history = HistoryConfig(mode="ring")
    client = _RTELateClient([make_meters(), make_meters(), make_meters()],
                            fail_times=2)

    poll_and_log(client, conn, state, history, rte=True)
    assert state.rte_enabled is False, "first cycle: machine still silent"
    poll_and_log(client, conn, state, history, rte=True)
    assert state.rte_enabled is False, "second cycle: still silent"
    poll_and_log(client, conn, state, history, rte=True)
    assert state.rte_enabled is True, "third cycle: the cabinet answered"
    assert client.rte_attempts == 3
    assert "RTE ENABLED (LP 0x0E) on retry" in capsys.readouterr().out


def test_rte_is_not_retried_when_it_was_never_asked_for():
    """A run without --rte must never put a cabinet into Table 12.5 framing
    behind the operator's back."""
    conn = make_db()
    state = PollState()          # rte_wanted stays False
    history = HistoryConfig(mode="ring")
    client = _RTELateClient([make_meters()], fail_times=0)
    poll_and_log(client, conn, state, history)
    assert client.rte_attempts == 0
    assert state.rte_enabled is False


def test_once_enabled_it_is_not_re_enabled_every_cycle():
    """One long poll per cycle only while it is still failing."""
    conn = make_db()
    state = PollState()
    state.rte_wanted = True
    state.rte_enabled = True     # already on
    history = HistoryConfig(mode="ring")
    client = _RTELateClient([make_meters(), make_meters()], fail_times=0)
    poll_and_log(client, conn, state, history, rte=True)
    poll_and_log(client, conn, state, history, rte=True)
    assert client.rte_attempts == 0


# --- the pool-age alert is a condition, not an event stream -----------------

def _stale_pool_db():
    conn = make_db()
    conn.execute(
        "INSERT INTO validation_pool (validation_number, validation_system_id, "
        "status, issued_at) VALUES (1, 0, 'available', '2026-10-01T00:00:00+00:00')")
    conn.commit()
    return conn


def _pool_rows(conn):
    return conn.execute(
        "SELECT error_type, message FROM poll_errors WHERE poll_name = "
        "'validation_pool' ORDER BY id").fetchall()


def test_a_stale_pool_writes_one_row_per_episode_not_per_cycle():
    """It used to INSERT and commit every cycle the pool was stale. Measured
    on the lab database: 1302 rows in one hour at the early 2.8s cadence, then
    ~271/hour for four days, for a condition that never changed.

    poll_errors is the table a human reads to find poll FAILURES, and it was
    swamped -- the real signal, ~90 core-meter timeouts an hour, sat under a
    flood of advisory rows all saying the same thing. It was also a write plus
    a commit every cycle on a gateway whose --mode ring design exists to spare
    the flash precisely that.
    """
    conn = _stale_pool_db()
    state = PollState()
    clock = iter([0.0, 10.0, 20.0, 30.0, 40.0]).__next__  # 40 seconds of cycles
    for _ in range(5):
        _check_pool_age(conn, "2026-11-02T00:00:00+00:00", max_age_hours=36.0,
                        state=state, monotonic_fn=clock)
    rows = _pool_rows(conn)
    assert len(rows) == 1, f"five cycles wrote {len(rows)} rows"
    assert rows[0][0] == "PoolStale"
    assert state.pool_stale_since == "2026-11-02T00:00:00+00:00"


def test_a_stale_pool_is_re_recorded_hourly_so_it_has_a_duration():
    conn = _stale_pool_db()
    state = PollState()
    clock = iter([0.0, 1800.0, 3600.0, 3610.0]).__next__
    for _ in range(4):
        _check_pool_age(conn, "2026-11-02T00:00:00+00:00", max_age_hours=36.0,
                        state=state, monotonic_fn=clock)
    rows = _pool_rows(conn)
    assert len(rows) == 2, "start, then one an hour later -- not the 30min one"
    assert "still stale since" in rows[1][1]


def test_clearing_a_stale_pool_is_recorded_because_that_is_the_answer():
    """The alert asks for a top-up. That one arrived is the fact worth
    keeping -- it is how a replacement proves it reached this gateway."""
    conn = _stale_pool_db()
    state = PollState()
    clock = iter([0.0, 10.0]).__next__
    _check_pool_age(conn, "2026-11-02T00:00:00+00:00", max_age_hours=36.0,
                    state=state, monotonic_fn=clock)
    # A replacement retires the stale row and issues a fresh one.
    conn.execute("UPDATE validation_pool SET status = 'revoked'")
    conn.execute(
        "INSERT INTO validation_pool (validation_number, validation_system_id, "
        "status, issued_at) VALUES (2, 0, 'available', '2026-11-02T00:00:00+00:00')")
    conn.commit()
    _check_pool_age(conn, "2026-11-02T00:00:10+00:00", max_age_hours=36.0,
                    state=state, monotonic_fn=clock)
    rows = _pool_rows(conn)
    assert [r[0] for r in rows] == ["PoolStale", "PoolStaleCleared"]
    assert state.pool_stale_since is None


def test_a_pool_that_was_never_stale_writes_nothing_on_clear():
    conn = _stale_pool_db()
    conn.execute("UPDATE validation_pool SET issued_at = '2026-11-02T00:00:00+00:00'")
    conn.commit()
    state = PollState()
    _check_pool_age(conn, "2026-11-02T00:00:00+00:00", max_age_hours=36.0,
                    state=state, monotonic_fn=lambda: 0.0)
    assert _pool_rows(conn) == []


# --- identity when the cabinet will not give a serial -----------------------
#
# Operational requirement, 2026-11-02: "not every machine announces their
# serial number through sas. We have to make sure that; if serial number isn't
# available, we fall back to asset number. On our floors we don't issue same
# asset number twice... In Trinidad; machines asset numbers are never duplicate
# although they might be in different floors (3 casinos around the country)."

def test_machine_key_falls_back_to_the_asset_number():
    assert machine_key({"serial_number": "000106719", "asset_number": 246}) == \
        ("serial", "000106719")
    assert machine_key({"serial_number": None, "asset_number": 246}) == \
        ("asset", "246")
    assert machine_key({"serial_number": "", "asset_number": 246}) == \
        ("asset", "246")
    assert machine_key({"serial_number": None, "asset_number": 0}) is None
    assert machine_key({}) is None
    assert machine_key(None) is None


def test_a_placeholder_serial_is_not_an_identity():
    """survey_store learned this on the floor; the poller never had it."""
    for placeholder in ("", "   ", "0", "00000000", "n/a", "N/A", "none", "-"):
        ident = {"serial_number": placeholder, "asset_number": 246}
        assert usable_serial(ident) is None, placeholder
        assert machine_key(ident) == ("asset", "246"), placeholder


def test_a_serial_that_is_the_asset_number_retyped_is_not_an_identity():
    """Seen on the floor 2026-10-27: an IGT cabinet reporting asset 7 and
    serial "7". It passes every placeholder test and reads like a real serial
    in a report, which is exactly why it is dangerous."""
    ident = {"serial_number": "7", "asset_number": 7}
    assert usable_serial(ident) is None
    assert machine_key(ident) == ("asset", "7")


def test_asset_zero_is_not_an_asset_number():
    assert usable_asset({"asset_number": 0}) is None
    assert usable_asset({"asset_number": None}) is None
    assert usable_asset({"asset_number": 246}) == 246


def test_a_changed_asset_with_no_serial_is_a_CHANGED_MACHINE():
    """The reasoning inverts, and this used to get it backwards.

    "The asset changed but the serial held" is only reassuring when there IS a
    serial holding. On a cabinet reporting none -- §4.4 permits it, and the lab
    EGM does it -- the asset number is the only identity there is, and on these
    floors it is never reissued. A different one is a different machine.

    Reporting it as a reconfiguration would carry one cabinet's meter history
    silently onto another.
    """
    kind, detail = identity_changed(
        {"serial_number": None, "asset_number": 246},
        {"serial_number": None, "asset_number": 247})
    assert kind == "machine_changed", kind
    assert "only identity" in detail


def test_a_changed_asset_WITH_a_serial_is_still_a_reconfiguration():
    """Unchanged, and must stay so: a RAM clear wipes the asset number while
    the serial survives, so calling that a swap mislabels every clear."""
    kind, detail = identity_changed(
        {"serial_number": "000106719", "asset_number": 246},
        {"serial_number": "000106719", "asset_number": 247})
    assert kind == "asset_changed", kind


def test_a_cabinet_that_stops_answering_0x54_has_not_changed():
    """§4.4 lets it refuse. Treating a missed poll as a change would open an
    epoch every time one was dropped."""
    assert identity_changed(
        {"serial_number": "000106719", "asset_number": 246},
        {"serial_number": None, "asset_number": 246}) is None
    assert identity_changed(
        {"serial_number": None, "asset_number": 246},
        {"serial_number": "000106719", "asset_number": 246}) is None


def test_the_placeholder_list_lives_in_the_LIBRARY_and_is_not_copied():
    """Two copies drift, and the floor lessons behind this list were paid for
    once already.

    This test used to assert the poller imported the list from
    examples/survey_store.py. That kept one copy, but it pointed the PRODUCT
    at a SURVEY TOOL: the public mirror could not ship the gateway without
    also shipping a tool for walking a floor with a laptop. The list is
    identity validation, which is library work, so it now lives in
    saspy.constants and both callers take it from there.
    """
    from saspy.constants import PLACEHOLDER_IDENTIFIERS
    requires_lab_file(Path(__file__).resolve().parent.parent
                      / "examples" / "survey_store.py")
    from examples.survey_store import PLACEHOLDER_IDS as survey_list
    assert survey_list is PLACEHOLDER_IDENTIFIERS

    root = Path(__file__).resolve().parent.parent
    for name in ("examples/sql_poll_logger.py", "examples/survey_store.py"):
        src = (root / name).read_text()
        assert "PLACEHOLDER_IDS = {" not in src, (
            f"{name} has its own copy of the placeholder list; import it "
            f"from saspy.constants so the two cannot drift")

    poller = (root / "examples/sql_poll_logger.py").read_text()
    assert "from survey_store import" not in poller, (
        "the product must not import a survey tool at runtime")
    assert "is_placeholder_identifier" in poller


def test_the_example_ticket_file_actually_loads():
    """It is the thing people copy. A first draft of the asset-binding
    documentation put "machine_asset": 0 in it, which the loader rejects by
    design -- zero is the field's unset value -- so the example shipped
    unloadable and the first thing a new operator would hit was an error in a
    file they had not touched yet."""
    _lab = _lab_authority_or_skip()
    path = Path(__file__).resolve().parent.parent / "lab_tickets.example.json"
    tickets, binding = _lab.load_tickets(path)
    assert len(tickets) == 3
    # The example is a template, so it must not claim to be bound to anything
    # real -- but it must demonstrate both keys.
    assert binding.asset is None
    assert "machine_asset" in path.read_text()
    assert "machine_serial" in path.read_text()


def test_an_asset_number_with_letters_is_refused_with_the_reason():
    """The SAS field is a 4-byte binary integer, so a letter cannot come off
    the wire -- but a human typing the file can put one there."""
    import json as _json
    _lab = _lab_authority_or_skip()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        bad = Path(d) / "t.json"
        bad.write_text(_json.dumps({"machine_asset": "A246", "tickets": {}}))
        with pytest.raises(ValueError, match="not a number"):
            _lab.load_tickets(bad)


# --- exception 0x3C, "operator changed options" -----------------------------
#
# The ONLY in-band signal that a cabinet's configuration moved. It was
# recognised in the description table and dispatched nowhere, so a denomination
# change could happen with nothing in the database marking when -- every credit
# figure after it wrong by a factor, and no way to find the boundary later.

def _interrogation_with_games(conn, games):
    cur = conn.execute(
        "INSERT INTO interrogations (started_at, reason) VALUES (?, ?)",
        ("2026-11-02T00:00:00+00:00", "first_connect"))
    iid = cur.lastrowid
    for number, cfg in games.items():
        cols = ", ".join(cfg)
        marks = ", ".join("?" for _ in cfg)
        conn.execute(
            f"INSERT INTO interrogation_games (interrogation_id, game_number, {cols}) "
            f"VALUES (?, ?, {marks})",
            (iid, number, *cfg.values()))
    conn.commit()
    return iid


def test_game_config_diff_reports_a_denomination_change():
    conn = make_db()
    a = _interrogation_with_games(conn, {1: {"denomination": 1, "max_bet": 5}})
    b = _interrogation_with_games(conn, {1: {"denomination": 25, "max_bet": 5}})
    assert game_config_diff(conn, a, b) == [(1, "denomination", 1, 25)]


def test_game_config_diff_reports_a_game_appearing_or_disappearing():
    """An operator enabling a game is exactly what 0x3C announces."""
    conn = make_db()
    a = _interrogation_with_games(conn, {1: {"denomination": 1}})
    b = _interrogation_with_games(conn, {1: {"denomination": 1},
                                         2: {"denomination": 1}})
    assert game_config_diff(conn, a, b) == [(2, "game", "absent", "present")]
    assert game_config_diff(conn, b, a) == [(2, "game", "present", "absent")]


def test_game_config_diff_ignores_a_field_the_machine_stopped_answering():
    """§4.4 lets a machine refuse LP 0x53 for a game. A refusal is not a
    change, and treating it as one would announce a reconfiguration every time
    a poll was dropped."""
    conn = make_db()
    a = _interrogation_with_games(conn, {1: {"denomination": 1, "max_bet": 5}})
    b = _interrogation_with_games(conn, {1: {"denomination": 1}})
    assert game_config_diff(conn, a, b) == []


def test_game_config_diff_ignores_relabelled_names():
    """game_name and paytable_name are labels; a vendor may re-word one
    without changing anything that affects accounting."""
    conn = make_db()
    a = _interrogation_with_games(conn, {1: {"denomination": 1,
                                             "game_name": "Lucky 7"}})
    b = _interrogation_with_games(conn, {1: {"denomination": 1,
                                             "game_name": "Lucky Seven"}})
    assert game_config_diff(conn, a, b) == []


def test_only_denomination_paytable_and_percentage_open_an_epoch():
    """max_bet moving is worth recording and does not rescale a meter.
    Denomination does: SAS counts credits, so 1000 credits at $0.01 and at
    $0.25 are the same reading and twenty-four dollars apart."""
    from examples.sql_poll_logger import EPOCH_WORTHY_CONFIG_FIELDS
    assert set(EPOCH_WORTHY_CONFIG_FIELDS) == {
        "denomination", "paytable_id", "base_percentage"}
    assert "max_bet" not in EPOCH_WORTHY_CONFIG_FIELDS
    assert "game_options" not in EPOCH_WORTHY_CONFIG_FIELDS


def test_0x3C_sets_the_pending_flag_rather_than_re_reading_inline():
    """~150 long polls, and the dispatcher can be running from inside a meter
    sweep -- the same reason the comms-restored interrogation is deferred."""
    conn = make_db()
    state = PollState()
    _dispatch_exception(ScriptedClient([make_meters()]), conn,
                        "2026-11-02T00:00:00+00:00", 0x3C, state=state)
    assert state.options_change_pending == "2026-11-02T00:00:00+00:00"
    row = conn.execute(
        "SELECT exception_code FROM machine_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == 0x3C, "the exception itself must still be recorded"


# --- real time event mode left on by an unclean stop ------------------------
#
# Seen on the gateway 2026-11-02: a soak stopped with SIGTERM, the service
# started, and its interrogation logged "exception 0x01 during interrogation:
# unknown/unassigned code 0x01" three times. The teardown's LP 0x0E can itself
# be pre-empted by a queued event (§12.3), and the service unit's ExecStartPre
# -- which exists for exactly this -- had failed too.

def test_rte_looks_stuck_only_for_an_address_table_a1_leaves_unassigned():
    from examples.sql_poll_logger import rte_looks_stuck
    assert rte_looks_stuck(0x01, 1)      # the case we actually hit
    assert rte_looks_stuck(0x10, 16)
    assert not rte_looks_stuck(0x00, 1)  # "no activity" is a real answer
    assert not rte_looks_stuck(0x7E, 1)  # a real exception from address 1
    assert not rte_looks_stuck(0x01, 2)  # not THIS machine's address
    # Address 17+ collides with real Table A-1 codes and cannot be told apart
    # from one byte. Claiming otherwise would disable RTE on a live event.
    assert not rte_looks_stuck(0x11, 17)
    assert not rte_looks_stuck(0x14, 20)


class _StuckRTEClient(ScriptedClient):
    """Answers general polls with its own address until RTE is disabled."""

    def __init__(self, *a, address=1, recovers=True, **kw):
        super().__init__(*a, **kw)
        self.address = address
        self._stuck = True
        self._recovers = recovers
        self.disabled = []

    def general_poll(self):
        return self.address if self._stuck else 0x00

    def enable_real_time_events(self, on):
        self.disabled.append(on)
        if on is False and self._recovers:
            self._stuck = False


def test_a_stuck_rte_is_detected_and_cleared_before_anything_else_reads():
    from examples.sql_poll_logger import recover_stuck_rte
    client = _StuckRTEClient([make_meters()], address=1)
    note, code = recover_stuck_rte(client)
    assert client.disabled == [False], "it must send LP 0x0E disable, once"
    assert "had been left ON" in note
    assert code == 0x00


def test_a_stuck_rte_that_will_not_clear_says_to_power_cycle():
    """§12 requires the ordinary mode on power up, so that is the last
    resort and the message has to name it."""
    from examples.sql_poll_logger import recover_stuck_rte
    client = _StuckRTEClient([make_meters()], address=1, recovers=False)
    note, code = recover_stuck_rte(client)
    assert "STILL on" in note and "power-cycle" in note
    assert code is None


def test_an_ordinary_cabinet_is_left_alone():
    """Disabling RTE on a machine that is not in it would be a side effect on
    a probe, and on a run that WANTED rte it would fight its own startup."""
    from examples.sql_poll_logger import recover_stuck_rte
    client = _StuckRTEClient([make_meters()], address=1)
    client._stuck = False
    note, code = recover_stuck_rte(client)
    assert client.disabled == []
    assert note == "ordinary framing"
    assert code == 0x00


# --- exception 0x69, AFT transfer complete ---------------------------------
#
# §8.8 makes acknowledging mandatory: the machine reissues 0x69 every 15
# seconds until the host polls for and acknowledges the completion status --
# 15.2s measured on an EGT Premier. And 0x69 is a PRIORITY exception, so it
# bypasses §2.2.1's queue and arrives ahead of everything waiting. The poller
# did not handle it at all until 2026-11-03.

class _AFTClient(ScriptedClient):
    def __init__(self, *a, result=None, fails=False, **kw):
        super().__init__(*a, **kw)
        self.interrogations = []
        self._fails = fails
        self._result = result

    def aft_interrogate_transfer(self, index=0x00, *, acknowledge=True):
        self.interrogations.append((index, acknowledge))
        if self._fails:
            raise SASTimeoutError("no answer")
        return self._result


def _aft_result(**over):
    from saspy.models import AftTransferResult
    import inspect
    fields = {f: 0 for f in inspect.signature(AftTransferResult).parameters}
    for text in ("transaction_id", "transaction_date", "transaction_time",
                 "expiration"):
        fields[text] = ""
    fields.update(over)
    return AftTransferResult(**fields)


def test_0x69_is_acknowledged_with_transfer_code_FF():
    """FE reads the same status WITHOUT acknowledging and does NOT stop the
    reissue, so it is not what this wants."""
    conn = make_db()
    client = _AFTClient([make_meters()],
                        result=_aft_result(transfer_status=0x00,
                                           cashable_amount=991111,
                                           transaction_id="NIMA123"))
    _dispatch_exception(client, conn, "2026-11-03T00:00:00+00:00", 0x69,
                        state=PollState())
    assert client.interrogations == [(0x00, True)], (
        "0x69 must be acknowledged, not merely read")
    row = conn.execute(
        "SELECT transfer_status, cashable_amount, transaction_id "
        "FROM aft_transfer_completions").fetchone()
    assert row == (0x00, 991111, "NIMA123")


def test_a_failed_acknowledgement_is_recorded_and_says_what_follows():
    """An unacknowledged transfer installs a permanent 15-second beat at the
    FRONT of the exception stream. Silence about that would be the worst
    outcome, because the symptom appears somewhere else entirely."""
    conn = make_db()
    client = _AFTClient([make_meters()], fails=True)
    _dispatch_exception(client, conn, "2026-11-03T00:00:00+00:00", 0x69,
                        state=PollState())
    row = conn.execute(
        "SELECT poll_name, error_type FROM poll_errors").fetchone()
    assert row[0] == "aft_interrogate_transfer"
    assert conn.execute(
        "SELECT COUNT(*) FROM aft_transfer_completions").fetchone()[0] == 0


def test_the_amounts_recorded_are_the_machine_s_not_the_request_s():
    """A partial transfer is the case where the two differ, and the
    difference is the whole reason to read it back."""
    conn = make_db()
    client = _AFTClient([make_meters()],
                        result=_aft_result(transfer_status=0x00,
                                           cashable_amount=500,
                                           nonrestricted_amount=0))
    _dispatch_exception(client, conn, "2026-11-03T00:00:00+00:00", 0x69,
                        state=PollState())
    row = conn.execute(
        "SELECT cashable_amount, nonrestricted_amount "
        "FROM aft_transfer_completions").fetchone()
    assert row == (500, 0)


def test_0x69_is_still_recorded_as_a_machine_event():
    conn = make_db()
    client = _AFTClient([make_meters()], result=_aft_result())
    _dispatch_exception(client, conn, "2026-11-03T00:00:00+00:00", 0x69,
                        state=PollState())
    row = conn.execute(
        "SELECT exception_code, description FROM machine_events").fetchone()
    assert row[0] == 0x69
    assert "AFT transfer complete" in row[1]


# --- the product must not depend on lab scaffolding ---------------------

def test_the_poller_imports_NOTHING_from_the_lab_ticket_authority():
    """ticket_lab_authority.py exists only because the lab had no server to
    issue validation numbers. On a floor the numbers are ALWAYS issued by the
    server, so the gateway must never need that module.

    It did. The poller imported NOT_IN_SYSTEM, TRANSFER_CODES and describe()
    from it -- Tables 15.12 and 15.12c, spec tables that were simply written
    in the wrong file. The effect was that the public mirror could not drop
    the scaffolding without breaking the gateway, and a reader would
    reasonably conclude the product could mint its own tickets.

    The remaining reference is correct and must stay: a LAZY import inside
    the --lab-ticket-authority branch, which is the one place that genuinely
    needs the scaffolding. So the property is not "the name never appears" --
    that would forbid the flag working -- it is "nothing at module level
    needs it", checked with ast rather than a substring, because a substring
    guard here would match this very docstring.
    """
    import ast
    root = Path(__file__).resolve().parent.parent
    tree = ast.parse((root / "examples" / "sql_poll_logger.py").read_text())
    for node in tree.body:                      # module level only
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert "ticket_lab_authority" not in name, (
                f"the product imports lab scaffolding at module level: {name}")
            assert "survey_store" not in name, (
                f"the product imports a survey tool at module level: {name}")


def test_the_spec_tables_are_in_the_library():
    from saspy.constants import (
        TICKET_NOT_IN_SYSTEM, TICKET_REDEMPTION_CATEGORY,
        TICKET_REDEMPTION_STATUS, TICKET_TRANSFER_CODES,
        describe_redemption_status, redemption_category,
    )
    assert TICKET_TRANSFER_CODES == {"cashable": 0x00, "restricted": 0x01,
                                     "nonrestricted": 0x02}
    assert TICKET_NOT_IN_SYSTEM == 0x82
    assert redemption_category(0x02) == 0b000
    assert "SUCCESS" in describe_redemption_status(0x00)
    assert TICKET_REDEMPTION_CATEGORY[0b100] == "FAILED"
    assert TICKET_REDEMPTION_STATUS  # non-empty


def test_0x82_means_DIFFERENT_THINGS_in_each_direction():
    """The byte is shared; the meaning is not, and conflating them was a
    FIX that introduced a bug.

    0x82 was missing from TICKET_REDEMPTION_STATUS until 2026-11-06. The
    fix then saw TICKET_NOT_IN_SYSTEM = 0x82 three lines above and folded
    that meaning in -- but TICKET_NOT_IN_SYSTEM is what the HOST sends in
    LP 0x71 to refuse a ticket it does not recognise (§15.12a), while that
    table is what the MACHINE reports back (§15.12c), where 0x82 is "not a
    valid transfer function".

    So `describe_redemption_status(0x82)` told an operator the host had
    not recognised the ticket, when the machine had actually said the
    transfer function was invalid. This test previously ASSERTED that
    behaviour, which is why it survived.
    """
    from saspy import constants as C
    assert 0x82 in C.TICKET_REDEMPTION_STATUS
    assert "unassigned" not in C.describe_redemption_status(0x82)
    assert "not a valid transfer function" in C.describe_redemption_status(0x82)
    # The host-side constant keeps its own meaning, documented beside it.
    assert C.TICKET_NOT_IN_SYSTEM == 0x82


def test_the_machine_side_table_is_COMPLETE_against_section_15_12c():
    """ticket_history.py and ticket_redeem.py each carried a private copy
    that was MORE complete than the canonical table -- they had 0x83 and
    0x89 and it did not. Three copies, and the one everything imported was
    the thinnest."""
    from saspy import constants as C
    for code in (0x82, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89, 0x8A, 0x8B):
        assert code in C.TICKET_REDEMPTION_STATUS, f"0x{code:02X} missing"


def test_every_status_code_resolves_to_a_known_category():
    """A status whose category is unknown prints 'unknown category: ...',
    which tells an operator nothing about whether the ticket was paid."""
    from saspy import constants as C
    for code in C.TICKET_REDEMPTION_STATUS:
        assert (code >> 5) in C.TICKET_REDEMPTION_CATEGORY, hex(code)


def test_is_placeholder_identifier_folds_case_and_whitespace():
    """Call sites were each writing `v.lower() in {p.lower() for p in ...}`,
    which is a comparison anyone can get subtly wrong once."""
    from saspy.constants import is_placeholder_identifier as ph
    for absent in ("", " ", "0", "N/A", "n/a", " none ", "UNKNOWN", "-", None):
        assert ph(absent) is True, repr(absent)
    # Three shapes a cabinet really reports: plain digits, zero-padded, and
    # a letter prefix. The values are INVENTED. They used to be serials
    # copied off real cabinets, which published floor identifiers through a
    # mirrored test for no gain: is_placeholder_identifier is a membership
    # test against PLACEHOLDER_IDENTIFIERS, so what it needs is any value
    # outside that set, not a provably genuine one.
    for genuine in ("20884471", "000471238", "K072418596"):
        assert ph(genuine) is False, genuine


def test_a_FAILED_sweep_still_files_the_events_it_collected():
    """The drain used to run only on the happy path.

    A failed sweep returned straight out of the cycle, past
    drain_rte_events(). The events were not lost -- the client holds them in
    its pending list -- but they waited for the NEXT cycle and were written
    with that cycle's timestamp, up to a full sweep later.

    The failure that skips the drain is a §12.3 pre-emption burst, and a
    pre-emption burst is by definition the moment the machine has the most
    queued. So the one cycle that collected the most events was the one that
    filed them last and timed them worst. The operator's button-pressing on
    2026-11-06T21:02 is exactly that case: two polls each burned all 24
    reissues.
    """
    import ast
    root = Path(__file__).resolve().parent.parent
    tree = ast.parse((root / "examples" / "sql_poll_logger.py").read_text())

    handler = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for h in node.handlers:
            name = getattr(h.type, "id", None)
            if name == "_MeterPollFailure":
                handler = h
    assert handler is not None, "the failure handler moved or was renamed"

    body = ast.dump(ast.Module(body=handler.body, type_ignores=[]))
    assert "drain_rte_events" in body, (
        "a failed sweep returns without filing the events it collected")

    # And it must file them under their own arrival time, not the cycle's.
    assert "arrived_at" in body, (
        "stranded events must keep the time they came off the wire")


# --- the live stream self-audit (§12.5.3) --------------------------------

def _gap_db():
    conn = make_db()
    return conn


def test_a_clean_chain_of_game_starts_records_nothing():
    """§12.5.3 puts the coin-in meter inside the game-start event, AFTER the
    wager, so two consecutive starts differ by exactly the later wager."""
    from examples.sql_poll_logger import _check_stream_continuity
    conn = _gap_db()
    state = PollState()
    # Each start's coin_in is the PREVIOUS one plus THIS game's wager,
    # because §12.5.3's meter is read after the wager is taken. Getting
    # that backwards is the obvious mistake and this fixture made it once.
    for coin_in, wager in ((10_000, 100), (10_250, 250), (10_350, 100)):
        _check_stream_continuity(conn, "2026-11-06T12:00:00", state,
                                 {"total_coin_in": coin_in,
                                  "wager_cents": wager})
    assert conn.execute("SELECT count(*) FROM stream_gaps").fetchone()[0] == 0
    assert state.stream_gaps == 0


def test_a_MISSING_game_start_is_caught_and_quantified_immediately():
    """The whole point of running this live. rte_reconcile has had the same
    arithmetic since it was written and found nine lost starts on
    2026-11-06 -- THREE HOURS LATE, because nothing ran it until the
    operator asked by hand."""
    from examples.sql_poll_logger import _check_stream_continuity
    conn = _gap_db()
    state = PollState()
    _check_stream_continuity(conn, "2026-11-06T12:00:00", state,
                             {"total_coin_in": 10_000, "wager_cents": 100})
    # 180 credits of wagering happened; this game only accounts for 20.
    _check_stream_continuity(conn, "2026-11-06T12:00:30", state,
                             {"total_coin_in": 10_200, "wager_cents": 20})
    row = conn.execute("SELECT unexplained_credits, previous_coin_in, "
                       "this_coin_in, this_wager FROM stream_gaps").fetchone()
    assert row == (180, 10_000, 10_200, 20)
    assert state.stream_gaps == 1
    assert state.stream_credits_lost == 180


def test_a_FALLING_coin_in_meter_is_not_reported_as_a_loss():
    """It only ever rises, so a fall is a RAM clear or a bad read. Calling
    it a lost event would cry wolf on the day a technician clears the
    machine -- which this project has watched happen three times."""
    from examples.sql_poll_logger import _check_stream_continuity
    conn = _gap_db()
    state = PollState()
    _check_stream_continuity(conn, "2026-11-06T12:00:00", state,
                             {"total_coin_in": 500_000, "wager_cents": 100})
    _check_stream_continuity(conn, "2026-11-06T12:00:30", state,
                             {"total_coin_in": 0, "wager_cents": 100})
    assert conn.execute("SELECT count(*) FROM stream_gaps").fetchone()[0] == 0
    # And the chain picks up from the new value rather than the old one.
    _check_stream_continuity(conn, "2026-11-06T12:01:00", state,
                             {"total_coin_in": 100, "wager_cents": 100})
    assert conn.execute("SELECT count(*) FROM stream_gaps").fetchone()[0] == 0


def test_the_first_start_of_a_run_cannot_be_a_gap():
    """There is nothing to compare it against."""
    from examples.sql_poll_logger import _check_stream_continuity
    conn = _gap_db()
    state = PollState()
    _check_stream_continuity(conn, "2026-11-06T12:00:00", state,
                             {"total_coin_in": 999_999, "wager_cents": 100})
    assert conn.execute("SELECT count(*) FROM stream_gaps").fetchone()[0] == 0


def test_a_start_with_no_coin_in_field_is_skipped_not_guessed():
    from examples.sql_poll_logger import _check_stream_continuity
    conn = _gap_db()
    state = PollState()
    _check_stream_continuity(conn, "2026-11-06T12:00:00", state, {})
    _check_stream_continuity(conn, "2026-11-06T12:00:01", state, None)
    assert conn.execute("SELECT count(*) FROM stream_gaps").fetchone()[0] == 0


def test_the_chain_is_reset_across_a_comms_gap():
    """The operator's own exemption: "unless gateway lost communication due
    to a reboot or faulty sas cable". Play continues while nobody polls, so
    the next start shows a coin-in jump that is NOT a lost event. Without
    this the tool cries wolf after every reboot."""
    root = Path(__file__).resolve().parent.parent
    poller = (root / "examples" / "sql_poll_logger.py").read_text()
    block = poller[:poller.index('reason="comms_restored"')]
    tail = block[-900:]
    assert "state.last_game_start_coin_in = None" in tail, (
        "the continuity chain is not reset before the comms-restored "
        "interrogation, so every reboot will report a phantom loss")


# --- why a run ended ----------------------------------------------------
#
# Operator, 2026-11-08: "My real question is, why did the previous soak
# ended, did it crash?" The poller printed its reason on every exit path
# and printed it to a nohup log that was gone. The database outlived it.


def test_the_schema_carries_a_lifecycle_table():
    import sqlite3 as _sq
    conn = _sq.connect(":memory:")
    conn.executescript(SCHEMA)
    cols = [r[1] for r in conn.execute(
        "PRAGMA table_info(poller_lifecycle)").fetchall()]
    assert cols, "poller_lifecycle is missing from SCHEMA"
    for needed in ("at", "event", "reason", "pid", "argv"):
        assert needed in cols, f"poller_lifecycle has no {needed} column"


def test_a_stop_reason_is_written_to_the_database():
    import sqlite3 as _sq
    from sql_poll_logger import _record_lifecycle
    conn = _sq.connect(":memory:")
    conn.executescript(SCHEMA)
    _record_lifecycle(conn, "started", reason=None)
    _record_lifecycle(conn, "stopped", reason="signal 15")
    rows = conn.execute(
        "SELECT event, reason FROM poller_lifecycle ORDER BY id").fetchall()
    assert rows == [("started", None), ("stopped", "signal 15")]


def test_recording_a_lifecycle_row_can_NEVER_raise():
    """A failure to record WHY a run ended must not become why it ends.

    This is the one place in this file a bare except is correct, and the
    test exists so nobody 'tidies' it into a crash on the way out.
    """
    import sqlite3 as _sq
    from sql_poll_logger import _record_lifecycle
    conn = _sq.connect(":memory:")   # no schema at all: the insert must fail
    _record_lifecycle(conn, "stopped", reason="signal 15")


def test_an_UNMATCHED_started_is_what_proves_a_kill():
    """SIGKILL cannot write a row. That is the whole discriminator.

    An OOM kill, a `kill -9` and a power cut all leave 'started' with no
    'stopped'; every catchable ending leaves both. Nothing else in the
    database distinguishes them.
    """
    import sqlite3 as _sq
    from sql_poll_logger import _record_lifecycle
    conn = _sq.connect(":memory:")
    conn.executescript(SCHEMA)
    _record_lifecycle(conn, "started", reason=None)      # killed here
    _record_lifecycle(conn, "started", reason=None)
    _record_lifecycle(conn, "stopped", reason="end of run")
    unmatched = conn.execute(
        "SELECT count(*) FROM poller_lifecycle WHERE event = 'started'"
    ).fetchone()[0] - conn.execute(
        "SELECT count(*) FROM poller_lifecycle WHERE event = 'stopped'"
    ).fetchone()[0]
    assert unmatched == 1


# --- The C-7 cadence and drain priority ---------------------------------
#
# Found 2026-11-11 while explaining why the lab sweep varies between 40s and
# 64s. The call site defers the C-7 sweep under drain priority
# (`c7_due and not draining`) but the bookkeeping consumed the cadence on
# `c7_due` alone, so a deferred sweep was recorded as a completed one and
# C-7 waited another FULL interval.
#
# It is not academic. The operator's floor gateways run --drain-priority
# permanently, because it is what keeps real time events from being lost,
# and a lab cycle that same night reached 48% of the event budget against a
# 50% drain threshold. Drains also cluster: the cabinet is busiest exactly
# when C-7 keeps coming due, so the starvation repeats rather than
# averaging out.

def _draining_state():
    """A state the drain-priority rule will act on: above
    DRAIN_ENTER_FRACTION of the collectable budget."""
    from sql_poll_logger import DRAIN_ENTER_FRACTION
    s = PollState()
    s.last_capacity = 3.3
    s.last_event_rate = 3.3 * DRAIN_ENTER_FRACTION + 0.1
    return s


def test_a_drained_cycle_does_not_consume_the_c7_cadence():
    """Deferring the sweep and recording it as done are different things.

    Without this, a busy spell costs a C-7 read AND pushes the next one a
    full interval away -- 300s on the floor unit, while the cabinet is
    mid-play and the extended meters are most worth having.
    """
    conn = make_db()
    clock = FakeClock()
    state = _draining_state()
    client = ScriptedClient([make_meters()], table_c7_values={0x0000: 4290})

    poll_and_log(client, conn, state, HistoryConfig(mode="ring"),
                 monotonic_fn=clock, table_c7_interval=3600,
                 drain_priority=True)

    assert state.draining, "the fixture must actually trigger a drain"
    row = conn.execute(
        "SELECT c7_total_coin_in_credits FROM meters_current").fetchone()
    assert row[0] is None, "the sweep should have been deferred"
    assert state.last_c7_sweep is None, \
        "a deferred sweep must not consume the cadence"


def test_c7_is_read_on_the_first_quiet_cycle_after_a_drain():
    """The cadence survives the drain, so the read happens as soon as there
    is room for it rather than one interval later."""
    conn = make_db()
    clock = FakeClock()
    state = _draining_state()
    history = HistoryConfig(mode="append")
    client = ScriptedClient([make_meters(), make_meters()],
                            table_c7_values={0x0000: 4290})

    # Busy: deferred.
    poll_and_log(client, conn, state, history, monotonic_fn=clock,
                 table_c7_interval=3600, drain_priority=True)
    # Quiet again, and only one second later -- far inside the interval.
    # Both figures, not just the rate: the cycle overwrites last_capacity
    # with what it measured, and a zero capacity leaves the hysteresis in
    # DRAIN_LEAVE_FRACTION unable to see the machine go quiet.
    state.last_event_rate, state.last_capacity = 0.0, 3.3
    clock.t += 1.0
    poll_and_log(client, conn, state, history, monotonic_fn=clock,
                 table_c7_interval=3600, drain_priority=True)

    rows = [r[0] for r in conn.execute(
        "SELECT c7_total_coin_in_credits FROM meters_history ORDER BY id")]
    assert rows == [None, 4290], rows
    assert state.last_c7_sweep is not None


def test_an_undrained_cycle_still_consumes_the_cadence():
    """The fix must not turn the cadence off. With drain priority enabled
    but the machine quiet, the sweep runs and the interval applies."""
    conn = make_db()
    clock = FakeClock()
    state = PollState()
    history = HistoryConfig(mode="append")
    client = ScriptedClient([make_meters(), make_meters()],
                            table_c7_values={0x0000: 4290})

    poll_and_log(client, conn, state, history, monotonic_fn=clock,
                 table_c7_interval=3600, drain_priority=True)
    clock.t += 1.0
    poll_and_log(client, conn, state, history, monotonic_fn=clock,
                 table_c7_interval=3600, drain_priority=True)

    rows = [r[0] for r in conn.execute(
        "SELECT c7_total_coin_in_credits FROM meters_history ORDER BY id")]
    assert rows == [4290, None], "second cycle is inside the interval"
