#!/usr/bin/env python3
"""The gateway client (spec §4.3, §10): the gateway's side of the server link.

    python3 -m gateway.client commission --db gateway.sqlite3 ...   (once)
    python3 -m gateway.client run --db gateway.sqlite3

This is the SECOND of the two processes §4.3 describes. The poller owns the
serial port and never opens a socket; this owns the socket and never opens
the serial port. They share one SQLite database, and everything that
passes between them is a row:

    poller writes                    this process
    -------------------------------  ----------------------------------------
    drained tables (synced_at NULL)  sends them on PULL_INVITE, sets
                                     synced_at on ACK (§10.4)
    validation_queries 'pending'     sends VALIDATION_QUERY, writes the
                                     VALIDATION_RESPONSE back (§9)
    validation_queries completion    sends REDEMPTION_RESULT
                                     writes validation_pool on a signed
                                     top-up or POOL_REPLACE (§7)

WHAT IT MAY INITIATE. §3: "only three kinds of gateway-initiated
transmission exist: the heartbeat, the ticket-in validation query, and the
rare ALERT frame." REDEMPTION_RESULT is the tail of a query. Nothing else
leaves this process unless the server invited it.

WHAT IS NOT BUILT, said here so it is not discovered on a floor:
ON_DEMAND_PULL (§10.7), ROTATE_KEY / REVOKE (§10.5) and DISABLE / ENABLE
(§8.1) are received, authenticated, refused with an ALERT naming them, and
otherwise ignored. A server that sends one learns immediately that this
gateway cannot do it.

A client fault must never stop polling (§4.3). That is structural here:
this is a different process, so the worst it can do to the poller is hold
the database write lock for one short transaction.

Standard library only. No pyserial, no asyncio: one connection, one
thread, one select() loop.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import select
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if __package__ in (None, ""):          # `python3 gateway/client.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import ed25519, frame
from gateway.db import (db_instance_id, ensure_link_schema, get_counter,
                        open_gateway_db, set_counter, utc_now)
from gateway import machine
from gateway.frame import MessageType
from gateway.pool import apply_batch, pool_status

HEARTBEAT_INTERVAL_S = 15.0        # §10.4: flat, no jitter
# THE FLOOR, not the threshold. This was a flat 10, derived in the comment
# as "20% of 50" -- the right rule frozen at one assumed pool size. The size
# is now recorded at every fill (pool_fills), so pool_status() computes
# max(this, 20% of it) and a 200-number pool asks for a refill at 40 rather
# than at 10. It must stay equal to the poller's DEFAULT_POOL_DEPTH_ALERT:
# the operator's alert and the server's refill trigger are the same number
# for the same pool, and a test asserts it.
POOL_LOW_WATER = 10                # §7.1 / §15
DRAIN_BYTE_BOUND = 64 * 1024       # §10.4
TICK_S = 0.02                      # how often validation_queries is read
RECONNECT_MIN_S, RECONNECT_MAX_S = 1.0, 15.0
# How long the server may say NOTHING before the link is presumed dead.
# Generous on purpose: being wrong costs one reconnect, being too eager
# costs a reconnect loop on a slow link. The server invites about once a
# second, so this is two orders of magnitude of headroom.
SERVER_SILENCE_TIMEOUT_S = 120.0
# §10.1: "a frame-completion timeout closes a connection feeding a
# legal-length frame one byte at a time." Starting value; §15 says
# "pending lab".
FRAME_COMPLETION_TIMEOUT_S = 10.0
# A machine whose meters_current row is older than this is reported
# offline. A full meter sweep is about 50 s, so anything tighter would
# report a healthy cabinet as down once a minute.
EGM_ONLINE_WINDOW_S = 120.0

# Money first. Within a table, oldest id first -- the order the poller
# received them (§11.1). Anything with a synced_at column that is not
# named here is drained after these, alphabetically.
DRAIN_PRIORITY = ("ticket_out_history", "ticket_in_events",
                  "ticket_in_completions", "handpay_queue",
                  "aft_transfer_completions", "machine_epochs")
# §6.2 said "the meters_history ring is never selected into the drain."
# SUPERSEDED by the operator's decision (b) of 2026-11-15; see below.
# meters_current is one row, replaced in place every cycle. It is a
# gauge of "now", carried in the heartbeat, and must never be drained --
# even if it one day has a synced_at column, which it may: the poller's
# `latest_c7_meters` view is `SELECT * FROM meters_current UNION ALL
# SELECT * FROM meters_history`, so the two tables must keep identical
# columns, and a synced_at added to one has to be added to both.
#
# meters_history is NOT here any more (operator's decision (b),
# 2026-11-15: meters drain to the server). It drains the day it has a
# synced_at column and not before -- drain_tables() requires one -- and
# that column is a change to the poller's schema, which is not this
# module's to make.
#
# THAT DAY ARRIVED, 2026-11-15/16. The column is on BOTH meter tables and
# meters_history drains; verified on hardware -- "meters_history drained?
# True, meters_current refused? True", nine tables draining. The sentence
# that used to end this paragraph ("Until then nothing about a gateway
# changes") read as present tense for a day after it stopped being true,
# and another session reading this file for the RamSys guide flagged it.
#
# meters_current still never drains, and the reason above is unchanged:
# it is one row replaced in place every cycle, a gauge of "now" carried
# in the heartbeat. Shipping it would hand the server the same meter over
# and over with no history behind it.
NEVER_DRAINED = frozenset({"meters_current"})
# A replay is a control primitive: it makes this gateway send again what it
# already sent. A confused or compromised server must not be able to keep
# a gateway re-draining forever, so the GATEWAY counts, whatever arrives.
REPLAY_MAX_PER_HOUR = 3
# Server-requested maintenance (examples/db_maintain.py run). The same
# reasoning as the replay limit: it is the server reaching into the
# gateway, so the gateway counts. Six an hour is ample for an operator
# testing and useless for a loop.
MAINTAIN_MAX_PER_HOUR = 6
# db_maintain stops itself at 120 s with work left. This is the client's
# own limit on top, for a run that has hung rather than one that is busy.
MAINTAIN_KILL_AFTER_S = 300.0
# "Wait for zero credits": the longest a server may ask the gateway to
# hold a deferred request (the timer comes round every twelve hours
# anyway), how often the client looks at the credit figure while holding
# one, and the least time between two starts of the tool for one request.
# The last matters when the figure reads zero but is stale: the tool
# refuses a stale figure, and without a spacing the client would start it
# again on every look.
# "Repeat until little comes back" (operator, 2026-11-19, after two
# hardware runs returned 8.6 and 8.5 MB each with ~50 MB still free): one
# request may run the tool again while each run shrinks the file by at
# least the amount the server names. Every run is a fresh start of the
# tool, so EVERY run passes the zero-credit gate again. Ten runs is twenty
# minutes of work at the tool's own 120 s limit; the pause lets the poller
# and the drain have the database to themselves in between.
MAINTAIN_REPEAT_MAX_RUNS = 10
MAINTAIN_REPEAT_PAUSE_S = 5.0
MAINTAIN_REPEAT_MAX_BYTES = 1 << 40
MAINTAIN_WAIT_MAX_S = 12 * 3600
MAINTAIN_WAIT_LOOK_S = 2.0
MAINTAIN_WAIT_RETRY_S = 30.0
MAINTAIN_SCRIPT = Path(__file__).resolve().parent.parent / "examples" / "db_maintain.py"

UNSUPPORTED = {
    MessageType.ON_DEMAND_PULL: "ON_DEMAND_PULL (§10.7)",
    MessageType.ROTATE_KEY: "ROTATE_KEY (§10.5)",
    MessageType.REVOKE: "REVOKE (§10.5)",
    MessageType.DISABLE: "DISABLE (§8.1)",
    MessageType.ENABLE: "ENABLE (§8.1)",
}


def log(message: str) -> None:
    print(f"[{utc_now()}] {message}", flush=True)


# --- configuration: counters for state, a key file for secrets ------------

def load_keyfile(path: Path) -> dict:
    """The gateway's key material: its HMAC key and the server's PUBLIC
    pool-signing key (§10.5). A file beside the database rather than a row
    inside it, because the database is the thing that gets copied off a
    gateway for analysis and a key must not travel with it."""
    try:
        data = json.loads(path.read_text())
        cfg = {"client_id": str(data["client_id"]),
               "hmac_key": bytes.fromhex(data["hmac_key"]),
               "server_public_key": bytes.fromhex(data["server_public_key"]),
               "server_host": str(data["server_host"]),
               "server_port": int(data["server_port"])}
    except (OSError, KeyError, TypeError, ValueError) as e:
        raise SystemExit(
            f"cannot use key file {path}: {type(e).__name__}: {e}\n"
            "  Create it with `python3 -m gateway.client commission ...` "
            "using the values the server printed when it commissioned this "
            "gateway.")
    frame.encode_client_id(cfg["client_id"])        # raises on a bad id
    if len(cfg["hmac_key"]) < 16:
        raise SystemExit(f"{path}: hmac_key is {len(cfg['hmac_key'])} bytes; "
                         "refusing a key shorter than 16")
    if len(cfg["server_public_key"]) != ed25519.PUBLIC_KEY_BYTES:
        raise SystemExit(f"{path}: server_public_key must be "
                         f"{ed25519.PUBLIC_KEY_BYTES} bytes of hex")
    return cfg



def claim_single_instance(db_path: str):
    """Refuse to be the second gateway client on one database.

    §10.3 gives the link a tx sequence counter and an rx floor, and both
    live in the database. TWO PROCESSES CANNOT SHARE THEM: each reads,
    increments and writes the same `tx_seq`, so the frames they send
    interleave out of order, the server rejects them, and it closes the
    connection with "nothing valid in 10s". Each client then reconnects
    -- and because the backoff resets on every successful CONNECT,
    before the frames start failing, it reconnects immediately. Two
    clients therefore produce a permanent one-per-second connect storm
    rather than a visible error.

    Observed on the lab gateway 2026-11-13T01:52, after an earlier
    client was started twice: sixty connects in sixty seconds, each from
    a new source port, then "closing, nothing valid in 10s". Nothing in
    either process said what was wrong, because from each one's own
    point of view it had connected.

    The poller already has this property for free -- it holds the serial
    port exclusively, so a second one cannot start. The client has no
    such resource, so the lock is explicit.

    Returns the open lock file, which must be kept referenced for as long
    as the process runs; the lock is released when it closes.
    """
    lock_path = Path(db_path).with_suffix(".client.lock")
    try:
        import fcntl
    except ImportError:                                   # not POSIX
        log(f"note: no fcntl on this platform, so {lock_path.name} is "
            "advisory only. Do not start a second gateway client against "
            "this database: §10.3's sequence counters cannot be shared.")
        return None
    handle = open(lock_path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        holder = handle.read().strip() or "an unknown pid"
        handle.close()
        raise SystemExit(
            f"another gateway client already holds {lock_path} "
            f"(started by {holder}).\n"
            "§10.3's tx sequence counter lives in the database and cannot "
            "be shared: two clients interleave their frames, the server "
            "rejects them, and both reconnect once a second forever.\n"
            "Stop the other one first:  pkill -f 'gateway.client run'")
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid {os.getpid()}\n")
    handle.flush()
    return handle


def default_keyfile(db: str) -> Path:
    return Path(db).with_suffix(".linkkey.json")


# --- the drain -------------------------------------------------------------

def drain_tables(conn: sqlite3.Connection, only: frozenset | None = None) -> list[str]:
    """Every table with BOTH an `id` and a `synced_at` column, read from the
    database rather than from a list in this file -- a table the poller
    gains tomorrow is drained without anyone remembering to add it here."""
    found = []
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"):
        if name in NEVER_DRAINED or name.startswith("sqlite_"):
            continue
        cols = {row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')}
        if {"id", "synced_at"} <= cols and (only is None or name in only):
            found.append(name)
    return (sorted((t for t in found if t in DRAIN_PRIORITY),
                   key=DRAIN_PRIORITY.index)
            + sorted(t for t in found if t not in DRAIN_PRIORITY))


def ensure_drain_indexes(conn: sqlite3.Connection, tables: list[str]) -> None:
    """A PARTIAL index over the undrained rows of each table.

    Without it, "what is still to send?" is a scan of the whole table, and
    the answer is asked for on every invitation. On a gateway whose
    machine_events holds a million acknowledged rows that is a million
    rows read to find none -- once a second. The partial index holds only
    rows where synced_at IS NULL, so it is as small as the backlog and
    empty when the gateway is caught up.

    It costs the poller one tiny index entry per event written, and it is
    created by the client, not the poller, because a gateway with no server
    link has no use for it.
    """
    for table in tables:
        try:
            conn.execute(
                f'CREATE INDEX IF NOT EXISTS "idx_{table}_undrained" '
                f'ON "{table}" (id) WHERE synced_at IS NULL')
        except sqlite3.OperationalError as e:
            log(f"could not index {table} for the drain ({e}); it still "
                "drains, by table scan")
    conn.commit()


def queue_depth(conn: sqlite3.Connection, tables: list[str]) -> int:
    return sum(conn.execute(
        f'SELECT COUNT(*) FROM "{t}" WHERE synced_at IS NULL').fetchone()[0]
        for t in tables)


def collect_events(conn: sqlite3.Connection, tables: list[str],
                   max_events: int, byte_bound: int = DRAIN_BYTE_BOUND,
                   epochs: list[dict] | None = None):
    """Up to `max_events` rows or `byte_bound` bytes, whichever first (§10.4).

    Returns (events, oversize). `oversize` lists rows that can never be sent
    because one row alone exceeds the byte bound: §10.4 says such a row
    "would stall the gateway silently while it appears healthy -- treat as
    an anomaly and alert; do not build chunking". They are reported and
    skipped so the rows behind them still drain.
    """
    events, oversize, used = [], [], 0
    for table in tables:
        if len(events) >= max_events:
            break
        cursor = conn.execute(
            f'SELECT * FROM "{table}" WHERE synced_at IS NULL ORDER BY id '
            f'LIMIT ?', (max_events - len(events) + 8,))
        columns = [c[0] for c in cursor.description]
        for raw in cursor.fetchall():
            row = {c: (v.hex() if isinstance(v, (bytes, bytearray)) else v)
                   for c, v in zip(columns, raw) if c != "synced_at"}
            event = {"table": table, "id": row["id"], "row": row}
            if epochs is not None:
                # WHICH MACHINE this row came from. One gateway carried
                # between cabinets drains every one of them under the same
                # client_id; without this the server cannot tell them
                # apart. See gateway/machine.py for how it is decided and
                # where that decision is soft.
                event["epoch_id"] = machine.epoch_for(epochs, row)
            size = len(json.dumps(event, separators=(",", ":")))
            if size > byte_bound:
                oversize.append((table, row["id"], size))
                continue
            if used + size > byte_bound or len(events) >= max_events:
                return events, oversize
            events.append(event)
            used += size
    return events, oversize


def acked_high_water(conn: sqlite3.Connection, tables: list[str]) -> dict:
    """{table: the highest id the server has acknowledged}, for every
    drained table with at least one acknowledged row.

    This is what lets a server notice, with nobody telling it, that it has
    LOST rows it once acknowledged -- a restore from backup. The server
    compares each figure with the newest row it actually holds; if the
    gateway's is higher, the difference is rows that exist only here.

    Read from `counters`, where apply_ack() keeps it, and not by scanning:
    "the newest acknowledged row" behind a large undrained backlog is a
    walk over the whole backlog, and this runs every heartbeat on a
    gateway whose storage is fifty times slower than a bench.
    """
    out = {}
    for table in tables:
        value = get_counter(conn, f"acked_max:{table}")
        if value is None:
            # First run on a database that was drained before this counter
            # existed. Pay for the scan once and remember the answer.
            row = conn.execute(
                f'SELECT id FROM "{table}" WHERE synced_at IS NOT NULL '
                f'ORDER BY id DESC LIMIT 1').fetchone()
            value = row[0] if row else 0
            set_counter(conn, f"acked_max:{table}", value)
            conn.commit()
        if int(value) > 0:
            out[table] = int(value)
    return out


def apply_replay(conn: sqlite3.Connection, request, instance: str,
                 tables: list[str], *, now_fn=utc_now) -> tuple[bool, str, dict]:
    """Put acknowledged rows back in the drain, because the server says it
    no longer holds them. Returns (applied, reason, {table: rows}).

    WHY IT EXISTS. The gateway keeps drained rows for 72 hours or 30 days
    as the server's backup of last resort (the retention spec, 2026-11-15).
    But the drain only ever selects rows with no synced_at, so after a
    server restore those rows sat here, inside the window, and would never
    be sent. This is the other half of that promise.

    BY ROW ID, NOT BY TIME. The request says, per table, the last id the
    server holds, and everything above it drains again. Not "since T":
    machine_events ids are arrival order within one collection path, a
    slower path files older events under newer ids, and a cut by time
    would re-send some rows twice and miss others. Ids are the row's
    identity on the wire and the drain acknowledges them in id order per
    table, so "above N" is exactly the set a restore lost.

    THREE REFUSALS, each recorded:
      * a request for another database. A restored server asking a
        RE-IMAGED gateway for "everything after row 4000" would otherwise
        get a different database's rows 4000 onward.
      * more than REPLAY_MAX_PER_HOUR that actually moved rows.
      * a request that is not the shape of one.
    A table this gateway does not drain is ignored, not refused: the
    server may know of tables an older gateway never had.
    """
    now = now_fn()

    def record(outcome, reason, after, rows):
        conn.execute(
            "INSERT INTO link_replays (requested_at, outcome, reason, "
            "after_json, rows_json) VALUES (?, ?, ?, ?, ?)",
            (now, outcome, reason,
             json.dumps(after, sort_keys=True) if after is not None else None,
             json.dumps(rows, sort_keys=True) if rows is not None else None))
        conn.commit()

    try:
        if request["db_instance_id"] != instance:
            raise LookupError(
                f"asked to replay database {request['db_instance_id']!r}; "
                f"this gateway's is {instance!r}")
        after = request["after"]
        if not isinstance(after, dict) or not after:
            raise ValueError("no tables named")
        wanted = {}
        for table, last in after.items():
            if isinstance(last, bool) or not isinstance(last, int) or last < 0:
                raise ValueError(f"{table}: {last!r} is not a row id")
            if table in tables:
                wanted[table] = last
    except (KeyError, TypeError, ValueError, LookupError) as e:
        reason = f"{type(e).__name__}: {e}"
        record("refused", reason, None, None)
        return False, reason, {}

    recent = conn.execute(
        "SELECT COUNT(*) FROM link_replays WHERE outcome = 'applied' AND "
        "rows_json <> '{}' AND datetime(requested_at) > datetime(?, '-1 hour')",
        (now,)).fetchone()[0]
    if recent >= REPLAY_MAX_PER_HOUR:
        reason = (f"{recent} replays in the last hour; this gateway allows "
                  f"{REPLAY_MAX_PER_HOUR}")
        record("refused", reason, wanted, None)
        return False, reason, {}

    rows = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, last in sorted(wanted.items()):
            count = conn.execute(
                f'UPDATE "{table}" SET synced_at = NULL '
                f'WHERE id > ? AND synced_at IS NOT NULL', (last,)).rowcount
            if count:
                rows[table] = count
            # The server holds nothing above `last`, so neither does this.
            set_counter(conn, f"acked_max:{table}", last)
        conn.execute(
            "INSERT INTO link_replays (requested_at, outcome, reason, "
            "after_json, rows_json) VALUES (?, 'applied', NULL, ?, ?)",
            (now, json.dumps(wanted, sort_keys=True),
             json.dumps(rows, sort_keys=True)))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return True, "", rows


def apply_ack(conn: sqlite3.Connection, acks, instance: str,
              tables: list[str]) -> int:
    """Set synced_at for exactly the rows the server names, and only for
    THIS database's instance id. §6.2: synced_at "is set only by the drain
    client on a durable-commit ACK"."""
    now, done = utc_now(), 0
    allowed = set(tables)
    highest: dict[str, int] = {}
    for ack in acks if isinstance(acks, list) else []:
        try:
            if ack["db_instance_id"] != instance or ack["table"] not in allowed:
                continue
            row_id = int(ack["id"])
            changed = conn.execute(
                f'UPDATE "{ack["table"]}" SET synced_at = ? '
                f'WHERE id = ? AND synced_at IS NULL', (now, row_id)).rowcount
            done += changed
            if changed:
                highest[ack["table"]] = max(highest.get(ack["table"], 0), row_id)
        except (KeyError, TypeError, ValueError):
            continue
    # The high-water mark the heartbeat reports, kept in the same
    # transaction as the acknowledgements it summarises.
    for table, row_id in highest.items():
        if row_id > int(get_counter(conn, f"acked_max:{table}", "0")):
            set_counter(conn, f"acked_max:{table}", row_id)
    conn.commit()
    return done


