"""Answering ticket-in from inside the poller, and the guards on doing so.

One port, one master: examples/ticket_lab_authority.py cannot run while a
soak holds /dev/ttyS0, so ticket-in was the one path a soak could never
exercise. Every rte_reconcile run so far has printed [NO DATA] for bill and
ticket continuity for exactly that reason.

What matters here is that the default does not move. Without a ticket file
this tool must stay as read-only on ticket-in as it has always been: a
poller that decides what a ticket is worth is a cash-dispensing defect, and
the refusal is the correct behaviour for a gateway, not an omission.
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
from sql_poll_logger import PollState, SCHEMA, _capture_ticket_in, migrate_epoch_columns

# 18 digits, Table 15.11b parsing code 00. Deliberately synthetic: fourteen
# zeros and four digits is a shape no printed ticket has, so a real validation
# number can never hide here. A real one did, until
# deploy/mirror_to_public.py found it in this mirrored file.
NUMBER = "000000000000000002"
BCD9 = encode_bcd(int(NUMBER), 9)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    migrate_epoch_columns(conn)
    return conn


def _ticket(parsing_code=0x00, data=BCD9, cents=500, escrow=True):
    return SimpleNamespace(ticket_in_escrow=escrow, amount_cents=cents,
                           parsing_code=parsing_code, validation_data=data)


class _Client:
    def __init__(self, ticket, status=0x01):
        self._ticket = ticket
        self.redeemed = []
        self._status = status

    def send_ticket_validation_data(self):
        return self._ticket

    def redeem_ticket(self, **kw):
        self.redeemed.append(kw)
        return SimpleNamespace(machine_status=self._status)


def _authority(kind="restricted", cents=500):
    return {NUMBER: {"printed": NUMBER, "kind": kind, "cents": cents,
                     "pool_id": 0, "expiration": 0}}


def _row(conn):
    return conn.execute(
        "SELECT amount_cents, authority_decision FROM ticket_in_events").fetchone()


# --- the default must not move ------------------------------------------

def test_without_a_ticket_file_nothing_is_redeemed():
    """The whole point of the guard. §15.12 then has the machine return the
    ticket after its own thirty seconds, which is correct for a gateway."""
    conn, client = _db(), _Client(_ticket())
    _capture_ticket_in(client, conn, "now", PollState())
    assert client.redeemed == []
    assert _row(conn) == (500, None)


def test_a_state_of_none_is_also_read_only():
    """_capture_ticket_in is called from paths that may not carry state."""
    conn, client = _db(), _Client(_ticket())
    _capture_ticket_in(client, conn, "now", None)
    assert client.redeemed == []


# --- with a file -------------------------------------------------------

def test_a_known_ticket_is_honoured_and_recorded():
    conn = _db()
    client = _Client(_ticket())
    state = PollState()
    state.ticket_authority = _authority()
    _capture_ticket_in(client, conn, "now", state)

    assert len(client.redeemed) == 1
    sent = client.redeemed[0]
    assert sent["validation_number"] == int(NUMBER), \
        "the number must be the BCD value, not the hex string"
    assert sent["transfer_amount_cents"] == 500
    amount, decision = _row(conn)
    assert "honoured as restricted for 500 cents" in decision
    assert "0x01" in decision


def test_an_unknown_ticket_is_denied_not_ignored():
    """Silence would leave the machine holding the ticket for its full
    thirty seconds and tell the player nothing. 0x82 is "not in system"."""
    conn = _db()
    client = _Client(_ticket(), status=0x80)
    state = PollState()
    state.ticket_authority = {}           # file loaded, number absent
    _capture_ticket_in(client, conn, "now", state)

    assert len(client.redeemed) == 1
    assert client.redeemed[0]["transfer_code"] == 0x82
    assert client.redeemed[0]["transfer_amount_cents"] == 0
    assert "DENIED 0x82 not in file" in _row(conn)[1]


def test_an_amount_mismatch_is_recorded_not_silently_honoured():
    conn = _db()
    client = _Client(_ticket(cents=999))
    state = PollState()
    state.ticket_authority = _authority(cents=500)
    _capture_ticket_in(client, conn, "now", state)
    assert "machine reported 999" in _row(conn)[1]


# --- the shapes it must refuse to answer --------------------------------

def test_a_parsing_code_it_cannot_read_is_not_answered():
    """Guessing at an unknown encoding means answering a ticket we have not
    identified. Table 15.11b's 00 is the only shape this file maps."""
    conn = _db()
    client = _Client(_ticket(parsing_code=0x01))
    state = PollState()
    state.ticket_authority = _authority()
    _capture_ticket_in(client, conn, "now", state)

    assert client.redeemed == [], "an unreadable ticket must not be redeemed"
    assert "NOT ANSWERED" in _row(conn)[1]


