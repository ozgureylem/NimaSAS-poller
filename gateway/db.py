"""The gateway database, as seen by every process that opens it.

§6.1: "Every process that touches the database opens it through one
function." For the poller and the pool replacement tool that function is
`open_gateway_db()` in examples/sql_poll_logger.py. THIS module has a
second copy of it, and that is a deliberate, tested exception rather than
an oversight:

  * the poller must not import this package -- the operator asked on
    2026-11-10 that the lab gateway keep running with no server link at
    all, and tests/test_gateway_frame.py asserts it;
  * the gateway client (§4.3) must not import the poller -- it is a second
    process that has to stay light on a 512 MB unit and has no business
    needing pyserial.

So the two pragmas are written twice, and tests/test_gateway_db.py opens a
database through each and asserts they come out identical. The same goes
for the validation_queries DDL below.

This module also owns the tables that exist only because there is a
server link:

    counters             §6.2 / Appendix B.2 -- identity, sequence numbers,
                         the persisted pool epoch
    validation_queries   §9 -- the poller's question about an escrowed
                         ticket, and the server's answer

`validation_queries` is a table for the same reason `host_requests` is
(see the note beside it in the poller's schema): one process owns the
serial port, the other owns the socket, and a durable row is the handover
between them. It is INBOUND AUTHORITY -- the only rows in this database
that let something outside the gateway decide what the cabinet is told
about money -- so nothing but the gateway client, on an authenticated
VALIDATION_RESPONSE, may ever set `decision`.
"""

from __future__ import annotations

import datetime
import sqlite3
import uuid

GATEWAY_DB_BUSY_TIMEOUT_MS = 30_000


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def open_gateway_db(path: str, *, busy_timeout_ms: int = GATEWAY_DB_BUSY_TIMEOUT_MS):
    """Open the gateway database the way every writer to it must.

    TWO PRAGMAS, AND BOTH WERE MISSING. Three documents in this repository
    asserted "SQLite is in WAL mode, so a second writer is fine"; nothing
    anywhere ever set it. The database was in the default rollback-journal
    mode, where a writer takes an EXCLUSIVE lock that blocks readers too --
    the opposite of what the claim promised, and nobody had run the two tools
    against each other to find out.

    It surfaced as `sqlite3.OperationalError: database is locked` from
    pool_replace.py's BEGIN IMMEDIATE while a soak was running, i.e. in
    exactly the live-replacement case §5.4 describes a CMS performing.

    journal_mode is persistent: set once, it stays in the file. busy_timeout
    is per-connection and must be set by every process that writes.

    AND A THIRD, WHOSE ORDER IS LOAD-BEARING. auto_vacuum can only be
    changed on a database that is still NEW -- no table created yet -- or
    by running a full VACUUM, which takes the write lock for as long as it
    takes to rewrite the file. On a gateway that is §15.7's ten seconds
    gone and a cashout turned into a handpay, so a database that was not
    created in INCREMENTAL mode can never be converted while the poller
    runs. Setting it HERE means no gateway ever needs that conversion.

    It must be set BEFORE journal_mode, and this is not style. Switching to
    WAL writes the database header, after which the file is no longer new
    and `PRAGMA auto_vacuum` is accepted, silently ignored, and reads back
    as NONE. MEASURED, both orders, on sqlite 3.45.1:

        auto_vacuum then WAL  ->  auto_vacuum = 2 (INCREMENTAL)
        WAL then auto_vacuum  ->  auto_vacuum = 0 (NONE), no error

    A database that already has tables is left exactly as it is. This
    function never runs a VACUUM: doing that on open would put the lock
    at poller startup, which is the one moment a gateway cannot afford it.
    `examples/db_maintain.py enable-auto-vacuum` converts an existing file,
    deliberately, with the poller stopped.
    """
    conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000)
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


LINK_SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- TICKET-IN, ASKED OF THE SERVER (§9). One row per escrowed ticket.
--
--   pending   the poller read LP 0x70 and is waiting, inside its own
--             deadline, for the server's answer
--   answered  the gateway client wrote the server's VALIDATION_RESPONSE
--   acted     the poller sent LP 0x71 with that answer
--   timeout   no answer arrived in time; the poller rejected the ticket
--             locally and a late answer must NOT be acted on, which is
--             why the client's UPDATE is conditional on state='pending'
--
-- The gateway holds no authority here (§9.1): it does not decide validity
-- and it does not supply the amount. `amount_cents` is the SERVER's
-- figure; `reported_amount_cents` is what the ticket said about itself
-- and is carried only so the server can compare the two (§12.2).
CREATE TABLE IF NOT EXISTS pool_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filled_at TEXT NOT NULL,
    source TEXT NOT NULL,              -- 'server-batch' | 'pool_replace' | 'poller-seed'
    issued INTEGER NOT NULL,           -- numbers this fill added
    revoked INTEGER NOT NULL DEFAULT 0,-- numbers it revoked, if a replacement
    available_after INTEGER NOT NULL,  -- THE POOL SIZE, counted after the fill
    pool_epoch INTEGER,
    batch_uuid TEXT,
    synced_at TEXT
);
-- KEEP IN STEP WITH examples/sql_poll_logger.py's SCHEMA, which defines
-- pool_fills too because either process may reach a fresh database first.
-- A test asserts the two definitions match.

