"""--server-ticket-authority: ticket-in asked of the server (spec §9).

The lab file made the gateway an authority on what tickets are worth. This
path is the opposite and the tests are mostly about what the poller must
NOT do: not look in its own pool, not use the ticket's own amount, not act
on an answer that arrived after it had already refused, and -- without the
flag -- not do anything it did not do before.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))

from saspy.bcd import encode_bcd
from saspy.exceptions import SASTimeoutError
from sql_poll_logger import (PollState, SCHEMA, VALIDATION_QUERIES_SCHEMA,
                             _ask_server_ticket_in, _capture_ticket_in,
                             _capture_ticket_in_completion, _dispatch_exception,
                             build_parser, migrate_epoch_columns)

# Synthetic by shape: fourteen zeros and four digits is no printed ticket.
NUMBER = "000000000000000002"
BCD9 = encode_bcd(int(NUMBER), 9)
NOW = "2026-11-11T00:00:00+00:00"


def _db(link=True):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    migrate_epoch_columns(conn)
    if link:
        conn.executescript(VALIDATION_QUERIES_SCHEMA)
    return conn


def _ticket(cents=0, parsing_code=0x00, data=BCD9):
    return SimpleNamespace(ticket_in_escrow=True, amount_cents=cents,
                           parsing_code=parsing_code, validation_data=data)


class _Client:
    def __init__(self, ticket=None, status=0x40, fail=False, completion=0x00):
        self._ticket = ticket or _ticket()
        self.redeemed, self._status, self._fail = [], status, fail
        self._completion = completion

    def send_ticket_validation_data(self):
        return self._ticket

    def redeem_ticket(self, **kw):
        self.redeemed.append(kw)
        if self._fail:
            raise SASTimeoutError("no reply")
        return SimpleNamespace(machine_status=self._status)

    def redeem_ticket_status(self):
        return SimpleNamespace(machine_status=self._completion, amount_cents=0,
                               parsing_code=0, validation_data=b"")


def _state(**kw):
    return PollState(server_ticket_authority=True, **kw)


class _Clock:
    """Deterministic time. Each sleep() advances the clock and, once, runs
    `on_sleep` -- which is how a test plays the gateway client writing an
    answer from the other process while the poller waits."""

    def __init__(self, on_sleep=None):
        self.now, self.on_sleep, self.sleeps = 100.0, on_sleep, 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds
        if self.on_sleep is not None:
            hook, self.on_sleep = self.on_sleep, None
            hook()


def _answer(conn, decision="allow", code=0x00, cents=2500, reason="Open",
            expiration=0, pool_id=0):
    """Exactly the statement gateway/client.py runs, condition included."""
    def write():
        conn.execute(
            "UPDATE validation_queries SET state = 'answered', decision = ?, "
            "transfer_code = ?, amount_cents = ?, restricted_expiration = ?, "
            "pool_id = ?, reason = ? WHERE state = 'pending'",
            (decision, code, cents, expiration, pool_id, reason))
        conn.commit()
    return write


def _ask(conn, client, state, clock):
    return _ask_server_ticket_in(client, conn, client._ticket, state, NOW,
                                 clock=clock, sleep=clock.sleep)


def _query(conn):
    return conn.execute(
        "SELECT state, validation_number, outcome, machine_status "
        "FROM validation_queries ORDER BY id DESC LIMIT 1").fetchone()


# --- the default must not move ------------------------------------------

def test_without_the_flag_no_table_no_query_and_no_redemption():
    conn, client = _db(link=False), _Client()
    _capture_ticket_in(client, conn, NOW, PollState())
    assert client.redeemed == []
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = "
                            "'validation_queries'").fetchone()
    assert conn.execute("SELECT authority_decision FROM ticket_in_events"
                        ).fetchone() == (None,)


def test_without_the_flag_a_completion_touches_no_link_table():
    conn = _db(link=False)
    _dispatch_exception(_Client(), conn, NOW, 0x68, state=PollState())
    assert conn.execute("SELECT COUNT(*) FROM ticket_in_completions"
                        ).fetchone()[0] == 1


def test_the_flag_defaults_off():
    args = build_parser().parse_args(["gateway.ini"])
    assert args.server_ticket_authority is False


# --- the server allows ----------------------------------------------------

def test_allow_sends_the_servers_amount_not_the_tickets():
    """§9.1: "the server's ledger figure is authoritative; the ticket's own
    reported value is never used.\""""
    conn, client = _db(), _Client(_ticket(cents=999))
    clock = _Clock(_answer(conn, cents=2500))
    note = _ask(conn, client, _state(), clock)
    assert client.redeemed == [dict(
        transfer_code=0x00, transfer_amount_cents=2500,
        validation_number=int(NUMBER), restricted_expiration=0, pool_id=0,
        parsing_code=0x00)]
    assert "SERVER ALLOW: 2500 cents" in note and "machine reported 999" in note
    assert _query(conn)[:2] == ("acted", NUMBER)
    assert _query(conn)[3] == 0x40


def test_a_restricted_allow_carries_its_pool_and_expiration():
    conn, client = _db(), _Client()
    clock = _Clock(_answer(conn, code=0x01, cents=500, expiration=12312026,
                           pool_id=7))
    _ask(conn, client, _state(), clock)
    assert client.redeemed[0]["transfer_code"] == 0x01
    assert client.redeemed[0]["restricted_expiration"] == 12312026
    assert client.redeemed[0]["pool_id"] == 7


def test_the_gateway_does_not_consult_its_own_pool():
    """§7.1: "the pool is a routing and issuing mechanism, not a validity
    list." A ticket this gateway printed is in validation_pool as
    'assigned' with an amount -- and with no server answer it is REFUSED,
    because only the server knows whether the cage already paid it."""
    conn, client = _db(), _Client()
    conn.execute("INSERT INTO validation_pool (validation_number, "
                 "validation_system_id, status, assigned_amount_cents) "
                 "VALUES (?, 0, 'assigned', 2500)", (int(NUMBER),))
    conn.commit()
    note = _ask(conn, client, _state(), _Clock())
    assert client.redeemed[0]["transfer_code"] == 0x80
    assert client.redeemed[0]["transfer_amount_cents"] == 0
    assert "REFUSED LOCALLY" in note
    assert conn.execute("SELECT status FROM validation_pool").fetchone() == (
        "assigned",), "the pool row must not be touched from here"


# --- the server rejects ---------------------------------------------------

@pytest.mark.parametrize("code", [0x80, 0x85, 0x86, 0x87])
def test_reject_relays_the_servers_code_with_no_amount(code):
    conn, client = _db(), _Client(status=0x80)
    clock = _Clock(_answer(conn, decision="reject", code=code, cents=0,
                           reason="Expired"))
    note = _ask(conn, client, _state(), clock)
    assert client.redeemed[0]["transfer_code"] == code
    assert client.redeemed[0]["transfer_amount_cents"] == 0
    assert f"SERVER REJECT 0x{code:02X} (Expired)" in note


@pytest.mark.parametrize("decision, code, cents", [
    ("allow", 0x00, 0),          # allow, worth nothing
    ("allow", 0x00, -5),
    ("allow", 0x07, 2500),       # allow, with a code that is not a ticket kind
    ("allow", 0x80, 2500),       # allow, with a REJECT code
    ("reject", 0x00, 0),         # reject, with an ALLOW code -- would pay
    ("reject", 0x01, 2500),
    ("maybe", 0x00, 2500),
])
def test_an_answer_that_is_not_a_thing_a_server_may_say_is_refused(
        decision, code, cents):
    """The frame was authentic, so this is a server defect rather than an
    attack -- and it still must not reach the cabinet as a payment."""
    conn, client = _db(), _Client()
    clock = _Clock(_answer(conn, decision=decision, code=code, cents=cents))
    _ask(conn, client, _state(), clock)
    assert client.redeemed[0]["transfer_code"] == 0x80
    assert client.redeemed[0]["transfer_amount_cents"] == 0


# --- nobody answers (§9.2) -------------------------------------------------

def test_no_answer_refuses_with_0x80_and_starts_the_backoff():
    conn, client, state = _db(), _Client(), _state(ticket_query_timeout_s=2.0)
    clock = _Clock()
    note = _ask(conn, client, state, clock)
    assert client.redeemed == [dict(transfer_code=0x80, transfer_amount_cents=0,
                                    validation_number=int(NUMBER),
                                    parsing_code=0x00)]
    assert "no answer from the server in 2.0s" in note
    assert _query(conn)[0] == "timeout"
    assert clock.now - 100.0 == pytest.approx(2.0, abs=0.05), "waited the timeout"
    assert state.ticket_query_backoff_until == pytest.approx(clock.now + 30.0)


def test_during_the_backoff_a_ticket_is_refused_without_a_query_or_a_wait():
    conn, client, state = _db(), _Client(), _state()
    clock = _Clock()
    _ask(conn, client, state, clock)
    sleeps = clock.sleeps
    note = _ask(conn, client, state, clock)
    assert "query backoff" in note
    assert clock.sleeps == sleeps, "the backoff must not wait"
    assert len(client.redeemed) == 2 and client.redeemed[1]["transfer_code"] == 0x80
    states = [r[0] for r in conn.execute(
        "SELECT state FROM validation_queries ORDER BY id")]
    assert states == ["timeout", "backoff"]
    # 'backoff' is not 'pending', so the gateway client never sends it.
    assert conn.execute("SELECT COUNT(*) FROM validation_queries WHERE "
                        "state = 'pending'").fetchone()[0] == 0


def test_the_backoff_ends_and_an_answer_clears_it():
    conn, client, state = _db(), _Client(), _state()
    clock = _Clock()
    _ask(conn, client, state, clock)
    clock.now += 31.0
    clock.on_sleep = _answer(conn)
    note = _ask(conn, client, state, clock)
    assert "SERVER ALLOW" in note
    assert state.ticket_query_backoff_until is None


def test_a_late_answer_cannot_land_on_a_query_already_refused():
    """The client's UPDATE is conditional on state = 'pending'. Without
    that, an ALLOW arriving after the timeout would sit in the row."""
    conn, client = _db(), _Client()
    _ask(conn, client, _state(), _Clock())
    _answer(conn)()                                   # arrives too late
    assert _query(conn)[0] == "timeout"
    assert conn.execute("SELECT decision FROM validation_queries").fetchone() == (
        None,)
    assert len(client.redeemed) == 1 and client.redeemed[0]["transfer_code"] == 0x80


def test_the_per_window_cap_refuses_without_asking():
    conn, client = _db(), _Client()
    state = _state(ticket_query_cap=2, ticket_query_cap_window_s=60.0)
    for _ in range(2):
        clock = _Clock(_answer(conn))
        _ask(conn, client, state, clock)
    note = _ask(conn, client, state, _Clock())
    assert "query cap" in note and client.redeemed[-1]["transfer_code"] == 0x80


# --- shapes it will not guess at -------------------------------------------

def test_a_parsing_code_it_cannot_read_is_not_answered_and_not_asked():
    conn, client = _db(), _Client(_ticket(parsing_code=0x01))
    note = _ask(conn, client, _state(), _Clock())
    assert note.startswith("NOT ANSWERED") and client.redeemed == []
    assert conn.execute("SELECT COUNT(*) FROM validation_queries").fetchone()[0] == 0


def test_a_failed_lp71_is_recorded_and_does_not_raise():
    conn, client = _db(), _Client(fail=True)
    note = _ask(conn, client, _state(), _Clock(_answer(conn)))
    assert "LP 0x71 failed" in note
    assert _query(conn)[0] == "acted" and _query(conn)[3] is None


# --- wiring ----------------------------------------------------------------

def test_exception_0x67_goes_to_the_server_path_and_is_written_down():
    conn = _db()
    client, state = _Client(), _state(ticket_query_timeout_s=0.1)
    _dispatch_exception(client, conn, NOW, 0x67, state=state)
    decision = conn.execute("SELECT authority_decision FROM ticket_in_events"
                            ).fetchone()[0]
    assert "REFUSED LOCALLY 0x80" in decision


@pytest.mark.parametrize("completion, stamped", [
    (0x00, 0x00),      # redeemed
    (0x80, 0x80),      # rejected
    (0x40, None),      # pending: not a completion
    (0x20, None),      # waiting for long poll 71
    (0xFF, None),      # no cycle since last poll
])
def test_a_completion_is_stamped_on_the_query_it_ends(completion, stamped):
    conn = _db()
    client, state = _Client(completion=completion), _state()
    _ask(conn, client, state, _Clock(_answer(conn)))
    _dispatch_exception(client, conn, NOW, 0x68, state=state)
    assert conn.execute("SELECT completion_status FROM validation_queries"
                        ).fetchone() == (stamped,)


def test_a_completion_is_never_stamped_on_a_question_still_being_asked():
    conn = _db()
    conn.execute("INSERT INTO validation_queries (asked_at, validation_number) "
                 "VALUES (?, ?)", (NOW, NUMBER))
    _capture_ticket_in_completion(_Client(completion=0x00), conn, NOW,
                                  stamp_query=True)
    assert conn.execute("SELECT completion_status FROM validation_queries"
                        ).fetchone() == (None,)


def test_the_two_authorities_cannot_be_combined(monkeypatch, capsys, tmp_path):
    import sql_poll_logger
    monkeypatch.setattr(sys, "argv", [
        "sql_poll_logger.py", "gateway.ini", "--server-ticket-authority",
        "--lab-ticket-authority", str(tmp_path / "t.json")])
    assert sql_poll_logger.main() == 2
    assert "cannot be combined" in capsys.readouterr().err


@pytest.mark.parametrize("timeout", ["0.01", "4.5", "30"])
def test_a_timeout_outside_the_poll_ceiling_is_refused(monkeypatch, capsys, timeout):
    import sql_poll_logger
    monkeypatch.setattr(sys, "argv", [
        "sql_poll_logger.py", "gateway.ini", "--server-ticket-authority",
        "--ticket-query-timeout", timeout])
    assert sql_poll_logger.main() == 2
    assert "outside 0.1-4.0 s" in capsys.readouterr().err
