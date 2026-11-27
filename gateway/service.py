#!/usr/bin/env python3
"""A STAND-IN for the server-side gateway service (spec §11) and for the
part of the CMS a gateway needs (§12.1, §12.2). Runs on a laptop.

    python -m gateway.service init
    python -m gateway.service commission LAB-001 --gateway-host-hint 192.168.1.10
    python -m gateway.service serve
    python -m gateway.service add-ticket 000000000000000001 --amount 25.00
    python -m gateway.service replace-pool LAB-001
    python -m gateway.service status

WHAT THIS IS FOR. The poller and the gateway client can only be proved
against something that speaks the other end of §10. RamSys has not built
that end yet, so this is the smallest honest version of it: enough to fill
a gateway's validation pool with signed batches, drain its events, and
answer ticket-in with the §12.2 state table and a real lease.

WHAT THIS IS NOT, and each of these is a decision rather than an omission:

  * It is not the CMS. Its "database" is one SQLite file, which needs
    nothing installed -- sqlite3 is part of Python. The real gateway
    service writes to the existing CMS database through that engine's own
    driver (§11); every table here is engine-neutral on purpose.
  * It mints validation numbers from a random generator. §11.2 says the
    real service uses "the existing validation-number scheme"; that scheme
    is RamSys's and is not reproduced here. Numbers minted by this program
    are LAB numbers.
  * It persists sequence counters on every frame. §10.3 batches them on
    the server because of write amplification at 127 heartbeats a second;
    with one gateway there is nothing to amplify.
  * It does not implement ON_DEMAND_PULL, ROTATE_KEY, REVOKE, DISABLE or
    ENABLE. Where §12.5 calls for an expedited pull it sends an immediate
    PULL_INVITE instead, which has the same effect when the link is up and
    none when it is down.
  * It signs with gateway/ed25519.py, which is not constant-time. The
    private key of a production service belongs in a restricted store or
    an HSM (§7.3, §16) and should be used through a vetted library.

It is, however, the same wire contract byte for byte, and it runs
unchanged on Windows and Linux: standard library only, no signals, no
fork, no path assumptions.

Requires Python 3.10 or newer (the same floor as the poller).
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 10):          # must run before anything 3.10-only
    sys.exit("gateway.service needs Python 3.10 or newer; this is Python "
             "%d.%d. Install a current Python alongside the old one (it does "
             "not have to replace it) and run this with that interpreter."
             % sys.version_info[:2])

import argparse
import asyncio
import datetime
import getpass
import json
import os
import secrets
import socket
import sqlite3
import time
import uuid
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import ed25519, frame, machine
from gateway.frame import MessageType
from gateway.pool import (MAX_BATCH_NUMBERS, NUMBER_DIGITS, join_number,
                          sign_batch)

# --- §15 starting values ---------------------------------------------------
# THE INITIAL POOL, not a cap (pool spec change 2026-11-12, rule 2): "Each
# gateway starts with 50 numbers. There is no cap." This is only the DEFAULT
# for a gateway nobody has sized; the size that is used is per gateway, in
# gateways.pool_size, because one module constant silently turned a
# 200-number pool into a 50-number one on first contact.
POOL_CAPACITY = 50
# Rule 5: a low-water refill is max(15, used_24h).
MIN_REFILL = 15
# Rule 6: the SERVER rotates at 35 hours; the gateway's own alert fires at
# 36 and then means one thing -- this gateway missed its rotation. The hour
# between them is the operator's margin and holds only while the sweep
# below runs every 5-15 minutes (open item 5). Do not change one without
# the other.
POOL_ROTATION_HOURS = 35.0
POOL_AGE_ALERT_HOURS = 36.0
ROTATION_SWEEP_S = 300.0
DRAIN_MAX_EVENTS = 25
LEASE_DURATION_S = 45.0            # must exceed the EGM's own ~30 s return
REJECT_CYCLE_THRESHOLD = 3
REJECT_CYCLE_WINDOW_S = 180.0
LIABILITY_WINDOW_DAYS = 15
AUTH_TIMEOUT_S = 10.0              # §10.1; "pending lab"
FRAME_COMPLETION_TIMEOUT_S = 10.0
MAX_UNAUTHENTICATED = 8            # §10.6 item 2; "pending lab"
INVITE_TIMEOUT_S = 30.0
# A gateway that reports rows waiting while this server receives NONE from
# it, for this long, has a stalled drain. Five minutes is twenty heartbeats
# and three hundred invitations: a healthy drain delivers within a second
# or two of each, and a backlog being worked through is delivering, so
# neither comes near it. Short enough to be told the same morning.
DRAIN_STALL_S = 300.0
# How often an idle gateway is invited to drain. §12.2 expects a ticket
# printed at one machine to be known to the server "in under a second" when
# it is inserted at another; the heartbeat alone (15 s) cannot give that.
# ONE second is a single-gateway lab value: across 1,500 gateways it would
# be 1,500 invitations a second, and the real service must pace invitations
# from the queue_depth each heartbeat reports (§10.4, §11.4).
DRAIN_INVITE_INTERVAL_S = 1.0
MISSED_HEARTBEATS = 15             # §15: about 4 minutes
DEFAULT_PORT = 5020
DEFAULT_SYSTEM_ID = 1

# --- Table 15.12a: what the host tells the machine -------------------------
ALLOW_CODES = {"cashable": 0x00, "restricted": 0x01, "nonrestricted": 0x02}
# §9.4: "Display what the customer can act on; refuse neutrally and log the
# specific code for everything else." The EGM renders its own text from the
# code, so the CODE is the message. Specific where the customer can act:
REJECT_EXPIRED = 0x85
REJECT_TRY_AGAIN = 0x86            # "validation information not available"
REJECT_AMOUNT_MISMATCH = 0x87
# ...and neutral for everything §9.4 lists as neutral. "Already redeemed"
# (0x84) and "not in system" (0x82) exist in the table and are deliberately
# NOT sent: the first implies fraud before a human has looked, the second
# has innocent causes. The specific reason goes in the log, never on the
# glass.
REJECT_NEUTRAL = 0x80

# Table 15.13c validation types, as recorded in ticket_out_history. Only
# the first four are tickets a machine may take back.
REDEEMABLE_TYPES = {0x00: "cashable", 0x01: "restricted",
                    0x02: "cashable", 0x03: "restricted"}
CAGE_ONLY_TYPES = {0x10: "handpay receipt (cancelled credits)",
                   0x20: "handpay receipt (jackpot)"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);

-- §10.5: "One machine, one gateway is a database constraint, not a
-- workflow convention." client_id is the key; recommissioning replaces the
-- HMAC key and resets both sequence counters in one statement.
CREATE TABLE IF NOT EXISTS gateways (
    client_id TEXT PRIMARY KEY,
    hmac_key TEXT NOT NULL,
    tx_seq INTEGER NOT NULL DEFAULT 0,
    rx_seq_floor INTEGER NOT NULL DEFAULT 0,
    commissioned_at TEXT NOT NULL,
    last_seen_at TEXT,
    last_source TEXT,
    last_heartbeat TEXT,
    reported_epoch INTEGER,
    reported_batch TEXT,
    -- What a full issue gives this gateway: its first pool, and every
    -- rotation. NULL means "nobody sized it", which is the default of 50.
    pool_size INTEGER
);

-- Every batch ever built for a gateway, kept so that a mismatch reported
-- in a heartbeat is answered by re-sending the IDENTICAL batch (§11.2),
-- never by minting another.
CREATE TABLE IF NOT EXISTS pool_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL,
    pool_epoch INTEGER NOT NULL,
    batch_uuid TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,              -- 'topup' | 'replace'
    batch_json TEXT NOT NULL,        -- the signed sub-object, exactly as sent
    created_at TEXT NOT NULL,
    sent_at TEXT,
    acknowledged_at TEXT,
    UNIQUE (client_id, pool_epoch)
);

-- §7.1: "numbers issued to a gateway are unique fleet-wide." The PRIMARY
-- KEY is that rule; a second issue of any number fails here, in the
-- database, whatever the code above it believes.
CREATE TABLE IF NOT EXISTS issued_numbers (
    validation_number TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    pool_epoch INTEGER NOT NULL,
    batch_uuid TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    misses INTEGER NOT NULL DEFAULT 0   -- ticket-in queries with no detail yet
);

-- §12.1. Validity is a lookup HERE, never in a pool (§7.1, §12.2).
CREATE TABLE IF NOT EXISTS tickets (
    validation_number TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    amount_cents INTEGER,
    kind TEXT NOT NULL DEFAULT 'cashable',
    pool_id INTEGER NOT NULL DEFAULT 0,
    restricted_expiration INTEGER NOT NULL DEFAULT 0,
    cage_only INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,            -- 'egm' | 'cashdesk'
    issuing_client_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    lease_client_id TEXT,
    lease_query_id INTEGER,
    redeemed_by TEXT,
    note TEXT
);

-- WHICH MACHINE. A gateway's own record of every cabinet it has been
-- attached to, mirrored here: one row per machine epoch per gateway
-- database. A lab gateway carried between cabinets drains them all under
-- one client_id, and this is what tells them apart. `ended_at` is filled
-- in when the gateway reports the epoch closed, so the row is upserted,
-- not inserted once.
CREATE TABLE IF NOT EXISTS machine_epochs (
    db_instance_id TEXT NOT NULL,
    epoch_id INTEGER NOT NULL,
    client_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    reason TEXT,
    serial_number TEXT,
    asset_number INTEGER,
    sas_version TEXT,
    first_reported_at TEXT NOT NULL,
    PRIMARY KEY (db_instance_id, epoch_id)
);

-- §11.1: idempotent upsert keyed on the row identity. A batch re-sent
-- after a missed ACK lands on the same primary key and changes nothing.
CREATE TABLE IF NOT EXISTS ingest (
    db_instance_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_id INTEGER NOT NULL,
    client_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    row_json TEXT NOT NULL,
    PRIMARY KEY (db_instance_id, table_name, row_id)
);

CREATE TABLE IF NOT EXISTS validation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    client_id TEXT NOT NULL,
    query_id INTEGER,
    validation_number TEXT,
    decision TEXT,
    transfer_code INTEGER,
    reason TEXT,
    amount_cents INTEGER,
    result_status INTEGER,
    result_at TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    origin TEXT NOT NULL,            -- 'server' | 'gateway'
    client_id TEXT,
    kind TEXT NOT NULL,
    detail TEXT
);

-- Operator commands for a RUNNING service, as rows. The same reasoning as
-- the gateway's host_requests: durable, auditable, and it needs no second
-- listening port on the server.
CREATE TABLE IF NOT EXISTS ticket_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    validation_number TEXT NOT NULL,
    action TEXT NOT NULL,            -- 'add' | 'edit'
    actor TEXT NOT NULL,             -- a person, not a process
    reason TEXT NOT NULL,            -- why; required, and that is the point
    field TEXT,                      -- NULL for 'add'
    old_value TEXT,
    new_value TEXT,
    host TEXT                        -- where the change was typed
);
-- "every change to this number" and "every re-valuation this week" are the
-- two questions an audit gets asked. One index each.
CREATE INDEX IF NOT EXISTS idx_ticket_audit_number
    ON ticket_audit (validation_number, id);
CREATE INDEX IF NOT EXISTS idx_ticket_audit_at
    ON ticket_audit (at);

CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    client_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    detail TEXT
);
"""


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def log(message: str) -> None:
    print(f"[{utc_now()}] {message}", flush=True)


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    # §10.4: an ACK only after a DURABLE commit. FULL is what makes the
    # commit durable; NORMAL in WAL mode can lose the last transactions on
    # power loss, and an ACK for a row that then vanished is the one
    # failure the drain model cannot recover from.
    conn.execute("PRAGMA synchronous = FULL")
    conn.executescript(SCHEMA)
    # Machine identity, added 2026-11-13. NULL in these columns is a
    # statement -- "this row arrived before the gateway said which machine"
    # -- and `attribute` fills in what the epochs can settle afterwards.
    #
    # EPOCH REFERENCES ONLY -- never a copy of the asset or serial. The
    # name is on machine_epochs and is read from there every time, so a
    # cabinet named after a RAM clear, or a mistyped asset corrected on
    # the gateway, changes one row here and every report follows.
    for table, columns in (
            ("ingest", ("epoch_id INTEGER",)),
            ("tickets", ("issuing_db_instance_id TEXT",
                         "issuing_epoch_id INTEGER",
                         "redeemed_db_instance_id TEXT",
                         "redeemed_epoch_id INTEGER")),
            ("validation_log", ("db_instance_id TEXT", "epoch_id INTEGER")),
            ("gateways", ("machine_json TEXT",))):
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            if column.split()[0] not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ingest_epoch "
                 "ON ingest (db_instance_id, epoch_id, table_name)")
    # A stand-in database created before 2026-11-12 has no pool_size.
    if "pool_size" not in {r[1] for r in conn.execute("PRAGMA table_info(gateways)")}:
        conn.execute("ALTER TABLE gateways ADD COLUMN pool_size INTEGER")
    # Rule 1: unique on the 16-DIGIT part too, "so a different system ID
    # cannot recreate a lookalike". The primary key is the 18 digits, and
    # --system-id can differ between two runs of this program.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_issued_numbers_16 "
                 "ON issued_numbers (substr(validation_number, 3))")
    conn.commit()
    return conn


def signing_key_path(db: str) -> Path:
    return Path(db).with_suffix(".pool-signing.key")


def load_seed(db: str) -> bytes:
    path = signing_key_path(db)
    try:
        seed = bytes.fromhex(path.read_text().strip())
    except (OSError, ValueError) as e:
        raise SystemExit(f"cannot read the pool signing key {path}: {e}\n"
                         "  Run `python -m gateway.service init` first.")
    if len(seed) != ed25519.SEED_BYTES:
        raise SystemExit(f"{path} is not a {ed25519.SEED_BYTES}-byte seed")
    return seed


def normalise_number(text: str) -> str:
    """Accept '00-0000-0000-0000-0001' or 18 bare digits."""
    digits = "".join(ch for ch in str(text) if ch.isdigit())
    if len(digits) != NUMBER_DIGITS:
        raise ValueError(f"a validation number is {NUMBER_DIGITS} digits; "
                         f"{text!r} has {len(digits)}")
    return digits


