#!/usr/bin/env python3
"""How many handpays should never have happened?

READ-ONLY on the database, never opens the serial port. Safe during a soak.

THIS IS THE MIGRATION'S OWN SCORECARD. The reason for moving off Windows and
RamSys-as-it-stands, in the operator's words, is "unwanted handpays and wrong
player data". Until this tool existed nothing counted the first one, so
"fewer unwanted handpays" was a claim nobody could audit -- including the
person making it.

THE SEQUENCE IS THE EVIDENCE, NOT THE AMOUNT.

    0x66  cash out button pressed
    0x57  system validation request     <- the machine asks the host
    0x51  handpay is pending            <- and locks up instead

A handpay that follows a validation request, with no ticket printed in
between, is a handpay caused by the host not answering. §15.7 gives the host
TEN SECONDS to reply with LP 0x71; past that the cabinet gives up and locks
up. That is a defect at the host, and the sequence says so without needing
to know anything about money.

The successful path ends differently, and that is the discriminator:

    0x66 -> 0x57 -> (host answers) -> 0x3D  a cash out ticket has been printed

WHY NOT THE AMOUNT. An earlier version of this tool classified by size: a
handpay below both the jackpot limit and the ticket-out limit "had no
business being a handpay". It worked on the lab's data, but the ticket-out
limit is VARIABLE -- per property, per cabinet, and changed by operators --
so the classifier needed a number that is only true on the day somebody
types it in. Worse, it inferred the cause from the size instead of reading
the cause off the wire.

A jackpot lockup raises NO validation request. It never asks the host for
anything, so it cannot appear here however large or small it is, and no
jackpot limit has to be configured to exclude it. That is the second reason
the sequence beats the threshold.

WHAT THE SEQUENCE STILL CANNOT SAY. It proves the request went unanswered.
It does not say why, and three defects end the same way:

  * the host was late or absent -- a CMS outage, a slow network, a busy
    gateway
  * the validation pool was empty, so the gateway had nothing to answer WITH
    (poll_errors carries a PoolExhausted row at that moment)
  * the printer could not print it anyway (0x61/0x74/0x60/0x78 nearby)

This tool separates the second from the others, because the gateway's own
database records whether it answered. The first and third it names rather
than guesses.

WHAT IS NEVER CHARGED TO THE HOST. A lockup whose events reached the
gateway as a reconnect backlog ('startup-backlog') happened while nothing
was polling, so no host was late -- there was no host. Those are reported
in their own bucket and left out of the count, on the operator's
instruction of 2026-11-08. The mechanism is identical to a CMS outage on a
live floor, which is the migration's own argument, so the evidence is kept
and printed; what changes is only whether it may be called a defect.

    python3 examples/unwanted_handpays.py ~/NimaSAS/gateway.sqlite3
    python3 examples/unwanted_handpays.py ~/NimaSAS/gateway.sqlite3 --window 90

No limits to configure, and that is the point.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CASHOUT_PRESSED = 0x66
VALIDATION_REQUEST = 0x57
HANDPAY_PENDING = 0x51
TICKET_PRINTED = 0x3D
# A printer that CANNOT PRINT. Advisories are deliberately not here.
#
# 0x74 "printer paper low" was in this list until 2026-11-06 and had to come
# out. It is advisory, and on EGT cabinets it fires with MORE THAN FIFTEEN
# TICKETS still loaded. A 0x74 near a lockup therefore says nothing about
# whether a ticket could print -- and because this tool reports a nearby
# printer fault as a reason the ticket "may have been unprintable whatever
# the host did", leaving it in would EXCUSE A GENUINE HOST FAILURE on a
# cabinet with plenty of paper. Exactly backwards for a scorecard whose job
# is to attribute blame correctly.
#
# The operator's policy, and the reason 0x74 is ignored rather than acted
# on: "what are we going to do with the 15 tickets left, and if we pull them
# out, where are we going to use them. We don't want to waste tickets,
# that's why we always wait until machine lock up because the ticket printer
# is empty." Running to empty is deliberate. 0x61 is the event that matters.
AFT_CASHOUT_REQUEST = 0x6A       # §8.7 stage 1, runs on EVERY cashout
AFT_WIN_CASHOUT_REQUEST = 0x6B

PRINTER_FAULTS = (0x60, 0x61, 0x75, 0x77, 0x78)

# Recorded, never treated as a fault. Worth knowing which cabinets are
# approaching empty so a refill can ride a scheduled walk instead of a
# special trip -- and so that when one does lock up, it reads as expected
# rather than as a defect.
PRINTER_ADVISORIES = (0x74,)

# §15.7 allows the host TEN SECONDS. The window here is far wider on
# purpose: these are GATEWAY CAPTURE times, not cabinet times, and under a
# full Table C-7 sweep an event can be stamped up to a whole cycle after it
# happened -- 46 to 58 seconds measured on this project. A 10-second window
# would miss the slow captures and under-count the defect, which is the
# wrong direction for a scorecard.
DEFAULT_WINDOW_SECONDS = 90.0


def _events(conn, codes, since, until):
    where = [f"exception_code IN ({','.join('?' * len(codes))})"]
    params = list(codes)
    if since:
        where.append("occurred_at >= ?")
        params.append(since)
    if until:
        where.append("occurred_at <= ?")
        params.append(until)
    # ORDER BY occurred_at, id -- NOT occurred_at alone.
    #
    # The poller stamps every event drained in one sweep with that cycle's
    # time, so ties are the normal case and not an edge. With ORDER BY on
    # the timestamp alone the order WITHIN a tie is whatever SQLite
    # happens to return, and the walks below read sequence off that order.
    # They were reading an accident.
    #
    # id is insert order, which for a batch IS arrival order: the drain
    # files events as it collects them, and the interleaved drain runs
    # before each heartbeat's own read specifically to keep it that way
    # (sql_poll_logger's "keeps row ids in arrival order").
    return conn.execute(
        "SELECT occurred_at, exception_code, id FROM machine_events WHERE "
        + " AND ".join(where) + " ORDER BY occurred_at, id", params).fetchall()


def _seconds(a: str, b: str) -> float | None:
    from datetime import datetime
    try:
        ta = datetime.fromisoformat(a.replace("Z", "+00:00").replace(" ", "T"))
        tb = datetime.fromisoformat(b.replace("Z", "+00:00").replace(" ", "T"))
    except (ValueError, AttributeError):
        return None
    return (tb - ta).total_seconds()


def find_unwanted(conn, since=None, until=None,
                  window: float = DEFAULT_WINDOW_SECONDS) -> list[dict]:
    """Every 0x51 that followed a 0x57 with no ticket printed in between.

    Walks the stream once in time order and keeps the most recent
    unanswered validation request. A 0x3D clears it -- the ticket printed,
    so the cycle ended the way it should.
    """
    codes = (CASHOUT_PRESSED, VALIDATION_REQUEST, HANDPAY_PENDING,
             TICKET_PRINTED)
    pending_request = None     # (at,) of the last 0x57 with no ticket yet
    last_button = None
    out: list[dict] = []
    for at, code, _id in _events(conn, codes, since, until):
        if code == CASHOUT_PRESSED:
            last_button = at
        elif code == VALIDATION_REQUEST:
            pending_request = at
        elif code == TICKET_PRINTED:
            pending_request = None          # answered, and it printed
        elif code == HANDPAY_PENDING and pending_request is not None:
            gap = _seconds(pending_request, at)
            # A ZERO GAP IS NOT EVIDENCE OF A SEQUENCE.
            #
            # The poller drains a cycle's events in one batch and, before
            # 2026-11-06, stamped them all with the cycle's own time. So a
            # 0x57 and a 0x51 captured in the same cycle share a timestamp
            # and their real order is unknowable -- the SQL ordering is an
            # accident of insert order, not of what the cabinet did.
            #
            # §7.8.1 makes this likely rather than rare: it REISSUES 0x51
            # every fifteen seconds until LP 0x1B reads the queue, so an
            # unrelated handpay already waiting will keep firing and will
            # land in the same batch as the next cashout's 0x57.
            #
            # That produced two false positives on the first real run, both
            # reading "locked up 0s later", and both followed by a ticket
            # eleven seconds afterwards -- the cashout had succeeded.
            if gap is not None and 0 < gap <= window:
                out.append({
                    "at": at,
                    "requested_at": pending_request,
                    "seconds": gap,
                    "button_at": last_button,
                })
                pending_request = None
    # A TICKET ANYWHERE IN THE WINDOW CLEARS THE REQUEST.
    #
    # The walk above clears on a 0x3D it meets BETWEEN the request and the
    # handpay, which assumes the stream is correctly ordered. With batch
    # stamps it is not. So anything whose request was followed by a ticket
    # inside the window is dropped here regardless of where the 0x51 sat:
    # the cabinet printed, so the host answered, so it is not this defect.
    printed = [at for at, code, _id in _events(conn, (TICKET_PRINTED,), since, until)]
    kept = []
    for item in out:
        answered = any(
            (d := _seconds(item["requested_at"], p)) is not None
            and 0 <= d <= window
            for p in printed)
        if not answered:
            kept.append(item)

    # THE CABINET'S OWN REFUSAL, which has no 0x57 and was therefore
    # invisible to everything above.
    #
    # When a cashout is over the machine's own ticket-out limit it does not
    # ask the host at all -- it knows up front it cannot print that much, so
    # it goes straight to a lockup. The shape is `0x66 -> 0x51` with no
    # validation request in between, and it is a LEGITIMATE handpay: the
    # limit doing its job.
    #
    # Collected rather than ignored because "the cabinet refused" and "the
    # host never answered" are the two explanations an operator is choosing
    # between when a player is standing at a machine, and a tool that only
    # reports one of them is answering half the question.
    charged = {item["at"] for item in out}
    pending_button = None
    button_id = None
    for at, code, _id in _events(conn, codes, since, until):
        if code == CASHOUT_PRESSED:
            pending_button, button_id = at, _id
        elif code == VALIDATION_REQUEST:
            pending_button = None      # it asked; handled above
        elif code == TICKET_PRINTED:
            pending_button = None
        elif code == HANDPAY_PENDING and pending_button is not None:
            gap = _seconds(pending_button, at)
            # A ZERO GAP HERE IS A SEQUENCE, AND IN THE 0x57 WALK IT IS NOT.
            #
            # That walk rejects gap == 0 on purpose, and the reason is
            # written above it: §7.8.1 REISSUES 0x51 every fifteen seconds
            # until LP 0x1B reads the queue, so an old handpay still
            # waiting will keep firing and can land in the same batch as a
            # new 0x57. Two false positives came from exactly that.
            #
            # The trigger here is different in the way that matters. A
            # CASH OUT BUTTON PRESS IS NOT REISSUED -- it happens once,
            # when a finger touches it. So a 0x66 and a 0x51 sharing a
            # cycle stamp are not two unrelated things that collided;
            # they are a sequence whose clock resolution is too coarse to
            # show it, and `id` resolves it because insert order is
            # arrival order.
            #
            # IT IS NOT HYPOTHETICAL. On 2026-11-11 a cash out and its
            # lockup landed in one 56-second sweep, every event stamped
            # 22:26:54.099103, and the tool reported "5 cash out(s) ended
            # in a lockup" when there had been six. Not misclassified --
            # not counted. And the longer the sweep the likelier it is,
            # so the scorecard went blind exactly when the host is most
            # under load. Sweeps that night ran 37s to 131s.
            same_batch_in_order = (gap == 0 and button_id is not None
                                   and _id > button_id)
            if (gap is not None and (0 < gap or same_batch_in_order)
                    and gap <= window and at not in charged):
                kept.append({
                    "at": at,
                    "requested_at": None,      # never asked
                    "seconds": gap,
                    "button_at": pending_button,
                })
                pending_button = None
    kept.sort(key=lambda i: i["at"])
    return kept


# WHERE IT WENT WRONG, read off the ORDER THE EVENTS ARRIVED IN.
#
# The operator's requirement, and the reason it cannot be done by amount:
#
#   "Any limit RamSys displays is unverifiable... that's why event order is
#    important to identify what is a legitimate handpay, what is undesired
#    handpay. Remember, ticket out amount can be limited AT THE SERVER.
#    What we have to check is the order of appearance of the events ... that
#    way we can clearly understand if it is sas communication error or
#    machine error or where it went wrong."
#
# A cabinet-side limit and a server-side limit produce DIFFERENT SHAPES, and
# that is the whole trick:
#
#   the cabinet's own ticket-out limit is exceeded
#       0x66 -> 0x51                      no 0x57 at all. The machine knows
#                                         up front it cannot print this, so
#                                         it never asks the host.
#
#   the SERVER's limit is exceeded
#       0x66 -> 0x57 -> 0x51              it asked, we answered, and the
#       with a number assigned            answer did not produce a ticket.
#
#   the host could not answer
#       0x66 -> 0x57 -> 0x51              it asked and nothing came back.
#       with NO number assigned           THE ONLY ONE THAT IS A FAULT.
#
#   a jackpot
#       0x51 alone                        no cashout was ever requested.
#
# Only the third is an unwanted handpay. The first two are policy working,
# and a detector that cannot tell them apart will either excuse real faults
# or report a correctly-enforced limit as a defect.
WHERE = {
    # THE GATEWAY WAS NOT THERE TO FAIL.
    #
    # The operator, 2026-11-08, on a lockup that happened while the poller
    # was deliberately stopped between soaks: "the earlier handpay doesn't
    # count as undesired one. because everything was in norms. gateway
    # wasn't running. don't add to the statistics."
    #
    # He is right, and the distinction is not cosmetic. This scorecard's
    # claim is "the host failed to answer in time". A host that was
    # switched off by the person running the test did not fail -- there was
    # no host. Counting it would inflate the migration's own headline
    # number with self-inflicted events, and the first person to notice
    # would be whoever is being asked to approve the migration.
    #
    # The tell is on the wire: an event the cabinet queued while nobody was
    # polling arrives with source 'startup-backlog' when the gateway
    # reconnects (§2.2.1's queue draining on the first messages). So the
    # exclusion is read off the data, not configured, and it cannot be
    # applied to a lockup that happened with the poller live.
    #
    # Kept and printed rather than dropped: the MECHANISM is identical to a
    # production CMS outage, and that is the migration's own argument. What
    # changes is only whether it may be counted as a defect.
    "outage": "THE GATEWAY WAS NOT POLLING. The cabinet queued this while "
              "nobody was listening and reported it on reconnect, so no "
              "host failed to answer -- there was no host. Excluded from "
              "the count on the operator's instruction; the mechanism is "
              "still exactly what a CMS outage does on a live floor",
    "jackpot": "a jackpot or other lockup. No cash out was requested, so no "
               "limit and no host is involved",
    "machine-limit": "NO SYSTEM VALIDATION REQUEST WAS EVER RAISED, so the "
                     "cabinet never asked anyone: it had already decided it "
                     "could not print this. That is its own ticket-out "
                     "limit, and it is the limit doing its job",

    "host-declined": "THE HOST answered and the ticket still did not print: "
                     "a number was assigned from the pool. A server-side "
                     "ticket-out limit looks exactly like this, and so does "
                     "a machine rejecting the answer",
    "pool-empty": "THE GATEWAY had nothing to answer with -- the validation "
                  "pool was empty. Ours, not the network's",
    "host-silent": "NOBODY ANSWERED inside §15.7's ten seconds. SAS "
                   "communication, the network, the CMS, or a blocked "
                   "gateway",
}
UNWANTED = ("host-silent", "pool-empty")
# Never counted as a defect and never counted as policy working either: it
# is evidence about the mechanism with the host removed from the question.
EXCLUDED = ("outage",)


def answered_near(conn, at: str, window: float = 120.0):
    """Did the gateway issue a validation number around this moment?

    `validation_pool.assigned_at` is the gateway's own record that it
    answered. Its presence separates "the host said no" from "the host said
    nothing", which is the difference between a policy working and a fault.
    """
    try:
        rows = conn.execute(
            "SELECT assigned_at, assigned_amount_cents FROM validation_pool "
            "WHERE assigned_at IS NOT NULL").fetchall()
    except sqlite3.OperationalError:
        return None
    for assigned_at, amount in rows:
        gap = _seconds(at, assigned_at)
        if gap is not None and abs(gap) <= window:
            return amount if amount is not None else -1
    return None


def aft_requested_near(conn, at: str, window: float) -> bool:
    """Was there a 0x6A between the button and the lockup?

    Without this, an AFT cashout request that went unanswered is
    indistinguishable from the cabinet refusing outright: both are
    `0x66 -> 0x51` with no 0x57. They are completely different faults and
    the 0x6A is the only thing on the wire that separates them.
    """
    for occurred, _, _id in _events(conn, (AFT_CASHOUT_REQUEST,
                                      AFT_WIN_CASHOUT_REQUEST), None, None):
        gap = _seconds(occurred, at)
        if gap is not None and 0 <= gap <= window:
            return True
    return False


def arrived_as_backlog(conn, at: str) -> bool:
    """Did this 0x51 reach the gateway as a reconnect backlog drain?

    `machine_events.source` is written by the poller at capture time and
    says HOW the event reached it. 'startup-backlog' means the cabinet had
    queued it while nothing was polling and handed it over on reconnect --
    so the lockup happened during a gap in which no host existed to answer.

    Read off the stored source rather than inferred from a gap in the
    timestamps: a gap can also mean a slow cycle, and guessing would put
    real host failures in the excluded bucket, which is the one direction
    this tool must never err in.
    """
    try:
        rows = conn.execute(
            "SELECT source FROM machine_events "
            "WHERE exception_code = ? AND occurred_at = ?",
            (HANDPAY_PENDING, at)).fetchall()
    except sqlite3.OperationalError:
        return False
    return any(src == "startup-backlog" for (src,) in rows)


def where_it_went_wrong(conn, item: dict, window: float) -> str:
    """Which of WHERE's cases this lockup is, from the order of events."""
    # FIRST, because it overrides every other reading: if nobody was
    # polling, no host failed and no limit was consulted by a host.
    if arrived_as_backlog(conn, item["at"]):
        return "outage"
    if item.get("requested_at") is None:
        if item.get("button_at") is None:
            return "jackpot"
        # NO 0x57 MEANS A LIMIT. The operator, settling this directly:
        # "if there's no system validation request, it is most definitely a
        # limit issue, it probably exceeds machine's ticket out limit."
        #
        # A 0x6A beforehand does NOT change that. Stage 1 of the ladder runs
        # on every cashout whether or not an AFT host exists, so its presence
        # says the cabinet tried AFT first -- which costs ~8 seconds -- not
        # that AFT is why this became a handpay. An earlier draft made it a
        # separate cause and would have blamed the network for a correctly
        # enforced limit.
        return "machine-limit"
    if pool_was_empty(conn, item["at"]):
        return "pool-empty"
    if answered_near(conn, item["requested_at"]) is not None:
        return "host-declined"
    return "host-silent"


