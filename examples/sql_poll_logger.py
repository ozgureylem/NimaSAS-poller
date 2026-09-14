#!/usr/bin/env python3
"""One poll loop, one SQLite database: meters, ticket-out history,
ticket-in capture, and gateway-local cashout validation, all from a
single continuous general-poll cycle.

This is a worked, runnable example of the "local SQL layer on top of
SASClient" pattern described in MANUAL.md — a starting point for
prototyping gateway architecture, not a production drain/sync service.
Every poll failure is logged rather than raised, so one bad exchange
never stops the loop.

It is deliberately one program, not several. Only one process can safely
own the serial port's poll loop, and ticket-in/ticket-out capture both
depend on seeing the *same* continuous general-poll stream that meters
share the connection with — splitting these into separate programs would
mean two loops competing over one shared serial line, and would break
the exception-draining guarantee a real gateway depends on (an
unread exception can be overwritten by the next one if nothing drains it
fast enough — see the SAS spec's exception-queue behavior, §2.2.1).

What each cycle does, in order:

1. A general poll. If it returns exception 0x67 (ticket inserted), read
   the ticket's validation data (LP 70) and log it to ticket_in_events —
   read-only; see "What this does NOT do" below. If it returns 0x3D or
   0x3E (a ticket-out record is ready), drain every currently-unread
   ticket-out record (LP 4D, function code 0x00) into ticket_out_history.
   If it returns 0x57 (system validation request — the machine is ready
   to print a cashout ticket and is waiting to be told what validation
   number to use), answer it locally from validation_pool: read the
   pending cashout amount (LP 57), take the next available number from
   the pool, and answer with it (LP 58) — see "Gateway-local cashout
   validation" below.
2. Meters — deliberately as many as this client can reach in one cycle,
   all in the same row, on the same cadence/ring-history/rollover logic
   (see the HistoryConfig docstring and MANUAL.md §4/§6): the six core
   meters (LP 0F), the eight cumulative ticket meters (LP 2F — the only
   way to reach these at all), LP 0x19/0x1C's own independent reads of
   several of the same core counters, games-since-power-up/door-closure
   (LP 0x18), total bill meters by denomination (LP 0x1E), hand-paid
   cancelled credits (LP 0x2D), current hopper status (LP 0x4F), and —
   unless --skip-full-meter-sweep is given — every other single-meter
   long poll this client knows how to read (LP 0x10-0x51/0x55, ~39
   polls). Several of these deliberately overlap: LP 0x19/0x1C, and most
   of the single-meter sweep, report counters LP 0x0F already reports,
   through entirely independent request/response exchanges. That
   redundancy is the point, not an oversight — three long polls
   disagreeing about "total coin in" this cycle is a real finding a
   single poll can never surface, and a reference/stress-testing tool
   has no reason to economize on wire traffic the way a production
   gateway might. A poll failure anywhere in this group aborts that
   cycle's entire meter write, rather than saving a row that's fresh in
   some columns and stale or missing in others.

On startup, the full ticket-out buffer (indices 1-31, non-destructive —
see send_enhanced_validation_information()'s docstring) is also read
once into ticket_out_history, so you get whatever the machine is already
holding, not just what happens from here forward.

Gateway-local cashout validation. Per the project's own Technical v3
§5.4: cashout is the one direction where the gateway IS meant to answer
without a server round-trip — the server pre-issues a batch of
validation numbers to the gateway in advance, and the gateway assigns
the next one itself when the machine asks. This is the opposite rule
from ticket-in (§5.8) below, not an inconsistency: a gateway may spend
from a pool it was already trusted with, but may never itself decide
whether someone else's ticket is genuine.

validation_pool here is a deliberately simplified stand-in for that
server-issued pool — a local SQLite table you seed (--seed-validation-
pool, or hand-insert real numbers yourself), with no protocol behind it
(no pool_epoch, no batch_uuid, no HMAC-authenticated top-up over the
network — that's real gateway<->server protocol machinery, out of scope
for a SAS wire-protocol reference client). If the pool runs out,
the cashout is left unanswered and the machine's own 10-second timeout
handles it, logged as a PoolExhausted row in poll_errors.

What this does NOT do: authorize or redeem tickets *coming in*. A
ticket-in event is read and logged (amount, validation data) but this
tool never calls redeem_ticket() — deciding whether to pay a ticket is
a real business/security decision (see the project's own Decisions
Annex on this), and a reference poller has no way to make that decision
correctly. Left unredeemed, the machine safely returns the ticket to
the player after its own 30-second timeout (spec-guaranteed), so
running this against a real machine does not risk paying out
incorrectly — it just means this tool's ticket_in_events table records
that a ticket came in, not what happened to it. This is a different
direction from cashout validation above, not a contradiction of it —
see §5.4 vs. §5.8 in the docstring paragraph above.

What this does NOT recover: ticket-IN history before this tool started,
or before the SAS 6.02 spec's own record — because there isn't any. LP
70/71 only ever expose the *current* redemption cycle; SAS has no
ticket-in buffer the way it has one for ticket-out (LP 4D). If you need
a full ticket-in audit trail, this tool has to be running continuously
from before the first ticket you care about.

What this does NOT cover, even with the full meter sweep: per-game
meters/configuration (LP 0x52/0x53). Those are indexed by game number,
which varies per machine, so they don't fit a fixed set of columns the
way every meter above does — a real gap, not scoped out on purpose, left
for a future child table keyed on game_number.

Nothing here ever deletes a ticket-in or ticket-out row to free space,
even under sustained disk pressure — that decision belongs to whatever
eventually syncs this data to a server (see synced_at, below), matching
the project's own Technical v3 §5.9: "Prune on confirmed ACK, never on
a timer." Instead: a write that actually fails (almost always a full
partition) is reported loudly rather than silently dropped — see
_safe_commit() — and --db-size-warning-mb gives you an early signal
before that happens, not just a fault report after.

Two history modes for meters, chosen with --mode (see MANUAL.md §4 and
§6 for the full reasoning):

- ``ring`` (default): a single current-value row plus a capped,
  decoupled-cadence history ring buffer. This is the flash-safe,
  production-shaped default — a growing per-poll log is a write-wear
  problem on flash-based hardware well before it's a space problem.
- ``append``: the original unbounded, every-poll history log. Right
  for lab and stress-testing runs on normal disks, where you want every
  sample and don't care about write volume.

Whichever mode is active, a meter *decrease*, a failed poll, or an
explicit anomaly signal (see poll_and_log()'s ``anomaly`` parameter, for
callers embedding this as a library function rather than running it as
a script) makes the next few polls log at full resolution regardless of
the normal cadence — that's where the diagnostic value actually is, and
it's cheap because it's rare. A handful of fields are gauges, not
cumulative counters (current hopper level/status, current credits,
selected game number) — a decrease there is normal, not anomalous, so
they're excluded from this check; see GAUGE_METER_FIELDS.

Usage:
    python3 examples/sql_poll_logger.py gateway.ini
    python3 examples/sql_poll_logger.py gateway.ini --db gateway.sqlite3 --interval 5
    python3 examples/sql_poll_logger.py gateway.ini --cycles 100   # stop after 100 cycles
    python3 examples/sql_poll_logger.py gateway.ini --mode append  # lab/stress-testing: log every meter poll, uncapped
    python3 examples/sql_poll_logger.py gateway.ini --skip-full-meter-sweep  # skip the ~39 single-meter polls, lighter per cycle

``gateway.ini`` is the file commission_gateway.py writes (see MANUAL.md),
or one you hand-wrote in the same format.
"""