# --- the heartbeat ---------------------------------------------------------

def _seconds_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.datetime.fromisoformat(iso)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds()


def _optional(conn, sql: str):
    """A read that answers None when the table or column is not there. The
    client may start against a database the poller has not created yet, and
    a heartbeat that says "unknown" is better than no heartbeat."""
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None



def sas_link_status(conn: sqlite3.Connection) -> dict | None:
    """What the poller last said about the serial link, with its own age.

    None when the poller has never written the row -- an older gateway, or
    one that has not started since the table was added. null says "this
    gateway cannot tell you", which is honest; a default of "up" would be a
    guess about the one thing being asked.
    """
    try:
        row = conn.execute(
            "SELECT state, since, updated_at, attempt, max_attempts, detail "
            "FROM sas_link_status WHERE one = 1").fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    state, since, updated_at, attempt, max_attempts, detail = row
    return {
        "state": state,
        "since": since,
        # How long the poller has held this opinion, and how long since it
        # last confirmed it. A large said_s_ago on state 'up' is the
        # poller-is-gone case.
        "for_s": _seconds_since(since),
        "said_s_ago": _seconds_since(updated_at),
        "attempt": attempt,
        "max_attempts": max_attempts,
        "detail": detail,
    }


# The systemd unit whose timer maintains THIS gateway's database. A floor
# unit has one; the lab box has two databases and therefore two units, which
# is why this is a flag rather than a constant. The maintenance PARAMETERS
# are identical between them -- operator, 2026-11-26: "We're getting the
# gateway ready for a live floor, we can't use two different parameters."
# Only the database path and the unit name differ, because they must.
DEFAULT_MAINTENANCE_TIMER = "nimasas-db-maintain.timer"
# Asked of systemd, not of the database, so it is cached: the heartbeat
# fires every 15 s and a subprocess per beat is 5,760 forks a day on a
# 1 GHz 32-bit gateway, to answer a question that changes when a human runs
# systemctl.
MAINTENANCE_TIMER_RECHECK_S = 900.0
_TIMER_CACHE: dict = {"unit": None, "at": None, "state": None}