class Service:
    """All state that outlives one connection. Synchronous and free of
    sockets, so every rule in it can be tested without a network."""

    def __init__(self, conn: sqlite3.Connection, seed: bytes, *,
                 system_id: int = DEFAULT_SYSTEM_ID, clock=time.monotonic,
                 lease_s: float = LEASE_DURATION_S,
                 pool_capacity: int = POOL_CAPACITY, now_fn=None):
        self.conn, self.seed, self.clock = conn, seed, clock
        self.system_id, self.lease_s = system_id, lease_s
        # The default for a gateway with no pool_size of its own.
        self.pool_capacity = pool_capacity
        # Wall clock, for batch ages. NOT self.clock: a rotation is due 35
        # hours after a batch was minted whether or not this process was
        # running for all of them, and a monotonic clock restarts with it.
        self.now_fn = now_fn or (
            lambda: datetime.datetime.now(datetime.timezone.utc))
        self.no_used_24h_noted: set[str] = set()
        self.missed_rotation_alerted: dict[str, float] = {}
        self.replay_alerted: set[tuple] = set()
        # client_id -> rows this server has committed from it in this run,
        # and for the stall check: (that count when it last moved, when
        # rows were first seen waiting with none arriving, alerted yet?,
        # when the last heartbeat came).
        self.rows_received: dict[str, int] = {}
        self.drain_progress: dict[str, tuple] = {}
        self.sas_link_seen: dict[str, str | None] = {}
        self._jump_send_sequences()
        self.public_key = ed25519.public_key(seed)
        # number -> monotonic deadline. §12.2: "Lease timing runs on the
        # monotonic clock; a server restart re-arms a fresh lease for every
        # Pending ticket."
        self.leases: dict[str, float] = {}
        for (number,) in conn.execute(
                "SELECT validation_number FROM tickets WHERE state = 'Pending'"):
            self.leases[number] = clock() + lease_s
        self.rejects: dict[tuple[str, str], list[float]] = {}

    # -- small helpers --

    def alert(self, kind: str, detail: str, client_id: str | None = None,
              origin: str = "server") -> None:
        log(f"ALERT [{origin}{'/' + client_id if client_id else ''}] "
            f"{kind}: {detail}")
        self.conn.execute(
            "INSERT INTO alerts (at, origin, client_id, kind, detail) "
            "VALUES (?, ?, ?, ?, ?)", (utc_now(), origin, client_id, kind, detail))
        self.conn.commit()

    def key_for(self, client_id: str) -> bytes | None:
        row = self.conn.execute("SELECT hmac_key FROM gateways WHERE client_id = ?",
                                (client_id,)).fetchone()
        return bytes.fromhex(row[0]) if row else None

    def rx_floor(self, client_id: str) -> int:
        row = self.conn.execute(
            "SELECT rx_seq_floor FROM gateways WHERE client_id = ?",
            (client_id,)).fetchone()
        return row[0] if row else 0

    def accept_seq(self, client_id: str, seq: int, source: str) -> None:
        self.conn.execute(
            "UPDATE gateways SET rx_seq_floor = ?, last_seen_at = ?, "
            "last_source = ? WHERE client_id = ?",
            (seq, utc_now(), source, client_id))
        self.conn.commit()

    def encode(self, client_id: str, message_type: int, payload: dict) -> bytes:
        self.conn.execute("UPDATE gateways SET tx_seq = tx_seq + 1 "
                          "WHERE client_id = ?", (client_id,))
        seq = self.conn.execute("SELECT tx_seq FROM gateways WHERE client_id = ?",
                                (client_id,)).fetchone()[0]
        self.conn.commit()
        return frame.encode(client_id=client_id, message_type=message_type,
                            seq=seq, payload=payload,
                            key=self.key_for(client_id))

    # -- commissioning (§10.5) --

    def commission(self, client_id: str) -> str:
        """Mint a FRESH key and reset both counters, in one statement. The
        old key -- and any clone of it -- stops working at that instant."""
        frame.encode_client_id(client_id)
        key = secrets.token_hex(32)
        self.conn.execute(
            "INSERT INTO gateways (client_id, hmac_key, commissioned_at) "
            "VALUES (?, ?, ?) ON CONFLICT(client_id) DO UPDATE SET "
            "hmac_key = excluded.hmac_key, tx_seq = 0, rx_seq_floor = 0, "
            "commissioned_at = excluded.commissioned_at",
            (client_id, key, utc_now()))
        self.conn.commit()
        return key

    # -- pool issuance (§7, §11.2) --

    # -- which machine (gateway/machine.py) --

    def machine_of(self, client_id: str) -> dict | None:
        """What this gateway's last heartbeat said it is on: the open epoch
        plus `db_instance_id`. None until it has said."""
        row = self.conn.execute(
            "SELECT machine_json FROM gateways WHERE client_id = ?",
            (client_id,)).fetchone()
        try:
            data = json.loads(row[0]) if row and row[0] else None
        except ValueError:
            return None
        epoch = machine.clean_epoch(data)
        if epoch is not None:
            epoch["db_instance_id"] = data.get("db_instance_id")
        return epoch

    def stay(self, instance, epoch_id) -> dict | None:
        """The cabinet an epoch belongs to, named as it is named NOW."""
        if instance is None or epoch_id is None:
            return None
        return machine.stay_of(list(self.epochs_of(instance).values()), epoch_id)

    def note_machine(self, client_id: str, reported, instance=None) -> None:
        """Record the epoch a heartbeat names, and say what kind of change
        it is when the epoch is a new one.

        THE POLLER'S `reason` DECIDES, never a comparison of names. A RAM
        clear blanks the typed-in asset and serial, so "asset 246" followed
        by no name at all is the SAME cabinet -- the poller knows that from
        the meters and says `ram_clear`. Comparing names here would have
        announced a move and filed the cabinet's next accounting under a
        different machine.

        A real move is logged and alerted, not refused: carrying one
        gateway between cabinets is how the lab works. It is alerted
        because of what travels with it -- the validation pool. Numbers
        issued to this client_id now print at another cabinet. On a floor
        §10.5 makes a move an unassign-then-assign with a fresh key, and
        this is the line that says one happened without that.
        """
        now_on = machine.clean_epoch(reported)
        if now_on is None:
            return
        before = self.machine_of(client_id)
        stored = dict(now_on, db_instance_id=instance)
        self.conn.execute("UPDATE gateways SET machine_json = ? WHERE client_id = ?",
                          (json.dumps(stored, sort_keys=True), client_id))
        self.conn.commit()
        if before is None:
            log(f"{client_id} is on {machine.describe(now_on)}")
            return
        same_epoch = (before.get("id") == now_on["id"]
                      and before.get("db_instance_id") == instance)
        if same_epoch:
            return           # a corrected name on the same epoch: just stored
        reason = now_on.get("reason")
        if before.get("db_instance_id") != instance:
            self.alert("gateway_database_changed",
                       f"reports a different database instance. Its epochs "
                       f"count from the start again; now on "
                       f"{machine.describe(now_on)}.", client_id)
        elif reason in machine.SAME_CABINET:
            log(f"{client_id}: new epoch {now_on['id']} ({reason}) on the same "
                f"cabinet, now reading {machine.describe(now_on)}")
        else:
            self.alert("machine_changed",
                       f"epoch {now_on['id']} ({reason}): now on "
                       f"{machine.describe(now_on)}; was on "
                       f"{machine.describe(before)}. Rows from here belong to "
                       "the new cabinet. Its validation pool moved with it.",
                       client_id)

    def _upsert_epochs(self, instance: str, client_id: str, epochs, now: str) -> dict:
        """Store the epochs a batch carries. Returns {epoch_id: epoch} for
        this gateway database, those just stored AND those stored before.
        Runs inside ingest's transaction."""
        for raw in epochs if isinstance(epochs, list) else []:
            epoch = machine.clean_epoch(raw)
            if epoch is None:
                continue
            self.conn.execute(
                "INSERT INTO machine_epochs (db_instance_id, epoch_id, client_id, "
                "started_at, ended_at, reason, serial_number, asset_number, "
                "sas_version, first_reported_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                # The gateway is the authority on its own epochs, so what
                # it says now replaces what it said before: ended_at when
                # the epoch closes, and the serial or asset if it was read
                # late or corrected.
                "ON CONFLICT(db_instance_id, epoch_id) DO UPDATE SET "
                "ended_at = excluded.ended_at, reason = excluded.reason, "
                "serial_number = excluded.serial_number, "
                "asset_number = excluded.asset_number, "
                "sas_version = excluded.sas_version",
                (instance, epoch["id"], client_id, epoch["started_at"],
                 epoch["ended_at"], epoch["reason"], epoch["serial_number"],
                 epoch["asset_number"], epoch["sas_version"], now))
        return self.epochs_of(instance)

    def epochs_of(self, instance: str) -> dict:
        return {row[0]: dict(zip(machine.EPOCH_FIELDS, row))
                for row in self.conn.execute(
                    "SELECT epoch_id, started_at, ended_at, reason, "
                    "serial_number, asset_number, sas_version FROM machine_epochs "
                    "WHERE db_instance_id = ? ORDER BY epoch_id", (instance,))}

    def attribute(self, batch: int = 2000) -> dict:
        """Give a machine to rows that were ingested without one, where the
        epochs this server now holds can settle it.

        Rows drained before 2026-11-13 arrived with no epoch. The gateway
        will not send them again -- they are acknowledged -- but each
        carries its own timestamp, and the epochs for its database have
        since arrived. Placed by the same rule the gateway uses
        (machine.epoch_for), in batches, each its own transaction.

        Returns counts: placed, and left unknown with the reason. A row
        whose database has reported no epochs, or that predates the first
        one, STAYS unknown. That is not a failure to be retried into a
        guess.
        """
        placed = no_epochs = before_first = 0
        last = ("", "", 0)
        cache: dict = {}
        while True:
            rows = self.conn.execute(
                "SELECT db_instance_id, table_name, row_id, row_json FROM ingest "
                "WHERE epoch_id IS NULL AND (db_instance_id, table_name, row_id) "
                "> (?, ?, ?) ORDER BY db_instance_id, table_name, row_id LIMIT ?",
                last + (int(batch),)).fetchall()
            if not rows:
                break
            for instance, table, row_id, row_json in rows:
                last = (instance, table, row_id)
                if instance not in cache:
                    cache[instance] = self.epochs_of(instance)
                epochs = cache[instance]
                if not epochs:
                    no_epochs += 1
                    continue
                try:
                    row = json.loads(row_json)
                except ValueError:
                    row = {}
                epoch_id = machine.epoch_for(list(epochs.values()), row)
                epoch = epochs.get(epoch_id)
                if epoch is None:
                    before_first += 1
                    continue
                self.conn.execute(
                    "UPDATE ingest SET epoch_id = ? WHERE db_instance_id = ? "
                    "AND table_name = ? AND row_id = ? AND epoch_id IS NULL",
                    (epoch_id, instance, table, row_id))
                placed += 1
            self.conn.commit()
        return {"placed": placed, "no_epochs_reported": no_epochs,
                "before_first_epoch": before_first}

    def note_drain(self, client_id: str, queue_depth) -> None:
        """Raise `drain_stalled` when a gateway has rows waiting and none
        are arriving. Called on every heartbeat.

        WHY THE SERVER, AND WHY NOW. The gateway used to say so itself:
        rows unacknowledged for 48 hours raised an alert from its
        maintenance job. That alert was removed on the operator's decision
        (2026-11-18, A48), and an undelivered row in a 30-day table is now
        reported on the gateway only after about a month. A gateway is
        also the wrong place to notice: it is headless, and the party that
        knows nothing is arriving is the one it is not arriving AT.

        It matters more than it did, too. The gateway now prunes on the
        strength of the acknowledgement, so a drain that has stopped is no
        longer just a disk filling -- it is the retention clock not
        starting, on rows that exist nowhere else.

        THE SIGNAL is this server's own count of rows committed from the
        gateway, against the gateway's own `queue_depth`. Not the
        gateway's `acked` marks: an older client does not send them, and
        "what I received" is a fact this side holds without being told.

        Three states it must not confuse, and does not:
          * nothing waiting           -> not stalled, however long
          * a backlog being worked    -> rows are arriving; not stalled
          * rows waiting, none coming -> stalled after DRAIN_STALL_S
        A gateway that is not connected sends no heartbeat and so never
        reaches here: silence is a different fault with a different sign
        (`last seen` in `status`).

        Once per episode. It says so again only after the drain has moved
        and stopped a second time.
        """
        if isinstance(queue_depth, bool) or not isinstance(queue_depth, int):
            return
        now, received = self.clock(), self.rows_received.get(client_id, 0)
        last_count, waiting_since, alerted, last_beat = self.drain_progress.get(
            client_id, (received, None, False, now))
        # THE CLOCK COUNTS ONLY TIME THIS SERVER WATCHED ROWS WAIT. It
        # starts at the first heartbeat that shows rows waiting, and starts
        # again after a gap in heartbeats: a gateway that reconnects after
        # an hour offline with a backlog has not had a stalled drain for an
        # hour -- it has not had a link, and it gets its five minutes from
        # now. Measuring from "when the queue was last seen empty" called
        # that gateway stalled on its first heartbeat back.
        if now - last_beat > 4 * 15.0:
            waiting_since = None
        if queue_depth <= 0 or received != last_count:
            if alerted:
                log(f"{client_id}: drain is moving again "
                    f"({queue_depth} row(s) still waiting)")
            self.drain_progress[client_id] = (
                received, now if queue_depth > 0 else None, False, now)
            return
        if waiting_since is None:
            waiting_since = now
        self.drain_progress[client_id] = (last_count, waiting_since, alerted, now)
        if not alerted and now - waiting_since >= DRAIN_STALL_S:
            self.drain_progress[client_id] = (last_count, waiting_since, True, now)
            self.alert("drain_stalled",
                       f"reports {queue_depth} row(s) waiting to drain and "
                       f"this server has received none for "
                       f"{(now - waiting_since) / 60:.0f} minutes, with the "
                       "link up and heartbeats arriving. Until it drains, "
                       "those rows exist only on the gateway and its "
                       "retention clock has not started for them. Look for "
                       "event_too_large or database alerts from this gateway "
                       "above this one.", client_id)

    def note_sas_link(self, client_id: str, link) -> None:
        """Log each change of the gateway's serial-link state.

        `egm_status.online` is derived from the age of the last meter row
        and cannot tell a cable fault from a dead poller. `sas_link` is the
        poller's own word: up, reconnecting, or down (it gave up). A log
        line per CHANGE, not per heartbeat, and no alert here -- the
        gateway raises `sas_link_down` itself, once per outage, and two
        alerts for one fault is how a channel gets ignored.
        """
        state = link.get("state") if isinstance(link, dict) else None
        if client_id in self.sas_link_seen and self.sas_link_seen[client_id] == state:
            return
        first = client_id not in self.sas_link_seen
        self.sas_link_seen[client_id] = state
        if state is None:
            if not first:
                log(f"{client_id}: serial link state no longer reported")
            return
        log(f"{client_id}: serial link to the cabinet is {str(state).upper()}"
            + (f" ({link.get('detail')})" if link.get("detail") else ""))

    def _jump_send_sequences(self) -> None:
        """On start, move every gateway's send sequence up to the clock.

        §10.3: send-side counters "take a safety jump forward on restart,
        sized above anything that could have been sent in the unflushed
        window". For a restart that window is seconds. For a RESTORE FROM
        BACKUP it is however old the backup is -- and without a jump that
        covers it, a restored server's every frame carries a sequence
        number the gateway has already seen, the gateway rejects all of
        them as replays, and the link is up and useless. The server could
        not then ask for the rows it lost, which is the one thing a
        restore needs to do.

        Milliseconds since 1970 is above anything a previous run sent
        unless that run averaged more than a thousand frames a second
        since 1970, and it needs no state that a backup could be missing.
        Only ever raises a counter. The gateway's floor moves up with the
        first frame it accepts.
        """
        floor = int(time.time() * 1000)
        self.conn.execute("UPDATE gateways SET tx_seq = ? WHERE tx_seq < ?",
                          (floor, floor))
        self.conn.commit()

    def replay_needed(self, client_id: str, instance, acked) -> dict | None:
        """Has this server LOST rows the gateway was told it had?

        The gateway reports, per table, the highest row id this server ever
        acknowledged. If this server holds less, it has been restored from
        a backup older than those acknowledgements (or its database was
        emptied), and the missing rows exist only on the gateway -- kept
        there for exactly this, for the retention window and no longer.

        Returns the replay request to send, or None. No human decides and
        none is needed (the gateway is headless and so, here, is the
        server): the comparison is exact, and the remedy is idempotent --
        rows re-sent land on the same primary keys.
        """
        if not isinstance(instance, str) or not isinstance(acked, dict):
            return None
        held = dict(self.conn.execute(
            "SELECT table_name, MAX(row_id) FROM ingest WHERE db_instance_id = ? "
            "GROUP BY table_name", (instance,)))
        after = {}
        for table, gateway_max in acked.items():
            if (isinstance(gateway_max, bool) or not isinstance(gateway_max, int)
                    or not isinstance(table, str)):
                continue
            if held.get(table, 0) < gateway_max:
                after[table] = held.get(table, 0)
        key = (client_id, instance)
        if not after:
            self.replay_alerted.discard(key)
            return None
        if key not in self.replay_alerted:
            self.replay_alerted.add(key)
            missing = {t: f"hold up to {a}, gateway was acknowledged up to "
                          f"{acked[t]}" for t, a in after.items()}
            self.alert("rows_lost_on_server",
                       f"this server holds fewer rows than it acknowledged "
                       f"({missing}). Restored from a backup? Asking the "
                       "gateway to send them again.", client_id)
        return {"db_instance_id": instance, "after": after}

    def rows_by_machine(self) -> list:
        """[(heading, [(table, count), ...])], one heading per cabinet.

        Grouped by STAY, not by name: two unnamed cabinets are two
        headings, and one cabinet across a RAM clear is one. The name in
        the heading is read from the epochs now, so it follows a cabinet
        that was named after its rows arrived.
        """
        groups: dict = {}
        cache: dict = {}
        for instance, epoch_id, table, count in self.conn.execute(
                "SELECT db_instance_id, epoch_id, table_name, COUNT(*) FROM "
                "ingest GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"):
            if instance not in cache:
                cache[instance] = list(self.epochs_of(instance).values())
            stay = machine.stay_of(cache[instance], epoch_id)
            if stay is None:
                key = (1, "", 0)
                heading = ("machine unknown (ingested before the gateway "
                           "said; run `attribute`)")
            else:
                key = (0, instance, stay["first_epoch_id"])
                heading = machine.describe_stay(stay)
            tables = groups.setdefault(key, (heading, {}))[1]
            tables[table] = tables.get(table, 0) + count
        return [(heading, sorted(tables.items()))
                for _key, (heading, tables) in sorted(groups.items())]

    def pool_size(self, client_id: str) -> int:
        """What a full issue gives this gateway."""
        row = self.conn.execute(
            "SELECT pool_size FROM gateways WHERE client_id = ?",
            (client_id,)).fetchone()
        return int(row[0]) if row and row[0] else self.pool_capacity

    def set_pool_size(self, client_id: str, size: int) -> None:
        if isinstance(size, bool) or not isinstance(size, int) \
                or not 1 <= size <= MAX_BATCH_NUMBERS:
            # Not a cap on the POOL -- rule 2 has none, and top-ups grow it.
            # A cap on ONE BATCH: the gateway refuses a batch larger than
            # this as malformed, so a full issue above it could never land.
            raise ValueError(
                f"a pool size is 1-{MAX_BATCH_NUMBERS}: a full issue travels "
                "as one batch and the gateway refuses a larger one")
        changed = self.conn.execute(
            "UPDATE gateways SET pool_size = ? WHERE client_id = ?",
            (size, client_id)).rowcount
        self.conn.commit()
        if not changed:
            raise ValueError(f"{client_id} is not commissioned")

    def refill_size(self, client_id: str, pool: dict) -> int:
        """Rule 5: max(15, used_24h), where used_24h is THE GATEWAY'S count.

        The server cannot count it. validation_pool is never drained, and
        ticket_out_history holds only tickets that printed -- so a server
        count misses a number that was assigned and then voided by a jam,
        which the pool has lost just the same (§11.2 never reuses it).

        Trusting a gateway's own figure means a faulty or compromised one
        can inflate its own pool. That is the spec change's stated,
        accepted decision ("no pool-size ceiling"); the only limit here is
        the size of one batch, and the low-water flag is still set on the
        next beat if one batch was not enough.
        """
        used = pool.get("used_24h")
        if isinstance(used, bool) or not isinstance(used, int) or used < 0:
            if client_id not in self.no_used_24h_noted:
                self.no_used_24h_noted.add(client_id)
                log(f"{client_id} does not report used_24h (an older gateway "
                    f"client). Refills are the minimum {MIN_REFILL} until it "
                    "is updated -- a busy machine will ask again and again.")
            used = 0
        return min(max(MIN_REFILL, used), MAX_BATCH_NUMBERS)

    @staticmethod
    def _parse(iso):
        if not iso:
            return None
        try:
            then = datetime.datetime.fromisoformat(iso)
        except ValueError:
            return None
        return then if then.tzinfo else then.replace(tzinfo=datetime.timezone.utc)

    def oldest_live_batch_age_hours(self, client_id: str):
        """How old the OLDEST numbers this gateway may still hold are, by
        this server's own clock -- never the gateway's (spec change, server
        notes). None when this server has issued it nothing.

        A replacement revokes every available number, so nothing older than
        the newest 'replace' batch can still be available; top-ups since
        then are younger. The newest replace's mint time is therefore the
        age of the oldest live number. Measured from created_at, the
        earliest of a batch's three timestamps, so the rotation is never
        late: the gateway's own 36 h alert counts from when it APPLIED the
        batch, which cannot be before it was minted.

        Parsed, not compared as text. This repository has already paid for
        comparing an isoformat() string against another as strings.
        """
        row = self.conn.execute(
            "SELECT created_at FROM pool_batches WHERE client_id = ? "
            "AND kind = 'replace' ORDER BY pool_epoch DESC LIMIT 1",
            (client_id,)).fetchone()
        then = self._parse(row[0]) if row else None
        if then is None:
            return None
        return (self.now_fn() - then).total_seconds() / 3600.0

    def rotation_due(self, client_id: str) -> bool:
        """Rule 6. True when this gateway's pool should be retired now.

        NOT while a batch is unacknowledged: until the gateway reports the
        newest epoch, both are potentially live (§7.4), and a rotation on
        top would make three. The unacknowledged batch is re-sent on the
        next heartbeat, and that re-send is the retry.
        """
        latest = self.latest_batch(client_id)
        if latest is None or latest[4] is None:
            return False
        age = self.oldest_live_batch_age_hours(client_id)
        return age is not None and age >= POOL_ROTATION_HOURS

    def rotation_missed(self, client_id: str):
        """The age in hours if this gateway is past the 36 h alert and has
        not been reported in the last hour; otherwise None. A gateway
        switched off for a week is a line an hour, not one every sweep."""
        age = self.oldest_live_batch_age_hours(client_id)
        if age is None or age < POOL_AGE_ALERT_HOURS:
            self.missed_rotation_alerted.pop(client_id, None)
            return None
        last = self.missed_rotation_alerted.get(client_id)
        if last is not None and self.clock() - last < 3600.0:
            return None
        self.missed_rotation_alerted[client_id] = self.clock()
        return age

    def latest_batch(self, client_id: str):
        return self.conn.execute(
            "SELECT pool_epoch, batch_uuid, kind, batch_json, acknowledged_at, "
            "sent_at FROM pool_batches WHERE client_id = ? "
            "ORDER BY pool_epoch DESC LIMIT 1", (client_id,)).fetchone()

    def mint_batch(self, client_id: str, count: int, kind: str,
                   floor_epoch: int = 0) -> dict:
        """Mint `count` numbers, sign them, record them. One transaction:
        a batch that exists on the wire and not in issued_numbers would be
        tickets this server cannot recognise as its own."""
        latest = self.latest_batch(client_id)
        epoch = max(latest[0] if latest else 0, floor_epoch) + 1
        batch_uuid = str(uuid.uuid4())
        # The SAME clock the rotation reads the age back with. Two clocks
        # here would be an age measured between two different instruments.
        now, numbers = self.now_fn().isoformat(), []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            while len(numbers) < count:
                number = join_number(self.system_id, secrets.randbelow(10 ** 16))
                if self.conn.execute(
                        "SELECT 1 FROM tickets WHERE validation_number = ?",
                        (number,)).fetchone():
                    continue
                try:
                    self.conn.execute(
                        "INSERT INTO issued_numbers (validation_number, "
                        "client_id, pool_epoch, batch_uuid, issued_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (number, client_id, epoch, batch_uuid, now))
                except sqlite3.IntegrityError:
                    continue        # already issued, to anyone, ever: never again
                numbers.append(number)
            batch = sign_batch(self.seed, client_id, epoch, batch_uuid, numbers)
            self.conn.execute(
                "INSERT INTO pool_batches (client_id, pool_epoch, batch_uuid, "
                "kind, batch_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (client_id, epoch, batch_uuid, kind, json.dumps(batch), now))
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        log(f"minted {kind} for {client_id}: epoch {epoch}, {count} number(s), "
            f"batch {batch_uuid}")
        return batch

    def pool_decision(self, client_id: str, pool: dict):
        """What, if anything, to send this gateway about its pool, given
        what its heartbeat just reported. Returns (kind, batch) or None.

        Four cases, in order, and the order is the rule:
          1. it reports the newest batch          -> that batch is acknowledged
          2. it is behind an unacknowledged batch -> RE-SEND THE IDENTICAL one
          3. this server has never issued to it   -> REPLACE whatever it holds
          4. it is low                            -> top it up
        """
        try:
            epoch = int(pool.get("pool_epoch") or 0)
            available = int(pool.get("available_ids") or 0)
        except (TypeError, ValueError, AttributeError):
            return None
        reported_batch = pool.get("active_batch_uuid")
        latest = self.latest_batch(client_id)

        if latest is not None:
            l_epoch, l_uuid, l_kind, l_json, l_acked, _ = latest
            if epoch == l_epoch and reported_batch == l_uuid:
                if l_acked is None:
                    self.conn.execute(
                        "UPDATE pool_batches SET acknowledged_at = ? "
                        "WHERE batch_uuid = ?", (utc_now(), l_uuid))
                    self.conn.commit()
                    log(f"{client_id} acknowledged epoch {l_epoch} ({l_kind})")
            elif epoch < l_epoch and l_acked is None:
                # §7.2: "On mismatch the server re-sends the identical
                # batch; it never mints a new one merely because time
                # passed."
                return l_kind, json.loads(l_json)
            elif epoch < l_epoch:
                # It ACKNOWLEDGED this epoch once and now reports an older
                # one: its database was re-created or restored from a
                # snapshot. Re-sending the old batch would hand it numbers
                # it may already have printed -- duplicate tickets. §7.2
                # allows a fresh mint only once the original is known to be
                # gone, and a gateway that no longer holds it is that
                # knowledge. The old numbers stay in issued_numbers, so any
                # ticket already printed from them is still recognised.
                self.alert("pool_state_lost",
                           f"acknowledged epoch {l_epoch} earlier and now "
                           f"reports {epoch}. Its database was re-created or "
                           "restored. Issuing a replacement, not a resend.",
                           client_id)
                return "replace", self.mint_batch(
                    client_id, self.pool_size(client_id), "replace")
            elif epoch == l_epoch:
                self.alert("pool_epoch_conflict",
                           f"reports epoch {epoch} as batch {reported_batch}; "
                           f"this server issued it as {l_uuid}", client_id)
                return None
            else:
                # THE GATEWAY HOLDS A BATCH THIS SERVER HAS NO RECORD OF
                # ISSUING. Two ways to get here, and the remedy is the same:
                # this server was restored from a backup taken before that
                # batch was minted, or the gateway is running on a database
                # that was filled by something else.
                #
                # This used to alert and return None -- on every heartbeat,
                # four alerts a minute for ever -- and do nothing. Measured
                # 2026-11-17 against a restored server: the gateway went on
                # dispensing from the unknown batch, and every ticket it
                # printed was then refused at ticket-in as "not in tickets
                # table", because validity is this server's issued_numbers
                # and those numbers are not in it. The retention windows
                # exist so a restore loses nothing; this was the place a
                # restore lost money.
                #
                # So: the same rule as first contact. Numbers no ticket
                # table knows must not be printable, and the only way to
                # make them unprintable is to retire them. Minted ABOVE the
                # gateway's epoch, or it would be refused as stale.
                #
                # WHAT THIS DOES NOT REPAIR, said here so it is not assumed:
                # tickets ALREADY printed from the unknown batch. They reach
                # this server by the drain or the replay, raise
                # ticket_number_not_issued, and stay unredeemable until an
                # operator adds them. Recovering them needs the gateway to
                # keep each signed batch it accepts so it can present it
                # again and this server can check its own signature. That
                # is not built.
                self.alert("pool_issuance_lost",
                           f"reports pool epoch {epoch}; the newest this "
                           f"server has a record of issuing is {l_epoch}. "
                           "Restored from a backup, or the gateway is on a "
                           "database something else filled. Its available "
                           "numbers are unknown here and are being REPLACED. "
                           "Tickets already printed from them will be "
                           "refused until added by hand.", client_id)
                return "replace", self.mint_batch(
                    client_id, self.pool_size(client_id), "replace",
                    floor_epoch=epoch)
        if latest is None:
            # Nothing this server issued is on that gateway, so whatever it
            # holds as 'available' are numbers no ticket table knows --
            # seeded lab numbers, or another server's. A top-up would leave
            # them in the pool to be printed. Replace.
            if available:
                log(f"{client_id} holds {available} available number(s) this "
                    "server never issued; replacing its pool rather than "
                    "topping it up")
            return "replace", self.mint_batch(
                client_id, self.pool_size(client_id), "replace",
                floor_epoch=epoch)
        if pool.get("low_water"):
            # The flag IS the request (A25), answered on the beat that
            # carries it. No "and below capacity" test any more: there is
            # no capacity (rule 2), and the gateway computed its mark from
            # what it was actually filled to.
            return "topup", self.mint_batch(
                client_id, self.refill_size(client_id, pool), "topup")
        return None

    def mark_sent(self, batch_uuid: str) -> None:
        self.conn.execute("UPDATE pool_batches SET sent_at = ? WHERE batch_uuid = ?",
                          (utc_now(), batch_uuid))
        self.conn.commit()

    # -- ingest (§11.1) --

    def ingest(self, client_id: str, payload: dict) -> list[dict]:
        """Store a batch and return the ACK list. ONE transaction: the ACK
        is built from rows that are already committed, never before."""
        instance = payload.get("db_instance_id")
        events = payload.get("events")
        if not isinstance(instance, str) or not isinstance(events, list):
            raise ValueError("EVENT_BATCH needs db_instance_id and events")
        now, acks, notes = utc_now(), [], []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # Epochs first, in the same transaction: a row must never be
            # committed naming an epoch this server cannot resolve.
            epochs = self._upsert_epochs(instance, client_id,
                                         payload.get("epochs"), now)
            for event in events:
                table, row_id, row = event["table"], int(event["id"]), event["row"]
                if not isinstance(table, str) or not isinstance(row, dict):
                    raise ValueError("malformed event")
                epoch_id = event.get("epoch_id")
                epoch = epochs.get(epoch_id) if isinstance(epoch_id, int) else None
                # An epoch id this server has never been told about is
                # stored as unknown, not as an id pointing at nothing.
                self.conn.execute(
                    "INSERT INTO ingest (db_instance_id, table_name, row_id, "
                    "client_id, received_at, row_json, epoch_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(db_instance_id, table_name, row_id) DO UPDATE "
                    "SET epoch_id = excluded.epoch_id "
                    # A re-sent row changes nothing (§11.1) -- except to
                    # give a machine to one that had none.
                    "WHERE ingest.epoch_id IS NULL AND excluded.epoch_id IS NOT NULL",
                    (instance, table, row_id, client_id, now,
                     json.dumps(row, sort_keys=True),
                     epoch["id"] if epoch else None))
                if table == "ticket_out_history":
                    note = self._ticket_printed(
                        client_id, row, now,
                        (instance, epoch["id"]) if epoch else None)
                    if note:
                        notes.append(note)
                acks.append({"db_instance_id": instance, "table": table,
                             "id": row_id})
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        for kind, detail in notes:
            self.alert(kind, detail, client_id)
        # Counted only here, after the commit: the stall check asks "has
        # anything ARRIVED", and a batch that was refused has not.
        self.rows_received[client_id] = (
            self.rows_received.get(client_id, 0) + len(acks))
        return acks

    def _ticket_printed(self, client_id: str, row: dict, now: str,
                        printed_in: tuple | None = None):
        """A drained ticket_out_history row becomes a ticket -- but only
        for a number THIS SERVER issued. Runs inside ingest's transaction."""
        try:
            number = join_number(int(row["validation_system_id"]),
                                 int(row["validation_number"]))
            amount = row.get("amount_cents")
            vtype = row.get("validation_type")
        except (KeyError, TypeError, ValueError):
            return None
        if amount is None or vtype is None:
            return None
        vtype = int(vtype)
        if vtype not in REDEEMABLE_TYPES and vtype not in CAGE_ONLY_TYPES:
            return None      # a redemption record or a no-receipt handpay
        issued = self.conn.execute(
            "SELECT client_id FROM issued_numbers WHERE validation_number = ?",
            (number,)).fetchone()
        existing = self.conn.execute(
            "SELECT state, amount_cents FROM tickets WHERE validation_number = ?",
            (number,)).fetchone()
        if existing is None and issued is None:
            # §7.1: a number the server did not issue is "at best, a ticket
            # the CMS has never heard of". It is recorded in `ingest` and
            # it is NOT made redeemable; an operator who knows what it is
            # can add it by hand.
            return ("ticket_number_not_issued",
                    f"a ticket for {amount} cents was printed with "
                    f"...{number[-6:]}, which this server never issued. Not "
                    "redeemable unless added with add-ticket.")
        kind = REDEEMABLE_TYPES.get(vtype, "cashable")
        cage_only = 1 if vtype in CAGE_ONLY_TYPES else 0
        if existing is None:
            self.conn.execute(
                "INSERT INTO tickets (validation_number, state, amount_cents, "
                "kind, pool_id, cage_only, source, issuing_client_id, "
                "created_at, updated_at, note, issuing_db_instance_id, "
                "issuing_epoch_id) VALUES (?, 'Open', ?, ?, ?, ?, "
                "'egm', ?, ?, ?, ?, ?, ?)",
                (number, int(amount), kind, int(row.get("pool_id") or 0),
                 cage_only, client_id, now, now,
                 CAGE_ONLY_TYPES.get(vtype),
                 # The EPOCH it printed in, which names the machine and
                 # is not the gateway: a pool issued to one client_id
                 # prints wherever that gateway is plugged in.
                 printed_in[0] if printed_in else None,
                 printed_in[1] if printed_in else None))
            return None
        state, known = existing
        if state == "WfA":
            self.conn.execute(
                "UPDATE tickets SET state = 'Open', amount_cents = ?, kind = ?, "
                "cage_only = ?, updated_at = ?, note = 'amount arrived by drain' "
                "WHERE validation_number = ?",
                (int(amount), kind, cage_only, now, number))
            return None
        if known is not None and int(known) != int(amount):
            # §11.1, ingest protection: acknowledge, do not re-mutate, and
            # do not swallow the contradiction either.
            return ("late_event_contradicts_ticket",
                    f"...{number[-6:]} is {state} for {known} cents; a drained "
                    f"record says {amount}. Balance NOT changed.")
        return None

    # -- ticket-in (§12.2) --

    def _audit(self, number: str, action: str, *, actor: str, reason: str,
               at: str, changes: list | None = None) -> None:
        """Append to ticket_audit. DOES NOT COMMIT.

        Called inside the caller's transaction on purpose. An audit row
        written in a second transaction can be missing while the change it
        describes is present -- which is the one failure an audit trail
        must not have, because the gap looks exactly like a change nobody
        recorded.

        One row per FIELD, not per command. "Show me every re-valuation"
        is the question this table exists to answer, and a single row
        holding a packed diff turns that into string matching.
        """
        host = socket.gethostname()
        rows = changes or [(None, None, None)]
        self.conn.executemany(
            "INSERT INTO ticket_audit (at, validation_number, action, actor, "
            "reason, field, old_value, new_value, host) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(at, number, action, actor, reason,
              field, None if old is None else str(old),
              None if new is None else str(new), host)
             for field, old, new in rows])

    def add_ticket(self, number: str, amount_cents: int, kind: str = "cashable",
                   pool_id: int = 0, restricted_expiration: int = 0, *,
                   actor: str = "unknown",
                   reason: str = "add-ticket") -> None:
        """§12.7: a cashdesk-issued ticket, never in any pool."""
        if kind not in ALLOW_CODES:
            raise ValueError(f"kind must be one of {sorted(ALLOW_CODES)}")
        if amount_cents <= 0:
            raise ValueError("a ticket is worth more than nothing")
        # NOT split_number(): that refuses system ID 00, which is right for
        # a pool number (Table 15.8a) and wrong here. A ticket printed under
        # secure-enhanced validation carries a machine-generated number
        # whose first two digits are 00, and old test media usually does.
        number = normalise_number(number)
        now = utc_now()
        # EXPLICIT, because the implicit one is not enough. sqlite3 opens a
        # transaction on the INSERT and leaves it open; if the audit write
        # then raises and the caller carries on, the next commit anywhere
        # on this connection carries the ticket along WITHOUT its audit
        # row. That is precisely the state an audit trail must never
        # reach, and it would arrive silently.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO tickets (validation_number, state, amount_cents, "
                "kind, pool_id, restricted_expiration, source, created_at, "
                "updated_at) VALUES (?, 'Open', ?, ?, ?, ?, 'cashdesk', ?, ?)",
                (number, amount_cents, kind, pool_id, restricted_expiration,
                 now, now))
            # A ticket appearing from nowhere is as interesting to an
            # auditor as one being re-valued, so both are recorded.
            self._audit(number, "add", actor=actor, reason=reason, at=now,
                        changes=[("amount_cents", None, amount_cents),
                                 ("kind", None, kind),
                                 ("pool_id", None, pool_id)])
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise


    def edit_ticket(self, number: str, *, amount_cents: int | None = None,
                    kind: str | None = None, pool_id: int | None = None,
                    reopen: bool = False, force: bool = False,
                    actor: str = "unknown", reason: str = "") -> dict:
        """Re-value or re-open a ticket already in the table. LAB USE.

        Operator, 2026-11-12: "i want to update value of a ticket that's
        printed on the machine to be able to reuse it on the machine."
        add-ticket refuses a number it already holds, which is right --
        §11.2 numbers are never reused -- and left no way to change the
        one field a lab needs to change: what the test media is worth.

        WHAT THIS IS. The stand-in's ticket table is the CMS's record of
        what a ticket is worth. Editing it does not alter the paper, the
        machine, or any meter; it changes what this server will ALLOW when
        that number is next presented. On a real floor that is a
        supervisor action with an audit trail behind it, which is why
        every change here is appended to the ticket's note with a
        timestamp, and why the old values are printed rather than
        silently replaced.

        A PENDING TICKET IS NOT TOUCHED without --force: Pending means a
        gateway holds a live lease on it right now (§12.2) and is waiting
        to tell us how the redemption went. Re-valuing underneath that
        would answer a question with one number and record another.
        """
        number = normalise_number(number)
        row = self.conn.execute(
            "SELECT state, amount_cents, kind, pool_id, note FROM tickets "
            "WHERE validation_number = ?", (number,)).fetchone()
        if row is None:
            raise ValueError(f"...{number[-6:]} is not in the tickets table; "
                             "add-ticket creates it")
        state, old_amount, old_kind, old_pool, note = row
        if state == "Pending" and not force:
            raise ValueError(
                f"...{number[-6:]} is Pending -- a gateway holds a live "
                "lease on it and is still to report the outcome. Wait for "
                "it, or pass --force if you know the lease is dead")
        if kind is not None and kind not in ALLOW_CODES:
            raise ValueError(f"kind must be one of {sorted(ALLOW_CODES)}")
        if amount_cents is not None and amount_cents <= 0:
            raise ValueError("a ticket is worth more than nothing")

        new_amount = old_amount if amount_cents is None else amount_cents
        new_kind = old_kind if kind is None else kind
        new_pool = old_pool if pool_id is None else pool_id
        new_state = "Open" if reopen else state

        if not reason.strip():
            raise ValueError(
                "--reason is required. An audit trail that records WHAT "
                "changed and not WHY answers the easy half of the question; "
                "the hard half is the one somebody asks months later")

        fields = []
        if new_amount != old_amount:
            fields.append(("amount_cents", old_amount, new_amount))
        if new_kind != old_kind:
            fields.append(("kind", old_kind, new_kind))
        if new_pool != old_pool:
            fields.append(("pool_id", old_pool, new_pool))
        if new_state != state:
            fields.append(("state", state, new_state))
        if not fields:
            return {"number": number, "state": state, "changes": []}
        changes = [f"{f} {o} -> {n}" for f, o, n in fields]

        now = utc_now()
        entry = f"[{now}] edit-ticket by {actor}: " + "; ".join(changes) \
                + f" ({reason})"
        # One transaction for the change and its audit row -- see
        # add_ticket for why the implicit one will not do.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._edit_within_transaction(
                number, new_amount, new_kind, new_pool, new_state,
                reopen, entry, now, actor, reason, fields)
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        if new_state == "Open":
            # §12.2's lease lives in memory as well as in the row.
            self.leases.pop(number, None)
        return {"number": number, "state": new_state, "changes": changes}

    def _edit_within_transaction(self, number, new_amount, new_kind, new_pool,
                                 new_state, reopen, entry, now, actor,
                                 reason, fields) -> None:
        self.conn.execute(
            "UPDATE tickets SET amount_cents = ?, kind = ?, pool_id = ?, "
            "state = ?, updated_at = ?, "
            # REOPENING CLEARS THE LEASE AND THE REDEEMER. Leaving either
            # behind would make the next redemption look like a replay of
            # the last one to anybody reading this row afterwards.
            "lease_client_id = CASE WHEN ? THEN NULL ELSE lease_client_id END, "
            "lease_query_id = CASE WHEN ? THEN NULL ELSE lease_query_id END, "
            "redeemed_by = CASE WHEN ? THEN NULL ELSE redeemed_by END, "
            "note = COALESCE(note || char(10), '') || ? "
            "WHERE validation_number = ?",
            (new_amount, new_kind, new_pool, new_state, now,
             reopen, reopen, reopen, entry, number))
        # SAME TRANSACTION as the UPDATE above. An audit row that can be
        # missing while its change is present is not an audit trail.
        self._audit(number, "edit", actor=actor, reason=reason, at=now,
                    changes=fields)

    def validate(self, client_id: str, payload: dict):
        """§12.2. Returns (response_payload, pull_from) where pull_from is
        the client_id whose drain should be invited at once, or None."""
        query_id = payload.get("query_id")
        try:
            number = normalise_number(payload["validation_number"])
        except (KeyError, ValueError):
            return self._reject(client_id, query_id, None, REJECT_NEUTRAL,
                                "malformed validation number"), None
        reported = payload.get("reported_amount_cents") or 0
        pull_from = None

        # The compare-and-set. BEGIN IMMEDIATE takes the write lock before
        # the state is READ, so two gateways asking about one ticket are
        # serialised here and the second sees Pending.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT state, amount_cents, kind, pool_id, "
                "restricted_expiration, cage_only, issuing_client_id "
                "FROM tickets WHERE validation_number = ?", (number,)).fetchone()
            decision = None
            if row is None:
                issued = self.conn.execute(
                    "SELECT client_id, misses FROM issued_numbers "
                    "WHERE validation_number = ?", (number,)).fetchone()
                if issued is None:
                    code, reason = REJECT_NEUTRAL, "not in tickets table"
                else:
                    pull_from = issued[0]
                    self.conn.execute(
                        "UPDATE issued_numbers SET misses = misses + 1 "
                        "WHERE validation_number = ?", (number,))
                    if issued[1] >= 1:
                        # A second attempt still finds no detail: this is
                        # WfA now, not an ordinary drain lag (§12.2, §12.5).
                        now = utc_now()
                        self.conn.execute(
                            "INSERT INTO tickets (validation_number, state, "
                            "source, issuing_client_id, created_at, updated_at) "
                            "VALUES (?, 'WfA', 'egm', ?, ?, ?)",
                            (number, issued[0], now, now))
                        code, reason = REJECT_TRY_AGAIN, "WfA: amount not yet known"
                    else:
                        code, reason = (REJECT_TRY_AGAIN,
                                        "recognised, detail not yet drained")
            else:
                state, amount, kind, pool_id, expiration, cage_only, issuer = row
                if state == "Open" and cage_only:
                    code, reason = REJECT_NEUTRAL, "cage-only instrument"
                elif state == "Open" and reported and int(reported) != int(amount):
                    code, reason = (REJECT_AMOUNT_MISMATCH,
                                    f"ticket reports {reported} cents, ledger "
                                    f"holds {amount}")
                elif state == "Open":
                    self.conn.execute(
                        "UPDATE tickets SET state = 'Pending', lease_client_id = ?, "
                        "lease_query_id = ?, updated_at = ? "
                        "WHERE validation_number = ? AND state = 'Open'",
                        (client_id, query_id, utc_now(), number))
                    self.leases[number] = self.clock() + self.lease_s
                    decision = {"query_id": query_id, "decision": "allow",
                                "transfer_code": ALLOW_CODES[kind],
                                "amount_cents": int(amount),
                                "restricted_expiration": int(expiration or 0),
                                "pool_id": int(pool_id or 0), "reason": "Open"}
                elif state == "WfA":
                    pull_from = issuer
                    code, reason = REJECT_TRY_AGAIN, "WfA: amount not yet known"
                elif state == "Expired":
                    code, reason = REJECT_EXPIRED, "Expired"
                elif state == "Pending":
                    code, reason = REJECT_NEUTRAL, "Pending: lease held by another claim"
                else:
                    # Redeemed, Paid, VOID, AUTHORISED_FOR_PAYMENT,
                    # UNKNOWN_REDEMPTION: neutral on the glass, specific in
                    # the log.
                    code, reason = REJECT_NEUTRAL, state
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

        if decision is not None:
            self._log_validation(client_id, query_id, number, "allow",
                                 decision["transfer_code"], "Open",
                                 decision["amount_cents"])
            return decision, None
        if reason.startswith("ticket reports"):
            self.alert("ticket_amount_mismatch", f"...{number[-6:]}: {reason}",
                       client_id)
        if reason == "not in tickets table":
            self.alert("ticket_unrecognised",
                       f"...{number[-6:]} is in no tickets table and no "
                       "issued pool; escalate to investigation", client_id)
        return self._reject(client_id, query_id, number, code, reason), pull_from

    def _reject(self, client_id, query_id, number, code, reason) -> dict:
        self._log_validation(client_id, query_id, number, "reject", code, reason, 0)
        if number is not None:
            at = self.clock()
            recent = [t for t in self.rejects.get((client_id, number), [])
                      if at - t < REJECT_CYCLE_WINDOW_S] + [at]
            self.rejects[(client_id, number)] = recent
            if len(recent) == REJECT_CYCLE_THRESHOLD:
                # §12.2: a flag for the technician queue. "It pages nobody."
                self.alert("validator_inspection_flag",
                           f"{len(recent)} reject cycles on ...{number[-6:]} "
                           f"within {REJECT_CYCLE_WINDOW_S:.0f}s", client_id)
        return {"query_id": query_id, "decision": "reject",
                "transfer_code": code, "amount_cents": 0, "reason": reason}

    def _log_validation(self, client_id, query_id, number, decision, code,
                        reason, amount) -> None:
        on = self.machine_of(client_id) or {}
        self.conn.execute(
            "INSERT INTO validation_log (at, client_id, query_id, "
            "validation_number, decision, transfer_code, reason, amount_cents, "
            "db_instance_id, epoch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (utc_now(), client_id, query_id, number, decision, code, reason,
             amount, on.get("db_instance_id"), on.get("id")))
        self.conn.commit()
        shown = f"...{number[-6:]}" if number else "(unreadable)"
        log(f"ticket-in {client_id} {shown}: {decision.upper()} "
            f"0x{code:02X} {reason}" + (f", {amount} cents" if amount else ""))

    def redemption_result(self, client_id: str, payload: dict) -> None:
        """§12.2 step 3: "Redeemed is written only on positive completion,
        never on authorisation.\""""
        try:
            number = normalise_number(payload["validation_number"])
            status = int(payload["machine_status"])
        except (KeyError, TypeError, ValueError):
            self.alert("redemption_result_invalid", json.dumps(payload)[:200],
                       client_id)
            return
        query_id = payload.get("query_id")
        self.conn.execute(
            "UPDATE validation_log SET result_status = ?, result_at = ? "
            "WHERE id = (SELECT id FROM validation_log WHERE client_id = ? "
            "AND query_id IS ? AND validation_number = ? ORDER BY id DESC LIMIT 1)",
            (status, utc_now(), client_id, query_id, number))
        row = self.conn.execute(
            "SELECT state, lease_client_id FROM tickets WHERE validation_number = ?",
            (number,)).fetchone()
        category = status >> 5
        positive, rejected = category == 0b000, category >= 0b100
        if row is not None and row[0] == "Pending" and row[1] == client_id:
            if positive:
                self.conn.execute(
                    "UPDATE tickets SET state = 'Redeemed', redeemed_by = ?, "
                    "updated_at = ?, lease_client_id = NULL, "
                    "redeemed_db_instance_id = ?, redeemed_epoch_id = ? "
                    "WHERE validation_number = ?",
                    (client_id, utc_now(),
                     (self.machine_of(client_id) or {}).get("db_instance_id"),
                     (self.machine_of(client_id) or {}).get("id"),
                     number))
                self.leases.pop(number, None)
                log(f"ticket ...{number[-6:]} REDEEMED at {client_id} "
                    f"(machine status 0x{status:02X})")
            elif rejected:
                # "The lease is released immediately on a rejected
                # completion status." Positive evidence that nothing moved.
                self.conn.execute(
                    "UPDATE tickets SET state = 'Open', updated_at = ?, "
                    "lease_client_id = NULL WHERE validation_number = ?",
                    (utc_now(), number))
                self.leases.pop(number, None)
                log(f"ticket ...{number[-6:]} back to Open: the machine "
                    f"reported 0x{status:02X} after authorisation")
            self.conn.commit()
        else:
            self.conn.commit()
            if positive:
                # A machine says it credited a ticket this server did not
                # have leased to it. That is money with no authorisation
                # behind it, and it must be loud.
                self.alert("redemption_without_lease",
                           f"machine status 0x{status:02X} (redeemed) for "
                           f"...{number[-6:]}, which is "
                           f"{row[0] if row else 'not in the tickets table'}",
                           client_id)

    def expire_leases(self) -> None:
        """§12.1: "A lease expiring never returns a ticket to Open: expiry
        proves no answer arrived in time, never that the answer was no.\""""
        at = self.clock()
        for number in [n for n, deadline in self.leases.items() if at >= deadline]:
            del self.leases[number]
            changed = self.conn.execute(
                "UPDATE tickets SET state = 'UNKNOWN_REDEMPTION', updated_at = ?, "
                "note = 'lease expired with no completion status' "
                "WHERE validation_number = ? AND state = 'Pending'",
                (utc_now(), number)).rowcount
            self.conn.commit()
            if changed:
                self.alert("lease_expired",
                           f"...{number[-6:]} had no REDEMPTION_RESULT in "
                           f"{self.lease_s:.0f}s; now UNKNOWN_REDEMPTION, locked")

    def expire_tickets(self, days: int = LIABILITY_WINDOW_DAYS) -> int:
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=days)).isoformat()
        changed = self.conn.execute(
            "UPDATE tickets SET state = 'Expired', updated_at = ? "
            "WHERE state = 'Open' AND created_at < ?", (utc_now(), cutoff)).rowcount
        self.conn.commit()
        return changed


