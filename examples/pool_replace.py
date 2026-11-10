"""Retire the validation numbers a gateway is still holding and issue new ones.

LAB STAND-IN FOR THE CMS. The numbers minted here are generated locally and
are not server-issued, exactly as seed_validation_pool's are. What this adds
is the half the lab never had: replacing a pool that is already in place,
on a live database, while the poller keeps running.

It does the thing §5.4 describes a server doing, and it does it under the
three rules that make a replacement safe rather than merely convenient.

REVOKE, NEVER DELETE. validation_pool doubles as this gateway's own record of
which number went onto which ticket. Rows marked 'assigned' are printed
tickets and are never touched here -- not the status, not the amount, not the
timestamp. Rows still 'available' become 'revoked' and stay in the table.
Deleting them would destroy the only evidence that the number ever existed,
which matters precisely when somebody is asking where a ticket came from.

ISSUED NUMBERS SURVIVE REVOCATION. §9.4: "Discard governs what a gateway may
still hand out; it never governs what the server recognises... A ticket
printed from that pool before the restore is genuine and may sit in a
customer's pocket for weeks." A number that was dispensed stays redeemable
forever. A number issued into the pool and never dispensed, then revoked, is
"simply retired -- never Open, because nothing was ever printed."

ALL OR NOTHING. The revoke and the re-issue happen in one transaction. A
revoke that committed while the insert failed would leave the gateway holding
zero available numbers, and the next cashout would go unanswered through the
ten-second window and become a handpay (§15.7). That is the exact failure
this whole mechanism exists to prevent, so it must not be reachable by
replacing the pool.

The running poller needs no change and no restart. It selects
`WHERE status = 'available'` at the moment it answers a 0x57, so revoked rows
drop out on the next cashout; and its pool-age alert reads MIN(issued_at)
over available rows only, so retiring the old ones is what actually clears it
-- adding fresh numbers alongside them never would.

    python3 examples/pool_replace.py gateway.sqlite3 --count 50
    python3 examples/pool_replace.py gateway.sqlite3 --count 50 --max-age-hours 36
    python3 examples/pool_replace.py gateway.sqlite3 --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sqlite3
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples.sql_poll_logger import open_gateway_db  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_VALIDATION_SYSTEM_ID = 1  # 0 means "deny" per Table 15.8a
DIGITS = 16


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def migrate_pool_columns(conn: sqlite3.Connection) -> list[str]:
    """Add the batch identity a replacement needs to a table created before
    replacements existed. Existing rows keep NULL, which is honest: they were
    minted before epochs were tracked, so which batch they belong to is
    genuinely unknown rather than epoch 1."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(validation_pool)")}
    if not cols:
        raise SystemExit("validation_pool does not exist in this database -- "
                         "run sql_poll_logger against it at least once first.")
    added = []
    for name, decl in (("pool_epoch", "INTEGER"),
                       ("batch_uuid", "TEXT"),
                       ("revoked_at", "TEXT")):
        if name not in cols:
            conn.execute(f"ALTER TABLE validation_pool ADD COLUMN {name} {decl}")
            added.append(name)
    if added:
        conn.commit()
    return added


def pool_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {row[0]: row[1] for row in conn.execute(
        "SELECT status, COUNT(*) FROM validation_pool GROUP BY status")}


def oldest_available_age_hours(conn: sqlite3.Connection, *, now_fn=utc_now) -> float | None:
    row = conn.execute(
        "SELECT MIN(issued_at) FROM validation_pool "
        "WHERE status = 'available' AND issued_at IS NOT NULL").fetchone()
    if row is None or row[0] is None:
        return None
    oldest = dt.datetime.fromisoformat(row[0])
    return (dt.datetime.fromisoformat(now_fn()) - oldest).total_seconds() / 3600.0