def test_a_short_validation_field_is_not_answered():
    conn = _db()
    client = _Client(_ticket(data=b"\x01\x02\x03"))
    state = PollState()
    state.ticket_authority = _authority()
    _capture_ticket_in(client, conn, "now", state)
    assert client.redeemed == []
    assert "NOT ANSWERED" in _row(conn)[1]


def test_nothing_in_escrow_is_recorded_rather_than_returned_from_silently():
    """This used to return with no row, no log line and no error, on the
    grounds that it was "a rare, harmless race". It was the single most
    visible defect in the whole tool.

    The cabinet raises 0x67 when the validator TAKES the ticket, before it
    reaches the escrow position. A poller answering within the same cycle
    reads LP 0x70 too early, sees no escrow, and declines to answer at all --
    so the machine waits out §15.12's thirty seconds and hands the ticket
    back. The player sees an unexplained rejection; the database shows
    nothing happened. Observed twice on 2026-11-01, reproducibly.

    The ticket is still not redeemed -- there is genuinely nothing to redeem.
    What changes is that the attempt leaves a trace.
    """
    conn = _db()
    client = _Client(_ticket(escrow=False))
    state = PollState()
    state.ticket_authority = _authority()
    _capture_ticket_in(client, conn, "now", state, escrow_reads=3)
    assert client.redeemed == [], "nothing in escrow is nothing to pay for"
    rows = conn.execute(
        "SELECT authority_decision FROM ticket_in_events").fetchall()
    assert len(rows) == 1, "the attempt must be recoverable afterwards"
    assert "no ticket in escrow" in rows[0][0]
    assert "3 read(s)" in rows[0][0]


def test_a_ticket_that_reaches_escrow_on_a_later_read_is_honoured():
    """The whole point of retrying: the validator is still moving the ticket
    when 0x67 fires, and a moment later it is there."""
    class _LateEscrow(_Client):
        def __init__(self, ticket, *, ready_on: int):
            super().__init__(ticket)
            self._late = ticket
            self._reads = 0
            self._ready_on = ready_on

        def send_ticket_validation_data(self):
            self._reads += 1
            if self._reads < self._ready_on:
                return _ticket(escrow=False)
            return self._late

    conn = _db()
    state = PollState()
    state.ticket_authority = _authority()
    client = _LateEscrow(_ticket(), ready_on=3)
    _capture_ticket_in(client, conn, "now", state, escrow_reads=4)
    assert client.redeemed, "the ticket was there by read 3 and must be paid"
    assert "honoured" in _row(conn)[1]


def test_a_failed_redeem_is_recorded_and_does_not_raise():
    class _Fails(_Client):
        def redeem_ticket(self, **kw):
            raise SASTimeoutError("no answer")

    conn = _db()
    state = PollState()
    state.ticket_authority = _authority()
    _capture_ticket_in(_Fails(_ticket()), conn, "now", state)
    assert "LP 0x71 failed" in _row(conn)[1]


def test_the_decision_is_stored_because_a_denial_moves_no_meter():
    """A denied ticket changes nothing on the machine, so if the decision is
    not written down it cannot be recovered from the meters afterwards."""
    conn = _db()
    state = PollState()
    state.ticket_authority = {}
    _capture_ticket_in(_Client(_ticket(), status=0x80), conn, "now", state)
    assert _row(conn)[1] is not None


# --- tickets this gateway printed itself --------------------------------
#
# The two halves used different books: ticket-OUT draws a number from
# validation_pool, ticket-IN looked only in lab_tickets.json. So a ticket the
# cabinet printed thirty seconds earlier came back DENIED 0x82 "not in
# system" -- the gateway refusing to recognise its own issuance. Observed on
# 2026-11-05 as "tickets are being rejected".

NUMBER_OUT = 424242
BCD_OUT = encode_bcd(NUMBER_OUT, 9)


def _issue(conn, number=NUMBER_OUT, status="assigned", cents=3190):
    conn.execute(
        "INSERT INTO validation_pool (validation_number, validation_system_id, "
        "status, assigned_amount_cents) VALUES (?, ?, ?, ?)",
        (number, 0, status, cents))
    conn.commit()


def _status(conn, number=NUMBER_OUT):
    return conn.execute(
        "SELECT status FROM validation_pool WHERE validation_number = ?",
        (number,)).fetchone()[0]


