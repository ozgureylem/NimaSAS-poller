"""Validation-pool batches: what is signed, and when a gateway accepts one.

Three rules from the specification, and each is a place a pool goes wrong
quietly if it is left to the caller:

  §7.3  A batch is signed over a DEFINED BYTE LAYOUT, not over its JSON
        text, and `client_id` is inside the signature.
  §7.2  A gateway accepts a batch only if its epoch is strictly greater
        than the persisted one, or equal with the same batch_uuid.
  §7.4  A replacement revokes the remaining available numbers and issues
        the new ones in ONE transaction, and never touches an assigned row.

Used by both ends: the server builds and signs with `canonical_bytes`, the
gateway verifies with the same function, so the two cannot disagree about
what was signed.

THE SIGNED LAYOUT, exactly (all integers big-endian):

    client_id   2-byte length, then that many ASCII bytes
    pool_epoch  8 bytes
    batch_uuid  16 bytes (the UUID's raw bytes, not its text)
    count       4 bytes
    numbers     each: 1-byte length, then that many ASCII digits,
                in the order sent

§7.3 names client_id, pool_epoch, batch_uuid and "each number in the order
sent". The count is added so that a truncated list is a different message
rather than a prefix of the same one.
"""

from __future__ import annotations

import sqlite3
import struct
import uuid
from dataclasses import dataclass

from gateway import ed25519
from gateway.db import (get_counter, next_issue_seq, pool_low_water,
                        pool_size_at_last_fill, pool_used_24h, set_counter,
                        utc_now)

# 2-digit validation system ID + 16-digit number (Table 15.11b).
NUMBER_DIGITS = 18
# Pool capacity is 50 (§15). A batch ten times that is not a top-up, it is
# a mistake or an attempt to fill a 32-bit gateway's memory with one frame.
MAX_BATCH_NUMBERS = 500


class BatchInvalid(ValueError):
    """The batch is malformed. Distinct from a refusal on epoch or
    signature, because a malformed batch is a server bug and the other two
    are §7.2/§7.3 working."""


def split_number(text: str) -> tuple[int, int]:
    """'SSNNNNNNNNNNNNNNNN' -> (validation_system_id, validation_number)."""
    if not isinstance(text, str) or len(text) != NUMBER_DIGITS or not text.isdigit():
        raise BatchInvalid(
            f"a validation number is {NUMBER_DIGITS} decimal digits (2-digit "
            f"system ID + 16), got {text!r}")
    system_id = int(text[:2])
    if system_id == 0:
        # Table 15.8a: system ID 00 in LP 0x58 means "validation DENIED".
        # A pool number carrying it would refuse the cashout it was issued
        # to answer.
        raise BatchInvalid(f"{text!r} has system ID 00, which Table 15.8a "
                           "defines as a denial")
    return system_id, int(text[2:])


def join_number(system_id: int, number: int) -> str:
    return f"{int(system_id):02d}{int(number):016d}"


def canonical_bytes(client_id: str, pool_epoch: int, batch_uuid: str,
                    numbers: list[str]) -> bytes:
    cid = client_id.encode("ascii")
    out = [struct.pack(">H", len(cid)), cid,
           struct.pack(">Q", int(pool_epoch)),
           uuid.UUID(batch_uuid).bytes,
           struct.pack(">I", len(numbers))]
    for n in numbers:
        raw = n.encode("ascii")
        out.append(bytes([len(raw)]))
        out.append(raw)
    return b"".join(out)


def sign_batch(seed: bytes, client_id: str, pool_epoch: int, batch_uuid: str,
               numbers: list[str]) -> dict:
    """SERVER SIDE. The `pool_topup` / POOL_REPLACE sub-object of §10.2."""
    signature = ed25519.sign(
        seed, canonical_bytes(client_id, pool_epoch, batch_uuid, numbers))
    return {"batch_uuid": batch_uuid, "pool_epoch": int(pool_epoch),
            "numbers": list(numbers), "signature": signature.hex()}


@dataclass(frozen=True)
class BatchResult:
    accepted: bool
    # 'applied'      a new epoch, written
    # 'idempotent'   the same epoch and batch_uuid again; nothing changed
    # 'bad-signature' | 'stale-epoch' | 'epoch-conflict' | 'malformed'
    # | 'duplicate-number'
    verdict: str
    detail: str
    pool_epoch: int | None = None
    batch_uuid: str | None = None
    issued: int = 0
    revoked: int = 0

    @property
    def alert(self) -> bool:
        """§7.2: anything but a clean accept "is rejected and alerted"."""
        return not self.accepted