def current_epoch(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(pool_epoch) FROM validation_pool").fetchone()
    return (row[0] or 0) if row else 0


class PoolLocked(RuntimeError):
    """Another process holds the write lock. Raised instead of letting
    sqlite3's own error escape, because an operator replacing a pool needs to
    know the pool was NOT touched -- a bare traceback out of BEGIN IMMEDIATE
    does not say that, and the obvious reaction to it is to kill the poller."""


def replace_pool(conn: sqlite3.Connection, count: int, *,
                 validation_system_id: int = DEFAULT_VALIDATION_SYSTEM_ID,
                 random_fn=random.getrandbits, now_fn=utc_now) -> dict:
    """Revoke every available number and mint ``count`` fresh ones, in one
    transaction. Returns what happened. 'assigned' rows are not read, not
    written and not counted against the new batch."""
    if count <= 0:
        raise ValueError("count must be positive -- a replacement that issues "
                         "nothing would leave the gateway unable to dispense")
    now = now_fn()
    epoch = current_epoch(conn) + 1
    batch = str(uuid.uuid4())

    # Take the write lock before reading counts. open_gateway_db() gives this
    # connection a 30s busy timeout, which covers the poller's 5-7s cycle many
    # times over -- but say something useful if it still cannot get in, rather
    # than handing the operator a traceback from the middle of a transaction.
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as e:
        if "locked" not in str(e).lower() and "busy" not in str(e).lower():
            raise
        raise PoolLocked(
            "could not take the database write lock: another process is "
            "holding it. The poller writes once per cycle, so this normally "
            "clears within seconds -- if it does not, check for a SECOND "
            "poller (pgrep -af sql_poll_logger) before stopping anything. "
            "NOTHING HAS BEEN CHANGED: the pool is exactly as it was."
        ) from e
    try:
        revoked = conn.execute(
            "UPDATE validation_pool SET status = 'revoked', revoked_at = ? "
            "WHERE status = 'available'", (now,)).rowcount
        added = 0
        attempts = 0
        while added < count:
            attempts += 1
            if attempts > count * 50:
                raise RuntimeError(
                    "could not mint enough distinct numbers -- the random "
                    "space is exhausted or the table is far larger than expected")
            number = random_fn(DIGITS * 4) % (10 ** DIGITS)
            try:
                conn.execute(
                    "INSERT INTO validation_pool (validation_number, "
                    "validation_system_id, status, issued_at, pool_epoch, batch_uuid) "
                    "VALUES (?, ?, 'available', ?, ?, ?)",
                    (number, validation_system_id, now, epoch, batch))
            except sqlite3.IntegrityError:
                continue  # collided with a number this gateway already holds
            added += 1
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"revoked": revoked, "issued": added, "pool_epoch": epoch,
            "batch_uuid": batch, "at": now}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", help="the gateway's SQLite database")
    ap.add_argument("--count", type=int, default=50,
                    help="how many numbers the new batch carries (default %(default)s)")
    ap.add_argument("--max-age-hours", type=float, default=None,
                    help="only replace if the oldest AVAILABLE number is at least this "
                         "old; otherwise report and do nothing. Omit to replace "
                         "unconditionally.")
    ap.add_argument("--system-id", type=int, default=DEFAULT_VALIDATION_SYSTEM_ID,
                    help="validation system ID for the new batch (default %(default)s; "
                         "0 means DENY per Table 15.8a and is refused)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would happen and change nothing")
    args = ap.parse_args()

    if args.system_id == 0:
        print("system ID 0 means 'deny' in Table 15.8a; refusing to mint a batch "
              "that every machine would reject.", file=sys.stderr)
        return 2

    path = Path(args.db)
    if not path.exists():
        print(f"{path} does not exist. Note that sqlite3 would have CREATED an "
              "empty database here rather than telling you -- which is why this "
              "checks first.", file=sys.stderr)
        return 2

    # Same pragmas as the poller: WAL so a reader is not blocked by the
    # writer, and a busy timeout long enough to wait out one 5-7s poll cycle.
    # Without them this raised "database is locked" against a running soak --
    # the one situation the tool exists for.
    conn = open_gateway_db(str(path))
    added_cols = migrate_pool_columns(conn)
    for c in added_cols:
        print(f"migrated: added {c} to validation_pool "
              "(existing rows keep NULL -- they predate batch tracking)")

    before = pool_counts(conn)
    age = oldest_available_age_hours(conn)
    print(f"\n{path}")
    print(f"  pool now: " + ", ".join(f"{v} {k}" for k, v in sorted(before.items()))
          or "  pool now: empty")
    print(f"  current epoch: {current_epoch(conn)}")
    print(f"  oldest available number: "
          + (f"{age:.1f}h old" if age is not None else "none available"))

    if args.max_age_hours is not None:
        if age is None:
            print(f"\nNo available numbers to age out. Replacing anyway would be "
                  f"right, but --max-age-hours was given and there is nothing to "
                  f"measure, so nothing is done. Re-run without it to issue a batch.")
            return 0
        if age < args.max_age_hours:
            print(f"\nOldest available number is {age:.1f}h, below the "
                  f"{args.max_age_hours:.1f}h threshold. Nothing to do.")
            return 0

    if args.dry_run:
        print(f"\n--dry-run: would revoke {before.get('available', 0)} available "
              f"number(s), leave {before.get('assigned', 0)} assigned untouched, "
              f"and issue {args.count} new one(s) as epoch {current_epoch(conn) + 1}.")
        return 0

    try:
        result = replace_pool(conn, args.count, validation_system_id=args.system_id)
    except PoolLocked as e:
        print(f"\n{e}", file=sys.stderr)
        return 3
    after = pool_counts(conn)
    print(f"\n  revoked : {result['revoked']} (kept in the table, status 'revoked')")
    print(f"  issued  : {result['issued']} as epoch {result['pool_epoch']}")
    print(f"  batch   : {result['batch_uuid']}")
    print(f"  assigned rows left untouched: {after.get('assigned', 0)}")
    print(f"\n  pool now: " + ", ".join(f"{v} {k}" for k, v in sorted(after.items())))
    print("\nRevoked numbers stay in the table on purpose: they are the record "
          "that this gateway\never held them. Any number already dispensed stays "
          "redeemable regardless (§9.4).")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
