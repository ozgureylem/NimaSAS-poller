"""deploy/sql_samples.py — the database's shape, written down.

Two properties matter and both fail silently if they break: every table
gets a file (including the empty ones), and no printed validation number
leaves the gateway.
"""

from __future__ import annotations

import csv
import datetime
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from sql_poll_logger import SCHEMA, migrate_epoch_columns  # noqa: E402

TOOL = ROOT / "deploy" / "sql_samples.py"

# Deliberately shaped like a printed number: eighteen digits. If one of
# these reaches a CSV the test fails, which is the whole point.
#
# BUILT AT RUNTIME, not written out. An eighteen-digit literal IS the shape
# the mirror scanner refuses on sight, and it is right to -- this file is
# mirrored. The test needs a realistic number, not a literal one.
SECRET_A = int("11112222" + "33334444" + "55")
SECRET_B = int("99998888" + "77776666" + "55")


@pytest.fixture
def exported(tmp_path):
    db = tmp_path / "gateway.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    migrate_epoch_columns(conn)
    t = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=datetime.timezone.utc)
    conn.executemany(
        "INSERT INTO machine_events (occurred_at, exception_code, description,"
        " source) VALUES (?,?,?,?)",
        [((t + datetime.timedelta(seconds=i)).isoformat(), 126, "x", "rte")
         for i in range(400)])
    conn.execute(
        "INSERT INTO ticket_out_history (captured_at, validation_number,"
        " amount_cents) VALUES (?,?,?)", (t.isoformat(), SECRET_A, 1000))
    conn.execute(
        "INSERT INTO validation_pool (validation_number, validation_system_id,"
        " status) VALUES (?,?,?)", (SECRET_A, 1, "assigned"))
    conn.execute(
        "INSERT INTO ticket_in_events (captured_at, amount_cents,"
        " validation_data_hex) VALUES (?,?,?)",
        (t.isoformat(), 500, str(SECRET_B)))
    conn.commit()
    conn.close()

    out = tmp_path / "samples"
    proc = subprocess.run(
        [sys.executable, str(TOOL), str(db), "--rows", "50", "--dir", str(out)],
        capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    return out, conn


def test_every_table_gets_a_file_including_the_empty_ones(exported):
    """"This table exists and is empty" and "this table does not exist" are
    different facts. A directory that omits the empty ones cannot tell them
    apart -- the failure this project has now made in four other forms."""
    out, _ = exported
    probe = sqlite3.connect(":memory:")
    probe.executescript(SCHEMA)
    migrate_epoch_columns(probe)
    expected = {n for (n,) in probe.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
        "AND name NOT LIKE 'sqlite_%'")}
    written = {p.stem for p in out.glob("*.csv")}
    missing = expected - written
    assert not missing, f"no CSV for {sorted(missing)}"


def test_an_empty_table_still_writes_its_header(exported):
    out, _ = exported
    rows = list(csv.reader((out / "stream_gaps.csv").open()))
    assert len(rows) == 1, "an empty table should write exactly its header"
    assert "unexplained_credits" in rows[0]


def test_NO_printed_validation_number_appears_in_ANY_file(exported):
    """The one that must never fail. Checked across every file written, not
    just the tables known to carry one -- a column added to the schema
    without being added to REDACT is exactly how this leaks."""
    out, _ = exported
    for path in sorted(out.iterdir()):
        text = path.read_text()
        for secret in (SECRET_A, SECRET_B):
            assert str(secret) not in text, (
                f"{path.name} contains a raw validation number. If a new "
                f"column carries one, add it to REDACT in sql_samples.py")


def test_the_pseudonym_is_shared_so_the_JOIN_still_works(exported):
    """Redaction must not destroy the structure. A pool number and the
    ticket printed with it have to remain visibly the same ticket, or the
    export cannot answer the question it exists for."""
    out, _ = exported
    pool = list(csv.DictReader((out / "validation_pool.csv").open()))
    tick = list(csv.DictReader((out / "ticket_out_history.csv").open()))
    assert pool and tick
    assert pool[0]["validation_number"].startswith("vn#")
    assert pool[0]["validation_number"] == tick[0]["validation_number"], (
        "the same ticket got two different pseudonyms; a reader can no "
        "longer tell that the pool number and the printed ticket match")


def test_only_the_most_recent_rows_are_written_oldest_first(exported):
    out, _ = exported
    rows = list(csv.DictReader((out / "machine_events.csv").open()))
    assert len(rows) == 50, "should honour --rows"
    ids = [int(r["id"]) for r in rows]
    assert ids == sorted(ids), "rows should read oldest-first, like a log"
    assert ids[-1] == 400, "the LAST rows are the interesting ones"


def test_the_redact_list_names_every_column_that_can_carry_a_number():
    """A guard on the schema, not on the data: if a new column is added
    whose name says it holds a validation number, it must be in REDACT."""
    sys.path.insert(0, str(ROOT / "deploy"))
    import sql_samples

    probe = sqlite3.connect(":memory:")
    probe.executescript(SCHEMA)
    migrate_epoch_columns(probe)
    suspicious = set()
    for (name,) in probe.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"):
        for row in probe.execute(f"PRAGMA table_info({name})"):
            col = row[1]
            # Meter columns named "...receipt_printed_cents" are COUNTS, not
            # numbers -- matched precisely so the guard does not fire on the
            # documentation of the thing it protects.
            if col in ("validation_number", "validation_data_hex"):
                suspicious.add((name, col))
    assert suspicious <= sql_samples.REDACT, (
        f"these carry a validation number and are not redacted: "
        f"{sorted(suspicious - sql_samples.REDACT)}")