def maintenance_timer_state(unit: str = DEFAULT_MAINTENANCE_TIMER, *,
                            clock=time.monotonic, run=None,
                            cache: dict | None = None) -> str | None:
    """'active' | 'inactive' | 'absent', or None when we cannot tell.

    None and 'absent' are DIFFERENT and the server must not merge them.
    'absent' is a measurement: systemd was asked and said the unit is not
    installed -- which is the 2026-11-26 failure, where the lab gateway ran
    six days with no timer and grew 80 MB -> 353 MB in silence. None means
    the question could not be put (no systemctl, a non-Linux host, the call
    failed), and reporting that as 'absent' would cry wolf on every gateway
    that is merely hard to interrogate.
    """
    cache = _TIMER_CACHE if cache is None else cache
    now = clock()
    if (cache.get("unit") == unit and cache.get("at") is not None
            and now - cache["at"] < MAINTENANCE_TIMER_RECHECK_S):
        return cache["state"]
    runner = run if run is not None else subprocess.run
    state: str | None = None
    try:
        done = runner(["systemctl", "show", unit,
                       "--property=LoadState", "--property=ActiveState"],
                      capture_output=True, text=True, timeout=10)
        fields = dict(
            line.split("=", 1) for line in (done.stdout or "").splitlines()
            if "=" in line)
        load = fields.get("LoadState")
        if load == "not-found":
            state = "absent"
        elif load:
            state = "active" if fields.get("ActiveState") == "active" else "inactive"
    except (OSError, ValueError, subprocess.SubprocessError):
        state = None
    cache.update({"unit": unit, "at": now, "state": state})
    return state


def maintenance_status(conn: sqlite3.Connection, unit: str = DEFAULT_MAINTENANCE_TIMER,
                       **kw) -> dict:
    """What the server needs to answer "is this gateway being maintained?"

    Both halves matter and neither is sufficient. `last_done_at` says whether
    the job is happening; `timer` says WHY it is not, which is the difference
    between a gateway that deferred twice today because a player was sitting
    at it and one where nobody ran the install step.
    """
    def one(where: str):
        try:
            return conn.execute(
                "SELECT requested_at, outcome FROM link_maintenance "
                f"WHERE {where} ORDER BY id DESC LIMIT 1").fetchone()
        except sqlite3.Error:
            return None

    done = one("outcome = 'done'")
    attempt = one("outcome <> 'refused'")
    since = _seconds_since(done[0]) if done else None
    return {
        "timer": maintenance_timer_state(unit, **kw),
        "timer_unit": unit,
        "last_done_at": done[0] if done else None,
        "since_done_h": None if since is None else round(since / 3600.0, 2),
        "last_attempt_at": attempt[0] if attempt else None,
        "last_attempt_outcome": attempt[1] if attempt else None,
    }