def amount_near(conn, at: str, window: float = 120.0):
    """The LP 0x1B reading closest to a lockup, for reporting only.

    Deliberately NOT part of the classification -- the sequence decides.
    """
    rows = conn.execute(
        "SELECT captured_at, amount FROM handpay_queue "
        "WHERE all_zeros = 0 AND amount IS NOT NULL").fetchall()
    best = None
    for captured, amount in rows:
        gap = _seconds(at, captured)
        if gap is None or abs(gap) > window:
            continue
        if best is None or abs(gap) < abs(best[0]):
            best = (gap, amount)
    return None if best is None else best[1]


def pool_was_empty(conn, at: str, window: float = 120.0) -> bool:
    """Did the gateway have nothing to answer WITH?

    This is the one cause the gateway's own database can settle, because it
    is the gateway's own failure: PoolExhausted is written at the moment a
    cashout arrives and the pool holds no available number.
    """
    rows = conn.execute(
        "SELECT occurred_at FROM poll_errors WHERE error_type = 'PoolExhausted'"
    ).fetchall()
    for (occurred,) in rows:
        gap = _seconds(at, occurred)
        if gap is not None and abs(gap) <= window:
            return True
    return False


def printer_fault_near(conn, at: str, window: float = 300.0):
    rows = _events(conn, PRINTER_FAULTS, None, None)
    for occurred, code, _id in rows:
        gap = _seconds(at, occurred)
        if gap is not None and abs(gap) <= window:
            return code
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--window", type=float, default=DEFAULT_WINDOW_SECONDS,
                    help="seconds a 0x51 may follow its 0x57 and still be "
                         "attributed to it (default: %(default)s). §15.7's "
                         "own budget is 10s; this is wider because these are "
                         "gateway capture times and a sweep can delay one by "
                         "a whole cycle")
    ap.add_argument("--units-per-currency", type=int, default=100,
                    help="meter units in one unit of currency (default 100)")
    args = ap.parse_args()

    path = Path(args.db)
    if not path.is_file():
        print(f"No such database: {path}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    print("=" * 72)
    print(f"Unwanted handpays: {path}")
    print("=" * 72)
    print("\n  A handpay is UNWANTED when it follows a system validation")
    print("  request with no ticket printed in between:")
    print()
    print("      0x66 cash out pressed -> 0x57 validation request -> 0x51 handpay")
    print()
    print("  The host did not answer in §15.7's ten seconds, so the cabinet")
    print("  gave up and locked up. A JACKPOT raises no validation request at")
    print("  all, so it cannot appear here -- no limit has to be configured")
    print("  to exclude it, and none is.")

    try:
        requests = _events(conn, (VALIDATION_REQUEST,), args.since, args.until)
        handpays = _events(conn, (HANDPAY_PENDING,), args.since, args.until)
        printed = _events(conn, (TICKET_PRINTED,), args.since, args.until)
    except sqlite3.OperationalError as e:
        print(f"\ncannot read machine_events: {e}", file=sys.stderr)
        return 2

    print(f"\n--- WHAT IS IN THIS WINDOW {'-' * 45}")
    print(f"  0x57 validation requests  {len(requests)}")
    print(f"  0x3D tickets printed      {len(printed)}")
    print(f"  0x51 handpays pending     {len(handpays)}")

    if not requests:
        print("\nNo validation request in this window, so no cashout ever")
        print("asked the host for anything. Nothing here can be an unwanted")
        print("handpay -- and nothing proves the gateway would answer one.")
        conn.close()
        return 3

    found = find_unwanted(conn, args.since, args.until, args.window)

    def money(units) -> str:
        return "unknown" if units is None else \
            f"{units / args.units_per_currency:,.2f}"

    print(f"\n--- VERDICT {'-' * 60}")
    if not found:
        print(f"  No unwanted handpay. All {len(requests)} validation "
              f"request(s) in this window")
        print("  were answered, or ended some way other than a lockup.")
        conn.close()
        return 0

    by_where: dict[str, list] = {}
    for item in found:
        by_where.setdefault(where_it_went_wrong(conn, item, args.window),
                            []).append(item)

    unwanted = [i for w in UNWANTED for i in by_where.get(w, [])]
    excluded = [i for w in EXCLUDED for i in by_where.get(w, [])]
    legitimate = [i for w, got in by_where.items()
                  if w not in UNWANTED and w not in EXCLUDED
                  for i in got]

    tail = (f", {len(excluded)} excluded (gateway not polling)"
            if excluded else "")
    print(f"  {len(found)} cash out(s) ended in a lockup. "
          f"{len(unwanted)} UNWANTED, {len(legitimate)} legitimate{tail}.")
    print()
    print("  The ORDER the events arrived in is what separates them, because")
    print("  no long poll reports a cabinet's ticket-out limit and a")
    print("  server-side limit cannot be seen at all (amendment A13).")
    print()

    for where in ("host-silent", "pool-empty", "host-declined",
                  "machine-limit", "jackpot", "outage"):
        got = by_where.get(where, [])
        if not got:
            continue
        mark = "**" if where in UNWANTED else "  "
        print(f"  {mark} {where}: {len(got)}")
        print(f"       {WHERE[where]}")
        for item in got[:8]:
            amount = amount_near(conn, item["at"])
            print(f"       {item['at'][:19]}  {money(amount):>12}")
            if item["button_at"]:
                print(f"           0x66 at {item['button_at'][:19]}", end="")
                if item["requested_at"]:
                    print(f" -> 0x57 at {item['requested_at'][:19]}"
                          f" -> 0x51 {item['seconds']:.0f}s later")
                else:
                    via = (" (via 0x6A, which costs ~8s and changes nothing)"
                           if aft_requested_near(conn, item["at"], args.window)
                           else "")
                    print(f" -> 0x51 {item['seconds']:.0f}s later, "
                          f"NO 0x57 ever raised{via}")
            fault = printer_fault_near(conn, item["at"])
            if fault is not None and where in UNWANTED:
                print(f"           a printer fault (0x{fault:02X}) is logged "
                      f"nearby, so the ticket may have been")
                print(f"           unprintable whatever the host did")
        if len(got) > 8:
            print(f"       ... and {len(got) - 8} more")
        print()

    if not unwanted:
        if excluded:
            print(f"  {len(excluded)} lockup(s) happened while the gateway was "
                  f"not polling and")
            print("  are excluded from the count. Of the rest, every one is")
            print("  policy working: a limit enforced, or a jackpot.")
        else:
            print("  Every lockup here is policy working: a limit enforced, or a")
            print("  jackpot. Nothing is waiting on the host.")
        conn.close()
        return 0

    found = unwanted

    print("  The gap above is measured from GATEWAY CAPTURE times, not from")
    print("  the cabinet's clock, so it is an upper bound on the real delay.")
    print("  A per-event answer latency would settle it; that is still open.")
    conn.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
