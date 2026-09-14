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
2. Meters, on the same cadence/ring-history logic as before (see the
   HistoryConfig docstring and MANUAL.md §4/§6).

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
it's cheap because it's rare.

Usage:
    python3 examples/sql_poll_logger.py gateway.ini
    python3 examples/sql_poll_logger.py gateway.ini --db gateway.sqlite3 --interval 5
    python3 examples/sql_poll_logger.py gateway.ini --cycles 100   # stop after 100 cycles
    python3 examples/sql_poll_logger.py gateway.ini --mode append  # lab/stress-testing: log every meter poll, uncapped

``gateway.ini`` is the file commission_gateway.py writes (see MANUAL.md),
or one you hand-wrote in the same format.
"""

from __future__ import annotations

import argparse
import datetime
import random
import sqlite3
import sys
import time
from dataclasses import dataclass

from saspy.config import connect_from_config
from saspy.constants import ExceptionCode
from saspy.exceptions import SASError
from saspy.models import EnhancedValidationInfo

DEFAULT_VALIDATION_SYSTEM_ID = 1  # 0 means "deny" per Table 15.8a — never use it for a real pool entry

SCHEMA = """
CREATE TABLE IF NOT EXISTS meters_current (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    polled_at TEXT NOT NULL,
    total_cancelled_credits INTEGER,
    total_coin_in INTEGER,
    total_coin_out INTEGER,
    total_drop INTEGER,
    total_jackpot INTEGER,
    games_played INTEGER
);

CREATE TABLE IF NOT EXISTS meters_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    polled_at TEXT NOT NULL,
    total_cancelled_credits INTEGER,
    total_coin_in INTEGER,
    total_coin_out INTEGER,
    total_drop INTEGER,
    total_jackpot INTEGER,
    games_played INTEGER
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
    UNIQUE(validation_number, ticket_date, ticket_time)
);

CREATE TABLE IF NOT EXISTS ticket_in_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    amount_cents INTEGER,
    parsing_code INTEGER,
    validation_data_hex TEXT
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
# ticket_out_history is deduplicated on (validation_number, ticket_date,
# ticket_time) — the startup backfill (non-destructive, by buffer index)
# and the live exception-driven drain (destructive, by "next unread") can
# both observe the same physical ticket, and a ring buffer position gets
# reused over time, so buffer_index alone is not a stable identity.
#
# Neither meters table nor the ticket tables carry a synced_at column.
# That pattern belongs to the event stream (poll_errors, and this
# project's broader event handling), which a separate drain process
# consumes and acknowledges. These are local diagnostic/capture buffers,
# not a queue — they're never drained by this tool, so there's nothing
# for a synced_at column to mean here.
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

METER_FIELDS = (
    "total_cancelled_credits",
    "total_coin_in",
    "total_coin_out",
    "total_drop",
    "total_jackpot",
    "games_played",
)


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
    conn.commit()


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
            conn.commit()
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
            conn.commit()
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
    conn.commit()
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
        conn.commit()
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
        conn.commit()
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
        conn.commit()
        return

    if status == 0x00:
        conn.execute(
            "UPDATE validation_pool SET status = 'assigned', assigned_at = ?, assigned_amount_cents = ? "
            "WHERE validation_number = ?",
            (now, info.amount_cents, validation_number),
        )
        conn.commit()
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
        conn.commit()
        return
    if not ticket.ticket_in_escrow:
        return  # exception fired, but nothing in escrow by the time we read it — rare, harmless race
    conn.execute(
        "INSERT INTO ticket_in_events (captured_at, amount_cents, parsing_code, validation_data_hex) "
        "VALUES (?, ?, ?, ?)",
        (now, ticket.amount_cents, ticket.parsing_code, ticket.validation_data.hex()),
    )
    conn.commit()
    print(f"[{now}] ticket-in captured: amount_cents={ticket.amount_cents} (not redeemed — see module docstring)")


def poll_and_log(
    client,
    conn: sqlite3.Connection,
    state: PollState,
    history: HistoryConfig,
    *,
    anomaly: bool = False,
    now_fn=utc_now,
    monotonic_fn=time.monotonic,
) -> None:
    """Run one full cycle: a general poll (dispatching to ticket-in/
    ticket-out capture on the relevant exception codes), then the meters
    poll — refreshing meters_current every cycle, and writing to
    meters_history according to ``history``'s mode/cadence/cap, or
    immediately (for the next ``history.burst_count`` cycles) on a meter
    decrease, a failed poll, or an ``anomaly=True`` signal from the
    caller.
    """
    now = now_fn()

    try:
        exception_code = client.general_poll()
    except SASError as e:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
            (now, "general_poll", type(e).__name__, str(e)),
        )
        conn.commit()
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
        meters = client.send_meters_10_through_15()
    except SASError as e:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
            (now, "send_meters_10_through_15", type(e).__name__, str(e)),
        )
        conn.commit()
        state.burst_remaining = max(state.burst_remaining, history.burst_count)
        print(f"[{now}] meters poll failed: {type(e).__name__}: {e}")
        return

    values = {field: getattr(meters, field) for field in METER_FIELDS}
    columns = ", ".join(METER_FIELDS)
    qmarks = ", ".join("?" for _ in METER_FIELDS)
    bind = tuple(values[f] for f in METER_FIELDS)

    conn.execute(
        f"INSERT OR REPLACE INTO meters_current (id, polled_at, {columns}) VALUES (1, ?, {qmarks})",
        (now, *bind),
    )

    decreased = state.last_meters is not None and any(
        values[f] < state.last_meters[f] for f in METER_FIELDS
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

    conn.commit()
    state.last_meters = values

    note = "history written" if write_history else "history skipped (cadence)"
    if decreased:
        note += ", meter decrease detected"
    elif anomaly:
        note += ", anomaly signaled"
    print(
        f"[{now}] coin_in={values['total_coin_in']} coin_out={values['total_coin_out']} "
        f"games_played={values['games_played']} — {note}"
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
            poll_and_log(client, conn, state, history)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