from __future__ import annotations

import argparse
import datetime
import os
import random
import sqlite3
import sys
import time
from dataclasses import dataclass

from saspy.config import connect_from_config
from saspy.constants import SIMPLE_METER_WIDTH_BCD, ExceptionCode, LongPoll, MeterCode
from saspy.exceptions import SASError
from saspy.models import EnhancedValidationInfo

DEFAULT_VALIDATION_SYSTEM_ID = 1  # 0 means "deny" per Table 15.8a — never use it for a real pool entry

# --- Meter columns: every long poll this tool reads into meters_current/
#     meters_history, grouped by the poll that produces it. Defined before
#     SCHEMA so the schema's own column list is generated from these tuples
#     rather than hand-duplicated — at ~80 columns, keeping one source of
#     truth matters more than reading a literal CREATE TABLE end to end.

METER_FIELDS = (  # LP 0x0F (Table 7.1a/7.1b) — the six core meters
    "total_cancelled_credits",
    "total_coin_in",
    "total_coin_out",
    "total_drop",
    "total_jackpot",
    "games_played",
)

# Column name and MeterCode are paired by position — zip(TICKET_METER_COLUMNS,
# TICKET_METER_CODES) is the single source of truth for that mapping, used
# both to build the LP 2F request and to place its response into the row.
TICKET_METER_COLUMNS = (  # LP 0x2F — the only way to reach these at all
    "ticket_in_cashable_cents",
    "ticket_in_cashable_count",
    "ticket_in_restricted_cents",
    "ticket_in_restricted_count",
    "ticket_out_cashable_cents",
    "ticket_out_cashable_count",
    "ticket_out_restricted_cents",
    "ticket_out_restricted_count",
)
TICKET_METER_CODES = (
    MeterCode.CASHABLE_TICKET_IN_CENTS,
    MeterCode.CASHABLE_TICKET_IN_QUANTITY,
    MeterCode.RESTRICTED_TICKET_IN_CENTS,
    MeterCode.RESTRICTED_TICKET_IN_QUANTITY,
    MeterCode.CASHABLE_TICKET_OUT_CENTS,
    MeterCode.CASHABLE_TICKET_OUT_QUANTITY,
    MeterCode.RESTRICTED_TICKET_OUT_CENTS,
    MeterCode.RESTRICTED_TICKET_OUT_QUANTITY,
)  # all 8 fit in one LP 2F poll (max 10 codes per request, §7.3)

LP19_METER_COLUMNS = (  # LP 0x19 (Table 7.2b) — independent re-read of 5 of the 6 core meters
    "lp19_total_coin_in",
    "lp19_total_coin_out",
    "lp19_total_drop",
    "lp19_total_jackpot",
    "lp19_games_played",
)

LP1C_METER_COLUMNS = (  # LP 0x1C (Table 7.2c) — independent re-read of 5, plus 3 new fields
    "lp1c_total_coin_in",
    "lp1c_total_coin_out",
    "lp1c_total_drop",
    "lp1c_total_jackpot",
    "lp1c_games_played",
    "lp1c_games_won",
    "lp1c_slot_door_opened",
    "lp1c_power_reset",
)

LP18_METER_COLUMNS = (  # LP 0x18 (Table 7.7)
    "lp18_games_since_power_up",
    "lp18_games_since_door_closure",
)

LP1E_METER_COLUMNS = (  # LP 0x1E — 6 "bills in" count meters by denomination
    "lp1e_bills_1",
    "lp1e_bills_5",
    "lp1e_bills_10",
    "lp1e_bills_20",
    "lp1e_bills_50",
    "lp1e_bills_100",
)

