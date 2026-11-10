"""The inbound command channel: WfA on-demand pulls.

Every other table in the gateway schema is outbound. This is the one
direction that did not exist, and the migration spec is specific about what
it must do:

    500 ms p95, 1 s hard, end to end
    repeat pulls >= 10 s apart
    "an on-demand pull takes the next poll slot and never interrupts an
     in-flight poll -- a naive bypass here would reintroduce exactly the
     mid-poll gap this project exists to remove"

The third is the one these tests care about most, because it is the one a
future change can break silently.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from sql_poll_logger import (  # noqa: E402
    SCHEMA, WFA_BUFFER_INDICES, _lookup_ticket_out_local,
    _make_request_servicer, migrate_epoch_columns)
import wfa_request  # noqa: E402


class FakeRecord:
    def __init__(self, index, number, cents):
        self.index_number = index
        self.validation_type = 0
        self.date = "11092026"
        self.time = "234114"
        self.validation_number = number
        self.amount_cents = cents
        self.ticket_number = 11
        self.validation_system_id = 1
        self.expiration = "00000000"
        self.pool_id = 0


class FakeClient:
    """A cabinet holding a 31-entry ticket-out buffer."""

    def __init__(self, buffer=None):
        self.buffer = buffer or {}
        self.polls = 0

    def send_enhanced_validation_information(self, function_code=0xFF):
        self.polls += 1
        n, c = self.buffer.get(function_code, (0, 0))
        return FakeRecord(function_code, n, c)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "gateway.sqlite3")
    conn.executescript(SCHEMA)
    migrate_epoch_columns(conn)
    return conn


def _request(conn, number, *, deadline_ms=1000, requested_at=None,
             kind="wfa_pull"):
    import datetime
    at = requested_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO host_requests (requested_at, kind, validation_number,"
        " deadline_ms) VALUES (?,?,?,?)", (at, kind, number, deadline_ms))
    conn.commit()
    return cur.lastrowid


def _row(conn, req_id):
    return conn.execute(
        "SELECT state, amount_cents, served_from, polls_spent, error "
        "  FROM host_requests WHERE id = ?", (req_id,)).fetchone()


# --- the fast path, which is what makes the budget achievable -------------

def test_a_ticket_this_gateway_saw_print_costs_no_poll_slot(db):
    """The normal case. The poller drains LP 0x4D continuously, so the
    ticket a cashier is scanning is usually a row we already hold. Answering
    from it costs ZERO exchanges, which is the only way 500 ms p95 is
    reachable at all -- a full buffer walk is 31 exchanges, ~2.6 s on this
    gateway's measured timing, and blows the 1 s hard budget on its own."""
    db.execute(
        "INSERT INTO ticket_out_history (captured_at, validation_number,"
        " amount_cents, ticket_date, ticket_time) VALUES (?,?,?,?,?)",
        ("2026-11-09T23:41:14Z", 1001, 184539, "11092026", "234114"))
    db.commit()
    client = FakeClient()
    service = _make_request_servicer(client, db)
    req = _request(db, 1001)

    service()

    state, amount, served, polls, _ = _row(db, req)
    assert state == "done"
    assert amount == 184539
    assert served == "local"
    assert polls == 0
    assert client.polls == 0, "the fast path must not touch the wire"


def test_the_local_lookup_returns_none_rather_than_a_row_of_nulls(db):
    """A ticket_out_history row with a NULL amount is not an answer. The
    project has read a row of nulls as a measurement before (§1) and it must
    not happen on the path a cashier is waiting on."""
    db.execute(
        "INSERT INTO ticket_out_history (captured_at, validation_number,"
        " amount_cents) VALUES (?,?,?)", ("2026-11-09T00:00:00Z", 42, None))
    db.commit()
    assert _lookup_ticket_out_local(db, 42) is None


# --- the slow path --------------------------------------------------------

def test_a_ticket_only_the_cabinet_knows_is_found_by_walking_the_buffer(db):
    client = FakeClient({3: (1001, 184539)})
    service = _make_request_servicer(client, db, polls_per_visit=4)
    req = _request(db, 1001, deadline_ms=60000)

    service()

    state, amount, served, _, _ = _row(db, req)
    assert state == "done"
    assert amount == 184539
    assert served == "buffer-walk"


