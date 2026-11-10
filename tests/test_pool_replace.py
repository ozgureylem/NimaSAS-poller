"""Replacing a validation pool without destroying what it recorded.

Three properties make a replacement safe rather than merely convenient, and
each of them is a way this could go wrong quietly:

  Assigned rows are printed tickets. Touching them would rewrite the only
  record of which number went onto which ticket.

  Revoked rows stay in the table. Deleting them destroys the evidence that
  the gateway ever held the number, which matters precisely when somebody is
  asking where a ticket came from.

  Revoke and re-issue are one transaction. A revoke that committed while the
  insert failed would leave the gateway holding zero available numbers, and
  the next cashout would run out the ten-second validation window and become
  a handpay (§15.7) -- the exact failure the pool exists to prevent.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))

from pool_replace import (
    current_epoch, migrate_pool_columns, oldest_available_age_hours,
    pool_counts, replace_pool)

SCHEMA = """
CREATE TABLE validation_pool (
    validation_number INTEGER PRIMARY KEY,
    validation_system_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'available',
    issued_at TEXT,
    assigned_at TEXT,
    assigned_amount_cents INTEGER
);
"""


def _db(rows=()):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    for n, status, issued, assigned, amount in rows:
        conn.execute(
            "INSERT INTO validation_pool (validation_number, validation_system_id, "
            "status, issued_at, assigned_at, assigned_amount_cents) VALUES (?,1,?,?,?,?)",
            (n, status, issued, assigned, amount))
    conn.commit()
    migrate_pool_columns(conn)
    return conn


OLD = "2026-10-26T00:00:00+00:00"
NOW = "2026-10-30T00:00:00+00:00"


def test_the_migration_adds_batch_identity_and_keeps_existing_rows():
    conn = _db([(111, "available", OLD, None, None)])
    cols = {r[1] for r in conn.execute("PRAGMA table_info(validation_pool)")}
    assert {"pool_epoch", "batch_uuid", "revoked_at"} <= cols
    row = conn.execute("SELECT pool_epoch, batch_uuid FROM validation_pool").fetchone()
    assert row == (None, None), "pre-existing rows must not be back-dated into a batch"


def test_the_migration_is_idempotent():
    conn = _db([(111, "available", OLD, None, None)])
    assert migrate_pool_columns(conn) == []


# --- what a replacement must never touch --------------------------------

def test_assigned_rows_are_untouched():
    """They are printed tickets. §9.4: a ticket printed before a discard "is
    genuine and may sit in a customer's pocket for weeks"."""
    conn = _db([(111, "available", OLD, None, None),
                (222, "assigned", OLD, "2026-10-27T10:00:00+00:00", 2500)])
    replace_pool(conn, 3, now_fn=lambda: NOW)
    row = conn.execute(
        "SELECT status, issued_at, assigned_at, assigned_amount_cents, revoked_at "
        "FROM validation_pool WHERE validation_number = 222").fetchone()
    assert row == ("assigned", OLD, "2026-10-27T10:00:00+00:00", 2500, None)


def test_revoked_rows_stay_in_the_table():
    conn = _db([(111, "available", OLD, None, None)])
    replace_pool(conn, 2, now_fn=lambda: NOW)
    row = conn.execute(
        "SELECT status, revoked_at, issued_at FROM validation_pool "
        "WHERE validation_number = 111").fetchone()
    assert row == ("revoked", NOW, OLD), "the number must still be on record"


def test_nothing_is_ever_deleted():
    conn = _db([(111, "available", OLD, None, None),
                (222, "assigned", OLD, NOW, 500)])
    before = conn.execute("SELECT COUNT(*) FROM validation_pool").fetchone()[0]
    replace_pool(conn, 4, now_fn=lambda: NOW)
    after = conn.execute("SELECT COUNT(*) FROM validation_pool").fetchone()[0]
    assert after == before + 4


# --- what it must produce -----------------------------------------------

def test_the_new_batch_is_available_with_a_fresh_epoch_and_one_uuid():
    conn = _db([(111, "available", OLD, None, None)])
    result = replace_pool(conn, 5, now_fn=lambda: NOW)
    assert result["revoked"] == 1 and result["issued"] == 5
    rows = conn.execute(
        "SELECT status, pool_epoch, batch_uuid, issued_at FROM validation_pool "
        "WHERE pool_epoch IS NOT NULL").fetchall()
    assert len(rows) == 5
    assert {r[0] for r in rows} == {"available"}
    assert {r[1] for r in rows} == {result["pool_epoch"]}
    assert len({r[2] for r in rows}) == 1, "one batch, one uuid"
    assert {r[3] for r in rows} == {NOW}