class Connection:
    """One gateway's TCP connection."""

    def __init__(self, server: "Server", reader, writer):
        self.server, self.service = server, server.service
        self.reader, self.writer = reader, writer
        peer = writer.get_extra_info("peername")
        self.source = f"{peer[0]}:{peer[1]}" if peer else "?"
        self.client_id: str | None = None
        # invite_id -> when it was sent. More than one only when a pool
        # top-up had to go out while a drain invitation was still open.
        self.invites: dict[str, float] = {}

    async def send(self, message_type: int, payload: dict) -> None:
        self.writer.write(self.service.encode(self.client_id, message_type, payload))
        await self.writer.drain()

    async def invite(self, pool=None, replay=None, maintain=None) -> None:
        """One outstanding invitation per gateway. The server sets the
        pace (§3): a second invite before the first is answered would be
        the server flooding itself."""
        now = time.monotonic()
        self.invites = {i: at for i, at in self.invites.items()
                        if now - at < INVITE_TIMEOUT_S}
        if (self.invites and pool is None and replay is None
                and maintain is None):
            return
        invite_id = f"inv_{secrets.token_hex(6)}"
        self.invites[invite_id] = now
        payload = {"invite_id": invite_id, "max_events": DRAIN_MAX_EVENTS}
        if pool is not None:
            payload["pool_topup"] = pool
            self.service.mark_sent(pool["batch_uuid"])
        if replay is not None:
            # Rides on the invitation: §10.2 has no message of its own for
            # it, and a replay is by nature followed by a drain.
            payload["replay"] = replay
        if maintain is not None:
            # The same carriage as a replay, for the same reason: §10.2
            # has no message for it. Two booleans; the gateway builds its
            # own command line and answers with an ALERT when the run ends.
            payload["maintain"] = maintain
        await self.send(MessageType.PULL_INVITE, payload)

    async def send_pool(self, kind: str, batch: dict) -> None:
        if kind == "replace":
            self.service.mark_sent(batch["batch_uuid"])
            await self.send(MessageType.POOL_REPLACE, batch)
        else:
            await self.invite(pool=batch)

    async def on_heartbeat(self, payload: dict) -> None:
        svc, cid = self.service, self.client_id
        pool = payload.get("pool_status") or {}
        svc.conn.execute(
            "UPDATE gateways SET last_heartbeat = ?, reported_epoch = ?, "
            "reported_batch = ? WHERE client_id = ?",
            (json.dumps(payload, sort_keys=True), pool.get("pool_epoch"),
             pool.get("active_batch_uuid"), cid))
        svc.conn.commit()
        svc.note_machine(cid, payload.get("machine"),
                         payload.get("db_instance_id"))
        svc.note_sas_link(cid, payload.get("sas_link"))
        svc.note_drain(cid, payload.get("queue_depth"))
        decision = svc.pool_decision(cid, pool)
        if decision is not None:
            await self.send_pool(*decision)
        replay = svc.replay_needed(cid, payload.get("db_instance_id"),
                                   payload.get("acked"))
        if replay is not None:
            await self.invite(replay=replay)
        elif payload.get("queue_depth") and (decision is None
                                             or decision[0] == "replace"):
            await self.invite()

    async def on_frame(self, got: frame.Frame) -> None:
        svc, cid, kind = self.service, self.client_id, got.message_type
        if kind == MessageType.HEARTBEAT:
            await self.on_heartbeat(got.payload)
        elif kind == MessageType.EVENT_BATCH:
            if self.invites.pop(got.payload.get("invite_id"), None) is None:
                # §3 design rule 3: no backlog is ever pushed. A batch
                # nobody invited is not ingested.
                svc.alert("uninvited_batch",
                          f"EVENT_BATCH for invite "
                          f"{got.payload.get('invite_id')!r}, which is not "
                          "outstanding", cid)
                return
            try:
                acks = svc.ingest(cid, got.payload)
            except (KeyError, TypeError, ValueError) as e:
                svc.alert("event_batch_invalid", f"{type(e).__name__}: {e}", cid)
                return
            if acks:
                await self.send(MessageType.ACK, {"acks": acks})
                await self.invite()          # keep going until a batch is empty
        elif kind == MessageType.VALIDATION_QUERY:
            response, pull_from = svc.validate(cid, got.payload)
            await self.send(MessageType.VALIDATION_RESPONSE, response)
            target = self.server.connections.get(pull_from) if pull_from else None
            if target is not None:
                await target.invite()
        elif kind == MessageType.REDEMPTION_RESULT:
            svc.redemption_result(cid, got.payload)
        elif kind == MessageType.ALERT:
            svc.alert(str(got.payload.get("kind")), str(got.payload.get("detail")),
                      cid, origin="gateway")
        # ON_DEMAND_RESULT: nothing here ever asks, so nothing is expected.

    async def run(self) -> None:
        svc, buffer, failures = self.service, bytearray(), 0
        try:
            while True:
                waiting_for = (AUTH_TIMEOUT_S if self.client_id is None
                               else FRAME_COMPLETION_TIMEOUT_S if buffer
                               else MISSED_HEARTBEATS * 15.0)
                try:
                    chunk = await asyncio.wait_for(self.reader.read(65536),
                                                   waiting_for)
                except asyncio.TimeoutError:
                    log(f"{self.client_id or self.source}: closing, nothing "
                        f"valid in {waiting_for:.0f}s")
                    return
                if not chunk:
                    return
                buffer += chunk
                while True:
                    try:
                        got = frame.decode(bytes(buffer), key_for=svc.key_for,
                                           seq_floor=svc.rx_floor,
                                           expect_direction=frame.GATEWAY_TO_SERVER)
                    except frame.FrameTooShort:
                        break
                    except (frame.FrameLengthInvalid,
                            frame.FrameVersionUnsupported) as e:
                        log(f"{self.source}: unusable stream ({e}); closing")
                        return
                    except frame.ClientIdUnknown as e:
                        # §10.6: counted per SOURCE, never per client_id.
                        svc.alert("unknown_client_id",
                                  f"from {self.source}: {e}")
                        return
                    except frame.SequenceRejected as e:
                        # §10.5: sequence reuse is the signature of a cloned
                        # key and "bypasses the ladder".
                        svc.alert("sequence_rejected",
                                  f"from {self.source}: {e}. A cloned key, or "
                                  "a gateway recommissioned on one side only.")
                        return
                    except frame.FrameError as e:
                        failures += 1
                        svc.alert("frame_rejected",
                                  f"from {self.source}: {type(e).__name__}: {e}. "
                                  "Check key provisioning before suspecting "
                                  "intrusion (§10.6).")
                        if failures >= 3:
                            return
                        del buffer[:4 + int.from_bytes(buffer[:4], "big")]
                        continue
                    del buffer[:got.size]
                    if self.client_id is None:
                        self.client_id = got.client_id
                        self.server.adopt(self)
                    elif got.client_id != self.client_id:
                        svc.alert("client_id_changed_mid_connection",
                                  f"{self.client_id} then {got.client_id} "
                                  f"from {self.source}")
                        return
                    svc.accept_seq(got.client_id, got.seq, self.source)
                    await self.on_frame(got)
        except (ConnectionError, OSError) as e:
            log(f"{self.client_id or self.source}: {type(e).__name__}: {e}")
        except Exception as e:      # noqa: BLE001 -- one gateway, not the service
            # A defect handling ONE gateway's frame must not take the
            # listener down with it. Loud, recorded, and that connection
            # closed; the gateway reconnects on its own.
            svc.alert("service_error", f"{type(e).__name__}: {e}", self.client_id)
        finally:
            self.server.drop(self)
            self.writer.close()