LP2D_METER_COLUMNS = ("lp2d_total_hand_paid_cancelled_credits",)  # LP 0x2D, game_number=0 (all games)

LP4F_METER_COLUMNS = (  # LP 0x4F (Table 7.19a/7.19b) — gauges, not cumulative counters
    "lp4f_hopper_status",
    "lp4f_hopper_percent_full",
    "lp4f_hopper_level",
)

# Every other single-meter long poll this client implements (SASClient.send_meter()),
# named "sm_" + a descriptive name so none can collide with a column above even
# where the underlying counter is the same one LP 0x0F/0x19/0x1C already report
# (e.g. sm_total_coin_in). Built from SIMPLE_METER_WIDTH_BCD so this list can
# never drift from what SASClient actually supports.
SINGLE_METER_COLUMNS: dict[LongPoll, str] = {
    LongPoll.SEND_TOTAL_CANCELLED_CREDITS_METER: "sm_total_cancelled_credits",
    LongPoll.SEND_TOTAL_COIN_IN_METER: "sm_total_coin_in",
    LongPoll.SEND_TOTAL_COIN_OUT_METER: "sm_total_coin_out",
    LongPoll.SEND_TOTAL_DROP_METER: "sm_total_drop",
    LongPoll.SEND_TOTAL_JACKPOT_METER: "sm_total_jackpot",
    LongPoll.SEND_GAMES_PLAYED_METER: "sm_games_played",
    LongPoll.SEND_GAMES_WON_METER: "sm_games_won",
    LongPoll.SEND_GAMES_LOST_METER: "sm_games_lost",
    LongPoll.SEND_CURRENT_CREDITS: "sm_current_credits",  # gauge: the player's current balance, not cumulative
    LongPoll.SEND_TOTAL_DOLLAR_VALUE_OF_BILLS: "sm_total_dollar_value_of_bills",
    LongPoll.SEND_TRUE_COIN_IN: "sm_true_coin_in",
    LongPoll.SEND_TRUE_COIN_OUT: "sm_true_coin_out",
    LongPoll.SEND_CURRENT_HOPPER_LEVEL: "sm_current_hopper_level",  # gauge
    LongPoll.SEND_BILLS_IN_METER_1: "sm_bills_in_1",
    LongPoll.SEND_BILLS_IN_METER_2: "sm_bills_in_2",
    LongPoll.SEND_BILLS_IN_METER_5: "sm_bills_in_5",
    LongPoll.SEND_BILLS_IN_METER_10: "sm_bills_in_10",
    LongPoll.SEND_BILLS_IN_METER_20: "sm_bills_in_20",
    LongPoll.SEND_BILLS_IN_METER_50: "sm_bills_in_50",
    LongPoll.SEND_BILLS_IN_METER_100: "sm_bills_in_100",
    LongPoll.SEND_BILLS_IN_METER_500: "sm_bills_in_500",
    LongPoll.SEND_BILLS_IN_METER_1000: "sm_bills_in_1000",
    LongPoll.SEND_BILLS_IN_METER_200: "sm_bills_in_200",
    LongPoll.SEND_BILLS_IN_METER_25: "sm_bills_in_25",
    LongPoll.SEND_BILLS_IN_METER_2000: "sm_bills_in_2000",
    LongPoll.SEND_BILLS_IN_METER_2500: "sm_bills_in_2500",
    LongPoll.SEND_BILLS_IN_METER_5000: "sm_bills_in_5000",
    LongPoll.SEND_BILLS_IN_METER_10000: "sm_bills_in_10000",
    LongPoll.SEND_BILLS_IN_METER_20000: "sm_bills_in_20000",
    LongPoll.SEND_BILLS_IN_METER_25000: "sm_bills_in_25000",
    LongPoll.SEND_BILLS_IN_METER_50000: "sm_bills_in_50000",
    LongPoll.SEND_BILLS_IN_METER_100000: "sm_bills_in_100000",
    LongPoll.SEND_BILLS_IN_METER_250: "sm_bills_in_250",
    LongPoll.SEND_CREDIT_AMOUNT_OF_ALL_BILLS_ACCEPTED: "sm_credit_amount_of_all_bills_accepted",
    LongPoll.SEND_COIN_AMOUNT_FROM_EXTERNAL_ACCEPTOR: "sm_coin_amount_from_external_acceptor",
    LongPoll.SEND_BILLS_IN_STACKER_COUNT: "sm_bills_in_stacker_count",
    LongPoll.SEND_BILLS_IN_STACKER_CREDIT_AMOUNT: "sm_bills_in_stacker_credit_amount",
    LongPoll.SEND_TOTAL_GAMES_IMPLEMENTED: "sm_total_games_implemented",  # config, not really a meter, kept for completeness
    LongPoll.SEND_SELECTED_GAME_NUMBER: "sm_selected_game_number",  # gauge: current game, not cumulative
}
assert set(SINGLE_METER_COLUMNS) == set(SIMPLE_METER_WIDTH_BCD), (
    "SINGLE_METER_COLUMNS must cover exactly what SASClient.send_meter() supports"
)

# Gauges: fields that go up AND down in normal operation, so a decrease here
# is not the diagnostic signal it is for a cumulative counter. Excluded from
# poll_and_log()'s burst-arming "decreased" check, not from the row itself.
GAUGE_METER_FIELDS = frozenset(
    {
        "lp4f_hopper_status",
        "lp4f_hopper_percent_full",
        "lp4f_hopper_level",  # also the one field here that can be None (Table 7.19a: no hopper-level sensor)
        "sm_current_credits",
        "sm_current_hopper_level",
        "sm_selected_game_number",
    }
)