-- EVERY REPLAY THIS GATEWAY WAS ASKED FOR, applied or refused. A replay
-- makes acknowledged rows drain again, so anything that counts drained
-- rows needs to be able to tell a replay from ordinary traffic, and the
-- gateway's own rate limit is counted from this table.
-- EVERY MAINTENANCE RUN THE SERVER ASKED FOR, run or refused, with what it
-- printed. The gateway is headless: this row and the alert sent back are
-- the only places the outcome exists.
CREATE TABLE IF NOT EXISTS link_maintenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_at TEXT NOT NULL,
    options_json TEXT,
    outcome TEXT NOT NULL,            -- 'running' | 'done' | 'deferred'
                                      -- | 'failed' | 'refused'
    finished_at TEXT,
    output TEXT
);

CREATE TABLE IF NOT EXISTS link_replays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_at TEXT NOT NULL,
    outcome TEXT NOT NULL,            -- 'applied' | 'refused'
    reason TEXT,                      -- why refused; NULL when applied
    after_json TEXT,                  -- {table: last id the server holds}
    rows_json TEXT                    -- {table: rows put back in the drain}
);

CREATE TABLE IF NOT EXISTS validation_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asked_at TEXT NOT NULL,
    validation_number TEXT NOT NULL,      -- 18 digits: system ID + 16
    parsing_code INTEGER,
    reported_amount_cents INTEGER,
    state TEXT NOT NULL DEFAULT 'pending',
    sent_at TEXT,                         -- VALIDATION_QUERY left the gateway
    answered_at TEXT,
    decision TEXT,                        -- 'allow' | 'reject'
    transfer_code INTEGER,                -- Table 15.12a, chosen by the server
    amount_cents INTEGER,
    restricted_expiration INTEGER,
    pool_id INTEGER,
    reason TEXT,
    outcome TEXT,                         -- what the poller did, in words
    machine_status INTEGER,               -- the immediate reply to LP 0x71
    completion_status INTEGER,            -- LP 0x71/FF after exception 0x68
    completed_at TEXT,
    result_sent_at TEXT                   -- REDEMPTION_RESULT left the gateway
);
CREATE INDEX IF NOT EXISTS idx_validation_queries_state
    ON validation_queries (state, id);
"""

# validation_pool predates batches. pool_replace.py adds the same
# columns; the names are shared on purpose so either tool can run first.
# KEEP IN STEP WITH examples/pool_replace.py's own list. The two are
# separate because examples/ does not import gateway/ and should not start:
# a test asserts they match rather than a refactor crossing that boundary.
POOL_BATCH_COLUMNS = (("pool_epoch", "INTEGER"),
                      ("batch_uuid", "TEXT"),
                      ("revoked_at", "TEXT"),
                      # issue_seq: a per-gateway monotonic counter, because
                      # issued_at TIES INSIDE A BATCH. Every row of one
                      # top-up shares a timestamp, so ordering by issued_at
                      # alone falls through to validation_number -- numeric
                      # order, which is the thing the pool rules forbid,
                      # scoped to a batch. Spec change 2026-11-12, rule 3.
                      ("issue_seq", "INTEGER"))




# THE SAME RULE AS examples/sql_poll_logger.py's pool_low_water(), and it
# has to be, because the two are the operator's alert and the server's
# refill trigger for the same pool. A gateway that prints "top up, 39 left"
# while telling the server it is fine until 10 is a gateway that asks a
# human to do by hand what the link exists to do automatically -- and the
# human is the one who has to notice. tests/test_gateway_pool.py compares
# the two implementations behaviourally, over a range of pool sizes,
# because examples/ does not import gateway/ and a text comparison would
# not survive either side being reformatted.
POOL_LOW_WATER_FRACTION = 0.20


def pool_size_at_last_fill(conn: sqlite3.Connection) -> int | None:
    """The pool size the last recorded fill left behind, or None.

    None is NOT zero. It means no fill has been recorded on this database,
    so the pool's intended size is genuinely unknown and 20% of it is not a
    number worth acting on.
    """
    try:
        row = conn.execute("SELECT available_after FROM pool_fills "
                           "ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return None
    return int(row[0])


def pool_low_water(conn: sqlite3.Connection, floor: int) -> tuple[int, str]:
    """The live low-water mark, and a phrase saying where it came from."""
    size = pool_size_at_last_fill(conn)
    if size is None:
        return floor, (f"--pool-depth-alert floor {floor}; no fill recorded "
                       "on this database, so there is no pool size to take "
                       f"{POOL_LOW_WATER_FRACTION:.0%} of")
    scaled = int(size * POOL_LOW_WATER_FRACTION)
    if scaled > floor:
        return scaled, (f"{POOL_LOW_WATER_FRACTION:.0%} of the {size} this "
                        "pool held after its last fill")
    return floor, (f"--pool-depth-alert floor {floor}; "
                   f"{POOL_LOW_WATER_FRACTION:.0%} of the last fill's {size} "
                   f"would be {scaled}, too few to act on")


# THE SAME STATEMENT, character for character, as the one in the poller's
# check_pool_depth(). The server sizes a refill from this number and the
# operator's runway alert is projected from that one; if they counted
# different things, the two would disagree about the same gateway.
# tests/test_gateway_pool.py lifts the poller's copy out with `ast` and
# compares them, because examples/ does not import gateway/.
#
# What it counts is open item 2 of the pool spec change, settled: a number
# that LEFT THE POOL BY ASSIGNMENT, whatever became of it afterwards --
# printed, jammed, voided. datetime() on both sides is deliberate; see the
# note beside the poller's copy for the 48-hour "24-hour" window the raw
# string comparison produced.
POOL_USED_24H_SQL = (
    "SELECT COUNT(*) FROM validation_pool WHERE assigned_at IS NOT NULL "
    "AND datetime(assigned_at) >= datetime(?, '-24 hours')")


def pool_used_24h(conn: sqlite3.Connection, now: str | None = None) -> int:
    return int(conn.execute(POOL_USED_24H_SQL, (now or utc_now(),)).fetchone()[0])


def next_issue_seq(conn: sqlite3.Connection) -> int:
    """The next value for validation_pool.issue_seq.

    MAX over the WHOLE table, revoked and assigned rows included. The counter
    must never go backwards: a revoke that reset it would make a new number
    sort ahead of an older one still in the pool, which is the ordering bug
    this column exists to close.
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(issue_seq), 0) FROM validation_pool").fetchone()
    return int(row[0] or 0) + 1