class Server:
    def __init__(self, service: Service):
        self.service = service
        self.connections: dict[str, Connection] = {}
        self.unauthenticated = 0
        # Far enough back that the first sweep runs as soon as the service
        # is up: a server restarted at hour 34.9 must not wait five minutes
        # more and deliver the rotation late.
        self.last_rotation_sweep = float("-inf")

    def adopt(self, connection: Connection) -> None:
        self.unauthenticated -= 1
        old = self.connections.get(connection.client_id)
        if old is not None and old is not connection:
            # One connection per gateway (§3). The newer one wins: a
            # gateway that rebooted reconnects before the server notices
            # the old socket is dead.
            old.writer.close()
        self.connections[connection.client_id] = connection
        log(f"{connection.client_id} connected from {connection.source}")

    def drop(self, connection: Connection) -> None:
        if connection.client_id is None:
            self.unauthenticated -= 1
        elif self.connections.get(connection.client_id) is connection:
            del self.connections[connection.client_id]
            log(f"{connection.client_id} disconnected")

    async def handle(self, reader, writer) -> None:
        if self.unauthenticated >= MAX_UNAUTHENTICATED:
            writer.close()
            return
        # The server already gives up after MISSED_HEARTBEATS, but only
        # because it is waiting to READ. Keepalive also catches a gateway
        # that vanished mid-write, and frees the slot without waiting out
        # that timeout.
        raw = writer.get_extra_info("socket")
        if raw is not None:
            frame.enable_keepalive(raw)
        self.unauthenticated += 1
        await Connection(self, reader, writer).run()

    async def rotation_sweep(self) -> None:
        """Rule 6, every ROTATION_SWEEP_S: retire any pool that has reached
        35 hours, and say so when one has passed 36 without it.

        A ROTATION HERE IS A FULL REPLACEMENT (POOL_REPLACE, §7.4) at the
        gateway's own pool size, and that is a known step short of the spec
        change's rule 4, which revokes only the numbers that are actually
        stale and replaces half of them. That needs the number-level revoke
        and a signature that covers the revoke list, neither of which
        exists yet -- and a list-based revoke without the signature is the
        attack the spec change names. Until both exist, retiring the whole
        pool is the only revoke there is. It errs toward freshness: a
        top-up from an hour ago is retired with the rest.

        A gateway that is not connected cannot be rotated. It is not
        skipped silently: past 36 hours it is alerted, once an hour.
        """
        svc = self.service
        for (client_id,) in svc.conn.execute(
                "SELECT client_id FROM gateways ORDER BY client_id").fetchall():
            connection = self.connections.get(client_id)
            if connection is not None and svc.rotation_due(client_id):
                age = svc.oldest_live_batch_age_hours(client_id)
                log(f"rotating {client_id}: its oldest numbers are "
                    f"{age:.1f}h old (rotation at {POOL_ROTATION_HOURS:.0f}h)")
                batch = svc.mint_batch(client_id, svc.pool_size(client_id),
                                       "replace")
                try:
                    await connection.send_pool("replace", batch)
                except (ConnectionError, OSError):
                    pass     # unacknowledged, so the next heartbeat re-sends it
                continue
            age = svc.rotation_missed(client_id)
            if age is not None:
                why = ("not connected" if connection is None else
                       "connected, but its last batch is still unacknowledged")
                svc.alert("rotation_missed",
                          f"oldest numbers are {age:.1f}h old, past the "
                          f"{POOL_AGE_ALERT_HOURS:.0f}h alert; {why}. Numbers "
                          "already printed stay redeemable.", client_id)

    async def housekeeping(self) -> None:
        svc, last_expiry = self.service, 0.0
        while True:
            await asyncio.sleep(DRAIN_INVITE_INTERVAL_S)
            try:
                await self._housekeep_once(svc, last_expiry)
            except Exception as e:      # noqa: BLE001
                log(f"housekeeping error (continuing): {type(e).__name__}: {e}")
            if time.monotonic() - last_expiry > 3600:
                last_expiry = time.monotonic()

    async def _housekeep_once(self, svc, last_expiry: float) -> None:
        svc.expire_leases()
        for connection in list(self.connections.values()):
            try:
                await connection.invite()     # a no-op while one is outstanding
            except (ConnectionError, OSError):
                pass                          # its own reader will notice
        if time.monotonic() - last_expiry > 3600:
            svc.expire_tickets()
        if time.monotonic() - self.last_rotation_sweep >= ROTATION_SWEEP_S:
            self.last_rotation_sweep = time.monotonic()
            await self.rotation_sweep()
        for cmd_id, client_id, kind in svc.conn.execute(
                "SELECT id, client_id, kind FROM commands "
                "WHERE state = 'pending' ORDER BY id").fetchall():
            connection = self.connections.get(client_id)
            if kind not in ("replace_pool", "aft_transfer", "maintain"):
                state, detail = "failed", f"unknown command {kind!r}"
            elif connection is None:
                continue         # stays pending until that gateway connects
            elif kind == "maintain":
                # "sent", not "done": the run takes up to two minutes on
                # the gateway and its outcome comes back as an ALERT
                # (maintenance_done / _deferred / _failed / _refused).
                options = json.loads(
                    svc.conn.execute("SELECT detail FROM commands WHERE id = ?",
                                     (cmd_id,)).fetchone()[0])
                await connection.invite(maintain=options)
                state, detail = "sent", json.dumps(options, sort_keys=True)
            elif kind == "aft_transfer":
                # The payload was validated when it was queued; this just
                # delivers it. The gateway answers through the ordinary
                # event drain (lab_auto_actions), not with a reply frame:
                # an AFT takes seconds and may poll the machine a dozen
                # times to resolve a PENDING status, which is far longer
                # than a request/response on this link should hold open.
                payload = json.loads(
                    svc.conn.execute("SELECT detail FROM commands WHERE id = ?",
                                     (cmd_id,)).fetchone()[0])
                await connection.send(MessageType.AFT_TRANSFER, payload)
                state, detail = "sent", json.dumps(payload)
            else:
                latest = svc.latest_batch(client_id)
                if latest is not None and latest[4] is None:
                    # Until the gateway acknowledges, BOTH epochs are
                    # potentially live (§7.4). A third would make three.
                    continue
                batch = svc.mint_batch(client_id, svc.pool_size(client_id),
                                       "replace")
                await connection.send_pool("replace", batch)
                state, detail = "done", f"epoch {batch['pool_epoch']}"
            svc.conn.execute("UPDATE commands SET state = ?, detail = ? "
                             "WHERE id = ?", (state, detail, cmd_id))
            svc.conn.commit()