ALL_METER_FIELDS = (
    METER_FIELDS
    + TICKET_METER_COLUMNS
    + LP19_METER_COLUMNS
    + LP1C_METER_COLUMNS
    + LP18_METER_COLUMNS
    + LP1E_METER_COLUMNS
    + LP2D_METER_COLUMNS
    + LP4F_METER_COLUMNS
    + tuple(SINGLE_METER_COLUMNS.values())
)
DECREASE_CHECK_FIELDS = tuple(f for f in ALL_METER_FIELDS if f not in GAUGE_METER_FIELDS)

_METER_COLUMN_DDL = ",\n    ".join(f"{field} INTEGER" for field in ALL_METER_FIELDS)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meters_current (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    polled_at TEXT NOT NULL,
    {_METER_COLUMN_DDL}
);

CREATE TABLE IF NOT EXISTS meters_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    polled_at TEXT NOT NULL,
    {_METER_COLUMN_DDL}
);

CREATE TABLE IF NOT EXISTS poll_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    poll_name TEXT NOT NULL,
    error_type TEXT NOT NULL,
    message TEXT
);

CREATE TABLE IF NOT EXISTS ticket_out_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    buffer_index INTEGER,
    validation_type INTEGER,
    ticket_date TEXT,
    ticket_time TEXT,
    validation_number INTEGER NOT NULL,
    amount_cents INTEGER,
    ticket_number INTEGER,
    validation_system_id INTEGER,
    expiration TEXT,
    pool_id INTEGER,
    synced_at TEXT,
    UNIQUE(validation_number, ticket_date, ticket_time)
);

CREATE TABLE IF NOT EXISTS ticket_in_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    amount_cents INTEGER,
    parsing_code INTEGER,
    validation_data_hex TEXT,
    synced_at TEXT
);