def persisted_epoch(conn: sqlite3.Connection) -> tuple[int, str | None]:
    """The epoch this gateway holds, and the batch it holds it as.

    The counter is the record (§7.2). The pool table is consulted as well
    because examples/pool_replace.py -- the reference tool, run by hand on
    the gateway -- stamps an epoch on the rows it writes and predates the
    counter. Ignoring that would let a server's epoch 1 be accepted over a
    hand-issued epoch 3, i.e. backwards.
    """
    epoch = int(get_counter(conn, "pool_epoch", "0"))
    batch = get_counter(conn, "batch_uuid")
    try:
        row = conn.execute(
            "SELECT pool_epoch, batch_uuid FROM validation_pool "
            "WHERE pool_epoch IS NOT NULL ORDER BY pool_epoch DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        row = None             # no table yet, or no batch columns yet
    if row is not None and row[0] > epoch:
        epoch, batch = int(row[0]), row[1]
    return epoch, batch


def apply_batch(conn: sqlite3.Connection, *, client_id: str, batch: dict,
                public_key: bytes, replace: bool, now_fn=utc_now) -> BatchResult:
    """Verify one batch and, if it passes, write it.

    THE ORDER IS THE SECURITY ARGUMENT. Shape, then signature, then epoch,
    then the write -- so that nothing an unauthenticated batch says about
    itself (its epoch least of all) is believed before the signature is.

    On ANY refusal the existing pool is retained exactly as it was. §7.3:
    "Never treat the discard as pool exhaustion -- a forged top-up must not
    be able to trigger a TITO disable."
    """
    # --- shape -------------------------------------------------------------
    try:
        batch_uuid = str(uuid.UUID(str(batch["batch_uuid"])))
        pool_epoch = batch["pool_epoch"]
        numbers = batch["numbers"]
        signature = bytes.fromhex(batch["signature"])
        if isinstance(pool_epoch, bool) or not isinstance(pool_epoch, int) \
                or pool_epoch < 1:
            raise BatchInvalid(f"pool_epoch {pool_epoch!r} is not a positive integer")
        if not isinstance(numbers, list) or not numbers:
            raise BatchInvalid("a batch with no numbers")
        if len(numbers) > MAX_BATCH_NUMBERS:
            raise BatchInvalid(f"{len(numbers)} numbers in one batch; the "
                               f"cap is {MAX_BATCH_NUMBERS}")
        parsed = [split_number(n) for n in numbers]
        if len(set(numbers)) != len(numbers):
            raise BatchInvalid("the batch repeats a number")
    except (KeyError, TypeError, ValueError) as e:
        return BatchResult(False, "malformed", f"{type(e).__name__}: {e}")

    # --- signature (§7.3) --------------------------------------------------
    if not ed25519.verify(
            public_key,
            canonical_bytes(client_id, pool_epoch, batch_uuid, numbers),
            signature):
        return BatchResult(
            False, "bad-signature",
            f"batch {batch_uuid} epoch {pool_epoch} does not verify for "
            f"client_id {client_id}; discarded, existing pool retained",
            pool_epoch, batch_uuid)

    # --- epoch (§7.2), decided and written under the write lock ------------
    now = now_fn()
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("BEGIN IMMEDIATE")
    try:
        held_epoch, held_uuid = persisted_epoch(conn)
        if pool_epoch == held_epoch and batch_uuid == held_uuid:
            conn.execute("ROLLBACK")
            return BatchResult(True, "idempotent",
                               f"epoch {pool_epoch} batch {batch_uuid} is "
                               "already held", pool_epoch, batch_uuid)
        if pool_epoch == held_epoch:
            conn.execute("ROLLBACK")
            return BatchResult(
                False, "epoch-conflict",
                f"epoch {pool_epoch} arrived as batch {batch_uuid} but this "
                f"gateway holds it as {held_uuid}. §7.2: an attack or a bug, "
                "never swallowed", pool_epoch, batch_uuid)
        if pool_epoch < held_epoch:
            conn.execute("ROLLBACK")
            return BatchResult(
                False, "stale-epoch",
                f"epoch {pool_epoch} is behind the persisted {held_epoch}",
                pool_epoch, batch_uuid)

        revoked = 0
        if replace:
            # ONLY 'available'. An assigned row is a printed ticket that may
            # sit in a customer's pocket for weeks (§7.4); it is not read,
            # not written and not counted here.
            revoked = conn.execute(
                "UPDATE validation_pool SET status = 'revoked', revoked_at = ? "
                "WHERE status = 'available'", (now,)).rowcount
        # issue_seq, not issued_at, is what the assignment orders by: every
        # row of this batch shares `now` to the second, so issued_at alone
        # would fall through to validation_number -- numeric order inside
        # the batch, which §11.2 forbids. The server's order in the batch IS
        # the issue order, so preserve it as given.
        base = next_issue_seq(conn)
        try:
            conn.executemany(
                "INSERT INTO validation_pool (validation_number, "
                "validation_system_id, status, issued_at, pool_epoch, "
                "batch_uuid, issue_seq) VALUES (?, ?, 'available', ?, ?, ?, ?)",
                [(number, system_id, now, pool_epoch, batch_uuid, base + i)
                 for i, (system_id, number) in enumerate(parsed)])
        except sqlite3.IntegrityError as e:
            # A number this gateway has held before. §11.2: numbers are
            # never reissued, so this is a server defect -- and loading the
            # REST of the batch would hide it. All or nothing.
            conn.execute("ROLLBACK")
            return BatchResult(
                False, "duplicate-number",
                f"batch {batch_uuid} carries a number this gateway has "
                f"already held ({e}); nothing was written, revoke included",
                pool_epoch, batch_uuid)
        # The pool size this batch leaves behind, recorded in the same
        # transaction as the numbers themselves. The poller's depth alert
        # takes 20% of this; with no fills row it falls back to the
        # --pool-depth-alert floor, which on a 200-number pool turns four
        # hours of warning into twenty minutes. COUNTED, not derived from
        # len(parsed): a top-up lands on a pool that already has rows.
        available_after = conn.execute(
            "SELECT COUNT(*) FROM validation_pool WHERE status = 'available'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO pool_fills (filled_at, source, issued, revoked, "
            "available_after, pool_epoch, batch_uuid) "
            "VALUES (?, 'server-batch', ?, ?, ?, ?, ?)",
            (now, len(parsed), revoked, available_after, pool_epoch,
             batch_uuid))
        set_counter(conn, "pool_epoch", pool_epoch)
        set_counter(conn, "batch_uuid", batch_uuid)
        # ONE commit: the revoke, the issue and the epoch. §7.4 -- a revoke
        # that committed while the issue failed would leave the gateway
        # unable to dispense, and the next cashout would be a handpay.
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return BatchResult(True, "applied",
                       f"epoch {pool_epoch} batch {batch_uuid}: "
                       f"{len(parsed)} issued, {revoked} revoked",
                       pool_epoch, batch_uuid, issued=len(parsed),
                       revoked=revoked)


def pool_status(conn: sqlite3.Connection, *, low_water: int) -> dict:
    """The `pool_status` object of the heartbeat (§10.4, Appendix B.3).

    `low_water` IS THE REFILL REQUEST. Operator, 2026-11-12: "low water
    alert should make the server to fill the pool immediately (at earliest
    convenience)". The heartbeat goes every 15 s and `service.py` mints a
    top-up on the beat that carries the flag, so the flag is the mechanism
    and nothing else needs building. Two consequences for this function:

    - It is a STANDING CONDITION, not an event. It stays true on every beat
      until the pool is refilled, so a dropped beat or a reconnect costs 15
      seconds rather than the refill. That is why the request does not ride
      on the one-per-episode PoolLow row in poll_errors, which can be
      missed exactly once and then never repeats.
    - The threshold must be THE SAME NUMBER the operator is alerted at.
      `low_water` here is the FLOOR; the live mark scales with the recorded
      pool size, as it does in the poller. A gateway that warns a human at
      39 while telling the server it is comfortable until 10 has turned an
      automatic refill into a manual one without saying so.

    Fails toward asking: an unreadable pool reports low_water (see the
    caller in client.py), because a refill nobody needed is cheap and a
    refill nobody sent turns every cashout into a handpay.
    """
    available = conn.execute(
        "SELECT COUNT(*) FROM validation_pool WHERE status = 'available'"
    ).fetchone()[0]
    epoch, batch = persisted_epoch(conn)
    mark, why = pool_low_water(conn, low_water)
    # EIGHT FIELDS: the four Appendix B.3 names, and four it does not.
    #
    # The extension was filed as amendment A25 and left for the server's
    # author to accept or refuse. Accepted 2026-11-12, all at once, because
    # the server cannot do rule 5 without one of them: the refill is
    # max(15, used_24h) and used_24h is a fact only the gateway holds.
    # validation_pool is never drained, and ticket_out_history carries only
    # tickets that PRINTED -- so a count made on the server would miss
    # exactly the voids (a jam burning fifty numbers) that most need
    # replacing.
    #
    #   used_24h          what the refill is sized from
    #   pool_size         what this gateway was actually filled to; None
    #                     when no fill is recorded, and None is not zero
    #   low_water_mark    the number low_water was compared against
    #   low_water_reason  where that number came from, in words
    #
    # A server that predates them reads the first four with .get() and
    # ignores the rest; a gateway that predates them sends four and the
    # server sizes its refill at the minimum and says so.
    return {"available_ids": available,
            # STRICTLY LESS THAN, matching check_pool_depth(). These were
            # <= and < respectively: a one-number disagreement about the
            # same pool, small and silent, and the kind that gets argued
            # about at 3am instead of read off the code.
            "low_water": available < mark,
            "active_batch_uuid": batch, "pool_epoch": epoch,
            "used_24h": pool_used_24h(conn),
            "pool_size": pool_size_at_last_fill(conn),
            "low_water_mark": mark, "low_water_reason": why}