async def serve(args) -> None:
    conn = open_db(args.db)
    service = Service(conn, load_seed(args.db), system_id=args.system_id)
    server = Server(service)
    listener = await asyncio.start_server(server.handle, args.host, args.port)
    names = ", ".join(str(s.getsockname()) for s in listener.sockets)
    log(f"gateway service STAND-IN listening on {names}; database {args.db}")
    log(f"pool signing public key: {service.public_key.hex()}")
    known = [r[0] for r in conn.execute("SELECT client_id FROM gateways")]
    log(f"commissioned gateways: {', '.join(known) if known else 'NONE -- run commission first'}")
    async with listener:
        await asyncio.gather(listener.serve_forever(), server.housekeeping())


# --- command line ------------------------------------------------------------

def cmd_init(args) -> int:
    conn = open_db(args.db)
    path = signing_key_path(args.db)
    if path.exists():
        print(f"{path} already exists; keeping it. (Replacing the signing key "
              "invalidates the public key on every commissioned gateway.)")
    else:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(secrets.token_hex(ed25519.SEED_BYTES) + "\n")
        print(f"created pool signing key {path}")
    print(f"database {args.db} ready")
    print(f"pool signing PUBLIC key: {ed25519.public_key(load_seed(args.db)).hex()}")
    conn.close()
    return 0