CREATE TABLE IF NOT EXISTS validation_pool (
    validation_number INTEGER PRIMARY KEY,
    validation_system_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'available',
    assigned_at TEXT,
    assigned_amount_cents INTEGER
);
"""
# meters_current always holds exactly one row (id=1, INSERT OR REPLACE) —
# most consumers only ever want the latest value, and a one-row table
# makes that a trivial read instead of an ORDER BY over a growing log.
#
# meters_history is the diagnostic time series. In ring mode it's capped
# at HistoryConfig.cap rows (oldest evicted) and written at most once per
# HistoryConfig.interval seconds, decoupled from --interval (the poll
# rate) — except during a burst (see burst_count), when it's written
# every cycle at full resolution. In append mode it's unbounded and
# written every cycle, unconditionally: the original behavior, still the
# right one for a lab/stress run on ordinary disk.
#
# Both tables carry ~80 meter columns, generated from ALL_METER_FIELDS
# (see above) rather than hand-typed here — deliberately redundant by
# design, not an accident: LP 0x0F/0x19/0x1C, and most of the "sm_"
# single-meter sweep, independently report several of the same
# underlying counters through entirely different request/response
# exchanges. Three long polls disagreeing about "total coin in" this
# cycle is a real finding; a schema that only kept one of them couldn't
# surface it. ticket_in_*/ticket_out_* (LP 2F) are a different thing
# again from ticket_in_events/ticket_out_history below: those record
# individual transactions (one row per ticket); these columns are
# running totals the machine itself maintains, useful for reconciling
# "does the machine's own count agree with what we captured per-ticket"
# without summing either table. Being ordinary BCD meters, all of these
# roll over exactly like the core six (§8.2) — decode_bcd() has no
# notion of "value decreased," so a wrapped meter is just a normal,
# smaller read, treated as diagnostic burst signal, not an error (except
# the handful of true gauges in GAUGE_METER_FIELDS, where a decrease is
# just normal operation, not a rollover).
#
# ticket_out_history is deduplicated on (validation_number, ticket_date,
# ticket_time) — the startup backfill (non-destructive, by buffer index)
# and the live exception-driven drain (destructive, by "next unread") can
# both observe the same physical ticket, and a ring buffer position gets
# reused over time, so buffer_index alone is not a stable identity.
#
# Neither meters table carries a synced_at column — they're a local
# diagnostic buffer, not a queue, and there's nothing for a sync column
# to mean on a table that's never drained (see §6.6's synced_at
# discussion). ticket_out_history and ticket_in_events are different:
# per Technical v3 §5.9, ticket events ARE the event stream ("sas_events
# — append-only, drained on invitation, pruned on confirmed ACK"), so
# they carry synced_at even though this reference tool has no real
# drain process to ever set it. The column exists so a future one can,
# and so the correct future cleanup query is obvious and safe:
# `DELETE ... WHERE synced_at IS NOT NULL` — never a row-count cap,
# and never anything keyed on age or local disk pressure alone. This
# tool does not implement that deletion itself; see the module
# docstring's note on why sustained disk pressure is reported loudly
# instead of resolved by deleting unsynced rows.
#
# validation_pool is different in kind from every other table here: it's
# not something this tool observes, it's something this tool spends
# from. A row starts 'available' (seeded by --seed-validation-pool, or
# hand-inserted with real numbers) and moves to 'assigned' the moment
# the machine acknowledges it (status 0x00 on LP 58) — never reused,
# and never removed, so validation_pool doubles as its own audit trail
# of what this gateway has ever handed out.

DEFAULT_HISTORY_CAP = 200
DEFAULT_HISTORY_INTERVAL = 60.0
DEFAULT_BURST_COUNT = 10


@dataclass
class HistoryConfig:
    mode: str = "ring"  # "ring" or "append"
    cap: int = DEFAULT_HISTORY_CAP  # ring mode only; ignored in append mode
    interval: float = DEFAULT_HISTORY_INTERVAL  # ring mode only; ignored in append mode
    burst_count: int = DEFAULT_BURST_COUNT

    def __post_init__(self) -> None:
        if self.mode not in ("ring", "append"):
            raise ValueError(f"mode must be 'ring' or 'append', got {self.mode!r}")


@dataclass
class PollState:
    """Mutable state carried between poll_and_log() calls across one run."""

    last_meters: dict | None = None
    last_history_write: float | None = None  # None: never written yet, forces a write on the first cycle
    burst_remaining: int = 0


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _safe_commit(conn: sqlite3.Connection, now: str) -> bool:
    """Commit, or print a loud, impossible-to-miss fault to stderr and
    return False rather than raising or failing silently. A commit
    failure here is almost always SQLITE_FULL — the partition holding
    the database file is out of space, most likely during an extended
    network outage with nothing draining this data. Per this project's
    own Technical v3 §5.9: "If the event-write path does not handle
    this explicitly, the gateway silently stops buffering and the
    original no-buffer defect is back. This must fail loudly." This
    never deletes anything to make room — see the module docstring.
    """
    try:
        conn.commit()
        return True
    except sqlite3.OperationalError as e:
        print(
            f"[{now}] FAULT: database write failed ({e}) — most likely the partition "
            "holding the database file is full. This row was not saved. This tool will "
            "never delete existing rows to make room; free space on the partition or "
            "move the database file.",
            file=sys.stderr,
        )
        return False


def _db_file_size_bytes(conn: sqlite3.Connection) -> int | None:
    """Best-effort size, in bytes, of the main database file backing
    ``conn`` — or None if it can't be determined (an in-memory database,
    used throughout this project's own test suite, has no file to size).
    """
    try:
        for _seq, name, file in conn.execute("PRAGMA database_list"):
            if name == "main" and file:
                return os.path.getsize(file)
    except (sqlite3.Error, OSError):
        pass
    return None


def _is_empty_ticket_out_record(record: EnhancedValidationInfo) -> bool:
    """Per §15.10: "If no unread records are in the buffer, all fields in
    the long poll 4D response will be zero" — same for an unused/invalid
    buffer index. Checked on three fields together rather than just
    validation_number, since a genuinely-zero value in any single field
    is far more plausible than all three being zero at once.
    """
    return record.validation_number == 0 and record.amount_cents == 0 and record.ticket_number == 0


def _insert_ticket_out_record(conn: sqlite3.Connection, record: EnhancedValidationInfo, captured_at: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO ticket_out_history "
        "(captured_at, buffer_index, validation_type, ticket_date, ticket_time, validation_number, "
        " amount_cents, ticket_number, validation_system_id, expiration, pool_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            captured_at,
            record.index_number,
            record.validation_type,
            record.date,
            record.time,
            record.validation_number,
            record.amount_cents,
            record.ticket_number,
            record.validation_system_id,
            record.expiration,
            record.pool_id,
        ),
    )
    _safe_commit(conn, captured_at)


def backfill_ticket_out_history(client, conn: sqlite3.Connection, *, now_fn=utc_now) -> int:
    """Read every buffer position (1-31, the spec's maximum — §15.6) once,
    non-destructively (indexed reads don't mark anything as read), so
    ticket_out_history starts with whatever the machine is already
    holding rather than only what happens from here forward. Safe to call
    more than once — inserts are deduplicated. Returns the number of new
    rows found.
    """
    now = now_fn()
    found = 0
    for index in range(1, 32):
        try:
            record = client.send_enhanced_validation_information(function_code=index)
        except SASError as e:
            conn.execute(
                "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
                (now, "send_enhanced_validation_information(backfill)", type(e).__name__, str(e)),
            )
            _safe_commit(conn, now)
            continue
        if _is_empty_ticket_out_record(record):
            continue
        before = conn.total_changes
        _insert_ticket_out_record(conn, record, now)
        if conn.total_changes > before:
            found += 1
    return found


def _drain_ticket_out_history(client, conn: sqlite3.Connection, now: str) -> None:
    """Called on exception 0x3D/0x3E: read every currently-unread
    ticket-out record (function code 0x00 — "next unread, mark as read")
    until the buffer reports empty. Unlike the startup backfill, this
    drains the FIFO "unread" pointer rather than reading by absolute
    index, which is what lets it see every new record even if several
    tickets printed between poll cycles.
    """
    for _ in range(31):  # hard cap — the buffer can never hold more than this
        try:
            record = client.send_enhanced_validation_information(function_code=0x00)
        except SASError as e:
            conn.execute(
                "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
                (now, "send_enhanced_validation_information(drain)", type(e).__name__, str(e)),
            )
            _safe_commit(conn, now)
            return
        if _is_empty_ticket_out_record(record):
            return
        _insert_ticket_out_record(conn, record, now)
        print(f"[{now}] ticket-out captured: validation_number={record.validation_number} amount_cents={record.amount_cents}")


def seed_validation_pool(
    conn: sqlite3.Connection,
    target_available: int,
    *,
    validation_system_id: int = DEFAULT_VALIDATION_SYSTEM_ID,
    random_fn=random.getrandbits,
) -> int:
    """Top up validation_pool to at least ``target_available`` 'available'
    rows, generating random 16-digit numbers (not real server-issued
    ones — see the module docstring). Safe to call every run: it only
    adds what's missing, and a random collision with an existing number
    just retries. Returns how many rows were actually added.
    """
    if target_available <= 0:
        return 0
    existing = conn.execute("SELECT COUNT(*) FROM validation_pool WHERE status = 'available'").fetchone()[0]
    added = 0
    while existing + added < target_available:
        candidate = random_fn(53) % 10**16
        try:
            conn.execute(
                "INSERT INTO validation_pool (validation_number, validation_system_id, status) VALUES (?, ?, 'available')",
                (candidate, validation_system_id),
            )
        except sqlite3.IntegrityError:
            continue  # collided with an existing validation_number (primary key) — try another
        added += 1
    _safe_commit(conn, utc_now())
    return added


def _handle_cashout_request(client, conn: sqlite3.Connection, now: str) -> None:
    """Called on exception 0x57 (system validation request): read the
    pending cashout amount (LP 57), take the next available number from
    validation_pool, and answer with it (LP 58) — the gateway-local
    cashout-validation flow described in the module docstring (§5.4).
    """
    try:
        info = client.send_pending_cashout_info()
    except SASError as e:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
            (now, "send_pending_cashout_info", type(e).__name__, str(e)),
        )
        _safe_commit(conn, now)
        return
    if info.cashout_type == 0x80:
        return  # "not waiting for system validation" (Table 15.7b) — the exception fired, but the race is over

    row = conn.execute(
        "SELECT validation_number, validation_system_id FROM validation_pool "
        "WHERE status = 'available' ORDER BY validation_number LIMIT 1"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
            (now, "validation_pool", "PoolExhausted", f"no available validation number for amount_cents={info.amount_cents}"),
        )
        _safe_commit(conn, now)
        print(f"[{now}] cashout pending (amount_cents={info.amount_cents}) but validation_pool is empty — left unanswered")
        return

    validation_number, validation_system_id = row
    try:
        status = client.send_validation_number(
            validation_system_id=validation_system_id, validation_number=validation_number
        )
    except SASError as e:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
            (now, "send_validation_number", type(e).__name__, str(e)),
        )
        _safe_commit(conn, now)
        return

    if status == 0x00:
        conn.execute(
            "UPDATE validation_pool SET status = 'assigned', assigned_at = ?, assigned_amount_cents = ? "
            "WHERE validation_number = ?",
            (now, info.amount_cents, validation_number),
        )
        _safe_commit(conn, now)
        print(f"[{now}] cashout answered: validation_number={validation_number} amount_cents={info.amount_cents}")
    else:
        # 0x80 not in cashout, 0x81 improper validation rejected (Table 15.8b) — the number was never
        # actually consumed, so leave it 'available' for the next attempt rather than burning it.
        print(f"[{now}] validation number rejected by machine (status=0x{status:02X}); left available for reuse")


def _capture_ticket_in(client, conn: sqlite3.Connection, now: str) -> None:
    """Called on exception 0x67: read the ticket's validation data
    (read-only — see this module's docstring for why redeem_ticket() is
    deliberately never called here).
    """
    try:
        ticket = client.send_ticket_validation_data()
    except SASError as e:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
            (now, "send_ticket_validation_data", type(e).__name__, str(e)),
        )
        _safe_commit(conn, now)
        return
    if not ticket.ticket_in_escrow:
        return  # exception fired, but nothing in escrow by the time we read it — rare, harmless race
    conn.execute(
        "INSERT INTO ticket_in_events (captured_at, amount_cents, parsing_code, validation_data_hex) "
        "VALUES (?, ?, ?, ?)",
        (now, ticket.amount_cents, ticket.parsing_code, ticket.validation_data.hex()),
    )
    _safe_commit(conn, now)
    print(f"[{now}] ticket-in captured: amount_cents={ticket.amount_cents} (not redeemed — see module docstring)")


class _MeterPollFailure(Exception):
    """Internal signal: one meter poll failed. Carries which poll and the
    original SASError so the caller can log a precise poll_errors row.
    Any failure anywhere in _poll_all_meters() aborts the whole cycle's
    meter write — see poll_and_log() and the module docstring for why:
    a row that's fresh in some meter columns and stale or missing in
    others would misrepresent what "as of polled_at" means.
    """

    def __init__(self, poll_name: str, original: SASError):
        super().__init__(f"{poll_name}: {original}")
        self.poll_name = poll_name
        self.original = original


def _poll_all_meters(client, *, full_sweep: bool = True) -> dict:
    """Poll every meter this tool knows how to read, in one pass, and
    return {column: value} covering every name in ALL_METER_FIELDS.
    Raises _MeterPollFailure on the first failure, naming exactly which
    poll failed. When ``full_sweep`` is False, the ~39 SINGLE_METER_COLUMNS
    are skipped and left as None (NULL) in the returned row rather than
    polled — a lighter-weight cycle for hardware where that much extra
    wire traffic per cycle isn't affordable; see --skip-full-meter-sweep.
    """
    values: dict = {}

    def poll(name, fn, *args):
        try:
            return fn(*args)
        except SASError as e:
            raise _MeterPollFailure(name, e) from e

    meters = poll("send_meters_10_through_15", client.send_meters_10_through_15)
    values.update({f: getattr(meters, f) for f in METER_FIELDS})

    ticket_meters = poll("send_selected_meters(ticket_meters)", client.send_selected_meters, list(TICKET_METER_CODES))
    values.update((c, ticket_meters.meters[code]) for c, code in zip(TICKET_METER_COLUMNS, TICKET_METER_CODES))

    m19 = poll("send_meters_11_through_15", client.send_meters_11_through_15)
    values.update(zip(LP19_METER_COLUMNS, (m19.total_coin_in, m19.total_coin_out, m19.total_drop, m19.total_jackpot, m19.games_played)))

    m1c = poll("send_extended_meters_group", client.send_extended_meters_group)
    values.update(
        zip(
            LP1C_METER_COLUMNS,
            (
                m1c.total_coin_in,
                m1c.total_coin_out,
                m1c.total_drop,
                m1c.total_jackpot,
                m1c.games_played,
                m1c.games_won,
                m1c.slot_door_opened,
                m1c.power_reset,
            ),
        )
    )

    m18 = poll("send_games_since_power_up_and_door_closure", client.send_games_since_power_up_and_door_closure)
    values.update(zip(LP18_METER_COLUMNS, (m18.games_since_power_up, m18.games_since_door_closure)))

    bills = poll("send_total_bill_meters", client.send_total_bill_meters)
    values.update(zip(LP1E_METER_COLUMNS, (bills.bills_1, bills.bills_5, bills.bills_10, bills.bills_20, bills.bills_50, bills.bills_100)))

    values[LP2D_METER_COLUMNS[0]] = poll(
        "send_total_hand_paid_cancelled_credits", client.send_total_hand_paid_cancelled_credits
    )

    hopper = poll("send_current_hopper_status", client.send_current_hopper_status)
    values.update(zip(LP4F_METER_COLUMNS, (hopper.status, hopper.percent_full, hopper.level)))

    if full_sweep:
        for lp, column in SINGLE_METER_COLUMNS.items():
            values[column] = poll(f"send_meter({lp.name})", client.send_meter, lp)
    else:
        for column in SINGLE_METER_COLUMNS.values():
            values[column] = None

    return values


def poll_and_log(
    client,
    conn: sqlite3.Connection,
    state: PollState,
    history: HistoryConfig,
    *,
    anomaly: bool = False,
    now_fn=utc_now,
    monotonic_fn=time.monotonic,
    db_size_warning_mb: int = 0,
    db_size_fn=_db_file_size_bytes,
    full_meter_sweep: bool = True,
) -> None:
    """Run one full cycle: a general poll (dispatching to ticket-in/
    ticket-out capture on the relevant exception codes), then the full
    meter poll (see _poll_all_meters()) — refreshing meters_current every
    cycle, and writing to meters_history according to ``history``'s
    mode/cadence/cap, or immediately (for the next ``history.burst_count``
    cycles) on a meter decrease, a failed poll, or an ``anomaly=True``
    signal from the caller. A failure in ANY meter poll aborts that
    cycle's entire meter write — see _poll_all_meters()'s docstring.
    ``db_size_warning_mb`` (0 = disabled) prints a warning every cycle
    the database file is at or above that size — an early signal ahead
    of an actual full-partition write failure, not a substitute for
    _safe_commit()'s own fault reporting when one happens anyway.
    """
    now = now_fn()

    if db_size_warning_mb:
        size = db_size_fn(conn)
        if size is not None and size >= db_size_warning_mb * 1024 * 1024:
            print(
                f"[{now}] WARNING: database file is {size / (1024 * 1024):.1f} MB, at or above "
                f"--db-size-warning-mb {db_size_warning_mb} — check available space on its partition. "
                "This tool never deletes ticket/event rows to free space; see the module docstring."
            )

    try:
        exception_code = client.general_poll()
    except SASError as e:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
            (now, "general_poll", type(e).__name__, str(e)),
        )
        _safe_commit(conn, now)
        print(f"[{now}] general poll failed: {type(e).__name__}: {e}")
    else:
        if exception_code == ExceptionCode.TICKET_INSERTED:
            _capture_ticket_in(client, conn, now)
        elif exception_code in (ExceptionCode.CASH_OUT_TICKET_PRINTED, ExceptionCode.HANDPAY_VALIDATED):
            _drain_ticket_out_history(client, conn, now)
        elif exception_code == ExceptionCode.SYSTEM_VALIDATION_REQUEST:
            _handle_cashout_request(client, conn, now)

    mono_now = monotonic_fn()

    try:
        values = _poll_all_meters(client, full_sweep=full_meter_sweep)
    except _MeterPollFailure as failure:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
            (now, failure.poll_name, type(failure.original).__name__, str(failure.original)),
        )
        _safe_commit(conn, now)
        state.burst_remaining = max(state.burst_remaining, history.burst_count)
        print(f"[{now}] meters poll failed ({failure.poll_name}): {type(failure.original).__name__}: {failure.original}")
        return

    columns = ", ".join(ALL_METER_FIELDS)
    qmarks = ", ".join("?" for _ in ALL_METER_FIELDS)
    bind = tuple(values[f] for f in ALL_METER_FIELDS)

    conn.execute(
        f"INSERT OR REPLACE INTO meters_current (id, polled_at, {columns}) VALUES (1, ?, {qmarks})",
        (now, *bind),
    )

    decreased = state.last_meters is not None and any(
        values[f] is not None and state.last_meters.get(f) is not None and values[f] < state.last_meters[f]
        for f in DECREASE_CHECK_FIELDS
    )
    if decreased or anomaly:
        state.burst_remaining = history.burst_count

    if history.mode == "append":
        write_history = True
    elif state.burst_remaining > 0:
        write_history = True
        state.burst_remaining -= 1
    else:
        write_history = (
            state.last_history_write is None
            or (mono_now - state.last_history_write) >= history.interval
        )

    if write_history:
        conn.execute(
            f"INSERT INTO meters_history (polled_at, {columns}) VALUES (?, {qmarks})",
            (now, *bind),
        )
        state.last_history_write = mono_now
        if history.mode == "ring" and history.cap:
            conn.execute(
                "DELETE FROM meters_history WHERE id NOT IN "
                "(SELECT id FROM meters_history ORDER BY id DESC LIMIT ?)",
                (history.cap,),
            )

    _safe_commit(conn, now)
    state.last_meters = values

    note = "history written" if write_history else "history skipped (cadence)"
    if decreased:
        note += ", meter decrease detected"
    elif anomaly:
        note += ", anomaly signaled"
    print(
        f"[{now}] coin_in={values['total_coin_in']} coin_out={values['total_coin_out']} "
        f"games_played={values['games_played']} ticket_in_cashable_cents={values['ticket_in_cashable_cents']} "
        f"ticket_out_cashable_cents={values['ticket_out_cashable_cents']} — {note}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", help="gateway .ini file (see saspy/config.py or MANUAL.md)")
    parser.add_argument("--db", default="gateway.sqlite3", help="SQLite file to write to")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between poll cycles")
    parser.add_argument("--cycles", type=int, default=0, help="stop after N cycles (default: run until Ctrl-C)")
    parser.add_argument(
        "--mode",
        choices=("ring", "append"),
        default="ring",
        help="'ring' (default): capped meters_history at a decoupled write cadence — the flash-safe default. "
        "'append': unbounded meters_history written every cycle — the original lab/stress-testing behavior. "
        "Ticket tables are always deduplicated, not capped — see the module docstring.",
    )
    parser.add_argument(
        "--history-cap",
        type=int,
        default=DEFAULT_HISTORY_CAP,
        help="max rows kept in meters_history in ring mode (default: %(default)s); ignored in append mode",
    )
    parser.add_argument(
        "--history-interval",
        type=float,
        default=DEFAULT_HISTORY_INTERVAL,
        help="minimum seconds between meters_history writes in ring mode, outside a burst "
        "(default: %(default)s); ignored in append mode",
    )
    parser.add_argument(
        "--burst-count",
        type=int,
        default=DEFAULT_BURST_COUNT,
        help="consecutive polls logged at full resolution after a meter decrease or failed poll "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--skip-ticket-out-backfill",
        action="store_true",
        help="skip the one-time startup read of the full ticket-out buffer (indices 1-31)",
    )
    parser.add_argument(
        "--seed-validation-pool",
        type=int,
        default=0,
        metavar="N",
        help="top up validation_pool to at least N 'available' rows with random test numbers "
        "(default: 0, don't seed — hand-insert real numbers yourself if you have them)",
    )
    parser.add_argument(
        "--db-size-warning-mb",
        type=int,
        default=0,
        metavar="MB",
        help="print a warning every cycle the database file is at or above this size, as an early "
        "signal before a full partition actually fails a write (default: 0, disabled)",
    )
    parser.add_argument(
        "--skip-full-meter-sweep",
        action="store_true",
        help=f"skip the ~{len(SINGLE_METER_COLUMNS)} individual single-meter long polls each cycle "
        "(sm_* columns are left NULL) — lighter per-cycle wire traffic, for hardware where that "
        "much extra polling isn't affordable. The six core meters, eight ticket meters, and the "
        "other grouped meter polls (LP 0x18/0x19/0x1C/0x1E/0x2D/0x4F) still run either way.",
    )
    args = parser.parse_args()

    history = HistoryConfig(
        mode=args.mode,
        cap=args.history_cap,
        interval=args.history_interval,
        burst_count=args.burst_count,
    )
    state = PollState()

    client = connect_from_config(args.config)
    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)
    conn.commit()

    print(f"Polling {args.config} every {args.interval}s, writing to {args.db} (mode={args.mode}).")
    if args.mode == "ring":
        print(
            f"  meters_history: capped at {args.history_cap} rows, written every {args.history_interval}s, "
            f"or every cycle for {args.burst_count} poll(s) after a meter decrease or failed poll."
        )
    else:
        print("  meters_history: unbounded, written every cycle (lab/stress-testing mode).")
    print(
        f"  meters: {len(ALL_METER_FIELDS)} columns per row "
        f"({'full single-meter sweep enabled' if not args.skip_full_meter_sweep else 'single-meter sweep skipped'})."
    )

    if not args.skip_ticket_out_backfill:
        print("Reading the existing ticket-out buffer (indices 1-31, non-destructive)...")
        found = backfill_ticket_out_history(client, conn)
        print(f"  {found} ticket-out record(s) found and stored.")

    if args.seed_validation_pool:
        added = seed_validation_pool(conn, args.seed_validation_pool)
        available = conn.execute("SELECT COUNT(*) FROM validation_pool WHERE status = 'available'").fetchone()[0]
        print(f"  validation_pool: added {added} test number(s), {available} available (lab numbers, not server-issued).")

    print("Ctrl-C to stop." if args.cycles == 0 else f"Will stop after {args.cycles} cycle(s).")

    cycle = 0
    try:
        while args.cycles == 0 or cycle < args.cycles:
            cycle += 1
            poll_and_log(
                client,
                conn,
                state,
                history,
                db_size_warning_mb=args.db_size_warning_mb,
                full_meter_sweep=not args.skip_full_meter_sweep,
            )
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