def build_heartbeat(conn: sqlite3.Connection, tables: list[str], db_path: str,
                    *, maintenance_timer: str = DEFAULT_MAINTENANCE_TIMER) -> dict:
    """§10.4 and Appendix B.3. Fields this gateway cannot yet measure are
    sent as null, never as a plausible-looking default: `last_poll_ms` and
    `headroom_pct` are not recorded anywhere the client can read."""
    age = _seconds_since(_optional(conn, "SELECT polled_at FROM meters_current"))
    credits = _optional(conn, "SELECT credits FROM credit_samples "
                              "ORDER BY id DESC LIMIT 1")
    if credits is None:
        credits = _optional(conn, "SELECT sm_current_credits FROM meters_current")
    try:
        stat = os.statvfs(os.path.dirname(os.path.abspath(db_path)))
        disk_critical = stat.f_bavail * stat.f_frsize < 32 * 1024 * 1024
    except (AttributeError, OSError):      # no statvfs on Windows
        disk_critical = None
    try:
        pool = pool_status(conn, low_water=POOL_LOW_WATER)
    except sqlite3.OperationalError:
        # Cannot read the pool: ASK FOR ONE ANYWAY. A refill nobody needed
        # is a few numbers wasted; a refill nobody sent is every cashout
        # becoming a handpay.
        pool = {"available_ids": 0, "low_water": True,
                "active_batch_uuid": None, "pool_epoch": 0}
    return {
        "timestamp": utc_now(),
        "queue_depth": queue_depth(conn, tables),
        "pool_status": pool,
        "egm_status": {
            "online": age is not None and age < EGM_ONLINE_WINDOW_S,
            "meters_age_s": None if age is None else round(age, 1),
            "sas_version": _optional(
                conn, "SELECT sas_version FROM machine_epochs "
                      "ORDER BY id DESC LIMIT 1"),
            "last_poll_ms": None, "headroom_pct": None},
        # THE SERIAL LINK, AS THE POLLER SEES IT -- which is not the same
        # question as egm_status.online above.
        #
        # online is derived from how stale meters_current is, and a stale
        # meter table has two causes that need opposite responses: the
        # cable is out and the poller is reopening it, or the poller itself
        # is gone and systemd is restarting it. Pairing what the poller
        # SAID about the link with how recently it WROTE separates them:
        #
        #   state 'up',           meters fresh  -> healthy
        #   state 'reconnecting', meters stale  -> cable out, poller fighting
        #   state 'up',           meters stale  -> poller gone or wedged
        #   state 'down',         meters stale  -> poller gave up; supervisor
        #
        # This client process keeps heartbeating either way, so the server
        # hears about a dead poller from a live client.
        "sas_link": sas_link_status(conn),
        "disk_critical": disk_critical,
        # IS THIS GATEWAY BEING MAINTAINED, and if not, why not. Added
        # 2026-11-26 after the lab gateway ran six days with no maintenance
        # timer installed -- service active, link up, heartbeat green, and
        # the database growing 80 MB -> 353 MB with nothing able to say so.
        # At ~1,000 units "did somebody enable the timer on that one" is not
        # answerable by looking at the fleet unless the fleet reports it.
        "maintenance": maintenance_status(conn, maintenance_timer),
        "credit_meter": credits,
        # The cabinet this gateway is on NOW: the open machine epoch, with
        # the serial and asset number the poller read off the wire. null
        # until the poller has identified one -- never a guess.
        "machine": machine.current(machine.read_epochs(conn)),
    }


# --- the link --------------------------------------------------------------