def cmd_commission(args) -> int:
    conn = open_db(args.db)
    service = Service(conn, load_seed(args.db))
    existed = service.key_for(args.client_id) is not None
    key = service.commission(args.client_id)
    if args.pool_size is not None:
        try:
            service.set_pool_size(args.client_id, args.pool_size)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
    host = args.server_host or "<THIS-LAPTOP'S-IP>"
    print(f"{'RE' if existed else ''}commissioned {args.client_id}. "
          "Sequence counters reset on this side.")
    if existed:
        print("The previous key for this client_id no longer works.")
    print()
    print("Run this ON THE GATEWAY, in the repository directory (one line).")
    print("REPLACE POLLER_DB.sqlite3 with the SAME database your poller")
    print("writes to (sql_poll_logger.py --db ...). The client and the")
    print("poller must share one file: the poller writes validation_queries")
    print("and the client is what carries them to this server.")
    print()
    # THE PLACEHOLDER MUST SURVIVE BEING PASTED INTO A SHELL.
    #
    # The first version of this read "--db <poller-db>", and the operator
    # pasted it verbatim on 2026-11-13: bash read < as a redirection and
    # answered "poller-db: No such file or directory". A placeholder that
    # breaks the line it is embedded in is worse than no placeholder.
    print(f"  python3 -m gateway.client commission --db POLLER_DB.sqlite3 "
          f"--client-id {args.client_id} --server-host {host} "
          f"--server-port {args.port} --hmac-key {key} "
          f"--server-public-key {service.public_key.hex()}"
          + (" --force" if existed else ""))
    print()
    # THIS LINE USED TO HARDCODE gateway.sqlite3 AND IT COST A LAB EVENING.
    #
    # 2026-11-13: the operator followed it exactly, so the client ran on
    # gateway.sqlite3 while the poller wrote soak.sqlite3. Separate files
    # mean the client never sees a single validation_queries row, so
    # --server-ticket-authority cannot work however correct everything
    # else is -- and the stale file carried its own pool epoch, which
    # surfaced later as "pool_epoch_ahead: reports epoch 4, ahead of the
    # newest this server issued (1)".
    #
    # The server cannot know the poller's path, so it must not pretend to.
    print("Then stop any client still running with the OLD key, and start")
    print("one on that same database:")
    print("  pkill -f 'gateway.client run'")
    print("  python3 -m gateway.client run --db POLLER_DB.sqlite3")
    print()
    print("A client left running from before this recommission still holds")
    print("the old key, so the server will log 'AuthenticationFailed: bad")
    print("tag' once a second until it is stopped.")
    print()
    print("The HMAC key above is a secret. §10.5 has the technician type it "
          "on the device rather than send it over the floor network; for a "
          "lab, pasting it into your SSH session to the gateway is the same "
          "thing. Do not put it in a chat, a ticket or a repository.")
    return 0


def cmd_pool_size(args) -> int:
    conn = open_db(args.db)
    service = Service(conn, load_seed(args.db))
    if service.key_for(args.client_id) is None:
        print(f"{args.client_id} is not commissioned.", file=sys.stderr)
        return 2
    if args.size is not None:
        try:
            service.set_pool_size(args.client_id, args.size)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
    size = service.pool_size(args.client_id)
    print(f"{args.client_id}: a full issue is {size} number(s).")
    if args.size is not None:
        if service.latest_batch(args.client_id) is None:
            print("Nothing has been issued to it yet, so its FIRST pool will "
                  "be this size.")
        else:
            print("It already holds a pool of the earlier size. This takes "
                  "effect at the next rotation (35 h after the last full "
                  "issue), or now with:  python -m gateway.service "
                  f"replace-pool {args.client_id}")
    return 0


def _duration(seconds) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return "an unknown time"
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def describe_sas_link(link, egm=None) -> str:
    """One line for `status`: is the cabinet being polled, and if not, why.

    Two signals, read TOGETHER, because neither answers it alone:

      * `sas_link.state` is the poller's own word about the serial port:
        up, reconnecting, or down (it gave up).
      * `egm_status.online` is whether a meter row was written in the last
        two minutes -- whether polling is actually HAPPENING.

    The poller writes the link state only when it CHANGES, to spare the
    gateway's flash. So "up" may have been written days ago, and the age
    of that write says nothing: on a healthy gateway it is always old. A
    poller that has died leaves "up" behind it. What exposes that is the
    meters going stale while the link still reads up -- and that
    combination is a dead or hung POLLER, not a cable, which is a
    different visit by a different person.

    All of it is as of the gateway's last heartbeat, so `last seen` above
    must be recent for any of it to mean anything.
    """
    egm = egm if isinstance(egm, dict) else {}
    if not isinstance(link, dict) or not link.get("state"):
        return ("serial link: not reported (a gateway client older than "
                "2026-11-18, or a poller that has not started since)")
    state = str(link["state"])
    held = _duration(link.get("for_s"))
    detail = f" ({link['detail']})" if link.get("detail") else ""
    if state == "reconnecting":
        tries = (f", attempt {link['attempt']} of {link.get('max_attempts', '?')}"
                 if link.get("attempt") else "")
        return (f"serial link: RECONNECTING for {held}{tries} -- the poller "
                f"is alive and cannot open the port; check the cable{detail}")
    if state == "down":
        return (f"serial link: DOWN for {held} -- the poller gave up "
                f"reopening the port; nothing is polling this cabinet{detail}")
    if state == "up" and egm.get("online") is False:
        age = egm.get("meters_age_s")
        stale = (f"no meter row for {_duration(age)}" if age is not None
                 else "no meter row at all")
        return (f"serial link: last reported UP, but {stale} -- the POLLER "
                "has stopped or hung; this is not a cable fault")
    return f"serial link: {state.upper()} for {held}"


