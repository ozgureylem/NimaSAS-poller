#!/usr/bin/env python3
"""Keep the gateway database from growing: scheduled, and safe beside a
running poller.

    python3 examples/db_maintain.py status  --db gateway.sqlite3
    python3 examples/db_maintain.py run     --db gateway.sqlite3
    python3 examples/db_maintain.py enable-auto-vacuum --db gateway.sqlite3

WHY THIS IS NOT A CRON LINE SAYING `VACUUM`.

VACUUM rewrites the whole file. While it does, nothing else can write --
and the poller has ten seconds to answer a cashout (SAS 6.02 §15.7) before
the machine gives up and locks into a handpay. On a 32-bit gateway with a
150 MB database on flash, a VACUUM is longer than that. A nightly VACUUM
is therefore a nightly window in which a cashout becomes a handpay, and an
event queue that nobody is draining. It also needs free disk about equal
to the database (spec §6.5), so it cannot rescue a disk that is filling.

What a running gateway can afford is INCREMENTAL vacuum: SQLite hands back
a few hundred free pages per call, each call a transaction of a few
milliseconds, and the poller writes between them. That needs the database
to be in `auto_vacuum = INCREMENTAL` mode, which spec §6.1 asks for "from
the first creation of the database" and which no database made so far has.
Turning it on afterwards takes exactly one full VACUUM -- so that one is a
separate, deliberate command, run with the poller stopped, and it checks
that it is.

So:

  enable-auto-vacuum   ONCE per database, poller and gateway client
                       STOPPED. The only full VACUUM in this tool.
  run                  the scheduled one. Never blocks for long. Safe
                       beside a running poller.
  status               read-only.

AND VACUUM RECLAIMS ONLY WHAT HAS BEEN DELETED. A database nothing is ever
deleted from has no free pages, and vacuuming it achieves nothing however
often it runs. Two things delete here:

  * the meter-history ring (`--mode ring`) evicts its oldest rows by
    itself. `--mode append` never deletes anything, by design;
  * `run --prune-acked-hours 48` deletes drained rows the SERVER has
    acknowledged, 48 hours after the acknowledgement (spec §6.4). It is
    OFF unless asked for, because on a lab gateway the analysis tools read
    this same database and a pruned event is gone from it.

Pruning follows §6.4 to the letter: "Prune on confirmed ACK, never on a
timer." A row with no `synced_at` is NEVER deleted, however old -- it is
reported instead, because an undelivered row past 48 hours is a fault to
look at, not space to reclaim.

`validation_pool`, the meter tables and anything without a `synced_at`
column are never touched.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples.sql_poll_logger import open_gateway_db, utc_now  # noqa: E402

# How far past its retention a row may sit before the run says so. 13 hours,
# and the number is derived, not chosen: the timer fires every 12 hours, so a
# row still here 13 hours past its window means AT LEAST ONE SCHEDULED RUN
# DID NOT TAKE IT. The extra hour covers the timer's RandomizedDelaySec
# (10 min) and a run that stops at its 120 s ceiling.
#
# THIS IS NOW THE ONLY AGE WARNING. Spec §6.4 / §15 also asks for an
# "Unacknowledged-event alert 48 h", and this tool carried one until
# 2026-11-18, when the operator removed it: both thresholds answer the single
# question "why is this row still on my disk?", and one answer is easier to
# act on than two. MIGRATION-SPEC-AMENDMENTS A48 records the divergence and
# what it costs, which is not nothing -- an undrained row in a 30-day table
# is now reported at 733 h rather than at 48.
#
# §6.4's other half is untouched: an unacknowledged row is still NEVER
# deleted, whatever its age. Only the reporting changed.
RETENTION_OVERDUE_GRACE_HOURS = 13.0
# How much one transaction may do. Small on purpose: every one of these
# holds the write lock the poller needs, and the poller is on a clock.
PRUNE_BATCH_ROWS = 500
VACUUM_STEP_PAGES = 200
PAUSE_S = 0.05
# A scheduled run stops here even with work left. Tomorrow's run continues;
# a job that runs until it is finished is a job that can run for an hour.
DEFAULT_MAX_SECONDS = 120.0
# §6.5 item 3: "VACUUM needs free space roughly equal to the database size."
VACUUM_FREE_SPACE_FACTOR = 1.2
AUTO_VACUUM_MODES = {0: "NONE", 1: "FULL", 2: "INCREMENTAL"}
# §6.2: the meter ring is local diagnostic state and is never drained, so it
# has no synced_at and is never a candidate. Named anyway, so that a column
# added to it tomorrow cannot make it one.
NEVER_PRUNED = frozenset({
    # One row, replaced in place every cycle, in both modes. Nothing to
    # prune, and the baseline a restart uses to recognise a RAM clear.
    "meters_current",
    # A GATE, NOT A DESCRIPTION -- and as of 2026-11-16 the condition it
    # was waiting for IS MET. Read the next paragraph before acting on it.
    #
    # It used to say: "Today the server never receives a meter row (the
    # client's NEVER_DRAINED), the table has no synced_at ... Remove this
    # entry in the same change that makes it drain, not before." Both
    # facts are now false. meters_history carries synced_at, it is out of
    # NEVER_DRAINED, and the drain is HARDWARE-PROVEN: "meters_history
    # drained? True, meters_current refused? True", nine tables draining.
    # The stale wording was caught by another session reading this file
    # for the RamSys guide, not by anyone who changed the drain.
    #
    # THE ENTRY STAYS ANYWAY, and deliberately. Removing a pruning guard
    # is a decision about deleting data, and docs/private/AGENT-BOARD.md
    # reserves it for the operator rather than for whoever notices the
    # precondition has cleared. The retention spec gives this table 72
    # hours once pruning is enabled; enabling it is one line here, plus a
    # soak long enough to show the server really has what the gateway is
    # about to drop.
    "meters_history",
    "validation_pool", "pool_fills", "counters",
    "validation_queries", "host_requests",
    # Which machine every other row belongs to. A gateway carried between
    # cabinets needs every epoch it ever recorded to place a late row.
    "machine_epochs"})

# THE RETENTION SPEC, decided by the operator on 2026-11-15 (AGENT-BOARD,
# "DECIDED -- the retention spec"). Hours a row is kept AFTER the server
# acknowledged it.
#
# WRITTEN AS "72 HOURS IS THE EXCEPTION". One table is big -- machine_events
# at roughly 39 MB a day -- and the rest together are about a megabyte a
# month. So the short window is named for the tables that need it, and
# everything else, INCLUDING A TABLE ADDED TOMORROW, gets the long one. A
# retention policy should fail toward keeping things.
#
# These are a DISASTER-RECOVERY window, not a tidiness threshold: the
# gateway is the server's backup of last resort, so a server restored from
# a backup younger than the window can pull again what it lost.
#
# THE OTHER HALF OF THAT PROMISE IS NOW KEPT. This used to end "that needs
# a 'replay since T' command which does not exist yet ... the window is a
# promise the drain cannot keep." It exists: gateway/client.py's
# apply_replay(), reached through the invite payload, puts acknowledged
# rows back in the drain and is applied BEFORE that exchange collects its
# events, so recovered rows ride the same batch.
#
# It is BY ROW ID, not by time, which is why the stale wording was wrong
# in its shape and not only in its tense -- "since T" is the thing that
# cannot be built correctly here. Event ids are arrival order within one
# collection path, and a slower path files an older event under a newer
# id, so a cut by time re-sends some rows twice and misses others. The
# server names the last id it holds per table and everything above it
# drains again.
#
# So the number below is a real budget now: it is how old a server backup
# may be and still be made whole from the floor. Shortening it to save
# disk shortens that.
RETENTION_HOURS = {"machine_events": 72.0,
                   "meters_history": 72.0}      # inert while in NEVER_PRUNED
                                                # (still listed there by
                                                # choice, not by blocker --
                                                # see the note above it)
DEFAULT_RETENTION_HOURS = 30 * 24.0


def retention_hours(table: str) -> float:
    return RETENTION_HOURS.get(table, DEFAULT_RETENTION_HOURS)


# What to look for in /proc to decide whether something is still writing.
WRITER_MARKERS = ("sql_poll_logger", "gateway.client", "gateway/client.py")


def log(message: str) -> None:
    print(f"[{utc_now()}] {message}", flush=True)


def mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


# --- reading the database's own state ----------------------------------------

def file_sizes(path: str) -> dict:
    out = {}
    for suffix in ("", "-wal", "-shm"):
        try:
            out[suffix or "db"] = os.path.getsize(path + suffix)
        except OSError:
            out[suffix or "db"] = 0
    return out


def page_facts(conn: sqlite3.Connection) -> dict:
    get = lambda name: conn.execute(f"PRAGMA {name}").fetchone()[0]   # noqa: E731
    return {"page_size": get("page_size"), "page_count": get("page_count"),
            "freelist_count": get("freelist_count"),
            "auto_vacuum": get("auto_vacuum"),
            "journal_mode": str(get("journal_mode")).lower()}


def prunable_tables(conn: sqlite3.Connection) -> list[str]:
    """Tables with BOTH an `id` and a `synced_at`, minus the ones that are
    never a candidate. Read from the database, not from a list here, so a
    drained table the poller gains tomorrow is covered -- and guarded by
    NEVER_PRUNED, so one it must keep cannot become covered by accident."""
    found = []
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
        if name in NEVER_PRUNED or name.startswith("sqlite_"):
            continue
        cols = {row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')}
        if {"id", "synced_at"} <= cols:
            found.append(name)
    return found


def _when_column(conn: sqlite3.Connection, table: str) -> str | None:
    """The column that says when a row was recorded. Every drained table
    has one and no two agree on its name."""
    cols = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
    for preferred in ("occurred_at", "captured_at", "started_at", "at",
                      "polled_at", "recorded_at"):
        if preferred in cols:
            return preferred
    for col in cols:
        if col.endswith("_at") and col != "synced_at":
            return col
    return None


# --- pruning (§6.4) ----------------------------------------------------------

def _has_ever_drained(conn: sqlite3.Connection, tables) -> bool:
    """Has this gateway ever had a server link? Judged by evidence -- a row
    that was once acknowledged -- not by a flag.

    Without a link every row is undrained forever, BY DESIGN, so the
    undrained half of `retention_overdue` would fire on every run of every
    lab gateway. That is how a warning gets ignored. A gateway that has
    never drained anything is a commissioning state or a bench tool;
    --db-size-warning-mb is what speaks about its disk.
    """
    return any(conn.execute(
        f'SELECT 1 FROM "{t}" WHERE synced_at IS NOT NULL LIMIT 1').fetchone()
        for t in tables)


def retention_overdue(conn: sqlite3.Connection,
                      grace_hours: float = RETENTION_OVERDUE_GRACE_HOURS,
                      *, now: str | None = None) -> dict[str, dict[str, float]]:
    """Rows past their retention window that are still on the disk, per
    table, split by WHY they are still here.

        {"machine_events": {"unpruned": 14.2, "undrained_age": 86.0}}

    A table appears only if at least one applies. The split is not
    cosmetic: the two have different causes and different fixes, and one
    line that merged them would send somebody to the wrong one.

      unpruned        HOURS PAST ITS RETENTION. Acknowledged, window
                      expired, not deleted -- the pruner should have taken
                      it. Measured from synced_at, exactly as `prune_acked`
                      deletes, so it can never report a row the pruner
                      would rightly have left alone.

      undrained_age   THE ROW'S OWN AGE IN HOURS, and deliberately NOT an
                      overrun, because an unacknowledged row has no
                      retention to be past.

    THE RETENTION CLOCK STARTS AT THE SERVER'S ACKNOWLEDGEMENT (operator,
    2026-11-18), and that decides the shape of this function. The gateway
    is the server's backup of last resort, so it holds a row for 30 days
    from the moment the server took responsibility for it -- not from when
    the cabinet produced it. A row drained late is therefore kept longer
    than its own age, never shorter.

    So an undrained row is NOT "past retention": its retention HAS NOT
    STARTED. It is reported anyway, past the same age threshold, because it
    occupies the disk exactly like any other row and nothing will reclaim
    it until the server takes it -- but it is reported as an age and named
    as a DRAIN fault, never as a pruner fault or a retention breach.

    `datetime()` on both sides throughout, for the reason `prune_acked`
    spells out: synced_at carries a 'T' and an offset, SQLite's datetime()
    does not, and a raw string compare is lexicographic.
    """
    now = now or utc_now()
    tables = prunable_tables(conn)
    linked = _has_ever_drained(conn, tables)
    out: dict[str, dict[str, float]] = {}
    for table in tables:
        keep = retention_hours(table)
        cutoff = keep + float(grace_hours)
        found: dict[str, float] = {}

        row = conn.execute(
            f'SELECT (julianday(?) - julianday(MIN(datetime(synced_at)))) * 24.0 '
            f'FROM "{table}" WHERE synced_at IS NOT NULL AND '
            f'datetime(synced_at) <= datetime(?, ?)',
            (now, now, f"-{cutoff} hours")).fetchone()
        if row and row[0] is not None:
            found["unpruned"] = float(row[0]) - keep

        when = _when_column(conn, table)
        if linked and when is not None:
            row = conn.execute(
                f'SELECT (julianday(?) - julianday(MIN(datetime("{when}")))) '
                f'* 24.0 FROM "{table}" WHERE synced_at IS NULL AND '
                f'"{when}" IS NOT NULL AND datetime("{when}") <= datetime(?, ?)',
                (now, now, f"-{cutoff} hours")).fetchone()
            if row and row[0] is not None:
                found["undrained_age"] = float(row[0])

        if found:
            out[table] = found
    return out


def prune_acked(conn: sqlite3.Connection, hours: float | None = None, *,
                now: str | None = None,
                deadline: float | None = None, batch: int = PRUNE_BATCH_ROWS,
                pause: float = PAUSE_S, clock=time.monotonic,
                sleep=time.sleep) -> dict[str, int]:
    """Delete rows the server acknowledged longer ago than their table's
    retention (`retention_hours`), or than `hours` for every table when
    one is given.

    ACKNOWLEDGED AND OLD, both, and "old" is measured from the
    ACKNOWLEDGEMENT, not from when the row was recorded. A row drained
    late is therefore kept longer than its window, never shorter -- the
    safe direction for something that exists as the server's backup.
    A row with synced_at NULL is not matched by any statement here.

    datetime() ON BOTH SIDES. synced_at is written by isoformat() and
    carries a 'T' and '+00:00'; SQLite's datetime() returns a space and no
    offset. Comparing the raw string is a lexicographic comparison in which
    'T' beats a space, and this repository has already found one "24-hour"
    window that was 48 hours wide because of it. Here the same mistake
    would delete rows up to a day early.

    In batches, each its own transaction, with a pause between: the poller
    needs the write lock this takes.
    """
    now = now or utc_now()
    deleted: dict[str, int] = {}
    for table in prunable_tables(conn):
        total = 0
        keep = retention_hours(table) if hours is None else float(hours)
        while deadline is None or clock() < deadline:
            done = conn.execute(
                f'DELETE FROM "{table}" WHERE id IN (SELECT id FROM "{table}" '
                f"WHERE synced_at IS NOT NULL AND datetime(synced_at) <= "
                f"datetime(?, ?) ORDER BY id LIMIT ?)",
                (now, f"-{keep} hours", int(batch))).rowcount
            conn.commit()
            total += done
            if done < batch:
                break
            sleep(pause)
        if total:
            deleted[table] = total
    return deleted


# --- space -------------------------------------------------------------------

def incremental_vacuum(conn: sqlite3.Connection, *, deadline: float | None = None,
                       step_pages: int = VACUUM_STEP_PAGES, pause: float = PAUSE_S,
                       clock=time.monotonic, sleep=time.sleep) -> int:
    """Return free pages to the filesystem a step at a time. Returns how
    many pages went back. Does nothing, and says nothing, unless the
    database is in INCREMENTAL mode -- the caller reports that."""
    if conn.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
        return 0
    freed = 0
    while deadline is None or clock() < deadline:
        before = conn.execute("PRAGMA freelist_count").fetchone()[0]
        if before == 0:
            break
        conn.execute(f"PRAGMA incremental_vacuum({int(step_pages)})").fetchall()
        conn.commit()
        after = conn.execute("PRAGMA freelist_count").fetchone()[0]
        if after >= before:
            break            # nothing moved; do not spin
        freed += before - after
        sleep(pause)
    return freed


def checkpoint(conn: sqlite3.Connection) -> str:
    """Fold the write-ahead log back into the database and truncate it.

    A WAL that is never checkpointed to completion grows without bound
    while any reader keeps a snapshot open. TRUNCATE needs every reader out
    of the way for an instant; if one is not, this reports that and leaves
    it for the next run rather than waiting on it.
    """
    conn.execute("PRAGMA busy_timeout = 2000")
    try:
        busy, log_frames, done = conn.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.OperationalError as e:
        return f"skipped ({e})"
    if busy:
        return "partial (a reader was in the way; the next run finishes it)"
    return "complete, log truncated"


# --- the one full VACUUM -------------------------------------------------------

def writers_running(proc: str = "/proc") -> list[str]:
    """Processes that write this kind of database, by their command line.

    Evidence, not a promise: a flag saying "I stopped the poller" is true
    on the day it is written into a runbook and false on the day it is
    pasted. Returns [] on a system with no /proc, which cannot be checked
    this way -- the caller says so rather than assuming it is clear.
    """
    found, mine = [], str(os.getpid())
    try:
        entries = os.listdir(proc)
    except OSError:
        return []
    for pid in entries:
        if not pid.isdigit() or pid == mine:
            continue
        try:
            with open(os.path.join(proc, pid, "cmdline"), "rb") as handle:
                cmdline = handle.read().replace(b"\x00", b" ").decode(
                    "utf-8", "replace").strip()
        except OSError:
            continue
        if any(marker in cmdline for marker in WRITER_MARKERS):
            found.append(f"pid {pid}: {cmdline[:100]}")
    return found


def enable_auto_vacuum(path: str, *, proc: str = "/proc",
                       free_bytes=None) -> tuple[bool, str]:
    """Convert a database to INCREMENTAL auto-vacuum with one full VACUUM.

    Refuses -- changing nothing -- if anything is still writing, if the
    disk cannot hold a second copy, or if the database is not sound to
    begin with. A VACUUM of a corrupt database is a way to make a readable
    corrupt file into an unreadable one.
    """
    running = writers_running(proc)
    if running:
        return False, ("something is still writing to a gateway database:\n    "
                       + "\n    ".join(running)
                       + "\n  Stop the poller and the gateway client first. A "
                       "full VACUUM holds the database for its whole duration, "
                       "and a cashout that arrives meanwhile becomes a handpay.")
    sizes = file_sizes(path)
    need = int((sizes["db"] + sizes["-wal"]) * VACUUM_FREE_SPACE_FACTOR)
    free = (free_bytes if free_bytes is not None
            else shutil.disk_usage(os.path.dirname(os.path.abspath(path))).free)
    if free < need:
        return False, (f"not enough free disk: VACUUM writes a second copy. "
                       f"Need about {mb(need)}, have {mb(free)} (spec §6.5).")
    conn = open_gateway_db(path)
    try:
        if conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2:
            return True, "already INCREMENTAL; nothing to do"
        verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if verdict != "ok":
            return False, (f"integrity_check says {verdict!r}. Not vacuuming a "
                           "database that is not sound; copy it off first.")
        started = time.monotonic()
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("VACUUM")
        took = time.monotonic() - started
        mode = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        after = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    if mode != 2 or journal != "wal" or after != "ok":
        return False, (f"VACUUM ran but left auto_vacuum={mode}, "
                       f"journal_mode={journal}, integrity_check={after!r}. "
                       "Do not start the poller on this file until that is "
                       "understood.")
    return True, (f"auto_vacuum is now INCREMENTAL. VACUUM took {took:.1f}s; "
                  f"{mb(sizes['db'])} -> {mb(file_sizes(path)['db'])}. "
                  "integrity_check ok, still WAL. From here `run` can return "
                  "space while the poller is running.")


# --- commands ----------------------------------------------------------------

def cmd_status(args) -> int:
    conn = open_gateway_db(args.db)
    facts, sizes = page_facts(conn), file_sizes(args.db)
    free = facts["freelist_count"] * facts["page_size"]
    print(f"{args.db}: {mb(sizes['db'])} (+ {mb(sizes['-wal'])} write-ahead log)")
    print(f"  journal_mode {facts['journal_mode']}, auto_vacuum "
          f"{AUTO_VACUUM_MODES.get(facts['auto_vacuum'], facts['auto_vacuum'])}")
    print(f"  free pages inside the file: {facts['freelist_count']} "
          f"({mb(free)}) -- what a vacuum could return")
    if facts["auto_vacuum"] != 2:
        print("  NOT in INCREMENTAL mode: `run` cannot return space on this "
              "database. Run `enable-auto-vacuum` once, with the poller "
              "stopped.")
    print("  drained tables (rows: total / acknowledged / waiting; kept "
          "after acknowledgement):")
    for table in prunable_tables(conn):
        total, acked = conn.execute(
            f'SELECT COUNT(*), COUNT(synced_at) FROM "{table}"').fetchone()
        keep = retention_hours(table)
        shown = f"{keep:g} h" if keep < 24 * 7 else f"{keep / 24:g} days"
        print(f"    {table:28s} {total:9d} / {acked:9d} / {total - acked:9d}"
              f"   {shown}")
    try:
        meters = conn.execute("SELECT COUNT(*) FROM meters_history").fetchone()[0]
        print(f"    {'meters_history':28s} {meters:9d} rows, NOT pruned: it "
              "does not drain yet, so the server holds no copy")
    except sqlite3.OperationalError:
        pass
    return 0


def cmd_run(args) -> int:
    conn = open_gateway_db(args.db)
    started = time.monotonic()
    deadline = started + args.max_seconds
    before, facts = file_sizes(args.db), page_facts(conn)

    if args.prune_acked or args.prune_acked_hours > 0:
        uniform = args.prune_acked_hours if args.prune_acked_hours > 0 else None
        rule = (f"older than {uniform:g}h" if uniform is not None else
                "past their retention ("
                + ", ".join(f"{t} {h:g}h" for t, h in sorted(RETENTION_HOURS.items())
                            if t not in NEVER_PRUNED)
                + f", everything else {DEFAULT_RETENTION_HOURS / 24:g} days)")
        deleted = prune_acked(conn, uniform, deadline=deadline)
        if deleted:
            log(f"pruned acknowledged rows {rule}: "
                + ", ".join(f"{t} {n}" for t, n in deleted.items()))
        else:
            log(f"no acknowledged rows {rule}; nothing pruned")
        # AFTER the prune, so it reports what is LEFT. Only when pruning is
        # on: with it off nothing is deleted by design, so every old row
        # would be "overdue" and this line would fire every run -- which is
        # how a warning gets ignored. The 30-day window exists for disaster
        # recovery, so being past it is not a tidiness question: it is
        # either the timer not firing or a run that cannot finish.
        behind = retention_overdue(conn)
        for table, over in sorted(behind.items()):
            keep = retention_hours(table)
            grace = RETENTION_OVERDUE_GRACE_HOURS
            if "unpruned" in over:
                log(f"ALERT: {table} still holds ACKNOWLEDGED rows "
                    f"{over['unpruned']:.0f}h past its {keep:g}h retention. "
                    f"More than {grace:g}h over means at least one scheduled "
                    "run did not prune them: check the timer fired, and that "
                    "a run is not hitting its time limit with work left.")
            if "undrained_age" in over:
                log(f"ALERT: {table} holds rows up to "
                    f"{over['undrained_age']:.0f}h old that the server has "
                    "still NOT acknowledged. Their retention has not even "
                    f"started -- it begins at the acknowledgement, so the "
                    f"{keep:g}h window has not yet applied to them -- and "
                    "§6.4 forbids deleting them meanwhile. They occupy the "
                    "disk all the same and nothing will reclaim them until "
                    "the server takes them. This is the DRAIN failing, not "
                    "the pruner.")
    else:
        log("pruning is off (no --prune-acked); nothing is deleted")

    if facts["auto_vacuum"] == 2:
        freed = incremental_vacuum(conn, deadline=deadline)
        left = conn.execute("PRAGMA freelist_count").fetchone()[0]
        log(f"returned {freed} page(s) ({mb(freed * facts['page_size'])}) to "
            "the filesystem"
            + (f"; {left} still free, continuing next run" if left else ""))
    else:
        free = facts["freelist_count"] * facts["page_size"]
        log(f"auto_vacuum is "
            f"{AUTO_VACUUM_MODES.get(facts['auto_vacuum'], facts['auto_vacuum'])}"
            f", so no space can be returned ({mb(free)} is free inside the "
            "file and will be reused). Run `enable-auto-vacuum` once with the "
            "poller stopped. This run did NOT vacuum.")

    log(f"write-ahead log checkpoint: {checkpoint(conn)}")
    after = file_sizes(args.db)
    log(f"database {mb(before['db'])} -> {mb(after['db'])}, log "
        f"{mb(before['-wal'])} -> {mb(after['-wal'])}, in "
        f"{time.monotonic() - started:.1f}s")
    # Exit 0 even with an ALERT above: under a systemd timer a non-zero exit
    # marks the UNIT failed, and "maintenance failed" is not what happened.
    return 0


def cmd_enable(args) -> int:
    ok, message = enable_auto_vacuum(args.db)
    print(message if ok else f"REFUSED: {message}", file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scheduled maintenance for the gateway database: return "
        "free space, checkpoint the log, optionally prune rows the server "
        "has acknowledged. `run` is safe beside a running poller; "
        "`enable-auto-vacuum` is not and checks.")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("status", help="sizes, modes and what is waiting (read-only)")
    s.add_argument("--db", default="gateway.sqlite3")
    s.set_defaults(func=cmd_status)

    r = sub.add_parser("run", help="the scheduled job")
    r.add_argument("--db", default="gateway.sqlite3")
    r.add_argument("--prune-acked", action="store_true",
                   help="delete drained rows the server acknowledged longer "
                   "ago than their table's retention: "
                   f"{RETENTION_HOURS['machine_events']:g} hours for "
                   "machine_events, "
                   f"{DEFAULT_RETENTION_HOURS / 24:g} days for every other "
                   "drained table. Off by default. Unacknowledged rows are "
                   "never deleted.")
    r.add_argument("--prune-acked-hours", type=float, default=0.0,
                   metavar="HOURS",
                   help="instead of the per-table retention, delete "
                   "acknowledged rows older than this for EVERY drained "
                   "table. For tests and one-off clean-ups; the timer uses "
                   "--prune-acked.")
    r.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS,
                   help="stop after this long even with work left "
                   "(default %(default)s); the next run continues")
    r.set_defaults(func=cmd_run)

    e = sub.add_parser("enable-auto-vacuum",
                       help="ONE-TIME: convert the database so `run` can "
                       "return space. A full VACUUM; poller and gateway "
                       "client must be stopped")
    e.add_argument("--db", default="gateway.sqlite3")
    e.set_defaults(func=cmd_enable)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "prune_acked_hours", 0) < 0:
        print("--prune-acked-hours cannot be negative", file=sys.stderr)
        return 2
    if not os.path.exists(args.db):
        print(f"{args.db} does not exist; refusing to create an empty "
              "database by maintaining it.", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