class Link:
    """One authenticated connection, and the two counters §10.3 persists."""

    def __init__(self, conn: sqlite3.Connection, cfg: dict, db_path: str,
                 only_tables: frozenset | None = None,
                 maintenance_timer: str = DEFAULT_MAINTENANCE_TIMER):
        self.conn, self.cfg, self.db_path = conn, cfg, db_path
        self.only_tables = only_tables
        self.maintenance_timer = maintenance_timer
        self.sock: socket.socket | None = None
        self.buffer = bytearray()
        self.buffer_since: float | None = None
        self.instance = db_instance_id(conn)
        self.tables: list[str] = []
        # A server-requested maintenance run: (process, its output file,
        # link_maintenance row id, monotonic start), and a finished result
        # still waiting to be told to the server.
        self.maintenance = None
        self.maintenance_report: tuple | None = None
        # A request that was deferred and asked to wait for zero credits:
        # {"command", "row_id", "deadline", "next_look", "next_start",
        #  "waited_from", "why"}. In memory only -- a client restart drops
        # it, and the server is told nothing further about that request.
        self.maintenance_wait: dict | None = None
        # A request that asked to be repeated: {"command", "row_id",
        # "below", "runs": [bytes each run gave back], "pending",
        # "next_start"}. In memory only, like the wait.
        self.maintenance_repeat: dict | None = None
        self.indexed: frozenset = frozenset()

    # -- sending --

    def send(self, message_type: int, payload: dict) -> None:
        """A fresh seq on every send, persisted BEFORE the bytes leave
        (§10.3: "synchronous write per frame"). If the process dies between
        the commit and the send, a number is skipped, which the server
        accepts; the other order would reuse one, which it must not."""
        seq = int(get_counter(self.conn, "tx_seq", "0")) + 1
        set_counter(self.conn, "tx_seq", seq)
        self.conn.commit()
        data = frame.encode(client_id=self.cfg["client_id"],
                            message_type=message_type, seq=seq,
                            payload=payload, key=self.cfg["hmac_key"])
        self.sock.sendall(data)

    def alert(self, kind: str, detail: str, *, quiet: bool = False) -> bool:
        """True if the frame went out, False if the link refused it.

        Every caller but one ignores the answer, because for them the log
        line above is the record and a dropped alert is subsumed by the link
        going down. `_alert_link_down_once` needs it: a once-only alert that
        counted a FAILED send as done would make the 3am give-up silent
        again, which is the whole thing it exists to prevent.
        """
        if not quiet:      # a retry of something already logged
            log(f"ALERT {kind}: {detail}")
        try:
            self.send(MessageType.ALERT, {"timestamp": utc_now(),
                                          "kind": kind, "detail": detail})
            return True
        except OSError:
            return False   # logged above; the link is going down anyway

    def _alert_link_down_once(self, link: dict | None) -> None:
        """Operator, 2026-11-18: `state: "down"` raises a ONE-TIME alert.

        `down` means the poller used all ten reconnect attempts and exited.
        Until now that reached the server only as a FIELD somebody had to
        read, so a gateway gave up quietly -- the opposite of the "loud"
        property the reconnect loop claims for itself.

        Three decisions in here, none of them arbitrary:

        * `reconnecting` does NOT alert. A cable seated badly could flap for
          an hour; ten attempts take 3.3 minutes, and alerting on each
          transition would train somebody to ignore the channel.
        * ONCE is keyed on the gauge's `since`, not on a boolean. `since`
          changes only when the state changes, so the same give-up cannot
          alert twice even if this process restarts and re-reads the row --
          while a link that recovers and gives up AGAIN has a new `since`
          and is a new alert, which it should be.
        * The marker is written only if the send SUCCEEDED, so an alert lost
          to a dead socket is retried on the next heartbeat rather than
          counted as delivered.
        """
        if not link or link.get("state") != "down":
            return
        # A gauge with no `since` is malformed, but it still gets one alert:
        # the default below is "", so "unknown" differs from it exactly once.
        since = str(link.get("since") or "unknown")
        if get_counter(self.conn, "sas_link_down_alerted", "") == since:
            return
        attempt, cap = link.get("attempt"), link.get("max_attempts")
        tried = f" after {attempt} of {cap} attempts" if attempt and cap else ""
        held = link.get("for_s")
        age = f", down for {held:.0f}s" if isinstance(held, (int, float)) else ""
        said = link.get("detail") or "no detail recorded"
        if self.alert("sas_link_down",
                      f"the poller gave up reopening the serial port{tried}"
                      f"{age}; nothing is polling this cabinet until the "
                      f"link is restored (since {since}: {said})"):
            set_counter(self.conn, "sas_link_down_alerted", since)
            self.conn.commit()

    # -- receiving --

    def _rx_floor(self, _client_id: str) -> int:
        return int(get_counter(self.conn, "rx_seq_floor", "0"))

    def frames(self):
        """Yield every complete, authentic frame in the buffer.

        A frame that fails authentication is dropped and alerted, never
        processed (§10.3). The stream stays usable afterwards because the
        length prefix was sane; a length that is NOT sane means the stream
        itself cannot be trusted, and the connection is closed.
        """
        while True:
            try:
                got = frame.decode(
                    bytes(self.buffer),
                    key_for=lambda cid: (self.cfg["hmac_key"]
                                         if cid == self.cfg["client_id"] else None),
                    seq_floor=self._rx_floor,
                    expect_direction=frame.SERVER_TO_GATEWAY)
            except frame.FrameTooShort:
                return
            except (frame.FrameLengthInvalid, frame.FrameVersionUnsupported) as e:
                raise ConnectionError(f"unusable stream: {e}")
            except frame.FrameError as e:
                size = 4 + int.from_bytes(self.buffer[:4], "big")
                del self.buffer[:size]
                self.alert("frame_rejected", f"{type(e).__name__}: {e}")
                continue
            del self.buffer[:got.size]
            set_counter(self.conn, "rx_seq_floor", got.seq)
            self.conn.commit()
            yield got

    # -- handlers --

    def on_pool(self, batch, *, replace: bool) -> None:
        if not isinstance(batch, dict):
            self.alert("pool_rejected", "pool batch is not an object")
            return
        try:
            result = apply_batch(self.conn, client_id=self.cfg["client_id"],
                                 batch=batch,
                                 public_key=self.cfg["server_public_key"],
                                 replace=replace)
        except sqlite3.Error as e:
            # §6.5 / B.2: a failed pool write is an alert, never a shrug.
            self.alert("pool_write_failed", f"{type(e).__name__}: {e}")
            return
        kind = "POOL_REPLACE" if replace else "top-up"
        if result.alert:
            self.alert("pool_rejected", f"{kind} {result.verdict}: {result.detail}")
        else:
            log(f"pool {kind} {result.verdict}: {result.detail}")
        # The acknowledgement IS the heartbeat's epoch report (§7.4, §10.2).
        # Send one now rather than up to 15 s from now, so the server stops
        # treating two epochs as live as soon as it truthfully can.
        self.heartbeat()

    def on_replay(self, request) -> None:
        applied, reason, rows = apply_replay(self.conn, request, self.instance,
                                             self.tables)
        if not applied:
            self.alert("replay_refused", reason)
            return
        total = sum(rows.values())
        log(f"REPLAY applied: {total} acknowledged row(s) back in the drain "
            f"({rows or 'nothing above what the server holds'})")
        # Told to the server through the alert channel on purpose: it is
        # the one stream a person reads, and "this gateway is re-sending
        # 40,000 rows because you asked" should not be inferred from load.
        if total:
            self.alert("replay_applied",
                       f"{total} row(s) will drain again: {rows}")

    # -- maintenance the server asked for --

    def on_maintain(self, request, *, clock=time.monotonic) -> None:
        """Start `db_maintain.py run` because the server asked.

        The gateway is headless: after commissioning nobody logs in, so
        the only parties that can decide "prune and reclaim NOW" are the
        timer on the gateway and the server. This is the server's way.

        WHAT THE SERVER MAY CHOOSE, and it is two switches and nothing
        else: whether to prune acknowledged rows, and whether to insist on
        zero credits first. It cannot name a database, a path, a retention
        or an argument. The command line is assembled HERE from fixed
        strings; nothing from the request reaches it but those two
        booleans. A request key this does not know is ignored.

        The credit gate defaults ON and stays on unless the server says
        otherwise in so many words, because of what it guards: a cashout
        that arrives while the write path is busy runs out its ten seconds
        (§15.7) and becomes a handpay.

        IT RUNS AS A SEPARATE PROCESS, and this function returns at once.
        A run can take two minutes; this loop also carries every ticket-in
        question to the server, and a ticket must not wait behind
        housekeeping. The outcome goes back as an ALERT when the process
        ends (pump_maintenance), and is kept in link_maintenance either
        way.

        Never `enable-auto-vacuum`. That is a full VACUUM, it needs the
        poller stopped, and it is not something to start from a distance.
        """
        now = utc_now()

        def refuse(reason, options=None):
            self.conn.execute(
                "INSERT INTO link_maintenance (requested_at, options_json, "
                "outcome, finished_at, output) VALUES (?, ?, 'refused', ?, ?)",
                (now, json.dumps(options, sort_keys=True) if options else None,
                 now, reason))
            self.conn.commit()
            self.alert("maintenance_refused", reason)

        if not isinstance(request, dict):
            refuse("the request is not an object")
            return
        options = {}
        for key, default in (("prune_acked", True),
                             ("require_zero_credits", True)):
            value = request.get(key, default)
            if not isinstance(value, bool):
                refuse(f"{key} must be true or false, got {value!r}")
                return
            options[key] = value
        wait_s = request.get("wait_for_zero_s", 0)
        if (isinstance(wait_s, bool) or not isinstance(wait_s, int)
                or not 0 <= wait_s <= MAINTAIN_WAIT_MAX_S):
            refuse("wait_for_zero_s must be a whole number of seconds from 0 "
                   f"to {MAINTAIN_WAIT_MAX_S}, got {wait_s!r}")
            return
        if wait_s and not options["require_zero_credits"]:
            refuse("wait_for_zero_s makes no sense with the credit gate off",
                   options)
            return
        options["wait_for_zero_s"] = wait_s
        below = request.get("repeat_min_freed_bytes", 0)
        if (isinstance(below, bool) or not isinstance(below, int)
                or not 0 <= below <= MAINTAIN_REPEAT_MAX_BYTES):
            refuse("repeat_min_freed_bytes must be a whole number of bytes from 0 "
                   f"to {MAINTAIN_REPEAT_MAX_BYTES}, got {below!r}")
            return
        options["repeat_min_freed_bytes"] = below
        if self.maintenance_repeat is not None:
            refuse("an earlier request is still repeating", options)
            return
        if self.maintenance is not None:
            refuse("a maintenance run is already in progress", options)
            return
        if self.maintenance_wait is not None:
            refuse("an earlier request is still waiting for zero credits",
                   options)
            return
        # source='server' ONLY. This limit exists to stop a SERVER hammering
        # the gateway; a local timer doing its scheduled job must not spend
        # the server's budget and get the next real request refused. Rows
        # written before the column existed default to 'server', which is
        # what they all were.
        recent = self.conn.execute(
            "SELECT COUNT(*) FROM link_maintenance WHERE outcome <> 'refused' "
            "AND source = 'server' "
            "AND datetime(requested_at) > datetime(?, '-1 hour')",
            (now,)).fetchone()[0]
        if recent >= MAINTAIN_MAX_PER_HOUR:
            refuse(f"{recent} maintenance runs in the last hour; this gateway "
                   f"allows {MAINTAIN_MAX_PER_HOUR}", options)
            return
        if not MAINTAIN_SCRIPT.exists():
            refuse(f"{MAINTAIN_SCRIPT} is not in this checkout", options)
            return

        # --no-record: db_maintain.py writes its own link_maintenance row
        # (source='local') since 2026-11-26, and this method writes one too.
        # Without the flag a server-requested run would appear twice.
        command = [sys.executable, str(MAINTAIN_SCRIPT), "run",
                   "--db", self.db_path, "--no-record"]
        if options["prune_acked"]:
            command.append("--prune-acked")
        if options["require_zero_credits"]:
            command.append("--require-zero-credits")
        size = self._db_bytes()
        output = tempfile.TemporaryFile(mode="w+")
        try:
            process = subprocess.Popen(command, stdout=output,
                                       stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL)
        except OSError as e:
            output.close()
            refuse(f"could not start the maintenance run: {e}", options)
            return
        row_id = self.conn.execute(
            "INSERT INTO link_maintenance (requested_at, options_json, outcome) "
            "VALUES (?, ?, 'running')",
            (now, json.dumps(options, sort_keys=True))).lastrowid
        self.conn.commit()
        self.maintenance = (process, output, row_id, clock(), size)
        if wait_s:
            self.maintenance_wait = {
                "command": command, "row_id": row_id, "why": "",
                "waited_from": clock(), "deadline": clock() + wait_s,
                "next_look": 0.0, "announced": False, "holding": False,
                "next_start": clock() + MAINTAIN_WAIT_RETRY_S}
        if below:
            self.maintenance_repeat = {
                "command": command, "row_id": row_id, "below": below,
                "runs": [], "pending": False, "next_start": 0.0}
        log(f"maintenance started at the server's request: {options}")

    def _record_maintenance(self, row_id, outcome, summary, *,
                            finished=True) -> None:
        """Best effort. The alert and the log line are the report; this
        row is the gateway's own copy, and a locked database must not cost
        the report."""
        try:
            self.conn.execute(
                "UPDATE link_maintenance SET outcome = ?, finished_at = ?, "
                "output = ? WHERE id = ?",
                (outcome, utc_now() if finished else None, summary, row_id))
            self.conn.commit()
        except sqlite3.Error as e:
            log(f"could not record maintenance outcome {outcome!r}: {e}")

    def _db_bytes(self) -> int:
        """The database file alone. What a run "gave back" is how much
        smaller this got: measured, not read out of the tool's wording."""
        try:
            return os.path.getsize(self.db_path)
        except OSError:
            return 0

    def _start_again(self, command, row_id, now) -> str | None:
        """Start the tool for a request that already has its row. Returns
        why not, or None."""
        size = self._db_bytes()
        output = tempfile.TemporaryFile(mode="w+")
        try:
            process = subprocess.Popen(command, stdout=output,
                                       stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL)
        except OSError as e:
            output.close()
            return f"could not start the maintenance run: {e}"
        self.maintenance = (process, output, row_id, now, size)
        return None

    def _repeat_summary(self, repeat) -> str:
        runs = repeat["runs"]
        return (f"{len(runs)} run(s), {sum(runs) / 1e6:.1f} MB returned in "
                f"total ({', '.join(f'{r / 1e6:.1f}' for r in runs)})")

    def _pump_maintenance_repeat(self, clock) -> None:
        repeat = self.maintenance_repeat
        if (repeat is None or not repeat["pending"]
                or self.maintenance is not None):
            return
        now = clock()
        if now < repeat["next_start"]:
            return
        why = self._start_again(repeat["command"], repeat["row_id"], now)
        if why is not None:
            repeat["next_start"] = now + MAINTAIN_WAIT_RETRY_S
            return
        repeat["pending"] = False
        log(f"maintenance run {len(repeat['runs']) + 1} starting (repeat)")

    def _credits_read_zero(self) -> bool:
        """The client's own cheap look, used only to decide when to START
        the tool again. It is not the gate: the tool checks for itself,
        including how old the figure is, and its answer is the one that
        counts."""
        credits = _optional(self.conn, "SELECT credits FROM credit_samples "
                                       "ORDER BY id DESC LIMIT 1")
        if credits is None:
            credits = _optional(self.conn,
                                "SELECT sm_current_credits FROM meters_current")
        return credits == 0

    def _pump_maintenance_wait(self, clock) -> None:
        wait = self.maintenance_wait
        if (wait is None or not wait["holding"]
                or self.maintenance is not None):
            return
        now = clock()
        if now >= wait["deadline"]:
            minutes = (wait["deadline"] - wait["waited_from"]) / 60
            repeat, outcome = self.maintenance_repeat, "deferred"
            if repeat is not None and repeat["runs"]:
                # Runs already happened; the machine then took credits and
                # kept them. That is work done, cut short -- not a deferral.
                outcome = "done"
                summary = (f"{self._repeat_summary(repeat)} | stopped early: "
                           f"the machine held credits until the {minutes:.0f} "
                           f"min wait ran out. Last answer: {wait['why']}")[-1400:]
            else:
                summary = (f"waited {minutes:.0f} min and the machine was "
                           "never at zero credits; nothing was pruned or "
                           f"reclaimed. Last answer: {wait['why']}")[-1400:]
            self.maintenance_wait = self.maintenance_repeat = None
            log(f"maintenance {outcome}: {summary}")
            self.maintenance_report = (f"maintenance_{outcome}", summary)
            self._record_maintenance(wait["row_id"], outcome, summary)
            return
        if now < wait["next_look"] or now < wait["next_start"]:
            return
        wait["next_look"] = now + MAINTAIN_WAIT_LOOK_S
        try:
            zero = self._credits_read_zero()
        except sqlite3.Error:
            return                          # the poller has the lock; look again
        if not zero:
            return
        wait["next_start"] = now + MAINTAIN_WAIT_RETRY_S
        why = self._start_again(wait["command"], wait["row_id"], now)
        if why is not None:
            wait["why"] = why
            return
        wait["holding"] = False
        log("credits read zero: starting the maintenance run that was waiting")

    def pump_maintenance(self, *, clock=time.monotonic) -> None:
        """Collect a finished run and tell the server how it ended. Called
        every tick; does nothing while a run is still going."""
        if self.maintenance is not None:
            process, output, row_id, started, size_before = self.maintenance
            code = process.poll()
            if code is None and clock() - started > MAINTAIN_KILL_AFTER_S:
                process.kill()
                process.wait()
                code = "killed"
            if code is not None:
                output.seek(0)
                text = output.read()
                output.close()
                self.maintenance = None
                # The tool's own lines, minus its timestamps: what was
                # pruned, what was reclaimed, or why it stood down.
                lines = [line.split("] ", 1)[-1].strip()
                         for line in text.splitlines() if line.strip()]
                if code == "killed":
                    outcome = "failed"
                    lines.append(f"killed after {MAINTAIN_KILL_AFTER_S:.0f}s")
                elif code != 0:
                    outcome = "failed"
                    lines.append(f"exit status {code}")
                elif any(line.startswith("deferred:") for line in lines):
                    outcome = "deferred"
                else:
                    outcome = "done"
                summary = " | ".join(lines)[-1400:] or "(no output)"
                wait = self.maintenance_wait
                repeat = self.maintenance_repeat
                if (outcome == "done" and repeat is not None
                        and repeat["row_id"] == row_id):
                    gave = max(0, size_before - self._db_bytes())
                    repeat["runs"].append(gave)
                    log(f"maintenance run {len(repeat['runs'])}: "
                        f"{gave / 1e6:.1f} MB returned | {summary}")
                    if (gave >= repeat["below"]
                            and len(repeat["runs"]) < MAINTAIN_REPEAT_MAX_RUNS):
                        repeat["pending"] = True
                        repeat["next_start"] = clock() + MAINTAIN_REPEAT_PAUSE_S
                        outcome = None
                    else:
                        why = ("the run limit was reached"
                               if gave >= repeat["below"] else
                               f"the last run returned under "
                               f"{repeat['below'] / 1e6:.1f} MB")
                        summary = (f"{self._repeat_summary(repeat)}; stopped "
                                   f"because {why} | last run: {summary}")[-1400:]
                elif (outcome == "deferred" and repeat is not None
                        and repeat["runs"]
                        and not (wait is not None and clock() < wait["deadline"])):
                    outcome = "done"
                    summary = (f"{self._repeat_summary(repeat)} | stopped "
                               f"early: {summary}")[-1400:]
                elif outcome == "failed" and repeat is not None and repeat["runs"]:
                    summary = (f"{self._repeat_summary(repeat)} before this "
                               f"| {summary}")[-1400:]
                if outcome is None:
                    pass
                elif wait is not None and wait["row_id"] == row_id:
                    if outcome == "deferred" and clock() < wait["deadline"]:
                        wait["holding"] = True
                        # Not an ending. Hold the request, and say so ONCE:
                        # a server that asked and heard nothing for half an
                        # hour could not tell waiting from lost.
                        wait["why"] = summary
                        self._record_maintenance(row_id, "waiting", summary,
                                                 finished=False)
                        if wait["announced"]:
                            # Hardware, 2026-11-19: a run started from the
                            # wait and then nothing was ever printed. A
                            # second deferral was the one ending that said
                            # nothing anywhere. The server is still told
                            # once; this log is told every time.
                            log(f"maintenance still waiting: {summary}")
                        if not wait["announced"]:
                            wait["announced"] = True
                            left = (wait["deadline"] - clock()) / 60
                            note = (f"{summary} | holding the request for up "
                                    f"to {left:.0f} more min and starting it "
                                    "when credits read zero")[-1400:]
                            log(f"maintenance waiting: {note}")
                            self.maintenance_report = ("maintenance_waiting", note)
                        outcome = None
                if outcome is not None:
                    self.maintenance_wait = self.maintenance_repeat = None
                    # Said and queued BEFORE the row is written. The write
                    # can meet the poller's lock, and an outcome lost to
                    # that is a run nobody ever hears the end of.
                    log(f"maintenance {outcome}: {summary}")
                    self.maintenance_report = (f"maintenance_{outcome}", summary)
                    self._record_maintenance(row_id, outcome, summary)
        self._pump_maintenance_wait(clock)
        self._pump_maintenance_repeat(clock)
        if self.maintenance_report is not None and self.sock is not None:
            # Kept until it is actually sent. A run that finishes while the
            # link is down must still be reported when it comes back.
            if self.alert(*self.maintenance_report, quiet=True):
                self.maintenance_report = None

    def on_invite(self, payload: dict) -> None:
        if payload.get("pool_topup") is not None:
            self.on_pool(payload["pool_topup"], replace=False)
        if payload.get("replay") is not None:
            # BEFORE collecting events, so the rows it puts back are in
            # this very batch.
            self.on_replay(payload["replay"])
        if payload.get("maintain") is not None:
            self.on_maintain(payload["maintain"])
        try:
            max_events = max(0, min(int(payload.get("max_events", 0)), 1000))
        except (TypeError, ValueError):
            max_events = 0
        epochs = machine.read_epochs(self.conn)
        events, oversize = collect_events(self.conn, self.tables, max_events,
                                          epochs=epochs)
        for table, row_id, size in oversize:
            self.alert("event_too_large",
                       f"{table} id {row_id} is {size} bytes, over the "
                       f"{DRAIN_BYTE_BOUND}-byte drain bound; it cannot be sent")
        # The epochs these events name, in full, in the SAME frame. The
        # server must be able to say what machine a row belongs to from the
        # batch alone -- not from a heartbeat that may have described a
        # different cabinet by the time this backlog drains.
        named = {e.get("epoch_id") for e in events}
        # The link state in the SAME frame as the data, for the same reason
        # the epochs are. A PULL_INVITE is the moment the server is about to
        # ACT on these rows, so it must be able to say "this batch came from
        # a gateway whose cable was down" from the batch alone, without
        # correlating against a heartbeat that arrived at some other second.
        # It does NOT replace the heartbeat's copy: an invite only tells the
        # server what it thought to ask, and a gateway that has gone quiet
        # at 3am is exactly the case no pull ever reaches.
        self.send(MessageType.EVENT_BATCH,
                  {"invite_id": payload.get("invite_id"),
                   "db_instance_id": self.instance, "events": events,
                   "epochs": [e for e in epochs if e["id"] in named],
                   "sas_link": sas_link_status(self.conn)})

    def on_validation_response(self, payload: dict) -> None:
        """Write the server's answer into the poller's question -- and only
        if the poller is still waiting for it. A late answer to a query the
        poller already refused must not turn into a payment on the next
        ticket, which is what an unconditional UPDATE would allow."""
        try:
            query_id = int(payload["query_id"])
            decision = payload["decision"]
            if decision not in ("allow", "reject"):
                raise ValueError(f"decision {decision!r}")
            values = (decision, int(payload["transfer_code"]),
                      int(payload.get("amount_cents") or 0),
                      int(payload.get("restricted_expiration") or 0),
                      int(payload.get("pool_id") or 0),
                      str(payload.get("reason") or ""))
        except (KeyError, TypeError, ValueError) as e:
            self.alert("validation_response_invalid", f"{type(e).__name__}: {e}")
            return
        changed = self.conn.execute(
            "UPDATE validation_queries SET state = 'answered', answered_at = ?, "
            "decision = ?, transfer_code = ?, amount_cents = ?, "
            "restricted_expiration = ?, pool_id = ?, reason = ? "
            "WHERE id = ? AND state = 'pending'",
            (utc_now(),) + values + (query_id,)).rowcount
        self.conn.commit()
        if changed:
            log(f"ticket-in query {query_id} answered: {values[0]} "
                f"0x{values[1]:02X} {values[5]}")
        else:
            log(f"late VALIDATION_RESPONSE for query {query_id} ignored: the "
                "poller had already refused that ticket")

    def dispatch(self, got: frame.Frame) -> None:
        kind = got.message_type
        if kind == MessageType.PULL_INVITE:
            self.on_invite(got.payload)
        elif kind == MessageType.ACK:
            done = apply_ack(self.conn, got.payload.get("acks"),
                             self.instance, self.tables)
            if done:
                log(f"ACK: {done} row(s) marked synced")
        elif kind == MessageType.POOL_REPLACE:
            self.on_pool(got.payload, replace=True)
        elif kind == MessageType.VALIDATION_RESPONSE:
            self.on_validation_response(got.payload)
        elif kind == MessageType.AFT_TRANSFER:
            self.on_aft_transfer(got.payload)
        elif kind in UNSUPPORTED:
            self.alert("unsupported_message",
                       f"{UNSUPPORTED[kind]} is not implemented by this "
                       "gateway client; the request was not acted on")

    # -- gateway-initiated --

    def heartbeat(self) -> None:
        self.tables = drain_tables(self.conn, self.only_tables)
        if frozenset(self.tables) != self.indexed:
            ensure_drain_indexes(self.conn, self.tables)
            self.indexed = frozenset(self.tables)
        beat = build_heartbeat(self.conn, self.tables, self.db_path,
                               maintenance_timer=self.maintenance_timer)
        # An epoch id means nothing without the database it counts in: a
        # re-imaged gateway starts again from epoch 1.
        beat["db_instance_id"] = self.instance
        # The highest row id the server has acknowledged, per table. A
        # server that holds less than this has lost rows, and can say so.
        beat["acked"] = acked_high_water(self.conn, self.tables)
        self.send(MessageType.HEARTBEAT, beat)
        # AFTER the beat, not before: the heartbeat is the obligation and the
        # alert is the addition, so a refused alert must not cost the
        # liveness signal. The beat already carries the state this explains.
        self._alert_link_down_once(beat.get("sas_link"))

    def pump_queries(self) -> None:
        """Ticket-in: send what the poller asked, report how it ended."""
        try:
            pending = self.conn.execute(
                "SELECT id, validation_number, parsing_code, "
                "reported_amount_cents, asked_at FROM validation_queries "
                "WHERE state = 'pending' AND sent_at IS NULL ORDER BY id"
            ).fetchall()
            finished = self.conn.execute(
                "SELECT id, validation_number, completion_status, completed_at, "
                "decision, outcome FROM validation_queries "
                "WHERE completion_status IS NOT NULL AND result_sent_at IS NULL "
                "AND sent_at IS NOT NULL ORDER BY id").fetchall()
        except sqlite3.OperationalError:
            return      # the poller has not created the table: nothing asked
        for query_id, number, parsing, reported, asked_at in pending:
            self.send(MessageType.VALIDATION_QUERY,
                      {"query_id": query_id, "validation_number": number,
                       "parsing_code": parsing,
                       "reported_amount_cents": reported, "asked_at": asked_at})
            self.conn.execute("UPDATE validation_queries SET sent_at = ? "
                              "WHERE id = ?", (utc_now(), query_id))
            self.conn.commit()
            # One line per question, and one per answer below. A ticket the
            # poller refused for want of an answer used to leave NOTHING in
            # this log, so "did the question even leave the gateway" could
            # not be read off anything.
            log(f"ticket-in query {query_id} ...{str(number)[-6:]} sent "
                f"(asked {asked_at})")
        for query_id, number, status, completed_at, decision, outcome in finished:
            self.send(MessageType.REDEMPTION_RESULT,
                      {"query_id": query_id, "validation_number": number,
                       "machine_status": status, "completed_at": completed_at,
                       "gateway_outcome": outcome})
            self.conn.execute("UPDATE validation_queries SET result_sent_at = ? "
                              "WHERE id = ?", (utc_now(), query_id))
            self.conn.commit()

    def on_aft_transfer(self, payload: dict) -> None:
        """Hand a server AFT push to the poller. §8.3.

        THIS PROCESS CANNOT DO IT. One serial port, one master: the poller
        owns the line and is the only thing that may speak to the cabinet.
        So this takes the same route as a WfA pull -- a row in
        host_requests -- rather than inventing a second path to the wire.
        That also means the transfer happens BETWEEN exchanges, under the
        poller's existing AFT caps, instead of cutting into a poll.

        Refusing is the poller's job, not this one's: it is the process
        that knows whether --allow-server-aft was given, and the refusal
        belongs in the same row as the request so the reason survives.
        """
        try:
            amounts = {k: int(payload.get(f"{k}_cents") or 0)
                       for k in ("cashable", "restricted", "nonrestricted")}
        except (TypeError, ValueError):
            self.alert("aft_malformed", "AFT_TRANSFER with non-integer cents")
            return
        total = sum(amounts.values())
        if total <= 0 or any(v < 0 for v in amounts.values()):
            self.alert("aft_malformed",
                       f"AFT_TRANSFER with nothing to move ({amounts})")
            return
        transfer_id = str(payload.get("transfer_id") or "")[:64]
        try:
            self.conn.execute(
                "INSERT INTO host_requests (requested_at, kind, deadline_ms, "
                "state, request_json) VALUES (?, 'aft_push', ?, 'pending', ?)",
                (utc_now(), int(payload.get("deadline_ms") or 60000),
                 json.dumps({**amounts,
                             "pool_id": int(payload.get("pool_id") or 0),
                             "expiration": int(payload.get("expiration") or 0),
                             "transfer_id": transfer_id})))
            self.conn.commit()
        except sqlite3.Error as e:
            self.alert("aft_not_queued",
                       f"AFT_TRANSFER {transfer_id} could not be handed to "
                       f"the poller: {e}")
            return
        log(f"AFT_TRANSFER {transfer_id} handed to the poller: "
            + ", ".join(f"{v / 100:,.2f} {k}" for k, v in amounts.items() if v))

    # -- the loop --

    def serve(self, *, clock=time.monotonic) -> None:
        """Run one connection until it fails. Raises OSError/ConnectionError."""
        next_heartbeat = clock()
        # BELT AND BRACES BESIDE KEEPALIVE. Keepalive is the OS noticing a
        # dead peer; this is the application noticing a peer that is still
        # reachable but has stopped talking -- a server wedged, paused at
        # a debugger, or behind a NAT that keeps the socket alive. The
        # server invites roughly once a second, so two minutes of total
        # silence is not a slow link, it is a link that is finished.
        last_rx = clock()
        while True:
            now = clock()
            if now - last_rx > SERVER_SILENCE_TIMEOUT_S:
                raise ConnectionError(
                    f"nothing from the server in {now - last_rx:.0f}s "
                    f"(limit {SERVER_SILENCE_TIMEOUT_S:.0f}s); treating the "
                    "link as down and reconnecting")
            if now >= next_heartbeat:
                self.heartbeat()
                # Flat 15 s from the SCHEDULE, not from when this one
                # finished, so a slow drain cannot stretch the interval.
                next_heartbeat = max(next_heartbeat + HEARTBEAT_INTERVAL_S,
                                     now + 1.0)
            self.pump_queries()
            self.pump_maintenance()
            readable, _, _ = select.select([self.sock], [], [], TICK_S)
            if readable:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise ConnectionError("the server closed the connection")
                last_rx = clock()
                if not self.buffer:
                    self.buffer_since = clock()
                self.buffer += chunk
                for got in self.frames():
                    self.dispatch(got)
                if not self.buffer:
                    self.buffer_since = None
            if (self.buffer and self.buffer_since is not None
                    and clock() - self.buffer_since > FRAME_COMPLETION_TIMEOUT_S):
                raise ConnectionError("frame-completion timeout: a partial "
                                      "frame sat unfinished")