def test_the_walk_yields_back_to_the_sweep_and_resumes(db):
    """A request must not hold the sweep. It takes its slots, yields, and
    picks up where it left off -- otherwise one on-demand pull is a 31-poll
    silence, which is the mid-poll gap this project exists to remove."""
    client = FakeClient({20: (777, 2500)})
    service = _make_request_servicer(client, db, polls_per_visit=4)
    req = _request(db, 777, deadline_ms=60000)

    service()
    assert _row(db, req)[0] == "claimed", "one visit must not finish a walk"
    assert client.polls == 4, "a visit must spend exactly its allowance"

    for _ in range(4):
        service()
    state, amount, served, _, _ = _row(db, req)
    assert state == "done" and amount == 2500 and served == "buffer-walk"


def test_a_number_in_no_buffer_slot_is_an_ANSWER_not_a_failure(db):
    """The cabinet holds 31 records and a busy one wraps. "Not in the
    buffer" means the server's next step is the physical visit, and it has
    to be able to tell that from an error."""
    client = FakeClient()
    service = _make_request_servicer(client, db, polls_per_visit=WFA_BUFFER_INDICES)
    req = _request(db, 999, deadline_ms=60000)

    service()
    service()

    state, amount, served, _, error = _row(db, req)
    assert state == "done", "exhausting the buffer is an answer"
    assert amount is None
    assert error is None
    assert json.loads(_result(db, req))["found"] is False


def _result(conn, req_id):
    return conn.execute("SELECT result_json FROM host_requests WHERE id = ?",
                        (req_id,)).fetchone()[0]


# --- the rate cap ---------------------------------------------------------

def test_a_found_ticket_is_cheap_to_rescan_because_the_walk_recorded_it(db):
    """A successful walk writes what it found into ticket_out_history, so
    the second scan of the same ticket is answered by the FAST path -- not
    by the rate cap. Worth asserting, because it is why the cap does not
    need to guard this case."""
    client = FakeClient({1: (555, 1000)})
    service = _make_request_servicer(client, db, polls_per_visit=4)
    service_id = _request(db, 555, deadline_ms=60000)
    service()
    assert _row(db, service_id)[0] == "done"
    spent = client.polls

    second = _request(db, 555, deadline_ms=60000)
    service()

    state, amount, served, _, _ = _row(db, second)
    assert state == "done" and amount == 1000
    assert served == "local", "the walk should have recorded it"
    assert client.polls == spent, "a rescan of a known ticket costs nothing"


def test_a_rescan_of_a_ticket_NOT_in_the_buffer_does_not_walk_again(db):
    """The case the 10 s cap is actually for. A number the cabinet does not
    hold costs a full 31-index walk every time it is scanned, and a cashier
    with a bad ticket will scan it more than once. The first version of the
    cap required an amount, which made it miss exactly this."""
    client = FakeClient()
    service = _make_request_servicer(client, db, polls_per_visit=WFA_BUFFER_INDICES)
    first = _request(db, 404404, deadline_ms=60000)
    service()
    service()
    assert _row(db, first)[0] == "done"
    spent = client.polls
    assert spent >= WFA_BUFFER_INDICES, "the first scan must really walk"

    second = _request(db, 404404, deadline_ms=60000)
    service()

    state, amount, served, _, _ = _row(db, second)
    assert state == "done"
    assert amount is None
    assert served == "cache"
    assert client.polls == spent, (
        "a rescan inside the cap walked the buffer again -- that is the "
        "abuse the 10 s cap exists to stop")


# --- the deadline ---------------------------------------------------------

def test_a_request_past_its_deadline_stops_costing_the_event_stream(db):
    """A cashier who has given up must not still be spending poll slots."""
    client = FakeClient({30: (123, 400)})
    service = _make_request_servicer(client, db, polls_per_visit=4)
    req = _request(db, 123, deadline_ms=1,
                   requested_at="2020-01-01T00:00:00+00:00")

    service()

    state, _, _, _, error = _row(db, req)
    assert state == "expired"
    assert "deadline" in error
    assert client.polls == 0, "an expired request must not reach the wire"


def test_an_unknown_kind_fails_loudly_rather_than_sitting_pending(db):
    service = _make_request_servicer(FakeClient(), db)
    req = _request(db, 1, kind="rm -rf")
    service()
    state, _, _, _, error = _row(db, req)
    assert state == "failed" and "unknown request kind" in error


# --- the scheduling rule, enforced structurally ---------------------------