def cmd_machines(args) -> int:
    conn = open_db(args.db)
    service = Service(conn, load_seed(args.db))
    print("CABINETS EACH GATEWAY HAS BEEN ON (oldest first)")
    instances = conn.execute(
        "SELECT DISTINCT client_id, db_instance_id FROM machine_epochs "
        "ORDER BY client_id, db_instance_id").fetchall()
    if not instances:
        print("  none reported yet. A gateway reports them with its next "
              "drain; one running a client from before 2026-11-13 never does.")
    for cid, instance in instances:
        epochs = service.epochs_of(instance)
        for stay in machine.stays(list(epochs.values())):
            marks = ",".join("?" * len(stay["epoch_ids"]))
            count = conn.execute(
                f"SELECT COUNT(*) FROM ingest WHERE db_instance_id = ? AND "
                f"epoch_id IN ({marks})", [instance] + stay["epoch_ids"]
            ).fetchone()[0]
            print(f"  {cid}: {machine.describe_stay(stay)}")
            print(f"      {stay['started_at']} -> {stay['ended_at'] or 'now'}; "
                  f"{count} row(s); epochs {stay['epoch_ids']}"
                  + (f"; RAM-cleared {stay['cleared']}x" if stay["cleared"] else ""))
    unknown = conn.execute(
        "SELECT COUNT(*) FROM ingest WHERE epoch_id IS NULL").fetchone()[0]
    if unknown:
        print(f"  {unknown} ingested row(s) have no machine yet; "
              "`attribute` places the ones the epochs above can settle.")
    return 0


def cmd_attribute(args) -> int:
    conn = open_db(args.db)
    result = Service(conn, load_seed(args.db)).attribute()
    print(f"placed {result['placed']} row(s) on a machine.")
    if result["no_epochs_reported"]:
        print(f"{result['no_epochs_reported']} left unknown: their gateway "
              "database has not reported any machine epochs yet (update the "
              "gateway client and let it drain once, then run this again).")
    if result["before_first_epoch"]:
        print(f"{result['before_first_epoch']} left unknown: recorded before "
              "the first epoch that gateway has, or carrying no timestamp.")
    return 0


def cmd_add_ticket(args) -> int:
    conn = open_db(args.db)
    service = Service(conn, load_seed(args.db))
    try:
        number = normalise_number(args.number)
        cents = round(float(args.amount) * 100)
        service.add_ticket(number, cents, args.kind, args.pool_id,
                           actor=args.actor or default_actor(),
                           reason=args.reason or "add-ticket")
    except sqlite3.IntegrityError:
        print(f"{args.number} is already in the tickets table.", file=sys.stderr)
        return 2
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"added ...{number[-6:]} as {args.kind}, {cents} cents, Open "
          "(source: cashdesk)")
    return 0


def default_actor() -> str:
    """Who is typing, as best this machine can tell.

    getpass.getuser() reads the environment, so it is a CONVENIENCE and
    not an identity: anyone who can run this command can set USER. That is
    honest for a stand-in on somebody's laptop and would not be enough for
    a real CMS, where the actor comes from an authenticated session. The
    field exists so the question "who" has somewhere to live now, and so
    the schema does not have to change when the answer gets stronger.
    """
    try:
        return getpass.getuser()
    except Exception:                                   # noqa: BLE001
        return "unknown"


def cmd_edit_ticket(args) -> int:
    conn = open_db(args.db)
    service = Service(conn, load_seed(args.db))
    try:
        result = service.edit_ticket(
            args.number,
            amount_cents=(None if args.amount is None
                          else round(float(args.amount) * 100)),
            kind=args.kind, pool_id=args.pool_id,
            reopen=args.reopen, force=args.force,
            actor=args.actor or default_actor(), reason=args.reason)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if not result["changes"]:
        print(f"...{result['number'][-6:]} already matches; nothing changed.")
        return 0
    print(f"...{result['number'][-6:]} is now {result['state']}:")
    for change in result["changes"]:
        print(f"    {change}")
    print("The paper, the machine and every meter are unchanged -- this is "
          "what the server will ALLOW when the number is next presented.")
    return 0


def cmd_ticket_history(args) -> int:
    """Every recorded change to one ticket, oldest first."""
    conn = open_db(args.db)
    number = normalise_number(args.number)
    rows = conn.execute(
        "SELECT at, action, actor, reason, field, old_value, new_value, host "
        "  FROM ticket_audit WHERE validation_number = ? ORDER BY id",
        (number,)).fetchall()
    if not rows:
        # An empty history is not proof of an untouched ticket: the table
        # was added 2026-11-13 and anything changed before that left only
        # the note column. Say so rather than implying a clean record.
        held = conn.execute("SELECT state, amount_cents, note FROM tickets "
                            "WHERE validation_number = ?", (number,)).fetchone()
        if held is None:
            print(f"...{number[-6:]} is not in the tickets table.")
            return 2
        print(f"...{number[-6:]} has no audit rows. The table was added on "
              "2026-11-13; anything changed before then is only in the "
              "ticket's note:")
        print("   " + (held[2] or "(nothing recorded)").replace("\n", "\n   "))
        return 0
    print(f"...{number[-6:]} — {len(rows)} recorded change(s)")
    for at, action, actor, reason, field, old, new, host in rows:
        what = (f"{field}: {old} -> {new}" if field
                else "created")
        print(f"  {at}  {action:<5}  {what}")
        print(f"      by {actor} on {host}: {reason}")
    return 0


def cmd_push_aft(args) -> int:
    """Queue an AFT credit push to a gateway's EGM.

    This replaces a hand-written script the operator was given on
    2026-11-12, which created its own `server_aft_queue` table, inserted a
    row and printed "[+] Successfully queued AFT transfer". Nothing read
    that table, and §10.2 had no AFT message type at all, so the transfer
    could not have reached a machine by any path. The success line was
    reporting a local INSERT -- an absence rendered as a measurement, in a
    tool rather than in the gateway.

    It also stored the amount as str(float). Money is integer cents
    everywhere else in this project for the reason floats are never used
    for money, and the wire carries cents.
    """
    conn = open_db(args.db)
    if not conn.execute("SELECT 1 FROM gateways WHERE client_id = ?",
                        (args.client_id,)).fetchone():
        print(f"{args.client_id} is not commissioned.", file=sys.stderr)
        return 2
    amounts = {"cashable": round(float(args.cashable or 0) * 100),
               "restricted": round(float(args.restricted or 0) * 100),
               "nonrestricted": round(float(args.nonrestricted or 0) * 100)}
    total = sum(amounts.values())
    if total <= 0:
        print("nothing to transfer: give --cashable, --restricted or "
              "--nonrestricted", file=sys.stderr)
        return 2
    if any(v < 0 for v in amounts.values()):
        print("a transfer moves money ONTO the machine; amounts are "
              "positive. §8.3 transfers OUT are a different transfer type "
              "and this stand-in does not issue them.", file=sys.stderr)
        return 2
    if amounts["restricted"] and args.pool_id <= 0:
        # §8.3: restricted credits belong to a promotional pool, and a
        # machine holds credits from ONE pool at a time. Pool 0 against an
        # existing restricted balance is refused by the cabinet, so a
        # transfer queued without one is a trip to the floor wasted.
        print("--restricted needs --pool-id (§8.3: restricted credits carry "
              "a pool, and a machine holds one pool at a time)",
              file=sys.stderr)
        return 2
    payload = {"transfer_id": str(uuid.uuid4()),
               "cashable_cents": amounts["cashable"],
               "restricted_cents": amounts["restricted"],
               "nonrestricted_cents": amounts["nonrestricted"],
               "pool_id": args.pool_id,
               "expiration": args.expiration,
               "queued_at": utc_now()}
    conn.execute(
        "INSERT INTO commands (created_at, client_id, kind, detail) "
        "VALUES (?, ?, 'aft_transfer', ?)",
        (utc_now(), args.client_id, json.dumps(payload)))
    conn.commit()
    print(f"queued AFT {payload['transfer_id']} for {args.client_id}: "
          + ", ".join(f"{v / 100:,.2f} {k.replace('_cents', '')}"
                      for k, v in payload.items()
                      if k.endswith("_cents") and v))
    print("The running service sends it when that gateway is connected. "
          "THE GATEWAY WILL REFUSE IT unless its poller was started with "
          "--allow-server-aft. Watch `status`, and the gateway's "
          "lab_auto_actions rows, for what the machine said.")
    return 0


def cmd_replace_pool(args) -> int:
    conn = open_db(args.db)
    if not conn.execute("SELECT 1 FROM gateways WHERE client_id = ?",
                        (args.client_id,)).fetchone():
        print(f"{args.client_id} is not commissioned.", file=sys.stderr)
        return 2
    conn.execute("INSERT INTO commands (created_at, client_id, kind) "
                 "VALUES (?, ?, 'replace_pool')", (utc_now(), args.client_id))
    conn.commit()
    print(f"queued. The running service sends POOL_REPLACE to {args.client_id} "
          "within a second if it is connected, and when it next connects if "
          "not. `status` shows the acknowledgement.")
    return 0


def cmd_maintain(args) -> int:
    """Ask a gateway to prune and reclaim NOW, instead of at its timer.

    The gateway is headless; this and its own 03:45/15:45 timer are the
    only two things that can start the job. What is asked for is two
    switches. The gateway decides everything else, and may refuse.
    """
    conn = open_db(args.db)
    if not conn.execute("SELECT 1 FROM gateways WHERE client_id = ?",
                        (args.client_id,)).fetchone():
        print(f"{args.client_id} is not commissioned.", file=sys.stderr)
        return 2
    wait_min = getattr(args, "wait_for_zero", 0) or 0
    if not 0 <= wait_min <= 720:
        print("--wait-for-zero is in minutes, from 0 to 720.", file=sys.stderr)
        return 2
    if wait_min and args.ignore_credits:
        print("--wait-for-zero and --ignore-credits contradict each other.",
              file=sys.stderr)
        return 2
    options = {"prune_acked": not args.no_prune,
               "require_zero_credits": not args.ignore_credits}
    if wait_min:
        options["wait_for_zero_s"] = int(wait_min * 60)
    below_mb = getattr(args, "repeat_min_freed_mb", 0) or 0
    if not 0 <= below_mb <= 100000:
        print("--repeat-min-freed-mb is in megabytes, 0 or more.", file=sys.stderr)
        return 2
    if below_mb:
        options["repeat_min_freed_bytes"] = int(below_mb * 1_000_000)
    conn.execute("INSERT INTO commands (created_at, client_id, kind, detail) "
                 "VALUES (?, ?, 'maintain', ?)",
                 (utc_now(), args.client_id, json.dumps(options, sort_keys=True)))
    conn.commit()
    print(f"queued for {args.client_id}: "
          + ("prune acknowledged rows, then reclaim space"
             if options["prune_acked"] else "reclaim space only (no prune)")
          + ("; ONLY if the machine holds zero credits."
             if options["require_zero_credits"]
             else "; EVEN IF the machine holds credits."))
    if wait_min:
        print(f"If the machine holds credits, the gateway answers "
              f"maintenance_waiting, holds the request for up to {wait_min:g} "
              "min, and starts the run by itself when credits read zero. If "
              "they never do, it answers maintenance_deferred at the end.")
    if below_mb:
        print(f"The gateway runs it again, up to ten times, while each run "
              f"shrinks the database file by at least {below_mb:g} MB. Every "
              "run checks the credits again. One alert at the end gives the "
              "total.")
    print("The running service sends it within a second if the gateway is "
          "connected. The run takes up to two minutes; the result arrives in "
          "the `serve` window and under ALERTS in `status` as "
          "maintenance_done, maintenance_deferred, maintenance_failed or "
          "maintenance_refused.")
    if not options["require_zero_credits"]:
        print("NOTE: without the credit gate, a cashout that lands while the "
              "database is busy can run out its ten seconds and become a "
              "handpay. Fine in the lab; not on a floor.")
    return 0


def describe_maintenance(m) -> str:
    """One line answering "is this gateway's database being looked after".

    Added 2026-11-26 with the heartbeat field (A52). The field existed for a
    day without this, which made it useless: the gateway reported that nobody
    had enabled its maintenance timer and the server printed everything
    except that.

    The four timer states are kept apart deliberately. 'absent' is the
    2026-11-26 failure -- six days, no timer installed, 80 MB -> 353 MB, and
    a gateway that looked healthy in every other line of this report. null is
    NOT that: it means the gateway could not ask systemd, and printing it as
    absent would send somebody to re-install a timer that is already running.
    """
    if not isinstance(m, dict):
        return ("maintenance: not reported (a gateway client older than "
                "2026-11-26)")
    timer, unit = m.get("timer"), m.get("timer_unit") or "its timer"
    since, outcome = m.get("since_done_h"), m.get("last_attempt_outcome")

    if m.get("last_done_at") is None:
        ran = "NEVER COMPLETED a run"
    elif since is None:
        ran = f"last completed {m['last_done_at']}"
    elif since < 48:
        ran = f"last completed {since:.1f}h ago"
    else:
        ran = f"LAST COMPLETED {since / 24:.1f} DAYS AGO"

    if timer == "absent":
        return (f"maintenance: *** {unit} IS NOT INSTALLED *** -- nothing "
                f"schedules pruning on this gateway and its database grows "
                f"unbounded; {ran}")
    if timer == "inactive":
        return (f"maintenance: {unit} is installed but NOT RUNNING; {ran}")
    if timer is None:
        return (f"maintenance: timer state unknown -- this gateway could not "
                f"ask systemd, which is not the same as no timer; {ran}")

    note = ""
    if outcome == "deferred":
        note = " (last attempt DEFERRED: the machine held credits)"
    return f"maintenance: {unit} active, {ran}{note}"