def run(args) -> int:
    keyfile = Path(args.key_file) if args.key_file else default_keyfile(args.db)
    cfg = load_keyfile(keyfile)
    only = (frozenset(t.strip() for t in args.drain_tables.split(",") if t.strip())
            if args.drain_tables else None)
    conn = open_gateway_db(args.db, busy_timeout_ms=args.busy_timeout_ms)
    conn.execute("PRAGMA synchronous = FULL")
    ensure_link_schema(conn)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = "
                        "'validation_pool'").fetchone():
        log(f"NOTE: {args.db} has no validation_pool table yet -- the poller "
            "has never run against it. Heartbeats will be sent; a pool "
            "top-up will be refused until the poller has created the table.")
    # Before anything touches the sequence counters.
    _lock = claim_single_instance(args.db)          # noqa: F841 -- held
    link = Link(conn, cfg, args.db, only,
                maintenance_timer=getattr(args, "maintenance_timer",
                                          DEFAULT_MAINTENANCE_TIMER))
    log(f"gateway client for {cfg['client_id']}, database {args.db} "
        f"(instance {link.instance}), server "
        f"{cfg['server_host']}:{cfg['server_port']}")
    delay = RECONNECT_MIN_S
    while True:
        try:
            link.sock = socket.create_connection(
                (cfg["server_host"], cfg["server_port"]), timeout=10)
            link.sock.settimeout(10)
            link.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # So a pulled cable becomes an error in about a minute rather
            # than in the fifteen the kernel's retransmit budget allows.
            frame.enable_keepalive(link.sock)
            link.buffer.clear()
            ensure_link_schema(conn)       # the poller may have started since
            log("connected")
            delay = RECONNECT_MIN_S
            link.serve()
        except KeyboardInterrupt:
            log("stopped")
            return 0
        except (OSError, ConnectionError) as e:
            # §8.1 / design rule 5: silence is not a fault of the machine.
            # Nothing is disabled here; the poller keeps dispensing from
            # the pool it holds, and this just tries again.
            log(f"link down: {type(e).__name__}: {e}; retrying in {delay:.0f}s")
        except sqlite3.OperationalError as e:
            # A LOCKED DATABASE IS AS TRANSIENT AS A DROPPED SOCKET, and
            # until 2026-11-13 it was the only one of the two that killed
            # the process. On the lab gateway:
            #
            #   File ".../client.py", line 377, in frames
            #     set_counter(self.conn, "rx_seq_floor", got.seq)
            #   sqlite3.OperationalError: database is locked
            #
            # The client died mid-frame, the server logged WinError 64,
            # and two queued AFT pushes sat undelivered for seven minutes
            # with nothing on either side saying why. The handler above
            # has the right words for exactly this case -- the poller
            # keeps dispensing from the pool it holds, and this just
            # tries again -- and a lock got none of them.
            #
            # open_gateway_db() already sets WAL and a 30 s busy timeout,
            # so reaching here means a writer held the database longer
            # than that (a schema migration on the poller's restart is
            # the likely one) or two writers deadlocked in a way
            # busy_timeout cannot wait out. Neither is a reason to stop
            # being a gateway. Retried on the same backoff as a network
            # failure; if the database is genuinely broken rather than
            # busy, this logs it once per attempt rather than exiting
            # into a file nobody is tailing.
            # An exception mid-write leaves this connection inside a
            # transaction; the next attempt must not start in one.
            if conn.in_transaction:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            log(f"database unavailable: {e}; retrying in {delay:.0f}s. The "
                "poller keeps dispensing from the pool it holds.")
        finally:
            if link.sock is not None:
                try:
                    link.sock.close()
                except OSError:
                    pass
                link.sock = None
        if args.once:
            return 1
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            log("stopped")
            return 0
        delay = min(delay * 2, RECONNECT_MAX_S)