def test_a_ticket_this_gateway_printed_is_honoured_for_the_amount_it_issued():
    conn = _db()
    _issue(conn)
    client = _Client(_ticket(data=BCD_OUT, cents=3190))
    state = PollState()
    state.ticket_authority = {}          # not in the lab file, and never will be
    _capture_ticket_in(client, conn, "now", state)

    assert len(client.redeemed) == 1, "the gateway must recognise its own ticket"
    assert client.redeemed[0]["transfer_amount_cents"] == 3190
    assert client.redeemed[0]["validation_number"] == NUMBER_OUT
    assert _status(conn) == "redeemed"
    assert "issued by this gateway" in _row(conn)[1]


def test_the_same_ticket_cannot_be_redeemed_twice():
    """A ticket is redeemable once. Without this the same voucher credits the
    machine every time it is fed back in."""
    conn = _db()
    _issue(conn, status="redeemed")
    client = _Client(_ticket(data=BCD_OUT))
    state = PollState()
    state.ticket_authority = {}
    _capture_ticket_in(client, conn, "now", state)

    assert len(client.redeemed) == 1
    assert client.redeemed[0]["transfer_code"] == 0x82
    assert client.redeemed[0]["transfer_amount_cents"] == 0
    assert "already 'redeemed'" in _row(conn)[1]


def test_a_number_still_available_is_not_a_printed_ticket():
    """'available' means issued to nobody. Honouring it would credit a
    number that was never on a ticket at all."""
    conn = _db()
    _issue(conn, status="available", cents=0)
    client = _Client(_ticket(data=BCD_OUT))
    state = PollState()
    state.ticket_authority = {}
    _capture_ticket_in(client, conn, "now", state)
    assert client.redeemed[0]["transfer_code"] == 0x82
    assert _status(conn) == "available"


def test_a_machine_refusal_gives_the_number_back_rather_than_burning_it():
    """The player is still holding the ticket. Only a clean refusal releases
    it -- a failed exchange leaves it reserved, because a redeem that failed
    after the machine acted looks identical from here."""
    conn = _db()
    _issue(conn)
    client = _Client(_ticket(data=BCD_OUT), status=0x80)   # not 0x00
    state = PollState()
    state.ticket_authority = {}
    _capture_ticket_in(client, conn, "now", state)
    assert _status(conn) == "assigned", "a refused ticket stays spendable"


def test_a_failed_exchange_leaves_the_number_reserved():
    """Blocking a second redemption is the safe direction: the money may
    already have moved."""
    class _Fails(_Client):
        def redeem_ticket(self, **kw):
            raise SASTimeoutError("no answer")

    conn = _db()
    _issue(conn)
    state = PollState()
    state.ticket_authority = {}
    _capture_ticket_in(_Fails(_ticket(data=BCD_OUT)), conn, "now", state)
    assert _status(conn) == "redeeming", \
        "an unknown outcome must not leave the ticket redeemable again"


def test_the_lab_file_still_wins_over_the_pool():
    conn = _db()
    _issue(conn, number=int(NUMBER), cents=9999)
    client = _Client(_ticket())
    state = PollState()
    state.ticket_authority = _authority(kind="restricted", cents=500)
    _capture_ticket_in(client, conn, "now", state)
    assert client.redeemed[0]["transfer_amount_cents"] == 500
    assert _status(conn, int(NUMBER)) == "assigned", "the pool row is untouched"


def test_without_an_authority_the_pool_is_not_consulted_either():
    """The read-only default must not become "redeems its own tickets"."""
    conn = _db()
    _issue(conn)
    client = _Client(_ticket(data=BCD_OUT))
    _capture_ticket_in(client, conn, "now", PollState())
    assert client.redeemed == []
    assert _status(conn) == "assigned"


def test_a_pending_redemption_stays_reserved_rather_than_released():
    """Category 0b010 is "pending": the machine has not said yet. Releasing a
    ticket that then completes pays it twice."""
    conn = _db()
    _issue(conn)
    client = _Client(_ticket(data=BCD_OUT), status=0x40)   # 0x40 >> 5 == 0b010
    state = PollState()
    state.ticket_authority = {}
    _capture_ticket_in(client, conn, "now", state)
    assert _status(conn) == "redeeming"


def test_success_is_the_category_not_a_single_code():
    """0x01 is "restricted promotional ticket redeemed" -- a SUCCESS. Testing
    machine_status == 0x00 marked every real redemption as a failure."""
    for status in (0x00, 0x01, 0x02):
        conn = _db()
        _issue(conn)
        state = PollState()
        state.ticket_authority = {}
        _capture_ticket_in(_Client(_ticket(data=BCD_OUT), status=status),
                           conn, "now", state)
        assert _status(conn) == "redeemed", f"status 0x{status:02X} is a success"