def backfill_issue_seq(conn: sqlite3.Connection) -> int:
    """Stamp issue_seq on rows that predate the column. Returns how many.

    IN THE ORDER THE POOL WAS ALREADY BEING CONSUMED -- oldest issue first,
    NULL issued_at last, number only as a tiebreak -- which is exactly the
    ORDER BY the assignment used before this column existed. So adding the
    column changes nothing about which number goes out next.

    The ordering is done here rather than in a window function inside the
    UPDATE on purpose: ROW_NUMBER() over the whole table would restart at 1
    and collide with sequences already handed out the first time a LATER
    batch of NULL rows arrived. Offsetting from MAX cannot.
    """
    rows = conn.execute(
        "SELECT validation_number FROM validation_pool WHERE issue_seq IS NULL "
        "ORDER BY issued_at IS NULL, issued_at, validation_number").fetchall()
    if not rows:
        return 0
    base = conn.execute(
        "SELECT COALESCE(MAX(issue_seq), 0) FROM validation_pool").fetchone()[0]
    conn.executemany(
        "UPDATE validation_pool SET issue_seq = ? WHERE validation_number = ?",
        [(int(base or 0) + i, number) for i, (number,) in enumerate(rows, 1)])
    return len(rows)


def ensure_link_schema(conn: sqlite3.Connection) -> None:
    """Create the link tables and, when validation_pool already exists, add
    its batch columns. Safe to call from both processes on every start."""
    conn.executescript(LINK_SCHEMA)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(validation_pool)")}
    if cols:
        for name, decl in POOL_BATCH_COLUMNS:
            if name not in cols:
                try:
                    conn.execute(
                        f"ALTER TABLE validation_pool ADD COLUMN {name} {decl}")
                except sqlite3.OperationalError:
                    # The other process added it between our PRAGMA and our
                    # ALTER. That is the outcome we wanted.
                    pass
        backfill_issue_seq(conn)
    conn.commit()


def get_counter(conn: sqlite3.Connection, name: str, default: str | None = None):
    row = conn.execute("SELECT value FROM counters WHERE name = ?",
                       (name,)).fetchone()
    return row[0] if row is not None else default


def set_counter(conn: sqlite3.Connection, name: str, value) -> None:
    """Does NOT commit. Counter writes belong to the transaction of the
    thing they count -- the pool epoch commits with the batch it names."""
    conn.execute(
        "INSERT INTO counters (name, value) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
        (name, str(value)))


def db_instance_id(conn: sqlite3.Connection) -> str:
    """Generated once, when first asked for, and never again (§6.2).

    A drained row's identity on the wire is (db_instance_id, table, id).
    Row ids restart from 1 when a database is re-created, so without this a
    re-imaged gateway's ticket_out_history id 7 would overwrite, on the
    server, the id 7 of the database it replaced.
    """
    existing = get_counter(conn, "db_instance_id")
    if existing:
        return existing
    fresh = str(uuid.uuid4())
    # INSERT OR IGNORE then re-read: two processes starting together must
    # agree on ONE id, and the loser of that race must adopt the winner's.
    conn.execute("INSERT OR IGNORE INTO counters (name, value) VALUES (?, ?)",
                 ("db_instance_id", fresh))
    conn.commit()
    return get_counter(conn, "db_instance_id")