def commission(args) -> int:
    """§10.5: the technician enters the key ON the device. Nothing here
    travels over the link being commissioned."""
    keyfile = Path(args.key_file) if args.key_file else default_keyfile(args.db)
    if keyfile.exists() and not args.force:
        print(f"{keyfile} already exists. A gateway holds one key; pass "
              "--force to replace it (the old key stops working the moment "
              "the server recommissions this client_id).", file=sys.stderr)
        return 2
    data = {"client_id": args.client_id, "hmac_key": args.hmac_key.strip(),
            "server_public_key": args.server_public_key.strip(),
            "server_host": args.server_host, "server_port": args.server_port}
    # Create it unreadable to anyone else BEFORE the key is written into it.
    fd = os.open(str(keyfile), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
    load_keyfile(keyfile)                  # refuse to leave a bad one behind
    conn = open_gateway_db(args.db)
    ensure_link_schema(conn)
    # §10.3: "Recommissioning resets key and sequence state atomically on
    # both sides. Otherwise a recommissioned gateway starting from zero has
    # every frame rejected." The server reset its side when it minted the key.
    set_counter(conn, "tx_seq", 0)
    set_counter(conn, "rx_seq_floor", 0)
    conn.commit()
    print(f"commissioned {args.client_id}: key file {keyfile} (mode 0600), "
          f"sequence counters reset, database instance {db_instance_id(conn)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m gateway.client",
        description="The gateway's side of the server link (spec §10): "
        "heartbeat, drain, signed pool top-up and replacement, and the "
        "ticket-in validation query. Never opens the serial port.")
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("commission", help="store this gateway's key material "
                       "(run once, with the values the server printed)")
    c.add_argument("--db", default="gateway.sqlite3")
    c.add_argument("--key-file", default=None,
                   help="default: the database path with .linkkey.json")
    c.add_argument("--client-id", required=True,
                   help="the floor position's stable identity, e.g. LAB-001")
    c.add_argument("--hmac-key", required=True, help="hex, from the server")
    c.add_argument("--server-public-key", required=True,
                   help="hex Ed25519 public key, from the server")
    c.add_argument("--server-host", required=True)
    c.add_argument("--server-port", type=int, default=5020)
    c.add_argument("--force", action="store_true")
    c.set_defaults(func=commission)

    r = sub.add_parser("run", help="connect and stay connected")
    r.add_argument("--db", default="gateway.sqlite3")
    r.add_argument("--key-file", default=None)
    r.add_argument("--drain-tables", default=None, metavar="A,B,...",
                   help="drain only these tables (default: every table with "
                   "a synced_at column). A lab database that has never been "
                   "drained can hold hundreds of thousands of machine_events "
                   "rows; naming the ticket tables here gets the money "
                   "records to the server without waiting behind them.")
    r.add_argument("--maintenance-timer", default=DEFAULT_MAINTENANCE_TIMER,
                   metavar="UNIT",
                   help="the systemd timer that maintains THIS database, "
                        "reported in the heartbeat so the server can see "
                        "gateways nobody enabled it on (default: "
                        "%(default)s). A floor unit has one and should not "
                        "set this. The lab box has two databases, so the "
                        "soak client passes nimasas-db-maintain-soak.timer. "
                        "The maintenance parameters are identical either "
                        "way; only the database and the unit name differ.")
    # For tests, which cannot wait out thirty real seconds to see what a
    # lock timeout does. Not a tuning knob: §6.1 fixes it at 30 s.
    r.add_argument("--busy-timeout-ms", type=int, default=30_000,
                   help=argparse.SUPPRESS)
    r.add_argument("--once", action="store_true",
                   help="exit when the connection drops instead of retrying")
    r.set_defaults(func=run)
    return parser


def _safe_console() -> None:
    """A Windows console in a legacy code page cannot print every character
    in this program's own help text, and Python's answer to that is to
    raise in the middle of a log line. Replace what cannot be shown."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    _safe_console()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
