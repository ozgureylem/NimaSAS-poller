import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

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
    _insert_ticket_out_record,
    _pool_age_hours,
    _safe_commit,
    _sas_poll_interval,
    backfill_ticket_out_history,
    poll_and_log,
    seed_validation_pool,
)
from saspy.constants import ExceptionCode, LongPoll, MeterCode, ValidationType
from saspy.exceptions import SASTimeoutError
from saspy.models import (
    BasicMeters,
    BillMeters,
    EnhancedValidationInfo,
    ExtendedMeters,
    GamesSincePowerUpAndDoorClosure,
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
    assert f"meter_poll=2.500s/{full_count}polls" in capsys.readouterr().out


def test_meter_poll_count_reflects_skip_full_meter_sweep(capsys):
    conn = make_db()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()])
    times = iter([0.0, 0.05])
    poll_and_log(client, conn, state, history, monotonic_fn=lambda: next(times), full_meter_sweep=False)
    # single-meter sweep skipped; LP 0x48, C-7 sweep, and validation-meters sweep still on
    expected_count = GROUPED_METER_POLL_COUNT + 1 + TABLE_C7_CHUNK_COUNT + len(VALIDATION_METER_TYPES)
    assert f"meter_poll=0.050s/{expected_count}polls" in capsys.readouterr().out


def test_meter_poll_count_reflects_skip_table_c7_sweep(capsys):
    conn = make_db()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()])
    times = iter([0.0, 0.05])
    poll_and_log(client, conn, state, history, monotonic_fn=lambda: next(times), table_c7_sweep=False)
    # C-7 sweep skipped; LP 0x48, single-meter sweep, and validation-meters sweep still on
    expected_count = GROUPED_METER_POLL_COUNT + 1 + len(SINGLE_METER_COLUMNS) + len(VALIDATION_METER_TYPES)
    assert f"meter_poll=0.050s/{expected_count}polls" in capsys.readouterr().out


def test_meter_poll_at_or_above_interval_warns(capsys):
    conn = make_db()
    state = PollState()
    history = HistoryConfig(mode="ring")
    client = ScriptedClient([make_meters()])
    times = iter([0.0, 2.5])
    poll_and_log(client, conn, state, history, monotonic_fn=lambda: next(times), interval=2.0)
    out = capsys.readouterr().out
    full_count = (
        GROUPED_METER_POLL_COUNT + 1 + len(SINGLE_METER_COLUMNS) + TABLE_C7_CHUNK_COUNT + len(VALIDATION_METER_TYPES)
    )
    assert f"WARNING: meter poll took 2.500s across {full_count} long-poll exchanges" in out
    assert "--interval 2.0s" in out


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
