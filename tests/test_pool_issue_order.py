"""The pool is consumed OLDEST ISSUED FIRST, by age, not by number.

Numbers here are small integers on purpose. An eighteen-digit
literal is the SHAPE of a printed validation number and the mirror
scanner refuses it on sight; ordering is the property under test and
it does not care how wide the number is.

Operator, 2026-11-10: "ORDER BY validation_number isn't a correct approach,
because we always look at the age of the validation number. So for the lab
it's not a problem, but for the floor it definitely needs to be ordered by
the age of the validation numbers and oldest must be issued."

Spec V2 §7.1 says only "the next available number" and fixes no order, so
this is the decision that settles it. Number order and issue order coincide
only while every batch is minted ascending; a second validation system id, a
gap-fill or a re-mint breaks that, and the failure is SILENT -- a newer
number handed out while an older one ages in the pool.

These tests exist because the change passed the whole suite without them.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from sql_poll_logger import SCHEMA, migrate_epoch_columns  # noqa: E402


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "gateway.sqlite3")
    conn.executescript(SCHEMA)
    migrate_epoch_columns(conn)
    return conn


def _seed(conn, rows):
    """rows: (validation_number, issued_at)"""
    conn.executemany(
        "INSERT INTO validation_pool (validation_number, validation_system_id,"
        " status, issued_at) VALUES (?, 1, 'available', ?)", rows)
    conn.commit()


# The query under test, kept identical to the poller's so a change there
# without a change here is a failure rather than a silent divergence.
ORDER = ("SELECT validation_number FROM validation_pool "
         "WHERE status = 'available' "
         "ORDER BY issued_at IS NULL, issued_at, validation_number LIMIT 1")


def test_the_query_in_the_poller_is_the_query_tested_here():
    """A test that drifts from its subject proves nothing about the
    subject. Matched on the SQL text, which here IS the behaviour."""
    src = (ROOT / "examples" / "sql_poll_logger.py").read_text()
    assert "ORDER BY issued_at IS NULL, issued_at, validation_number" in src, (
        "the poller's pool assignment no longer orders by issue age -- "
        "either it regressed to number order, or this test needs updating "
        "to match a deliberate change")


def test_the_oldest_issued_number_wins_even_with_the_highest_number(db):
    """The case the old code got wrong. Issued first, numbered last."""
    _seed(db, [
        (9001, "2026-11-01T00:00:00Z"),   # oldest, highest
        (1002, "2026-11-05T00:00:00Z"),
        (2003, "2026-11-09T00:00:00Z"),
    ])
    assert db.execute(ORDER).fetchone()[0] == 9001


def test_number_order_would_have_picked_the_wrong_one(db):
    """States the regression explicitly, so the next reader can see what
    was wrong rather than taking it on trust."""
    _seed(db, [
        (9001, "2026-11-01T00:00:00Z"),
        (1002, "2026-11-05T00:00:00Z"),
    ])
    by_number = db.execute(
        "SELECT validation_number FROM validation_pool WHERE status='available'"
        " ORDER BY validation_number LIMIT 1").fetchone()[0]
    by_age = db.execute(ORDER).fetchone()[0]
    assert by_number != by_age
    assert by_age == 9001


def test_an_unknown_age_is_consumed_LAST_not_first(db):
    """A NULL issued_at is a hand-inserted number with no batch info. Its
    age is unknown, and unknown is not evidence of being oldest -- the
    plain `ORDER BY issued_at` SQLite gives you sorts NULLs FIRST, which
    would hand out the one number nobody can vouch for, first."""
    _seed(db, [
        (1009, None),
        (9001, "2026-11-09T00:00:00Z"),
    ])
    assert db.execute(ORDER).fetchone()[0] == 9001

    db.execute("UPDATE validation_pool SET status='assigned' "
               "WHERE validation_number = 9001")
    db.commit()
    assert db.execute(ORDER).fetchone()[0] == 1009, (
        "a NULL-age number must still be usable once the dated ones are gone")


def test_a_seeded_pool_is_unchanged_because_every_row_shares_an_issued_at(db):
    """--seed-validation-pool stamps every row with the same instant, so the
    validation_number tiebreak keeps the lab deterministic and this change
    is a no-op there. That is why the old behaviour was never wrong in the
    lab and would have been wrong on the floor."""
    same = "2026-11-10T00:00:00Z"
    _seed(db, [(3003, same),
               (1001, same),
               (2002, same)])
    assert db.execute(ORDER).fetchone()[0] == 1001


def test_the_pool_is_drained_in_full_age_order(db):
    """Not just the first pick -- the whole sequence."""
    _seed(db, [
        (5005, "2026-11-03T00:00:00Z"),
        (1001, "2026-11-01T00:00:00Z"),
        (3003, "2026-11-02T00:00:00Z"),
    ])
    picked = []
    for _ in range(3):
        n = db.execute(ORDER).fetchone()[0]
        picked.append(n)
        db.execute("UPDATE validation_pool SET status='assigned' "
                   "WHERE validation_number = ?", (n,))
    assert picked == [1001,
                      3003,
                      5005]
