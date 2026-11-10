#!/usr/bin/env python3
"""Could the cabinet's exception queue have overflowed? And was the gateway why?

READ-ONLY. Touches no serial port, so run it while a soak is going.

THE RULE THIS ENFORCES, in the operator's words:

    "Unless the CPU is exhausted or the gateway is rebooting, EGM's buffer
    cannot be overflowing even with 400ms polling. Exception overflows are
    only acceptable if only the gateway lost connection (reboot, faulty
    cable). I won't ever accept buffer overflow because gateway is busy."

That is not a preference to be accommodated. It follows from §12.3 and it is
what this project's own data shows.

WHY EVENTS-PER-CYCLE IS THE WRONG MEASURE, since that is the mistake this
tool exists to stop repeating. It is tempting to compare the events arriving
in a cycle against the queue depth -- 41 events against a 67-deep queue, 61%
occupied, looks like a margin worth watching. It is meaningless, because it
assumes the queue is drained once per cycle.

§12.3 lets an event pre-empt the reply to ANY long poll. So every host
message is a drain opportunity, and a cycle contains scores of them. On the
lab database `rte-interleaved` carries 76,481 of 80,102 game starts -- 95%
of them arrived mid-cycle, not at the cycle's general poll. The queue is
being emptied continuously.

So the queue can only fill when MESSAGES STOP. The depth that matters is not
how many events a cycle sees; it is how many can arrive in the longest gap
between two consecutive host messages:

    deepest the queue can get  =  event rate  x  longest message gap

which rearranges into the one number worth enforcing:

    max safe gap  =  queue depth  /  event rate

Everything below is that arithmetic, measured rather than assumed, plus the
attribution every overflow needs: a gap because the gateway was DOWN is
acceptable and expected; a gap because the gateway was BUSY is a defect.

    python3 examples/queue_safety.py ~/NimaSAS/gateway.sqlite3
    python3 examples/queue_safety.py ~/NimaSAS/gateway.sqlite3 --queue-depth 20
    python3 examples/queue_safety.py ~/NimaSAS/gateway.sqlite3 --since 2026-11-05T18:00
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BUFFER_OVERFLOW = 0x70

# §2.2.1's floor: "a FIFO exception queue of at least 20 elements". A machine
# may hold more and this one does, but 20 is the only number a gateway may
# rely on for a cabinet it has not measured.
SPEC_MIN_QUEUE = 20

# Measured on the lab EGT, 2026-11-02, by filling the queue during an outage.
MEASURED_LAB_QUEUE = 67

# A gap this long is a different kind of event: the gateway was not running,
# or the link was down. Those are the acceptable causes and they are
# attributed separately rather than counted against the polling rate.
OUTAGE_GAP_SECONDS = 120.0


def _ts(v: str) -> datetime | None:
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00").replace(" ", "T"))
    except (ValueError, AttributeError):
        return None


def cycle_gaps(conn, since, until):
    """Every gap between consecutive meter snapshots, newest-last.

    `meters_history.polled_at` is one row per cycle, so these are CYCLE gaps,
    not message gaps -- a cycle sends many messages. That makes every number
    here an UPPER bound on the true message gap, which is the safe direction:
    this tool cannot clear a run that was actually unsafe, it can only fail to
    clear one that was actually fine.
    """
    where, params = [], []
    if since:
        where.append("polled_at >= ?")
        params.append(since)
    if until:
        where.append("polled_at <= ?")
        params.append(until)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = [r[0] for r in conn.execute(
        f"SELECT polled_at FROM meters_history{clause} ORDER BY id", params)]
    out = []
    for a, b in zip(rows, rows[1:]):
        ta, tb = _ts(a), _ts(b)
        if ta and tb:
            out.append(((tb - ta).total_seconds(), a, b))
    return out, rows


def event_rate(conn, since, until):
    """Events per second, and events per game, over the window.

    Events per game is the number that does not transfer between cabinet
    models: this EGT emits two (game start, game end) and sends no reel
    stops at all, while a five-reel cabinet with reel stops enabled emits
    about eight. A polling interval proved safe on the first is not thereby
    safe on the second.
    """
    where, params = ["source != 'startup-backlog'"], []
    if since:
        where.append("occurred_at >= ?")
        params.append(since)
    if until:
        where.append("occurred_at <= ?")
        params.append(until)
    clause = " WHERE " + " AND ".join(where)
    n, first, last = conn.execute(
        f"SELECT count(*), min(occurred_at), max(occurred_at) "
        f"FROM machine_events{clause}", params).fetchone()
    if not n or not first or not last:
        return None
    ta, tb = _ts(first), _ts(last)
    span = (tb - ta).total_seconds() if ta and tb else 0.0
    starts = conn.execute(
        f"SELECT count(*) FROM machine_events{clause} AND exception_code = 126",
        params).fetchone()[0]
    return {"events": n, "span": span, "games": starts,
            "per_second": (n / span) if span > 0 else None,
            "per_game": (n / starts) if starts else None}


def peak_hour_rate(conn, since, until):
    """The busiest hour's event rate, and when it was.

    THE BUG THIS FIXES. The first version of this tool divided all events by
    the whole span, and on the lab database that is 162,455 events over
    258.69 hours = 0.174 events/second. But most of those hours are idle: the
    busiest hour ran 1,557 games, which at 2.01 events a game is 0.869
    events/second -- FIVE TIMES the mean. The budget is depth/rate, so using
    the mean inflated a 20-deep budget from 23 s to 114.6 s and the tool
    reported OK on a 100.6 s gap it should have questioned.

    A safety check that averages over the quiet hours is not a safety check.
    """
    where, params = ["source != 'startup-backlog'"], []
    if since:
        where.append("occurred_at >= ?")
        params.append(since)
    if until:
        where.append("occurred_at <= ?")
        params.append(until)
    clause = " WHERE " + " AND ".join(where)
    row = conn.execute(
        f"SELECT substr(occurred_at,1,13) h, count(*) n FROM machine_events"
        f"{clause} GROUP BY h ORDER BY n DESC LIMIT 1", params).fetchone()
    if not row:
        return None
    return {"hour": row[0], "events": row[1], "per_second": row[1] / 3600.0}


def local_rate(conn, at: str, span_seconds: float = 1800.0):
    """Events per second in the half hour each side of ``at``.

    Used per gap, because a gap only matters at the rate that was running
    WHEN IT HAPPENED. A 100-second stall while the cabinet is idle costs
    nothing; the same stall under autoplay is 87 events on a 20-deep queue.
    The global rate cannot tell those apart and so must not be used to clear
    either of them.
    """
    t = _ts(at)
    if t is None:
        return None
    from datetime import timedelta
    lo = (t - timedelta(seconds=span_seconds)).isoformat()
    hi = (t + timedelta(seconds=span_seconds)).isoformat()
    n = conn.execute(
        "SELECT count(*) FROM machine_events WHERE source != 'startup-backlog' "
        "AND occurred_at >= ? AND occurred_at <= ?", (lo, hi)).fetchone()[0]
    return n / (2 * span_seconds)


def timestamps_are_cycle_granular(conn, since, until):
    """Can this database answer "was the poller sending during the gap"?

    ON THIS POLLER IT CANNOT, and the tool has to know that rather than
    report a stall it cannot see.

    sql_poll_logger.py calls client.drain_rte_events() ONCE PER CYCLE and
    stamps every event it returns with the same `now` -- the cycle's
    timestamp. So ~47 interleaved events from one cycle all carry one
    identical time, and NO interleaved event can ever fall strictly inside
    a gap between two cycle rows. The drained-inside test then reports
    "NOTHING COLLECTED" for every single cycle, which looks exactly like a
    stall and is an artifact of when the row was written.

    The tell is cheap: compare events against distinct timestamps. Several
    events per distinct time means the stamps are cycle-granular and arrival
    order inside a cycle was not recorded.
    """
    where, params = ["source != 'startup-backlog'"], []
    if since:
        where.append("occurred_at >= ?")
        params.append(since)
    if until:
        where.append("occurred_at <= ?")
        params.append(until)
    clause = " WHERE " + " AND ".join(where)
    n, distinct = conn.execute(
        f"SELECT count(*), count(DISTINCT occurred_at) FROM machine_events"
        f"{clause}", params).fetchone()
    if not distinct:
        return False, 0.0
    per = n / distinct
    return per >= 2.0, per


def drains_inside(conn, start: str, end: str):
    """Events the cabinet delivered DURING a gap, and the worst sub-gap.

    THE DISCRIMINATOR THAT MAKES THIS TOOL DECISIVE RATHER THAN BOUNDING.

    `meters_history` holds one row per cycle, so a "gap" between rows is a
    whole cycle -- and a cycle of the full Table C-7 sweep takes 46-58
    seconds while sending 73 long polls and 73 general polls. Under §12.3
    every one of those 146 messages is a chance for the cabinet to unload an
    event, so the real gap between messages is about 0.4 s, not 58.

    Scoring such a cycle as `gap x event rate` reads 47 elements when the
    true depth is 0.33 -- wrong by the number of messages in a cycle, in the
    pessimistic direction. An earlier version of this tool did exactly that
    and flagged four ordinary cycles as near-overflow.

    The database can tell the two apart, because an event collected
    mid-cycle is stored with its own capture time. So:

      events arrived inside the gap  -> the poller was sending and the
          cabinet was draining. The depth is set by the worst interval
          BETWEEN those arrivals, not by the whole gap.

      nothing arrived inside the gap, while play was going on -> the poller
          sent nothing for the whole gap. That is a real stall, and the
          depth is the whole gap x the rate.
    """
    rows = [r[0] for r in conn.execute(
        "SELECT occurred_at FROM machine_events "
        "WHERE source != 'startup-backlog' AND occurred_at > ? "
        "AND occurred_at < ? ORDER BY occurred_at", (start, end))]
    if not rows:
        return 0, None
    marks = [_ts(start)] + [_ts(r) for r in rows] + [_ts(end)]
    marks = [m for m in marks if m is not None]
    worst = 0.0
    for a, b in zip(marks, marks[1:]):
        worst = max(worst, (b - a).total_seconds())
    return len(rows), worst


def overflows(conn, since, until):
    where, params = [f"exception_code = {BUFFER_OVERFLOW}"], []
    if since:
        where.append("occurred_at >= ?")
        params.append(since)
    if until:
        where.append("occurred_at <= ?")
        params.append(until)
    clause = " WHERE " + " AND ".join(where)
    return conn.execute(
        f"SELECT source, count(*), min(occurred_at), max(occurred_at) "
        f"FROM machine_events{clause} GROUP BY source ORDER BY count(*) DESC",
        params).fetchall()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--queue-depth", type=int, default=None,
                    help=f"elements to assume (default: both {SPEC_MIN_QUEUE}, "
                         f"the spec floor, and {MEASURED_LAB_QUEUE}, measured "
                         f"on the lab EGT)")
    ap.add_argument("--events-per-game", type=float, default=None,
                    help="override the measured rate, to ask what a cabinet "
                         "that also sends reel stops would do")
    args = ap.parse_args()

    path = Path(args.db)
    if not path.is_file():
        print(f"No such database: {path}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    print("=" * 72)
    print(f"Queue safety: {path}")
    print("=" * 72)

    rate = event_rate(conn, args.since, args.until)
    if rate is None or not rate["per_second"]:
        print("\nNo events in this window, so there is no rate to measure and "
              "nothing to clear.")
        print("An idle cabinet cannot overflow a queue. Play it and re-run.")
        conn.close()
        return 3

    print(f"\n--- EVENT RATE {'-' * 57}")
    print(f"  {rate['events']} events over {rate['span'] / 3600:.2f} h "
          f"= {rate['per_second']:.3f} events/second")
    if rate["per_game"]:
        print(f"  {rate['games']} game starts = {rate['per_game']:.2f} events "
              f"per game")
        print("  Events per game is a PER-MODEL fact. This EGT sends no reel")
        print("  stops; a five-reel cabinet that does sends about 8 per spin,")
        print("  so a rate proved safe here does not transfer. Use")
        print("  --events-per-game to ask what such a cabinet would do.")

    peak = peak_hour_rate(conn, args.since, args.until)
    if peak:
        print(f"\n  BUSIEST HOUR {peak['hour']}: {peak['events']} events "
              f"= {peak['per_second']:.3f} events/second")
        print(f"  That is {peak['per_second'] / rate['per_second']:.1f}x the "
              f"whole-run mean. The mean is NOT what a safety budget may use:")
        print(f"  it averages over the idle hours, and a queue overflows "
              f"during the busy ones.")

    per_second = rate["per_second"]
    if args.events_per_game and rate["per_game"]:
        scale = args.events_per_game / rate["per_game"]
        per_second *= scale
        print(f"\n  ASSUMING {args.events_per_game} events/game instead of the "
              f"measured {rate['per_game']:.2f}:")
        print(f"  event rate scales to {per_second:.3f} events/second")

    depths = ([args.queue_depth] if args.queue_depth
              else [SPEC_MIN_QUEUE, MEASURED_LAB_QUEUE])

    print(f"\n--- THE SAFETY BUDGET {'-' * 50}")
    print("  max safe gap between host messages = queue depth / event rate")
    budgets = {}
    for d in depths:
        budget = d / per_second
        budgets[d] = budget
        note = ("  (§2.2.1 floor — the only depth an unmeasured cabinet "
                "guarantees)" if d == SPEC_MIN_QUEUE else
                "  (measured on the lab EGT, 2026-11-02)"
                if d == MEASURED_LAB_QUEUE else "")
        print(f"    {d:>3} elements -> {budget:7.1f} s{note}")

    gaps, rows = cycle_gaps(conn, args.since, args.until)
    if not gaps:
        print("\nFewer than two cycles in this window: no gap to measure.")
        conn.close()
        return 3

    gaps.sort(key=lambda g: g[0])
    worst, w_from, w_to = gaps[-1]
    median = gaps[len(gaps) // 2][0]
    outage = [g for g in gaps if g[0] >= OUTAGE_GAP_SECONDS]
    connected = [g for g in gaps if g[0] < OUTAGE_GAP_SECONDS]

    print(f"\n--- GAPS ACTUALLY ACHIEVED {'-' * 45}")
    print(f"  {len(gaps)} cycle(s), median {median:.2f}s")
    print(f"  NOTE these are CYCLE gaps, and a cycle sends many messages, so")
    print(f"  every figure here is an UPPER bound on the real message gap.")
    print(f"  That is the safe direction: this tool can fail to clear a run")
    print(f"  that was fine, but cannot clear one that was unsafe.")
    print(f"  worst overall {worst:.1f}s  ({w_from} -> {w_to})")
    if outage:
        print(f"  {len(outage)} gap(s) of {OUTAGE_GAP_SECONDS:.0f}s or more are "
              f"treated as the gateway being DOWN or the link being dead,")
        print(f"  which is the one acceptable cause, and are excluded below.")

    # EVERY GAP AT THE RATE THAT WAS RUNNING WHEN IT HAPPENED.
    #
    # The longest gap is not the most dangerous one. A 100-second stall on an
    # idle cabinet queues nothing; a 30-second stall under autoplay queues 26
    # events. So the quantity to rank by is gap x local rate -- the depth the
    # queue actually reached -- not the gap alone.
    print(f"\n--- THE DEPTH EACH GAP ACTUALLY REACHED {'-' * 32}")
    print("  gap x the event rate local to it (the half hour either side),")
    print("  which is the only honest way to compare a stall under play")
    print("  against the same stall on an idle machine.")
    cycle_granular, per_stamp = timestamps_are_cycle_granular(
        conn, args.since, args.until)
    if cycle_granular:
        print()
        print("  ** THIS DATABASE CANNOT ANSWER THE QUESTION EXACTLY.")
        print(f"     {per_stamp:.1f} events share each distinct timestamp, so "
              f"the stamps are CYCLE-granular:")
        print("     sql_poll_logger.py drains interleaved events once per "
              "cycle and stamps them all")
        print("     with the cycle's own time. No event can fall inside a gap "
              "between cycle rows, so")
        print("     'nothing arrived during the gap' is guaranteed by "
              "construction and says nothing")
        print("     about whether the poller was sending.")
        print("     Everything below is therefore the PESSIMISTIC BOUND -- "
              "the whole cycle gap --")
        print("     and a cycle running a full sweep sends ~146 messages "
              "inside it, so the true")
        print("     depth is smaller by about that factor. Treat a failure "
              "here as 'not proven',")
        print("     not as a stall. To measure it properly the poller must "
              "stamp each event as it")
        print("     arrives rather than at the end of the cycle.")

    scored = []
    for g, a, b in connected:
        lr = local_rate(conn, a)
        if lr is None:
            continue
        if args.events_per_game and rate["per_game"]:
            lr *= args.events_per_game / rate["per_game"]
        n_inside, worst_sub = drains_inside(conn, a, b)
        if n_inside and worst_sub is not None:
            # The cabinet was unloading throughout, so the queue only built
            # during the worst quiet stretch INSIDE the cycle.
            effective, kind = worst_sub, f"drained {n_inside}x"
        elif cycle_granular:
            # The database CANNOT say. Every event carries its cycle's
            # timestamp, so none can fall inside a gap and the absence of
            # arrivals is meaningless. Report the bound and label it.
            effective, kind = g, "cannot tell (bound)"
        else:
            effective, kind = g, "NOTHING COLLECTED"
        scored.append((effective * lr, effective, g, lr, kind, a, b))
    if not scored:
        print("  no connected gap could be scored")
        conn.close()
        return 3
    scored.sort(reverse=True)
    print("  A cycle gap is NOT a message gap: a full Table C-7 sweep takes")
    print("  46-58s and sends ~146 messages inside it, every one a chance for")
    print("  the cabinet to unload an event (§12.3). So where events arrived")
    print("  DURING the gap, the depth is set by the worst quiet stretch")
    print("  between those arrivals -- not by the whole gap.")
    print()
    print(f"  {'depth':>7}  {'quiet':>8}  {'of gap':>8}  {'rate':>9}  "
          f"{'what happened':<18} when")
    for depth, eff, g, lr, kind, a, _ in scored[:6]:
        print(f"  {depth:7.1f}  {eff:7.1f}s  {g:7.1f}s  {lr:7.3f}/s  "
              f"{kind:<18} {a[:19]}")
    worst_depth, wd_eff, wd_gap, wd_rate, wd_kind, wd_at, _ = scored[0]

    print(f"\n--- VERDICT {'-' * 60}")
    verdict = 0
    for d in depths:
        if worst_depth <= d:
            print(f"  OK   {d}-deep: the deepest the queue reached was "
                  f"{worst_depth:.1f} elements ({100 * worst_depth / d:.0f}% of it)")
        else:
            print(f"  **   {d}-deep: the queue reached {worst_depth:.1f} "
                  f"elements, OVER {d}")
            print(f"       {wd_eff:.1f}s of silence at {wd_at[:19]} while "
                  f"{wd_rate:.3f} events/s were being generated")
            print(f"       ({wd_kind} across a {wd_gap:.1f}s cycle gap).")
            if wd_kind == "cannot tell (bound)":
                print(f"       NOT PROVEN. That is the whole cycle gap, and "
                      f"this database cannot say whether the")
                print(f"       poller was sending inside it (see the note "
                      f"above). A full sweep sends ~146")
                print(f"       messages in that time, which would make the "
                      f"real depth under one element.")
                print(f"       Do not treat this as a stall. Fix the "
                      f"timestamping, then re-run.")
                verdict = 3 if verdict == 0 else verdict
            else:
                print(f"       On a {d}-element cabinet that loses events with "
                      f"the gateway connected the whole time -- the case the")
                print(f"       operator's standard does not allow. Find what "
                      f"blocked the poller for {wd_gap:.0f}s.")
                verdict = 1
    print(f"\n  Two readings this tool used to give, kept here so the "
          f"mistakes cannot return:")
    print(f"    whole-run mean rate ({per_second:.3f}/s) on this gap: "
          f"{wd_gap * per_second:.1f} elements")
    print(f"      wrong because it averages over the idle hours. On the lab "
          f"database that made it")
    print(f"      5x too permissive; on a uniformly busy window it reads high "
          f"instead. The direction")
    print(f"      is not predictable, which is the point -- a mean is not a "
          f"safety figure either way.")
    print(f"    whole cycle gap at the local rate: {wd_gap * wd_rate:.1f} "
          f"elements")
    print(f"      wrong because it ignores the ~146 messages sent inside one "
          f"sweep, so it scored")
    print(f"      four ordinary C-7 cycles at 47 elements when the true depth "
          f"was 0.33. Always")
    print(f"      too pessimistic, by about the number of messages in a "
          f"cycle.")

    rows_o = overflows(conn, args.since, args.until)
    print(f"\n--- OVERFLOWS, AND WHETHER THEY ARE ALLOWED {'-' * 28}")
    if not rows_o:
        print("  none in this window")
    for source, n, first, last in rows_o:
        allowed = source == "startup-backlog"
        tag = "ALLOWED  " if allowed else "NOT ALLOWED"
        print(f"  {tag} {source:<18} n={n:<4} {first[:19]} .. {last[:19]}")
        if allowed:
            print("            the queue filled while the gateway was not "
                  "polling. §2.2.1 says the cabinet reports this, and it "
                  "doing so is the audit trail working.")
        else:
            print("            the gateway was CONNECTED and still lost "
                  "events. By the operator's standard this is a gateway "
                  "defect and needs a named, fixed cause -- not a faster "
                  "poll rate.")
            verdict = 1

    print("\n" + "=" * 72)
    if verdict:
        print("VERDICT: something here needs a cause. See the ** lines above.")
    else:
        print("VERDICT: no gap large enough to overflow any assumed queue "
              "depth while connected,")
        print("  and no overflow that is not attributable to the gateway "
              "being down.")
    conn.close()
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