def _aft_gateway_answer(conn, transfer_id) -> tuple:
    """The GATEWAY's own answer to a pushed AFT: (state, the text verbatim).

    `commands.state` only ever says what the SERVER did: 'pending' until a
    gateway connects, then 'sent' forever. It cannot say what the machine
    answered, and for months it read `[sent]` for transfers the cabinet had
    declined. Operator, 2026-11-26, after a 0x87: "AFT didn't land."

    READ FROM lab_auto_actions, AND THE FIRST VERSION OF THIS DID NOT.

    It read the gateway's `host_requests` row, on the reasoning that the
    poller finishes that row with a state and a reason, the client drains
    it, and it lands in `ingest` -- so nothing new needed sending. **That
    was wrong twice over, and hardware found it within minutes:** every
    command printed `gateway: pending`, including ones whose money had
    demonstrably landed.

      1. The drain selects `WHERE synced_at IS NULL`, sends, then stamps
         synced_at. A row travels EXACTLY ONCE, in whatever state it held
         at that instant. host_requests rows are created 'pending' and
         finished later, and the client drains every second, so it
         reliably catches them pending and the final state never leaves
         the gateway.
      2. Clearing synced_at on finish would not help. The ingest upsert is
         deliberately append-only -- "a re-sent row changes nothing
         (§11.1)" -- so the server would discard the finished content on
         principle. **A state that changes after creation cannot be
         carried by an append-only ingest.**

    lab_auto_actions is append-only BY NATURE: one row per action, written
    once, with the outcome already in it. That is why it has carried the
    real answer all along -- `status 0x00 SUCCESSFUL`, `status 0x87
    FAILED: ...`, `REFUSED: inside the 120.0s minimum interval` -- while
    the row this function used to read said `pending`.

    THE TEXT IS RETURNED VERBATIM, not re-worded. It was written for a
    person to act on and it already names the remedy; re-deriving a
    category from it would be a third chance to lose the reason.

    Returns (None, None) when no answer has arrived, which is a real state
    and not an error: a queued transfer the gateway has not reached yet.
    """
    if not transfer_id:
        return None, None
    row = conn.execute(
        "SELECT row_json FROM ingest WHERE table_name = 'lab_auto_actions' "
        "   AND row_json LIKE ? ORDER BY row_id DESC LIMIT 1",
        (f"%{transfer_id}%",)).fetchone()
    if row is None:
        return None, None
    try:
        parsed = json.loads(row[0])
    except (ValueError, TypeError):
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    text = parsed.get("outcome")
    if not text:
        return None, None
    text = str(text)
    if text.startswith("status 0x00"):
        state = "done"
    elif text.startswith("REFUSED"):
        state = "refused"
    elif "FAILED" in text:
        state = "failed"
    else:
        state = "answered"
    return state, text


def cmd_status(args) -> int:
    conn = open_db(args.db)
    service = Service(conn, load_seed(args.db))
    print("GATEWAYS")
    for cid, seen, src, hb, epoch in conn.execute(
            "SELECT client_id, last_seen_at, last_source, last_heartbeat, "
            "reported_epoch FROM gateways ORDER BY client_id"):
        beat = json.loads(hb) if hb else {}
        pool = beat.get("pool_status") or {}
        egm = beat.get("egm_status") or {}
        print(f"  {cid}: last seen {seen or 'never'} from {src or '-'}")
        on = service.machine_of(cid)
        print("    on " + (machine.describe_stay(
            service.stay(on.get("db_instance_id"), on.get("id")))
            if on and service.stay(on.get("db_instance_id"), on.get("id"))
            else machine.describe(on)))
        if beat:
            print(f"    pool: {pool.get('available_ids')} available, epoch "
                  f"{pool.get('pool_epoch')}, low_water={pool.get('low_water')} "
                  f"(mark {pool.get('low_water_mark', 'not reported')}), "
                  f"used_24h={pool.get('used_24h', 'not reported')}")
            print(f"    egm online={egm.get('online')} sas={egm.get('sas_version')} "
                  f"queue_depth={beat.get('queue_depth')} "
                  f"credit_meter={beat.get('credit_meter')}")
            print("    " + describe_sas_link(beat.get("sas_link"), egm))
            print("    " + describe_maintenance(beat.get("maintenance")))
        age = service.oldest_live_batch_age_hours(cid)
        print(f"    full issue: {service.pool_size(cid)} number(s); oldest "
              "live numbers: " + ("nothing issued yet" if age is None else
              f"{age:.1f}h old, rotation at {POOL_ROTATION_HOURS:.0f}h"))
    # QUEUED COMMANDS. push-aft and replace-pool both say "queued" and
    # then nothing ever says whether the thing was sent -- so on
    # 2026-11-13 two AFT pushes sat pending with the gateway's client
    # down, and the only visible evidence was their absence at the far
    # end. A queue nobody can look at is a queue that swallows things.
    print("COMMANDS (newest first)")
    rows = conn.execute(
        "SELECT id, created_at, client_id, kind, state, detail "
        "FROM commands ORDER BY id DESC LIMIT ?", (args.limit,)).fetchall()
    if not rows:
        print("  (none)")
    for cmd_id, created, cid, kind, state, detail in rows:
        note = ""
        if state == "pending":
            note = ("  <- NOT SENT. Stays pending until that gateway's "
                    "`python3 -m gateway.client run` connects.")
        print(f"  {cmd_id:4d} {created} {cid} {kind} [{state}]{note}")
        if detail and kind == "maintain":
            print(f"       {detail}")
        if detail and kind == "aft_transfer":
            try:
                d = json.loads(detail)
                print("       " + ", ".join(
                    f"{d[k] / 100:,.2f} {k[:-6]}" for k in
                    ("cashable_cents", "restricted_cents",
                     "nonrestricted_cents") if d.get(k))
                    + (f", pool {d['pool_id']}" if d.get("pool_id") else ""))
                # WHAT THE MACHINE SAID, not what we did with the request.
                answer, why = _aft_gateway_answer(conn, d.get("transfer_id"))
                if answer:
                    print(f"       gateway: {answer}"
                          + (f" -- {why}" if why else ""))
                elif state == "sent":
                    print("       gateway: no answer yet. 'sent' means the "
                          "server handed it over, NOT that the machine "
                          "took it.")
            except (ValueError, TypeError, KeyError):
                pass
    pending = sum(1 for r in rows if r[4] == "pending")
    if pending:
        print(f"  {pending} command(s) waiting for a gateway to connect. "
              "A queued command is not a delivered one.")
    print()

    print("POOL BATCHES (newest first)")
    for row in conn.execute(
            "SELECT client_id, pool_epoch, kind, created_at, acknowledged_at "
            "FROM pool_batches ORDER BY id DESC LIMIT ?", (args.limit,)):
        print(f"  {row[0]} epoch {row[1]} {row[2]:7s} created {row[3]} "
              f"acknowledged {row[4] or 'NOT YET'}")
    print("TICKETS")
    for state, count, cents in conn.execute(
            "SELECT state, COUNT(*), COALESCE(SUM(amount_cents), 0) FROM tickets "
            "GROUP BY state ORDER BY state"):
        print(f"  {state:22s} {count:5d}   {cents / 100:12.2f}")
    for row in conn.execute(
            "SELECT validation_number, state, amount_cents, kind, source, "
            "updated_at FROM tickets ORDER BY updated_at DESC LIMIT ?",
            (args.limit,)):
        print(f"    ...{row[0][-6:]} {row[1]:20s} "
              f"{'' if row[2] is None else row[2]:>8} {row[3]:13s} {row[4]:8s} {row[5]}")
    print("TICKET-IN (newest first)")
    for row in conn.execute(
            "SELECT at, client_id, validation_number, decision, transfer_code, "
            "reason, amount_cents, result_status FROM validation_log "
            "ORDER BY id DESC LIMIT ?", (args.limit,)):
        result = "no result yet" if row[7] is None else f"machine 0x{row[7]:02X}"
        print(f"  {row[0]} {row[1]} ...{(row[2] or '??????')[-6:]} {row[3]} "
              f"0x{row[4]:02X} {row[5]} -> {result}")
    print("INGESTED ROWS, BY MACHINE")
    for heading, tables in service.rows_by_machine():
        print(f"  {heading}")
        for table, count in tables:
            print(f"    {table:28s} {count}")
    print("ALERTS (newest first)")
    for row in conn.execute(
            "SELECT at, origin, client_id, kind, detail FROM alerts "
            "ORDER BY id DESC LIMIT ?", (args.limit,)):
        print(f"  {row[0]} [{row[1]}/{row[2] or '-'}] {row[3]}: {row[4]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m gateway.service",
        description="A laptop stand-in for the server-side gateway service "
        "(spec §11) and the ticket rules a gateway depends on (§12.1-12.2). "
        "LAB USE: it mints its own validation numbers.")
    parser.add_argument("--db", default="cms_standin.sqlite3",
                        help="the stand-in's own database (default %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and the pool signing "
                   "key").set_defaults(func=cmd_init)

    c = sub.add_parser("commission", help="mint a gateway's HMAC key and print "
                       "the command to run on the gateway")
    c.add_argument("client_id")
    c.add_argument("--server-host", default=None,
                   help="this machine's address as the gateway reaches it")
    c.add_argument("--port", type=int, default=DEFAULT_PORT)
    c.add_argument("--pool-size", type=int, default=None,
                   help=f"numbers in this gateway's pool at first issue and "
                   f"at each rotation (default {POOL_CAPACITY})")
    c.set_defaults(func=cmd_commission)

    ps = sub.add_parser("pool-size", help="show or set what a full issue "
                        "gives one gateway, WITHOUT recommissioning it")
    ps.add_argument("client_id")
    ps.add_argument("size", type=int, nargs="?", default=None)
    ps.set_defaults(func=cmd_pool_size)

    s = sub.add_parser("serve", help="listen for gateways")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--system-id", type=int, default=DEFAULT_SYSTEM_ID,
                   help="validation system ID for minted numbers, 1-99 "
                   "(default %(default)s; 0 means 'deny' in Table 15.8a)")
    s.set_defaults(func=lambda a: asyncio.run(serve(a)) or 0)

    t = sub.add_parser("add-ticket", help="enter a ticket by hand (a "
                       "cashdesk-issued ticket, or old test media)")
    t.add_argument("number", help="18 digits; dashes are ignored")
    t.add_argument("--amount", required=True, help="in currency units, e.g. 25.00")
    t.add_argument("--kind", default="cashable", choices=sorted(ALLOW_CODES))
    t.add_argument("--pool-id", type=int, default=0,
                   help="restricted tickets only")
    t.add_argument("--actor", default=None,
                   help="who is doing this (default: this machine's user)")
    t.add_argument("--reason", default=None, help="why, for the audit trail")
    t.set_defaults(func=cmd_add_ticket)

    e = sub.add_parser("edit-ticket", help="re-value or re-open a ticket "
                       "already in the table (LAB: test media reuse)")
    e.add_argument("number", help="18 digits; dashes are ignored")
    e.add_argument("--amount", default=None,
                   help="new value in currency units, e.g. 91.92")
    e.add_argument("--kind", default=None, choices=sorted(ALLOW_CODES))
    e.add_argument("--pool-id", type=int, default=None,
                   help="restricted tickets only")
    e.add_argument("--reopen", action="store_true",
                   help="set the state back to Open so the number can be "
                        "presented again, clearing any lease and redeemer. "
                        "This is how a ticket the machine already redeemed "
                        "becomes reusable test media.")
    e.add_argument("--force", action="store_true",
                   help="edit even a Pending ticket, whose lease is live")
    e.add_argument("--actor", default=None,
                   help="who is doing this (default: this machine's user)")
    e.add_argument("--reason", required=True,
                   help="REQUIRED. Why this ticket's value is changing. An "
                        "audit trail that records what changed and not why "
                        "answers the easy half of the question.")
    e.set_defaults(func=cmd_edit_ticket)

    h = sub.add_parser("ticket-history",
                       help="every recorded change to one ticket")
    h.add_argument("number", help="18 digits; dashes are ignored")
    h.set_defaults(func=cmd_ticket_history)

    a = sub.add_parser("push-aft", help="push credits to a gateway's EGM "
                       "(AFT_TRANSFER, §8.3). The gateway refuses it unless "
                       "started with --allow-server-aft.")
    a.add_argument("client_id")
    a.add_argument("--cashable", default=None, help="currency units, e.g. 19.19")
    a.add_argument("--restricted", default=None, help="needs --pool-id")
    a.add_argument("--nonrestricted", default=None)
    a.add_argument("--pool-id", type=int, default=0,
                   help="promotional pool for restricted credits (§8.3)")
    a.add_argument("--expiration", type=int, default=0,
                   help="0 = none; otherwise MMDDYYYY or a day count (§8.3)")
    a.set_defaults(func=cmd_push_aft)

    r = sub.add_parser("replace-pool", help="retire a gateway's pool and issue "
                       "a new one (POOL_REPLACE, §7.4)")
    r.add_argument("client_id")
    r.set_defaults(func=cmd_replace_pool)

    m = sub.add_parser("maintain", help="ask a gateway to prune acknowledged "
                       "rows and reclaim space now (it reports back as an "
                       "alert)")
    m.add_argument("client_id")
    m.add_argument("--no-prune", action="store_true",
                   help="reclaim free pages only; delete nothing")
    m.add_argument("--ignore-credits", action="store_true",
                   help="run even if the machine holds credits (lab use: "
                        "the default waits for zero)")
    m.add_argument("--wait-for-zero", type=float, default=0, metavar="MINUTES",
                   help="if the machine holds credits, have the gateway hold "
                        "the request this long and run it when credits reach "
                        "zero (default 0: one attempt, no waiting)")
    m.add_argument("--repeat-min-freed-mb", type=float, default=0, metavar="MB",
                   help="run again, up to ten times, until a run shrinks the "
                        "database file by less than this (default 0: one run)")
    m.set_defaults(func=cmd_maintain)

    sub.add_parser("machines", help="which cabinets each gateway has been "
                   "attached to, and how many rows came from each"
                   ).set_defaults(func=cmd_machines)
    sub.add_parser("attribute", help="give a machine to rows ingested before "
                   "the gateway reported which machine it was on"
                   ).set_defaults(func=cmd_attribute)

    st = sub.add_parser("status", help="gateways, pools, tickets, alerts")
    st.add_argument("--limit", type=int, default=10)
    st.set_defaults(func=cmd_status)
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
    if getattr(args, "system_id", 1) not in range(1, 100):
        print("--system-id must be 1-99", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except KeyboardInterrupt:
        log("stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