def test_each_replacement_advances_the_epoch():
    conn = _db()
    first = replace_pool(conn, 2, now_fn=lambda: NOW)
    second = replace_pool(conn, 2, now_fn=lambda: NOW)
    assert second["pool_epoch"] == first["pool_epoch"] + 1
    assert second["revoked"] == 2, "the first batch is what the second retires"


def test_the_age_alert_only_clears_because_the_old_rows_are_retired():
    """The poller's alert reads MIN(issued_at) over AVAILABLE rows. Adding
    fresh numbers beside stale ones never moves a minimum -- retiring them
    is the only thing that does, which is why a top-up is not a replacement."""
    conn = _db([(111, "available", OLD, None, None)])
    assert oldest_available_age_hours(conn, now_fn=lambda: NOW) == pytest.approx(96.0)
    replace_pool(conn, 3, now_fn=lambda: NOW)
    assert oldest_available_age_hours(conn, now_fn=lambda: NOW) == pytest.approx(0.0)


def test_a_collision_with_a_revoked_number_is_retried_not_reused():
    """validation_number is the primary key, so a revoked number cannot be
    minted again -- and must not be, or two tickets could carry it."""
    conn = _db([(42, "revoked", OLD, None, None)])
    seq = iter([42, 42, 7, 8, 9])
    replace_pool(conn, 2, random_fn=lambda bits: next(seq), now_fn=lambda: NOW)
    assert conn.execute(
        "SELECT status FROM validation_pool WHERE validation_number = 42"
    ).fetchone()[0] == "revoked"
    assert sorted(r[0] for r in conn.execute(
        "SELECT validation_number FROM validation_pool WHERE status = 'available'"
    )) == [7, 8]


# --- all or nothing ------------------------------------------------------

def test_a_failed_mint_rolls_the_revoke_back():
    """The property that matters most. A gateway left holding zero available
    numbers answers no 0x57, the ten-second window expires, and the cashout
    becomes a handpay -- the failure the pool exists to prevent."""
    conn = _db([(111, "available", OLD, None, None),
                (222, "available", OLD, None, None)])

    def explode(bits):
        raise RuntimeError("mint failed")

    with pytest.raises(RuntimeError):
        replace_pool(conn, 3, random_fn=explode, now_fn=lambda: NOW)
    assert pool_counts(conn) == {"available": 2}, "the revoke must not survive"
    assert current_epoch(conn) == 0


def test_issuing_nothing_is_refused_outright():
    conn = _db([(111, "available", OLD, None, None)])
    with pytest.raises(ValueError):
        replace_pool(conn, 0, now_fn=lambda: NOW)
    assert pool_counts(conn) == {"available": 1}


# --- the pragmas nobody had set --------------------------------------------

def test_the_gateway_db_is_opened_in_wal_with_a_long_busy_timeout(tmp_path):
    """Three documents in this repo asserted "SQLite is in WAL mode, so a
    second writer is fine". Nothing set it. The database was in the default
    rollback-journal mode, where a writer takes an EXCLUSIVE lock that blocks
    readers too -- the opposite of the promise -- and pool_replace.py's
    BEGIN IMMEDIATE raised "database is locked" against a running soak, which
    is the one case the tool exists for.
    """
    from examples.sql_poll_logger import (GATEWAY_DB_BUSY_TIMEOUT_MS,
                                          open_gateway_db)
    db = tmp_path / "gateway.sqlite3"
    conn = open_gateway_db(str(db))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == \
        GATEWAY_DB_BUSY_TIMEOUT_MS
    # Long enough to wait out a 5-7s poll cycle several times over.
    assert GATEWAY_DB_BUSY_TIMEOUT_MS >= 20_000
    conn.close()


def test_a_second_writer_can_replace_the_pool_while_a_reader_holds_the_db(tmp_path):
    """The live-replacement case, with two connections open at once."""
    from examples.sql_poll_logger import open_gateway_db
    db = tmp_path / "gateway.sqlite3"
    poller = open_gateway_db(str(db))
    poller.executescript(SCHEMA)
    poller.execute(
        "INSERT INTO validation_pool (validation_number, validation_system_id, "
        "status, issued_at) VALUES (1, 0, 'available', "
        "'2026-10-30T00:00:00+00:00')")
    poller.commit()

    cms = open_gateway_db(str(db))
    migrate_pool_columns(cms)
    result = replace_pool(cms, 5)
    assert result["revoked"] == 1
    assert result["issued"] == 5
    # The poller's own connection sees the new pool without reopening.
    assert pool_counts(poller).get("available") == 5
    poller.close()
    cms.close()