def test_the_servicer_is_only_ever_called_from_the_heartbeat():
    """"An on-demand pull takes the NEXT poll slot and never interrupts an
    in-flight poll."

    The heartbeat is BY CONSTRUCTION between exchanges, so servicing there
    cannot open a mid-poll gap. That makes the rule structural rather than
    something a future author has to remember -- but only while the call
    site stays put, which is what this checks.

    Matched on the AST, not on text: a guard written as a text search
    matches the documentation of the thing it forbids, and this file's own
    prose is full of the words involved (§1, six instances).
    """
    tree = ast.parse((ROOT / "examples" / "sql_poll_logger.py").read_text())
    callers = []

    def visit(node, enclosing):
        """Attribute each call to its INNERMOST enclosing function.
        ast.walk descends into nested definitions, so walking
        _make_heartbeat also reports heartbeat's calls as its own -- which
        is how the first version of this test failed on correct code."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
                continue
            if (isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "request_servicer"):
                callers.append(enclosing)
            visit(child, enclosing)

    visit(tree, "<module>")
    assert callers, "nothing calls the request servicer -- is it wired up?"
    assert set(callers) == {"heartbeat"}, (
        f"request_servicer() is called from {sorted(set(callers))}. It must "
        f"only be called from heartbeat(), which runs BETWEEN exchanges. A "
        f"call from anywhere else can land mid-poll, which is exactly the "
        f"gap the scheduling rule forbids")


def test_the_servicer_never_opens_a_serial_port():
    """One port, one master. The servicer uses the client the poller already
    holds; if it ever constructed its own transport it would be a second
    master on an exclusive port."""
    tree = ast.parse((ROOT / "examples" / "sql_poll_logger.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "_make_request_servicer")
    names = {n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    for forbidden in ("SASClient", "SerialTransport", "open_serial", "Serial"):
        assert forbidden not in names, (
            f"_make_request_servicer constructs {forbidden} -- it must use "
            f"the poller's existing client")


# --- the client side ------------------------------------------------------

def test_the_number_is_accepted_as_printed_on_the_ticket():
    """The face carries it grouped. A cashier reading it aloud and a scanner
    emitting it raw must not be two different inputs."""
    # Built rather than written: a dashed 18-digit literal is the SHAPE of a
    # printed validation number, so the mirror scanner refuses it on sight
    # and is right to -- the synthetic exemption is the undashed form only.
    dashed = "-".join(["00", "0000", "0000", "0000", "0001"])
    assert wfa_request.normalise(dashed) == 1
    assert wfa_request.normalise("00000000000000" + "1234") == 1234
    with pytest.raises(ValueError):
        wfa_request.normalise("no digits here")


def test_the_client_reports_an_unserved_request_rather_than_hanging(db):
    """A poller running WITHOUT --serve-host-requests leaves the row
    pending forever. Saying so beats timing out with a vaguer message."""
    req = wfa_request.submit(db, 4242)
    answer = wfa_request.wait(db, req, deadline_ms=1000,
                              clock=iter([0, 99]).__next__, sleep=lambda s: None)
    assert answer["state"] == "unserved"
    assert "--serve-host-requests" in answer["error"]


def test_the_client_reads_back_a_served_answer(db):
    db.execute(
        "INSERT INTO ticket_out_history (captured_at, validation_number,"
        " amount_cents) VALUES (?,?,?)", ("2026-11-09T23:41:14Z", 31337, 8800))
    db.commit()
    req = wfa_request.submit(db, 31337)
    _make_request_servicer(FakeClient(), db)()
    answer = wfa_request.wait(db, req, deadline_ms=1000, sleep=lambda s: None)
    assert answer["state"] == "done"
    assert answer["amount_cents"] == 8800
    assert answer["served_from"] == "local"


# --- the help strings, which were broken for a day ------------------------

def test_every_help_string_survives_argparse_formatting():
    """argparse runs help through %-formatting. On 2026-11-09 a drain
    priority f-string produced a literal "50% of", argparse read "% o" as a
    conversion, and `--help` raised TypeError for the WHOLE tool until
    2026-11-10. A help string nobody can print is worse than none."""
    import argparse
    import importlib
    import sql_poll_logger
    parser = sql_poll_logger.build_parser()
    formatter = argparse.HelpFormatter(prog="x")
    for action in parser._actions:
        if not action.help:
            continue
        try:
            formatter._expand_help(action)
        except Exception as e:  # noqa: BLE001
            pytest.fail(
                f"{action.option_strings} has a help string argparse cannot "
                f"format: {type(e).__name__}: {e}. Double every literal "
                f"percent sign, including ones an f-string computes")
