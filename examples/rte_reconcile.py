#!/usr/bin/env python3
"""Did real time event reporting lose anything at this polling rate?

Read-only. Touches no serial port, so run it while the soak is still going.

THE CLAIM UNDER TEST. It is said that a host must poll faster than 40 ms or
real time events are lost. The specification does not say that. §12.2, in
full:

    "The polling format defined in Section 2 is used by the host to obtain
    meter information. However, the polling rate CAN be increased to 40 ms
    in order to better approximate real time reporting."

Two words decide it. "Can", not must; and the stated purpose is to "better
approximate real time" -- that is LATENCY, how soon the host hears, not
RETENTION, whether the host hears at all. Retention is §2.2.1's job, and §12
does not touch it:

    "It is possible for a gaming machine to generate a series of exceptions
    at a rate that is faster than the polling cycle of the host. To
    accommodate this, gaming machines must maintain a first in/first out
    (FIFO) exception queue of at least 20 elements in non-volatile memory."

§12.5 says "all EXCEPTIONS are reported using the event message", so RTE
changes the wire format of an exception, not the queue behind it. A host
polling every 450 ms loses events only if the machine generates more than
twenty between polls.

BUT THE CLAIM IS NOT SILLY, and this tool exists because the argument above
is not evidence. RTE adds events that ordinary polling does not have at all
-- reel stops (§12.5.5) and card held/not held (§12.5.7) are both marked
"sent only if real time event reporting is enabled" -- so the event rate
under RTE is strictly higher than the exception rate without it. A five-reel
game emits game-selected, game-start, five reel-stops and game-end: seven
events for one spin. Twenty elements is under three spins of headroom. On a
machine that spins faster than a player can, or buffers reel stops and
releases them together, 450 ms could genuinely be too slow. That is an
empirical question about one cabinet, which is what a soak answers.

SEVEN INDEPENDENT DETECTORS, so a clean result is not one tool's opinion:

 1. The machine's own admission. §2.2.1: "If one or more exceptions have
    been lost from the queue, exception 70, buffer overflow, should be
    reported at the next opportunity." A machine that drops events is
    required to say so. Any 0x70 in the run is a direct confession.

 2. The event stream's own continuity. §12.5.3 puts the coin-in meter
    AFTER the wager inside every game-start event. Consecutive game starts
    must therefore differ by exactly the later one's wager. If they differ
    by more, a game start went missing in between and the excess says how
    much was wagered in it. This needs no meters at all -- the stream
    audits itself.

 6. The OTHER sign of the same arithmetic. Detector 2 only flags coin in
    rising by MORE than the wager. A game start that repeats the previous
    coin-in reading gives the negative sign, and detector 2 drops it -- so
    its PASS means "nothing missing", never "nothing repeated". Detector 6
    reads the negative sign, and pairs a negative that cancels the positive
    before it as an out-of-order delivery (A10) rather than either fault.

 7. The credit meter as a running ledger, which is the operator's own
    check: credits before - wagers + wins = credits after, over every
    consecutive pair of gauge readings. The anchor is a POLL, not an
    event, so unlike detector 2 it does not audit the stream against
    itself -- and it is the only detector that scores an individual game
    END rather than ends in aggregate.

 3. Bill-event continuity. §12.5.1's bill-accepted event carries the
    running count of bills of that denomination, so consecutive bill events
    for one denomination must step by exactly one.

 4. Meter reconciliation, WAGER side. Over the same window, the meters must
    agree with the events: games played against the count of game starts,
    coin in against the summed wagers.

 5. Meter reconciliation, AWARD side -- the payout identity. Every credit a
    game-end event says was awarded must land in a payout meter. This is
    separate from detector 4 because a win that is NOT paid to the credit
    meter does not increment coin out: an attendant-paid jackpot increments
    `total_jackpot`, and a progressive award may increment a progressive
    meter, `total_jackpot`, or coin out, at the vendor's choice. Comparing
    summed wins against coin out alone therefore under-counts by exactly
    the jackpots -- and the symptom is a return-to-player figure that looks
    implausibly low rather than an error message. Detector 5 tries each
    convention and reports which one closes, because section 5b already
    measured three Novomatic meter codes reporting one quantity: the
    convention is a per-model fact.

    THE ACCEPTANCE RULE this implements, stated by the operator: absent a
    buffer overflow, the awarded credits -- jackpot and progressive pays
    included -- MUST match the meter deltas. If there IS an overflow, the
    identity cannot be clean, and the overflow's own source has to be found
    and prevented rather than tolerated. So detector 5 refuses to report
    PASS on any window where detector 1 found an overflow.

A detector can be silent because nothing was lost or because nothing
happened. Every section below therefore reports its sample size, and the
verdict refuses to clear a run that had nothing to lose.

    python3 examples/rte_reconcile.py ~/soak-rte.sqlite3
    python3 examples/rte_reconcile.py ~/soak-rte.sqlite3 --since 2026-10-28T20:00:00
"""

from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Every source that carries a real Table 12.5 event. Leaving one out does
# not under-report events, it INVENTS losses: the meter moves for a game
# whose event the detectors were never shown.
#
# 'rte-heartbeat' was missing here for exactly one run. The heartbeat is the
# general poll issued BETWEEN long polls during a meter sweep, so on a quiet
# machine it is often the poll that happens to catch a lone game -- and a
# lone game on a quiet machine is precisely the case a continuity check is
# looking at. The 2026-10-28 soak reported "200 credits of wagering
# unaccounted for" across 7.7 hours; the event was in the database the whole
# time, at 09:36:51, with credits_wagered 10 and wager_cents 200, filed
# under the one source the filter did not list.
RTE_SOURCES = ("rte", "rte-interleaved", "rte-heartbeat", "startup-backlog")

GAME_START = 0x7E
GAME_END = 0x7F
BILL_ACCEPTED = 0x4F
BUFFER_OVERFLOW = 0x70
REEL_STOPPED = 0x88
CARD_HELD = 0x8B
GAME_SELECTED = 0x8C

# §2.2.1's floor. A machine may hold more; none may hold less.
SPEC_MIN_QUEUE = 20


def _events(conn, since: str | None, until: str | None) -> list[dict]:
    where, params = [], []
    if since:
        where.append("occurred_at >= ?")
        params.append(since)
    if until:
        where.append("occurred_at <= ?")
        params.append(until)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        "SELECT id, occurred_at, exception_code, description, source, rte_fields "
        f"FROM machine_events{clause} ORDER BY id", params).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r[0], "at": r[1], "code": r[2], "description": r[3],
            "source": r[4], "fields": json.loads(r[5]) if r[5] else {},
        })
    return out


def _meter_window(conn, since: str | None, until: str | None, columns: list[str]):
    """First and last meters_history row in the window, as dicts."""
    have = {row[1] for row in conn.execute("PRAGMA table_info(meters_history)")}
    cols = [c for c in columns if c in have]
    if not cols:
        return None, None, []
    where, params = [], []
    if since:
        where.append("polled_at >= ?")
        params.append(since)
    if until:
        where.append("polled_at <= ?")
        params.append(until)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    sel = ", ".join(["polled_at"] + cols)
    first = conn.execute(
        f"SELECT {sel} FROM meters_history{clause} ORDER BY id LIMIT 1", params).fetchone()
    last = conn.execute(
        f"SELECT {sel} FROM meters_history{clause} ORDER BY id DESC LIMIT 1", params).fetchone()
    if first is None or last is None:
        return None, None, cols
    keys = ["polled_at"] + cols
    return dict(zip(keys, first)), dict(zip(keys, last)), cols


def check_buffer_overflow(events: list[dict], window_start: str | None) -> tuple[str, list[str]]:
    """Detector 1: the machine's own admission -- but WHEN it confessed decides
    what the confession is about.

    §2.2.1's queue is non-volatile and fills whether or not a host is
    listening, and 0x70 is reported "at the next opportunity" after a loss.
    So a 0x70 collected in the startup drain, or in the first cycle, is
    evidence that the queue overflowed at some earlier point -- very likely
    while nobody was polling at all -- and says nothing whatever about this
    run's polling rate. Only an overflow during steady-state polling does.

    Treating the two alike was this tool's own first mistake: a lab run that
    lost nothing was reported as a failure on the strength of one 0x70 that
    arrived before the first meter reading.
    """
    hits = [e for e in events if e["code"] == BUFFER_OVERFLOW]
    if not hits:
        return "PASS", ["no exception 0x70 anywhere: the machine never reported "
                        "losing an event from its queue"]

    # SOURCE FIRST, THEN TIMESTAMP -- and the order matters.
    #
    # The first version of this tested the timestamp first, so in a
    # whole-database run (where window_start is the LAST gateway restart)
    # every overflow in the file's history fell into `pre` and was printed
    # under "the machine is played with no host draining it". On
    # 2026-11-07 that sentence was covering 61 overflows whose source is
    # `rte` or `rte-interleaved` -- events collected WHILE THE GATEWAY WAS
    # POLLING, which is the single C5 violation this project has on record.
    # A verdict of PASS over a sentence that misdescribes a C5 violation is
    # the exact shape of bug this file exists to catch in others.
    #
    # A `startup-backlog` source IS an undrained queue, whenever it
    # happened. Any other source was collected mid-poll, and no window
    # makes that untrue -- it is only out of scope for THIS window.
    polled = {"rte", "rte-interleaved", "rte-heartbeat", "heartbeat", "cycle"}
    pre, earlier_polled, during = [], [], []
    for e in hits:
        if e["source"] == "startup-backlog":
            pre.append(e)
        elif window_start is not None and e["at"] <= window_start:
            (earlier_polled if e["source"] in polled else pre).append(e)
        else:
            during.append(e)

    lines = []
    if pre:
        lines.append(f"{len(pre)} buffer-overflow event(s) drained at STARTUP, "
                     f"before the window opened:")
        for e in pre[:5]:
            lines.append(f"    {e['at']}  (source {e['source']})")
        lines.append("  The queue had already overflowed when this run began. That "
                     "happens whenever the machine is played with no host draining "
                     "it, and is not evidence about this run's polling rate.")
    if earlier_polled:
        first, last = earlier_polled[0]["at"], earlier_polled[-1]["at"]
        lines.append(
            f"** {len(earlier_polled)} buffer-overflow event(s) before this "
            f"window were collected WHILE A GATEWAY WAS POLLING "
            f"({first[:19]} .. {last[:19]}). Those are connected overflows -- "
            f"C5 violations -- in an EARLIER run, not an undrained queue. Out "
            f"of scope for this window's verdict; judge them with "
            f"queue_safety.py over the whole database.")
        for e in earlier_polled[:5]:
            lines.append(f"    {e['at']}  (source {e['source']})")
    if not during:
        return "PASS", lines + [
            "no overflow once polling was under way in THIS window"]
    lines.append(f"{len(during)} buffer-overflow event(s) DURING steady-state "
                 f"polling -- the machine reports dropping events while this run "
                 f"was draining it:")
    for e in during[:10]:
        lines.append(f"    {e['at']}  (source {e['source']})")
    if len(during) > 10:
        lines.append(f"    ... and {len(during) - 10} more")
    return "FAIL", lines


def gateway_restarts(conn) -> list[str]:
    """When the GATEWAY was started, from the interrogations table.

    A restart is a hole in the event stream that is nobody's fault but ours.
    The machine keeps playing and keeps queueing; §2.2.1's queue overflows
    after about eight seconds of that, and everything past the twentieth
    event is gone before the gateway is back. Blaming the polling rate for
    it would be like blaming the speedometer for the miles driven while the
    car was someone else's.

    The overnight run of 2026-10-28 is the case in point: three restarts
    during debugging, and the reconciler reported their 1,223,200 credits of
    play as "events were lost. At this polling rate this cabinet does NOT
    retain every event" -- a sentence about a polling rate, from evidence
    that was entirely about a process being stopped.
    """
    try:
        rows = conn.execute(
            "SELECT started_at FROM interrogations "
            "WHERE reason = 'first_connect' ORDER BY started_at").fetchall()
    except sqlite3.OperationalError:
        return []  # a database from before interrogations existed
    return [r[0] for r in rows]


def _straddles(boundaries: list[str], earlier: str, later: str) -> str | None:
    """The restart that falls between these two events, if any."""
    for b in boundaries:
        if earlier < b <= later:
            return b
    return None


def check_game_start_continuity(events: list[dict],
                                boundaries: list[str] | None = None
                                ) -> tuple[str, list[str]]:
    """Detector 2: §12.5.3's coin-in-after-the-wager audits the stream."""
    starts = [e for e in events
              if e["code"] == GAME_START and "total_coin_in" in e["fields"]]
    lines = [f"{len(starts)} game-start event(s) carrying total_coin_in"]
    if len(starts) < 2:
        return "NO DATA", lines + [
            "fewer than two: nothing to check continuity against. Play the "
            "machine during the run or this detector says nothing."]

    boundaries = boundaries or []
    gaps, checked, spanned = [], 0, []
    for prev, cur in zip(starts, starts[1:]):
        before, after = prev["fields"]["total_coin_in"], cur["fields"]["total_coin_in"]
        wager = cur["fields"].get("wager_cents")
        if wager is None or after < before:
            # A meter clear mid-run, or a multi-denom game whose cents could
            # not be resolved. Not a gap; just not checkable.
            continue
        restart = _straddles(boundaries, prev["at"], cur["at"])
        if restart is not None:
            # The gateway was down in here. Whatever the machine queued and
            # then lost is the gateway's absence, not the polling rate.
            spanned.append((prev, cur, (after - before) - wager, restart))
            continue
        checked += 1
        excess = (after - before) - wager
        if excess > 0:
            gaps.append((prev, cur, excess, wager))
    lines.append(f"{checked} consecutive pair(s) checkable")
    if spanned:
        lost = sum(g[2] for g in spanned)
        lines.append(f"{len(spanned)} pair(s) span a GATEWAY RESTART and are not "
                     f"counted: {lost:,} credits of play happened while this "
                     f"program was not running, which no polling rate could have "
                     f"captured")
        for prev, cur, excess, restart in spanned[:5]:
            lines.append(f"    {prev['at'][11:19]} -> {cur['at'][11:19]}: "
                         f"{excess:,} credits, gateway restarted at {restart[11:19]}")
    if not checked:
        return "NO DATA", lines
    if not gaps:
        lines.append("every pair differs by exactly the later game's wager: "
                     "no game-start event went missing")
        return "PASS", lines
    total = sum(g[2] for g in gaps)
    lines.append(f"{len(gaps)} gap(s), {total} credits of wagering unaccounted for:")
    for prev, cur, excess, wager in gaps[:10]:
        lines.append(f"    {prev['at']} -> {cur['at']}: coin in rose "
                     f"{cur['fields']['total_coin_in'] - prev['fields']['total_coin_in']}"
                     f" but this game wagered {wager}; {excess} unexplained")
    if len(gaps) > 10:
        lines.append(f"    ... and {len(gaps) - 10} more")

    # WHEN, not just how many. A truncated list of ten hides the one thing
    # that decides whether a number matters: is it history or is it now?
    #
    # The whole-database run of 2026-11-06 reported "255 gap(s), 74,835
    # credits" and printed ten, all from 2026-10-29/30 -- every one of them
    # the "coin in rose 50 but this game wagered 25" in ADJACENT PAIRS that
    # this project already identified as an event-ORDERING swap, fixed
    # since. Nothing had been lost, and the headline number said otherwise
    # because it spanned every bug the database remembers.
    by_day: dict[str, list] = {}
    for prev, cur, excess, wager in gaps:
        by_day.setdefault(str(cur.get("at", ""))[:10], []).append(excess)
    if len(by_day) > 1:
        lines.append("")
        lines.append("  by day -- history or now?")
        for day in sorted(by_day):
            got = by_day[day]
            lines.append(f"    {day}  {len(got):>4} gap(s), "
                         f"{sum(got):>8} credits")
        newest = sorted(by_day)[-1]
        lines.append(f"    A number that stops at an old date is a fossil. "
                     f"Anything on {newest} is this build's problem.")
    return "FAIL", lines


def check_repeated_events(events: list[dict],
                          boundaries: list[str] | None = None
                          ) -> tuple[str, list[str]]:
    """Detector 6: THE OTHER SIGN. Detector 2 is blind to it by construction.

    Detector 2 flags `(after - before) - wager > 0` -- coin in rose by more
    than this game's wager, so a game start went missing in between. The
    negative sign is dropped on the floor, and it is a different defect:

        delta == 0, wager > 0    the SAME game start twice. The coin-in
                                 meter cannot un-move, so a repeat claims a
                                 wager that no meter movement backs.
        delta  < 0               the meter went backwards: a RAM clear, a
                                 misread, or two starts delivered OUT OF
                                 ORDER (amendment A10).
        0 < delta < wager        short: part of this wager is unaccounted,
                                 or the pair sits beside a swap.

    WHY IT EXISTS. The operator, 2026-11-07, reading detector 4: "How did
    events report more bet than meter delta? You call it benign but I feel
    like a few exceptions were issued more than once." Nothing in this tool
    could answer him. Detector 2 had reported PASS on 8,702 pairs while
    being structurally incapable of seeing a duplicate -- a PASS that means
    "nothing missing", never "nothing repeated".

    A SWAP IS NOT A LOSS, and adjacency tells them apart. Two starts
    delivered out of order produce a positive excess immediately followed
    by a negative one that cancels it. That pattern is the 2026-10-29/30
    "fossil" -- 211 gaps, 64,805 credits -- which this project diagnosed by
    hand over a day and then had to remember forever after, because nothing
    computed it. This detector computes it: where a negative cancels the
    positive before it, detector 2's "gap" for that pair is a REORDERING,
    not a lost game, and both are named here.
    """
    starts = [e for e in events
              if e["code"] == GAME_START and "total_coin_in" in e["fields"]]
    lines = [f"{len(starts)} game-start event(s) carrying total_coin_in"]
    if len(starts) < 2:
        return "NO DATA", lines + [
            "fewer than two: nothing to compare. Play the machine during the "
            "run or this detector says nothing."]

    boundaries = boundaries or []
    pairs: list[tuple[dict, dict, int, int, int]] = []
    for prev, cur in zip(starts, starts[1:]):
        before = prev["fields"]["total_coin_in"]
        after = cur["fields"]["total_coin_in"]
        wager = cur["fields"].get("wager_cents")
        if wager is None:
            # Multi-denom game whose cents could not be resolved. Detector 2
            # skips these too; counting them here would invent anomalies out
            # of a missing denomination code.
            continue
        if _straddles(boundaries, prev["at"], cur["at"]) is not None:
            continue
        pairs.append((prev, cur, after - before, wager, (after - before) - wager))

    lines.append(f"{len(pairs)} consecutive pair(s) checkable")
    if not pairs:
        return "NO DATA", lines

    # WHAT A SWAP ACTUALLY LOOKS LIKE, worked out from the arithmetic rather
    # than guessed -- the first draft of this guessed, and the test caught it.
    #
    # Wagers a, b, c, d arriving with the middle two transposed give the
    # consecutive excesses:
    #
    #     +b,  -(b + c),  +c
    #
    # so the negative in the middle cancels the SUM of its two neighbours,
    # not the one before it. Detector 2 skips the middle pair outright
    # (`after < before`) and reports the two positives as TWO separate gaps.
    # That is precisely the 2026-10-29 fossil: adjacent pairs each reading
    # "coin in rose 50 but this game wagered 25", with a -50 between them
    # that nothing printed.
    swapped: set[int] = set()
    for i in range(1, len(pairs) - 1):
        prev_x, here, next_x = pairs[i - 1][4], pairs[i][4], pairs[i + 1][4]
        if here < 0 < prev_x and next_x > 0 and prev_x + here + next_x == 0:
            swapped.update((i - 1, i, i + 1))

    repeats, backwards, short = [], [], []
    for i, (prev, cur, delta, wager, excess) in enumerate(pairs):
        if excess >= 0 or i in swapped:
            continue
        if delta == 0 and wager > 0:
            repeats.append((prev, cur, wager))
        elif delta < 0:
            backwards.append((prev, cur, delta, wager))
        else:
            short.append((prev, cur, delta, wager))

    if swapped:
        reordered = len(swapped) // 3
        lines.append(
            f"{reordered} group(s) of game starts arrived OUT OF ORDER and "
            f"cancel exactly (A10: arrival order is not event order). These "
            f"are NOT losses. Detector 2 skips the negative pair and reports "
            f"the two positives around it as separate gaps, so subtract "
            f"{2 * reordered} from its gap count before quoting it:")
        for i in sorted(swapped)[:9:3]:
            lines.append(f"    {pairs[i][0]['at']}: excess +{pairs[i][4]}, "
                         f"{pairs[i + 1][4]}, +{pairs[i + 2][4]} -- they sum "
                         f"to zero, so nothing left the stream")

    if not (repeats or backwards or short):
        lines.append("no game start repeats a coin-in reading and none moves "
                     "it backwards: nothing was delivered twice")
        return "PASS", lines

    if repeats:
        total = sum(w for _, _, w in repeats)
        lines.append(
            f"** {len(repeats)} game start(s) REPEAT the previous coin-in "
            f"reading while claiming a wager -- {total:,} credits of wagering "
            f"with no meter movement behind it. The coin-in meter cannot "
            f"un-move, so the same game was reported more than once:")
        for prev, cur, wager in repeats[:10]:
            lines.append(f"    {prev['at']} -> {cur['at']}: coin in unchanged "
                         f"at {cur['fields']['total_coin_in']}, "
                         f"wager {wager} claimed  [{prev['source']} -> "
                         f"{cur['source']}]")
        if len(repeats) > 10:
            lines.append(f"    ... and {len(repeats) - 10} more")

    if backwards:
        lines.append(
            f"{len(backwards)} pair(s) move the coin-in meter BACKWARDS with "
            f"no cancelling partner: a RAM clear, a misread, or a reordering "
            f"whose other half fell outside this window:")
        for prev, cur, delta, wager in backwards[:5]:
            lines.append(f"    {prev['at']} -> {cur['at']}: coin in fell "
                         f"{-delta}, wager {wager}")

    if short:
        total = sum(w - d for _, _, d, w in short)
        lines.append(
            f"{len(short)} pair(s) are SHORT: coin in rose, but by less than "
            f"the later game's wager -- {total:,} credits. Not a repeat and "
            f"not a loss; most often a reordering whose partner is further "
            f"than one pair away.")
        for prev, cur, delta, wager in short[:5]:
            lines.append(f"    {prev['at']} -> {cur['at']}: coin in rose "
                         f"{delta}, this game wagered {wager}")

    if repeats:
        return "FAIL", lines
    return "INCONCLUSIVE", lines


def check_bill_continuity(events: list[dict]) -> tuple[str, list[str]]:
    """Detector 3: §12.5.1's per-denomination running count steps by one."""
    per_denom: dict[int, list[dict]] = defaultdict(list)
    for e in events:
        if e["code"] == BILL_ACCEPTED and "bills_of_this_type" in e["fields"]:
            per_denom[e["fields"].get("denomination_code")].append(e)
    total = sum(len(v) for v in per_denom.values())
    lines = [f"{total} bill-accepted event(s) across "
             f"{len(per_denom)} denomination(s)"]
    pairs, gaps = 0, []
    for denom, evs in sorted(per_denom.items(), key=lambda kv: (kv[0] is None, kv[0])):
        for prev, cur in zip(evs, evs[1:]):
            a = prev["fields"]["bills_of_this_type"]
            b = cur["fields"]["bills_of_this_type"]
            if b < a:
                continue
            pairs += 1
            if b - a > 1:
                gaps.append((denom, prev, cur, b - a - 1))
    lines.append(f"{pairs} consecutive pair(s) checkable")
    if not pairs:
        return "NO DATA", lines + ["no two bills of one denomination: nothing to check"]
    if not gaps:
        lines.append("every denomination's count steps by exactly one: no bill "
                     "event went missing")
        return "PASS", lines
    lines.append(f"{len(gaps)} gap(s), {sum(g[3] for g in gaps)} bill event(s) missing:")
    for denom, prev, cur, missing in gaps[:10]:
        lines.append(f"    denom code {denom}: count jumped "
                     f"{prev['fields']['bills_of_this_type']} -> "
                     f"{cur['fields']['bills_of_this_type']} at {cur['at']} "
                     f"({missing} missing)")
    return "FAIL", lines


def _wonlost_trend(conn, window_start, window_end, per_cycle, have):
    """Is (won + lost) - played DRIFTING, or oscillating around a constant?

    Two endpoint readings cannot tell sampling skew from a real third
    category, and on 2026-11-07 that was the whole open question: the gap
    read +3 on one bundle and +4 twenty minutes later. Guessing from two
    samples is not a measurement.

    Every meters_history row already holds all three meters, so the gap can
    be computed per reading for nothing. Skew oscillates around a constant
    because each cycle reads the three meters a few seconds apart; a real
    third category -- a push, or a meter this model does not maintain --
    accumulates, so the gap climbs with the games played.

    Reported as first-third against last-third means, which is blunt and
    robust. A trend smaller than one cycle's play is not a trend.
    """
    cols = {c for c in ("sm_games_won", "c7_games_won", "lp1c_games_won",
                        "sm_games_lost", "c7_games_lost", "games_played")
            if c in have}
    won = [c for c in ("sm_games_won", "c7_games_won", "lp1c_games_won") if c in cols]
    lost = [c for c in ("sm_games_lost", "c7_games_lost") if c in cols]
    if not won or not lost or "games_played" not in cols:
        return []
    w = won[0] if len(won) == 1 else "coalesce(%s)" % ",".join(won)
    l = lost[0] if len(lost) == 1 else "coalesce(%s)" % ",".join(lost)
    try:
        rows = conn.execute(
            f"SELECT ({w}) + ({l}) - games_played FROM meters_history "
            f"WHERE polled_at >= ? AND polled_at <= ? AND {w} IS NOT NULL "
            f"AND {l} IS NOT NULL AND games_played IS NOT NULL ORDER BY id",
            (window_start, window_end)).fetchall()
    except sqlite3.Error:
        return []
    gaps = [r[0] for r in rows if r[0] is not None]
    if len(gaps) < 6:
        return ["      (too few readings to say whether that gap drifts)"]
    third = len(gaps) // 3
    early = sum(gaps[:third]) / third
    late = sum(gaps[-third:]) / third
    drift = late - early
    out = [f"      the gap across {len(gaps)} readings: min {min(gaps)}, "
           f"max {max(gaps)}, first third {early:+.1f}, last third "
           f"{late:+.1f}"]
    if abs(drift) <= per_cycle:
        out.append(f"      NOT DRIFTING ({drift:+.1f} across the window, "
                   f"against {per_cycle} for one cycle's play): it "
                   f"oscillates around a constant, which is sampling skew. "
                   f"Three meters, three instants, same sweep.")
    else:
        out.append(f"      ** DRIFTING {drift:+.1f} across the window, more "
                   f"than the {per_cycle} one cycle's play explains. That is "
                   f"a real third category accumulating, not skew: a push, "
                   f"or a meter this model does not maintain. One bench "
                   f"check settles which.")
    return out


def check_meters(conn, events, since, until,
                 boundaries: list[str] | None = None) -> tuple[str, list[str]]:
    """Detector 4: the meters must agree with the events over the window."""
    wanted = ["total_coin_in", "games_played", "c7_total_won_credits",
              "sm_total_dollar_value_of_bills",
              # The operator's end-side anchor, 2026-11-07: "game end can be
              # sorted with the sum of games won and games lost". Every one
              # of these is already polled every cycle -- LP 0x16/0x17 in the
              # single-meter sweep, 0x06/0x07 in Table C-7, LP 0x1C for the
              # won count -- so the check below costs no new wire traffic.
              "sm_games_won", "sm_games_lost",
              "c7_games_won", "c7_games_lost", "lp1c_games_won"]
    first, last, cols = _meter_window(conn, since, until, wanted)
    if first is None:
        return "NO DATA", ["no meters_history rows in this window"]

    # ONLY events the meter delta could possibly account for.
    #
    # A meter delta measured from the first history row cannot explain an
    # event that arrived before that row was written. Those are a backlog --
    # the queue the machine had filled before this run began, plus anything
    # the startup work shook loose -- and counting them here invents losses
    # that never happened, with the sign REVERSED: the events exceed the
    # meters, which is the tell.
    #
    # Measured on the lab cabinet: 24 game starts against a meter delta of 11
    # games, reported as a failure. The 13 extra were a pre-run backlog, and
    # the giveaway was that the wager per game came out identical on both
    # sides of the line -- 3,000 credits in the window and 3,000 in the
    # backlog. Nothing had been lost at all.
    window_start, window_end = first["polled_at"], last["polled_at"]

    # Start the window at the LAST gateway restart inside it. Meters are
    # cumulative and keep climbing while this program is stopped, so a
    # window spanning a restart compares a meter delta that includes the
    # downtime against an event stream that cannot. The overnight run of
    # 2026-10-28 reported 874 phantom missing games that way, every one of
    # them played while the gateway was being debugged.
    restarts = [b for b in (boundaries or [])
                if window_start < b <= window_end]
    if restarts:
        window_start = max(restarts)
        lines_prefix = [
            f"window moved to the last gateway restart at {window_start}: "
            f"{len(restarts)} restart(s) in this database, and a meter delta "
            f"spanning one counts play this program was not running for"]
    else:
        lines_prefix = []

    def in_window(e):
        return e["source"] != "startup-backlog" and window_start < e["at"] <= window_end

    excluded = [e for e in events if not in_window(e)
                and e["code"] in (GAME_START, GAME_END)]
    # EVERY game start, before the timestamp filter runs.
    #
    # The meter-anchored check below exists precisely to escape this
    # filter, so feeding it the filtered list defeats the whole thing --
    # which is what the first version did, and a test written for the
    # smeared case caught it immediately: four games arriving after the
    # last meter reading were dropped here and then reported as "400
    # credits SHORT" by the check meant to prove nothing was missing.
    #
    # No backlog exclusion is needed either: a startup-backlog event
    # belongs to a game played before this run, so its coin-in is at or
    # below C_first and the meter range leaves it out by itself. The
    # meter value decides, which is the point.
    all_starts = [e for e in events if e["code"] == GAME_START]
    events = [e for e in events if in_window(e)]

    starts = [e for e in events if e["code"] == GAME_START]
    ends = [e for e in events if e["code"] == GAME_END]
    wagers = [e["fields"]["wager_cents"] for e in starts
              if "wager_cents" in e["fields"]]
    wins = [e["fields"]["game_win"] for e in ends if "game_win" in e["fields"]]

    first, last, cols = _meter_window(conn, window_start, window_end, wanted)
    if first is None:
        return "NO DATA", lines_prefix + [
            "no meters_history rows after the last gateway restart"]
    window_start, window_end = first["polled_at"], last["polled_at"]
    lines = lines_prefix + [f"meter window {window_start} .. {window_end}"]
    if excluded:
        lines.append(f"{len(excluded)} game event(s) excluded as outside it "
                     f"(pre-run backlog or the opening cycle) -- a meter delta "
                     f"starting here cannot account for them")
    verdict = "PASS"
    checks = 0

    # One game of slack at each edge, and no more.
    #
    # A cycle reads its general poll BEFORE its meters and drains interleaved
    # events AFTER, so a game landing inside a cycle can put its event on one
    # side of the window boundary and its meter movement on the other. That
    # is worth at most one game at the start and one at the end.
    #
    # Reporting that as FAIL cries wolf on a clean run -- the lab's first
    # corrected run came out meters 11 against events 10, exactly one game
    # apart. Reporting it as PASS would hide a real single lost event. So it
    # is neither: INCONCLUSIVE, with the instruction to narrow the window,
    # which is the only thing that actually settles it.
    edge_slack = {"games played": 1}

    def compare(label, delta, from_events, note="", per_game=None):
        nonlocal verdict, checks
        if delta is None:
            lines.append(f"    {label}: meter not in this database")
            return
        checks += 1
        diff = abs(delta - from_events)
        slack = edge_slack.get(label)
        if slack is None and per_game:
            slack = per_game  # one game's worth, in this meter's units
        if diff == 0:
            mark = "OK "
        elif slack is not None and diff <= slack:
            mark = "~? "
            if verdict == "PASS":
                verdict = "INCONCLUSIVE"
        else:
            mark = "** "
            verdict = "FAIL"
            failed_labels.append(label)
        lines.append(f"    {mark}{label}: meters moved {delta}, events account "
                     f"for {from_events}{note}")

    failed_labels: list[str] = []

    def delta(col):
        if col not in cols or first[col] is None or last[col] is None:
            return None
        return last[col] - first[col]

    # Games played against game-start events. The meter counts a game when it
    # STARTS, which is the event being counted, so these are the same thing.
    # One game's wager, for the edge slack on the amount meters. Taken from
    # the events themselves rather than assumed, so a mixed-denomination run
    # gets the largest single wager as its tolerance rather than an average.
    biggest_wager = max(wagers) if wagers else None
    biggest_win = max(wins) if wins else None

    compare("games played", delta("games_played"), len(starts))
    compare("coin in", delta("total_coin_in"), sum(wagers),
            f" (from {len(wagers)} wager-bearing starts)",
            per_game=biggest_wager)
    compare("total won", delta("c7_total_won_credits"), sum(wins),
            f" (from {len(wins)} win-bearing ends)",
            per_game=biggest_win)

    # ---------------------------------------------------------------- #
    # THE METER-ANCHORED WINDOW: the same question with no boundary left
    # to be fuzzy about.
    #
    # Everything above selects events by TIMESTAMP -- "which events
    # arrived between these two meter readings". But an event's arrival
    # time is not when its game happened. Delivery lag, the startup drain
    # and §12.3's interleaving all smear the edge, and that smear is what
    # the lab run reported for eighteen hours as "+18 games, -790 credits"
    # -- numbers frozen since the five-hour mark, because a fixed edge
    # effect does not grow while a real loss does.
    #
    # §12.5.3 removes the boundary entirely. Every game start carries
    # total_coin_in AFTER ITS OWN WAGER: the meter value that game caused.
    # So instead of asking which events arrived in a time window, ask
    # which games fall in a METER window:
    #
    #     C_first < total_coin_in <= C_last
    #
    # That set telescopes. Sum its wagers and you get C_last - C_first
    # exactly, by construction, because each game's coin-in IS the running
    # total. There is no edge to straddle: the event carries its own
    # position on the meter.
    #
    # It can therefore only come out SHORT, and only by the wager of a
    # game whose event is missing. A non-zero result here is a finding,
    # not a boundary artefact, and that is the whole point.
    #
    # WHAT IT COSTS, stated because it is a real loss and not a detail:
    # anchoring the window to the events' own coin-in values gives up
    # detector 4's INDEPENDENCE. The timestamp comparison above comes from
    # two unrelated sources of truth; this one does not -- it is closer to
    # a restatement of detector 2. So both are printed. Where the
    # timestamp rows differ and the anchored rows are zero, the difference
    # is a boundary artefact and can be dismissed by arithmetic instead of
    # by argument, which is what this project has been doing by hand.
    anchored_exact = False
    c_first = first.get("total_coin_in") if cols else None
    c_last = last.get("total_coin_in") if cols else None
    if c_first is None or c_last is None or c_last < c_first:
        lines.append("    (no meter-anchored check: total_coin_in is not "
                     "readable at both ends of this window)")
    else:
        anchored = [e for e in all_starts
                    if e["fields"].get("total_coin_in") is not None
                    and c_first < e["fields"]["total_coin_in"] <= c_last]
        anchored_wagers = [e["fields"]["wager_cents"] for e in anchored
                           if e["fields"].get("wager_cents") is not None]
        lines.append("")
        lines.append(f"    METER-ANCHORED (§12.5.3: each game start carries "
                     f"the coin-in it caused, so this window has no fuzzy "
                     f"edge) — coin in {c_first} -> {c_last}")
        if len(anchored_wagers) != len(anchored):
            lines.append(f"      {len(anchored) - len(anchored_wagers)} start(s) "
                         f"in range carry no resolvable denomination and are "
                         f"not summed; the identity below is short by their "
                         f"wagers for that reason alone")
        short = (c_last - c_first) - sum(anchored_wagers)
        anchored_exact = short == 0
        if short == 0:
            lines.append(f"      EXACT: {len(anchored)} game(s) in the meter "
                         f"range, wagers sum to {sum(anchored_wagers)} = the "
                         f"coin-in delta. Nothing is missing from the wager "
                         f"side -- not 'within a boundary', nothing.")
        else:
            lines.append(f"      ** {short} credits SHORT: {len(anchored)} "
                         f"game(s) in range sum to {sum(anchored_wagers)} "
                         f"against a coin-in delta of {c_last - c_first}. "
                         f"This window has no edge to blame, so that is a "
                         f"game start that did not arrive. Detector 2 names "
                         f"where.")
            verdict = "FAIL"
        gp = delta("games_played")
        if gp is not None:
            off = gp - len(anchored)
            if abs(off) > 1:
                # Two or more is not a boundary. ONE is exactly a boundary:
                # the meter is read at a single instant and the game that
                # straddles it counts on one side only. The first version
                # cleared the exact flag on any difference at all, so the
                # lab run read FAIL while its own anchored identity closed
                # to the credit -- the precise noise this was built to end.
                anchored_exact = False
            if off:
                lines.append(f"      games played: meter moved {gp}, "
                             f"{len(anchored)} game(s) in the meter range "
                             f"({off:+}). Worth one bench check: whether this "
                             f"cabinet increments games_played at game START "
                             f"or at game END. If at END, the last game of "
                             f"the window counts on one side only.")
            else:
                lines.append(f"      games played: meter moved {gp}, and "
                             f"{len(anchored)} game(s) fall in the meter "
                             f"range. Exact.")

    # A PROVEN-EXACT WAGER SIDE OUTRANKS A SMEARED ONE.
    #
    # The anchored identity can only come out short, and only by a missing
    # game start. Where it is exact, no game start is missing -- so the
    # timestamp rows above disagreeing is not evidence of loss, it is the
    # boundary being fuzzy, and reporting FAIL on their account is the
    # noise the operator asked to be rid of. The award side is NOT covered
    # by the anchored check (§12.5.4 carries no cumulative win meter), so
    # a failing `total won` still fails the detector.
    # ---------------------------------------------------------------- #
    # THE END-SIDE COUNT: games won + games lost, the operator's idea.
    #
    # "Game start can be sorted by games played. Game end can be sorted
    # with the sum of games won and game lost."
    #
    # The sorting half cannot work and the counting half is worth having,
    # and the difference is where the number lives. §12.5.3 puts coin-in
    # INSIDE the game-start event, which is the only reason that anchor
    # positions individual events. games_played is a meter and nothing
    # else -- it is not in the event, so there is nothing in a game start
    # to sort by it.
    #
    # As a COUNT it is a different matter, and it fills the gap §19 named.
    # Detectors 2 and 6 audit starts. Detector 5 audits award CREDITS.
    # Nothing has ever counted game ENDS -- and ends are the half that
    # survives more often, so the unaudited side was the one the evidence
    # pointed at.
    #
    # Cost: nothing. games_played is LP 0x0F, every cycle. Games won and
    # lost are LP 0x16/0x17 in the single-meter sweep and 0x06/0x07 in
    # Table C-7, both already running.
    #
    # The identity is CHECKED, not assumed. If won + lost does not equal
    # played on this cabinet -- a push, a zero-win game counted neither
    # way, or a model that simply does not maintain 0x06/0x07 -- the
    # comparison is useless and says so instead of inventing a finding.
    won_d = next((d for d in (delta("sm_games_won"), delta("c7_games_won"),
                              delta("lp1c_games_won")) if d is not None), None)
    lost_d = next((d for d in (delta("sm_games_lost"), delta("c7_games_lost"))
                   if d is not None), None)
    played_d = delta("games_played")
    lines.append("")
    if won_d is None or lost_d is None:
        lines.append("    END-SIDE COUNT: this database has no games-won/"
                     "games-lost pair, so game ENDS have no count anchor. "
                     "They are the half nothing else audits.")
    else:
        # ONE CYCLE OF SAMPLING SKEW, because these are three meters read at
        # three different moments of the same sweep -- games played from LP
        # 0x0F, won and lost from LP 0x16/0x17 tens of seconds later. The
        # first version demanded they agree exactly and declared the cabinet
        # broken when they did not. That is the SAME bug this file had just
        # fixed in detector 5's per-meter window, rebuilt one function away.
        #
        # Measured on the lab cabinet: won 4,753 + lost 23,271 = 28,024
        # against played 28,021 -- three games on twenty-eight thousand,
        # which is about seven seconds of play.
        n_rows = conn.execute(
            "SELECT count(*) FROM meters_history WHERE polled_at >= ? "
            "AND polled_at <= ?", (window_start, window_end)).fetchone()[0]
        per_cycle = max(1, (played_d or 0) // max(1, n_rows - 1))
        gap = (won_d + lost_d - played_d) if played_d is not None else 0
        # THE TREND RUNS IN BOTH BRANCHES, because it is the better
        # discriminator and the branch above is only a size test. A gap
        # bigger than one cycle still deserves the question "is it
        # accumulating or did it just start the window offset?", and a gap
        # smaller than one cycle can still be climbing.
        trend = _wonlost_trend(
            conn, window_start, window_end, per_cycle,
            {r[1] for r in conn.execute("PRAGMA table_info(meters_history)")})
        if played_d is not None and abs(gap) > per_cycle:
            lines.append(
                f"    END-SIDE COUNT UNUSABLE on this cabinet: games won "
                f"{won_d} + games lost {lost_d} = {won_d + lost_d}, against "
                f"games played {played_d} — {gap:+}, which is more than the "
                f"{per_cycle} games a cycle's sampling skew can explain. "
                f"Something is a third category here: a push, or a meter "
                f"this model does not maintain. One bench check settles it.")
            lines.extend(trend)
        else:
            if gap:
                lines.append(
                    f"    won {won_d} + lost {lost_d} = {won_d + lost_d} "
                    f"against played {played_d} ({gap:+}). Inside one "
                    f"cycle's worth of play ({per_cycle} games), which is "
                    f"what three meters read at three moments of the same "
                    f"sweep look like.")
                lines.extend(trend)
            compare("game ends (won + lost)", won_d + lost_d, len(ends),
                    f" (games won {won_d} + lost {lost_d})",
                    per_game=per_cycle)

    if anchored_exact and failed_labels and all(
            lbl in ("games played", "coin in") for lbl in failed_labels):
        verdict = "PASS"
        lines.append("")
        lines.append(f"    VERDICT FROM THE ANCHORED WINDOW, not the smeared "
                     f"one: {', '.join(failed_labels)} differ only because "
                     f"events are selected by arrival time there. The meter "
                     f"range has no edge, and it closes exactly, so nothing "
                     f"is missing from the wager side.")

    if not checks:
        return "NO DATA", lines
    if verdict == "PASS":
        lines.append("    the meters and the event stream agree")
    elif verdict == "INCONCLUSIVE":
        lines.append("    within one game of agreement, which is exactly the "
                     "ambiguity a window edge creates: a cycle reads its general "
                     "poll before its meters and drains interleaved events after, "
                     "so one game can land on opposite sides of the boundary.")
        lines.append("    This is not evidence either way. Narrow the window with "
                     "--since/--until to a period with quiet minutes at both ends "
                     "and the ambiguity disappears.")
    else:
        lines.append("    READ THE SIGN BEFORE CONCLUDING. If the EVENTS exceed "
                     "the meters, nothing was lost -- events are being counted "
                     "that the window's meter delta never covered, which is a "
                     "windowing problem, not a protocol one. Only the meters "
                     "exceeding the events is consistent with a lost event.")
        lines.append("    Either way a single game straddling an edge moves a "
                     "meter whose event falls on the other side, so a difference "
                     "of one game is noise. Narrow it with --since/--until.")
    return verdict, lines


# --- Detector 5: the payout-side money identity --------------------------

LEGACY_BONUS = 0x7C

# Every meter that can legitimately receive a credit the game awarded, plus
# the two cash-out meters that must NOT be added to the win side. Which of
# these a given cabinet actually uses is a per-model fact, not a constant:
# section 5b of the handover measured three Novomatic meter codes reporting
# one quantity. So this detector MEASURES the convention rather than
# assuming one.
WIN_SIDE_METERS = (
    "total_coin_out",       # LP 0x0F — credits paid to the credit meter/hopper
    "total_jackpot",        # LP 0x0F — attendant-paid jackpots
    "c7_total_won_credits",  # Table C-7 — the total award, if the sweep ran
)
CASHOUT_SIDE_METERS = (
    "total_cancelled_credits",                 # LP 0x0F
    "lp2d_total_hand_paid_cancelled_credits",  # LP 0x2D
)


def _meter_span(conn, since, until, columns):
    """Per column, the first and last NON-NULL value in the window.

    `_meter_window` takes the first and last ROW, which is wrong for any
    meter that is only populated on some cycles. `c7_*` refreshes solely on
    a Table C-7 sweep and routine runs throttle that, so the newest row
    almost always has NULL there — and a NULL endpoint reads as "this
    database does not have the meter", which is a different and much more
    comforting statement than "the sweep did not run this cycle".
    """
    have = {row[1] for row in conn.execute("PRAGMA table_info(meters_history)")}
    cols = [c for c in columns if c in have]
    if not cols:
        return {}, []
    where, params = [], []
    if since:
        where.append("polled_at >= ?")
        params.append(since)
    if until:
        where.append("polled_at <= ?")
        params.append(until)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    out = {}
    for c in cols:
        row = conn.execute(
            f"SELECT {c}, polled_at FROM meters_history{clause}"
            f"{' AND' if clause else ' WHERE'} {c} IS NOT NULL "
            f"ORDER BY id LIMIT 1", params).fetchone()
        if row is None:
            out[c] = None
            continue
        last = conn.execute(
            f"SELECT {c}, polled_at FROM meters_history{clause}"
            f"{' AND' if clause else ' WHERE'} {c} IS NOT NULL "
            f"ORDER BY id DESC LIMIT 1", params).fetchone()
        n = conn.execute(
            f"SELECT count(*) FROM meters_history{clause}"
            f"{' AND' if clause else ' WHERE'} {c} IS NOT NULL", params).fetchone()[0]
        out[c] = {"first": row[0], "first_at": row[1],
                  "last": last[0], "last_at": last[1], "rows": n,
                  "delta": last[0] - row[0]}
    return out, cols


# Money crossing the credit meter's boundary. An interval where any of these
# moved is reported separately rather than accused: the operator's own
# clause -- "if there's any discontinuation, it may be money in or out or
# event loss" -- and the three cannot be told apart from the gauge alone.
#
# total_cancelled_credits is deliberately the ONLY cash-out term. It already
# covers a ticket-out and a handpay both, so adding ticket_out_*_cents or
# lp2d_* beside it would subtract the same money twice.
LEDGER_MONEY_METERS = (
    "sm_total_dollar_value_of_bills",   # money IN: bills
    "ticket_in_cashable_cents",         # money IN: tickets
    "ticket_in_restricted_cents",
    "total_cancelled_credits",          # money OUT: ticket-out AND handpay
)


def _credit_samples(conn, since, until):
    """The poller's --credit-poll-every rows, or [] if it was not running.

    A missing table is the ordinary case, not an error: the flag defaults
    to off and most databases predate it.
    """
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='credit_samples'").fetchone()
    except sqlite3.Error:
        return []
    if not rows:
        return []
    where, params = [], []
    if since:
        where.append("sampled_at >= ?")
        params.append(since)
    if until:
        where.append("sampled_at <= ?")
        params.append(until)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        return conn.execute(
            f"SELECT sampled_at, credits FROM credit_samples{clause} "
            f"ORDER BY id", params).fetchall()
    except sqlite3.Error:
        return []


def _with_bracketing_money(fine, coarse, n_money):
    """Give each fine reading the money meters in force around it.

    credit_samples carries the gauge and nothing else, on purpose: the
    sampler runs in the one instant a poll costs no event, and reading four
    more meters there would spend what that instant is for. So the money
    check borrows meters_history, which is sampled per cycle.

    Each fine reading inherits the LAST cycle reading at or before it. Two
    consecutive fine readings then disagree on money exactly when a cycle
    boundary between them saw a meter move -- and the interval is skipped.
    That is conservative in the right direction: a cash-out is never scored
    as a lost event, and at worst a clean interval beside one is skipped
    with it.
    """
    out, i = [], 0
    for at, credits in fine:
        while i + 1 < len(coarse) and coarse[i + 1][0] <= at:
            i += 1
        money = list(coarse[i][2:]) if coarse else [None] * n_money
        out.append(tuple([at, credits] + money))
    return out


def _indexed(pairs):
    """Time-sorted values with prefix sums, for O(log n) interval queries.

    THE FIRST VERSION OF THIS WAS QUADRATIC AND IT MATTERED. The ledger
    scans one interval per credit reading and asked "which events fall in
    it?" with a list comprehension over every event. On the whole-database
    run that is ~27,000 intervals against ~114,000 events -- 3.1 billion
    string comparisons, twice -- and it did not finish. The operator's
    analysis bundle sat there after the report had already pushed, with
    nothing to say why.

    A detector nobody can afford to run is a detector that does not exist,
    and the per-cycle version of this (one reading per cycle) hid the cost
    until `--credit-poll-every` multiplied the interval count.
    """
    seq = sorted((at, v) for at, v in pairs if v is not None)
    ats = [at for at, _ in seq]
    vals = [v for _, v in seq]
    pref = [0]
    for v in vals:
        pref.append(pref[-1] + v)
    return ats, vals, pref


def _total_between(idx, t0, t1):
    """Sum, count, and the edge values of (t0, t1]. O(log n)."""
    ats, vals, pref = idx
    lo = bisect.bisect_right(ats, t0)
    hi = bisect.bisect_right(ats, t1)
    if hi <= lo:
        return 0, 0, []
    edges = [vals[lo]] if hi - lo == 1 else [vals[lo], vals[hi - 1]]
    return pref[hi] - pref[lo], hi - lo, edges


def check_credit_ledger(conn, events, since, until, boundaries=None):
    """Detector 7: the credit meter as a running ledger. The operator's idea.

    His words, 2026-11-07:

        "Credit - bet + won cannot be equal to initial credit amount
        unless bet = won. Initial credit amount - 1st bet + 1st won = next
        credit amount... If there's any discontinuation, it may be money
        in or out or event loss."

    ONE CORRECTION TO THE PREMISE, and it makes the check stronger rather
    than weaker. **RTE does not carry the credit amount.** §12.5.3's game
    start is 8 bytes -- credits wagered, total coin in, wager type,
    progressive group -- and §12.5.4's game end is 4: the win. Neither
    holds the balance. The balance is a POLL: LP 0x1A, or Table C-7 code
    0x0C, landing in meters_history as sm_current_credits /
    c7_current_credits.

    That the anchor is independent of the event stream is exactly what
    makes this worth having. Detector 2 audits the event stream against
    ITSELF (§12.5.3's coin-in meter travels inside the event), so a fault
    that corrupts the stream uniformly is invisible to it. This audits the
    stream against a gauge the stream does not write.

    WHAT IT CATCHES THAT NOTHING ELSE DOES:

      * GAME ENDS. Detectors 2 and 6 only read game STARTS. Detector 5
        reads ends, but only as one aggregate against the payout meters
        over a whole run. Nothing until now checked an individual game end,
        and §19 measures ends as the half that survives more often -- so
        the unaudited side was the one the evidence pointed at.
      * BOTH SIGNS AT ONCE. A lost start and a duplicated end push the
        residual the same way; a duplicated start and a lost end push it
        the other. One number, four faults, and the sign halves them.

    THE INTERVAL IS THE POINT. Each consecutive pair of credit readings is
    its own independent check -- roughly 340 of them in an overnight run --
    so a single bad interval localises the fault to about a minute instead
    of to a night.

    DISPLACEMENT VERSUS LOSS, which is the discriminator and the same shape
    as detector 6's swap rule: a game whose events land on one side of a
    reading boundary and whose credits land on the other produces equal and
    opposite residuals in adjacent intervals, so the SIGNED TOTAL stays
    near zero. A genuine loss does not cancel -- it accumulates. The
    per-interval count alone cannot tell them apart and the signed total
    can.

    NOT YET INCLUDED, deliberately: intervals where money crossed the
    boundary are counted and reported, not scored. Folding bills and
    tickets into the identity needs the unit convention of each of those
    meters pinned on hardware (bills are metered as a dollar value, tickets
    in cents, the gauge in accounting units), and guessing a conversion
    here would turn a clean detector into a confident wrong answer.
    """
    have = {row[1] for row in conn.execute("PRAGMA table_info(meters_history)")}
    if not ({"c7_current_credits", "sm_current_credits"} & have):
        return "NO DATA", ["this database has no current-credits column: "
                           "neither c7_current_credits nor sm_current_credits. "
                           "The ledger needs the gauge and the gauge is a poll, "
                           "not an event."]

    # PREFER credit_samples WHEN THE POLLER WAS WRITING IT.
    #
    # meters_history carries the gauge once per cycle -- about 25 games, far
    # coarser than a one-game fault, which is why the signed total rather
    # than any single interval is what this detector leans on. The poller's
    # --credit-poll-every writes credit_samples instead: LP 0x1A on an EMPTY
    # queue only, so the reading costs no event, and at a few seconds apart
    # it brings the interval down to roughly one game. That is the operator's
    # per-game chain, exactly as he wrote it.
    #
    # The money check still comes from meters_history, because credit_samples
    # deliberately carries nothing but the gauge: an interval is skipped when
    # the money meters BRACKETING it disagree, which is conservative in the
    # right direction. A cash-out inside an interval is never scored as a
    # lost event.
    fine = _credit_samples(conn, since, until)

    # ONE GAUGE, NEVER COALESCED -- and the first real run is what taught
    # this. c7_current_credits is read during the Table C-7 sweep;
    # sm_current_credits during the single-meter sweep. They are the same
    # quantity read by different long polls at DIFFERENT MOMENTS of the
    # same cycle, tens of seconds apart. coalesce() therefore produced a
    # sequence of readings whose boundaries jumped back and forth inside
    # each cycle, so the events "between" two readings were not the events
    # whose credits moved between them.
    #
    # Measured 2026-11-07, first run against real data: 246 of 369
    # intervals did not close, split almost evenly 130 positive / 116
    # negative, with individual residuals of 50,000 credits in intervals
    # that only held a few thousand credits of play. That is not event
    # loss -- it is two clocks being read as one.
    #
    # So: pick the column with the most readings and use only that one.
    counts = {}
    for c in ("c7_current_credits", "sm_current_credits"):
        if c not in have:
            continue
        try:
            counts[c] = conn.execute(
                f"SELECT count(*) FROM meters_history WHERE {c} IS NOT NULL"
            ).fetchone()[0]
        except sqlite3.Error:
            counts[c] = 0
    if not any(counts.values()):
        return "NO DATA", ["the current-credits column exists but is empty: "
                           "the sweep that fills it was skipped for this run"]
    gauge = max(counts, key=lambda c: counts[c])
    gauge_note = (f"gauge {gauge} ({counts[gauge]} readings)"
                  + (f"; {', '.join(f'{c} has {n}' for c, n in counts.items() if c != gauge)}"
                     " NOT mixed in -- different long polls read at different"
                     " moments of the same cycle" if len(counts) > 1 else ""))
    money = [c for c in LEDGER_MONEY_METERS if c in have]
    where, params = ["%s IS NOT NULL" % gauge], []
    if since:
        where.append("polled_at >= ?")
        params.append(since)
    if until:
        where.append("polled_at <= ?")
        params.append(until)
    sel = ", ".join(["polled_at", f"{gauge} AS credits"] + money)
    coarse = conn.execute(
        f"SELECT {sel} FROM meters_history WHERE {' AND '.join(where)} "
        f"ORDER BY id", params).fetchall()

    if fine and len(fine) > len(coarse):
        rows = _with_bracketing_money(fine, coarse, len(money))
        source_note = (f"{len(fine)} reading(s) from credit_samples "
                       f"(--credit-poll-every), against {len(coarse)} cycle "
                       f"reading(s) in meters_history -- "
                       f"{len(fine) / max(1, len(coarse)):.0f}x the resolution")
    else:
        rows = coarse
        source_note = (f"{gauge_note}; {len(coarse)} reading(s) from "
                       f"meters_history, one per "
                       f"cycle. Run the poller with --credit-poll-every to "
                       f"sample between games instead; at one reading per "
                       f"cycle an interval holds ~25 games and its tolerance "
                       f"is wider than a one-game fault.")

    lines = [f"{len(rows)} credit-meter reading(s) in the window",
             f"    {source_note}"]
    if len(rows) < 2:
        return "NO DATA", lines + [
            "fewer than two: a ledger needs a before and an after. The gauge "
            "is only written on a single-meter or Table C-7 sweep, so a run "
            "with both throttled off has nothing to anchor to."]

    starts = _indexed((e["at"], e["fields"].get("wager_cents")) for e in events
                      if e["code"] == GAME_START)
    ends = _indexed((e["at"], e["fields"].get("game_win")) for e in events
                    if e["code"] == GAME_END)
    boundaries = boundaries or []

    checked = skipped_money = skipped_restart = 0
    residuals: list[tuple[str, str, int, int, int, int]] = []
    off: list[tuple[str, str, int, int, int, int]] = []
    for (t0, c0, *m0), (t1, c1, *m1) in zip(rows, rows[1:]):
        if _straddles(boundaries, t0, t1) is not None:
            skipped_restart += 1
            continue
        if any(a is not None and b is not None and a != b for a, b in zip(m0, m1)):
            skipped_money += 1
            continue
        wagered, nw, wl = _total_between(starts, t0, t1)
        won, nv, vl = _total_between(ends, t0, t1)
        if not nw and not nv:
            continue
        checked += 1
        residual = c1 - (c0 - wagered + won)
        # At most one game can straddle each edge, so the displacement a
        # boundary can create is bounded by the first and last games in the
        # interval -- not by the largest, which would make the biggest win
        # on the cabinet permanently unreportable.
        edge = 0
        for lst in (wl, vl):
            if lst:
                edge += abs(lst[0]) + (abs(lst[-1]) if len(lst) > 1 else 0)
        row = (t0, t1, residual, wagered, won, edge)
        residuals.append(row)
        if abs(residual) > edge:
            off.append(row)

    lines.append(f"{checked} interval(s) scored"
                 + (f", {skipped_money} skipped because money crossed the "
                    f"credit meter (bills, tickets or a cash-out)"
                    if skipped_money else "")
                 + (f", {skipped_restart} skipped for spanning a gateway "
                    f"restart" if skipped_restart else ""))
    if not checked:
        return "NO DATA", lines + [
            "no interval had a game in it: nothing to reconcile"]

    signed = sum(r[2] for r in residuals)

    # TWO BOUNDS, BECAUSE ONE OF THEM WAS DOING NO WORK.
    #
    # `loose` is the largest single interval's tolerance. The first version
    # tested the signed total against that alone, and on the first real run
    # it was 202,550 -- a bound almost nothing could exceed, so "the
    # residuals CANCEL" was being printed without having been earned.
    #
    # `tight` is what pure displacement actually allows. A game that lands
    # on the wrong side of a reading leaves one interval and enters its
    # neighbour, so the residuals come in exactly cancelling pairs and the
    # sum TELESCOPES: only the window's two outer edges have no partner.
    #
    # Between the two is honest uncertainty, not a verdict. The gauge is
    # read at one instant in the cycle while events are timestamped as they
    # are drained, and that skew can be most of a sweep -- so the real
    # boundary slop per interval is more than one game and less than the
    # worst interval in the run. Nobody has measured where in that range it
    # sits, so a total landing there is reported as unresolved rather than
    # as either answer.
    loose = max(r[5] for r in residuals)
    tight = residuals[0][5] + (residuals[-1][5] if len(residuals) > 1 else 0)
    worst_edge = loose

    # THE SIGNED TOTAL IS THE PRIMARY SIGNAL, not the per-interval count,
    # and the first draft of this had it the other way round.
    #
    # A boundary can displace one game start and one game end at each edge,
    # so an interval's tolerance is two games' worth -- which is larger than
    # a one-game fault. A single interval therefore CANNOT resolve a single
    # lost or repeated game, and no tightening of the bound fixes that: a
    # displaced start moves the residual by its whole wager, so the bound is
    # correct and simply coarse.
    #
    # What separates the two is accumulation. Displacement moves a game from
    # one interval into its neighbour, so the residuals are equal and
    # opposite and the SUM stays inside one boundary's worth however many
    # intervals there are. A loss leaves the same sign in every interval it
    # touches, so the sum grows with them. Over the ~340 intervals of an
    # overnight run that turns a fault too small for any single interval to
    # see into one the total cannot hide.
    # A boundary model that fails on most intervals is not measuring loss,
    # it is mis-modelling the boundary. Say that instead of totalling it up.
    misfit = off and len(off) > len(residuals) // 3
    if abs(signed) <= worst_edge:
        if not off:
            lines.append(
                f"every interval closes: credits before - wagers + wins = "
                f"credits after, within what a boundary can displace")
            lines.append(
                f"    and the signed total over {checked} interval(s) is "
                f"{signed:+}, inside the {worst_edge} a single boundary can "
                f"move -- so nothing accumulated either")
            return "PASS", lines

    pos = [r for r in off if r[2] > 0]
    neg = [r for r in off if r[2] < 0]
    if off:
        lines.append(
            f"** {len(off)} of {checked} interval(s) do not close: "
            f"{len(pos)} with MORE credits than the events explain, {len(neg)} "
            f"with fewer")
    else:
        drift = [r for r in residuals if r[2]]
        lines.append(
            f"** every interval is individually within what a boundary can "
            f"displace, AND THE RESIDUALS ACCUMULATE: {len(drift)} of "
            f"{checked} interval(s) are off in the same direction. A "
            f"one-game fault is smaller than one interval's tolerance, so "
            f"this is the shape a steady loss takes -- invisible per "
            f"interval, unmistakable in the sum.")
        off = sorted(drift, key=lambda r: -abs(r[2]))
    lines.append(f"    signed total across ALL intervals: {signed:+}")
    for t0, t1, residual, wagered, won, edge in sorted(
            off, key=lambda r: -abs(r[2]))[:8]:
        lines.append(f"    {t0[11:19]}..{t1[11:19]}: wagered {wagered}, won "
                     f"{won}, credits off by {residual:+} (boundary can "
                     f"displace {edge})")

    lines.append("    READ THE SIGN:")
    lines.append("      MORE credits than the events explain  -> wagers "
                 "over-counted (a game start arrived twice) or wins "
                 "under-counted (a game end was lost)")
    lines.append("      FEWER credits than the events explain -> wagers "
                 "under-counted (a game start was lost) or wins over-counted "
                 "(a game end arrived twice)")
    lines.append("    Cross-read it with detector 2 (starts missing) and "
                 "detector 6 (starts repeated): whichever of those is silent "
                 "names the END as the side at fault, which nothing else on "
                 "this project can do.")

    # Displacement cancels, loss accumulates. Same discriminator as the swap
    # rule in detector 6, applied to a different quantity.
    if misfit:
        lines.append(
            f"    ** {len(off)} of {checked} intervals failing is too many to "
            f"be lost events: a loss that frequent would show in detectors 2, "
            f"4 and 5 as well. THE BOUNDARY MODEL DOES NOT FIT THIS DATA, and "
            f"until it does the signed total means nothing. Most likely the "
            f"gauge and the events are not being read on the same clock -- "
            f"the credit meter is sampled at one instant of the cycle while "
            f"events are timestamped as they drain.")
        lines.append(
            f"    Do not act on this detector until that is settled. Detectors "
            f"2 and 6 cover game STARTS and both read clean; it is only the "
            f"END side this was built to add, and it is not yet trustworthy.")
        return "INCONCLUSIVE", lines
    if abs(signed) <= tight:
        lines.append(f"    The residuals CANCEL ({signed:+} across "
                     f"{checked} intervals, inside the {tight} that pure "
                     f"displacement allows: a game moving into its neighbour "
                     f"leaves an equal and opposite pair, so the sum "
                     f"telescopes to the window's two outer edges). "
                     f"Displacement, not loss.")
        return "INCONCLUSIVE", lines
    if abs(signed) <= loose:
        lines.append(f"    UNRESOLVED. The signed total {signed:+} is above "
                     f"the {tight} pure displacement allows and below the "
                     f"{loose} of the single widest interval. The gauge is "
                     f"read at one instant of the cycle while events are "
                     f"timestamped as they drain, and nobody has measured "
                     f"that skew -- so this is neither a clean cancellation "
                     f"nor evidence of loss.")
        lines.append("    Narrow it with --credit-poll-every on the poller: "
                     "a reading every few seconds shrinks every interval and "
                     "with it both bounds.")
        return "INCONCLUSIVE", lines
    lines.append(f"    The residuals DO NOT cancel: {signed:+} across "
                 f"{checked} intervals, above even the {loose} of the single "
                 f"widest interval. Displacement telescopes to about zero, so "
                 f"this is a real imbalance.")
    return "FAIL", lines


def check_payout_identity(conn, events, since, until,
                          boundaries=None, overflowed=False):
    """Detector 5: every credit the game AWARDED must land in a payout meter.

    Detector 4 checks the wager side (coin in against summed wagers) and the
    award side against ONE meter, `c7_total_won_credits`. That leaves the
    question this detector exists for:

        a win that is not paid to the credit meter does not increment
        `total_coin_out`.

    An attendant-paid jackpot increments `total_jackpot`. A progressive
    award may increment a progressive meter, `total_jackpot`, or be folded
    into coin out — vendor's choice. So comparing summed `game_win` against
    `total_coin_out` alone UNDER-COUNTS by exactly the jackpots, and reads
    as a suspiciously low return to player rather than as a missing meter.
    That is the failure mode this catches.

    THE ONE TRAP, stated because adding meters until the sum matches is the
    obvious wrong way to do this. `total_cancelled_credits` and
    `total_hand_paid_cancelled_credits` are CASH-OUT meters, not award
    meters. A jackpot that is awarded and then handpaid moves BOTH an award
    meter and a cash-out meter, so adding the cash-out side to the win side
    double-counts it. They are reported here, never summed into the
    identity.
    """
    lines: list[str] = []

    win_events = [(e["at"], e["fields"]["game_win"]) for e in events
                  if e["code"] == GAME_END and "game_win" in e["fields"]]
    wins = [w for _, w in win_events]
    bonuses = [e["fields"]["bonus_amount"] for e in events
               if e["code"] == LEGACY_BONUS and "bonus_amount" in e["fields"]]
    awarded = sum(wins)

    if not wins:
        return "NO DATA", ["no game-end events carried a win amount in this "
                           "window: nothing to reconcile against the payout "
                           "meters"]

    spans, _ = _meter_span(conn, since, until,
                           list(WIN_SIDE_METERS) + list(CASHOUT_SIDE_METERS))

    lines.append(f"events awarded {awarded} from {len(wins)} game-end event(s)"
                 + (f", plus {sum(bonuses)} in {len(bonuses)} legacy-bonus "
                    f"event(s) (12.5.2)" if bonuses else ""))
    if bonuses:
        lines.append("    the bonus total is reported SEPARATELY and not added: "
                     "12.5.2's bonus may or may not already be inside the "
                     "game-end win on a given cabinet, and that is a per-model "
                     "fact to measure, not to assume")
    lines.append("")
    lines.append("  payout meters over the same window:")
    for col in WIN_SIDE_METERS + CASHOUT_SIDE_METERS:
        s = spans.get(col)
        side = "win" if col in WIN_SIDE_METERS else "CASH-OUT, not a win"
        if s is None:
            lines.append(f"    --  {col}: not in this database")
        elif s["delta"] == 0 and s["rows"]:
            lines.append(f"    00  {col}: did not move ({s['rows']} readings) [{side}]")
        else:
            lines.append(f"    ++  {col}: +{s['delta']} "
                         f"({s['first']} -> {s['last']}, {s['rows']} readings) [{side}]")

    def d(col):
        s = spans.get(col)
        return None if s is None else s["delta"]

    def awarded_for(cols):
        """The wins THIS candidate's meter delta can actually cover.

        EVERY METER HAS ITS OWN WINDOW, and comparing all of them against
        one event sum is a bug that reads as event loss. `total_coin_out`
        refreshes every cycle; `c7_total_won_credits` only on a Table C-7
        sweep, which this run throttles to once a minute. So their first
        and last readings bracket spans up to a minute apart, while the
        event sum covered the whole window -- and the offset between them
        was reported as credits that went missing.

        Measured on the lab database before this fix: the same run read
        2.2% unexplained over a 45-minute window and 0.1% over 5h52m. A
        real loss grows with the window. A fixed sampling offset divided
        by a longer window shrinks, which is what that was. In seconds of
        play the two came to 54s and 23s -- both inside the 60s C-7
        cadence, which is the tell.

        A meter delta is `last - first`, so the first reading already
        contains everything up to `first_at`. The events it can account
        for are those AFTER `first_at` and up to `last_at`.
        """
        ss = [spans.get(c) for c in cols]
        if any(s is None for s in ss):
            return None
        start = max(s["first_at"] for s in ss)
        end = min(s["last_at"] for s in ss)
        inside = [w for at, w in win_events if start < at <= end]
        return {"total": sum(inside), "n": len(inside),
                "start": start, "end": end,
                # The edge tolerance belongs to THIS span's own first and
                # last games, not the window's.
                "tolerance": (inside[0] + inside[-1]) if len(inside) > 1
                             else (inside[0] if inside else 0)}

    # Which convention does THIS cabinet use? Try each candidate and report
    # the one that closes, rather than picking one and calling a mismatch a
    # fault. The order matters: the narrowest explanation that works wins.
    candidates = [
        ("total_coin_out alone (coin out is inclusive of jackpots)",
         ["total_coin_out"]),
        ("total_coin_out + total_jackpot (jackpots metered separately)",
         ["total_coin_out", "total_jackpot"]),
        ("c7_total_won_credits (Table C-7's own award total)",
         ["c7_total_won_credits"]),
    ]
    # The edge tolerance is the FIRST and LAST games' wins, not the biggest.
    #
    # A cycle reads its general poll before its meters and drains interleaved
    # events after, so a game can put its event on one side of a window
    # boundary and its meter movement on the other. But only a game AT an
    # edge can do that -- a game in the middle of the window has both inside
    # it. Using max(wins) as the tolerance, which is what detector 4 does for
    # its per-game slack, would mean the single largest win on the cabinet
    # could never be reported missing: exactly the one that matters most, and
    # exactly the size of a jackpot or progressive hit.
    tolerance = (wins[0] + wins[-1]) if len(wins) > 1 else wins[0]
    # A losing spin at the boundary awards nothing, so the first and last
    # wins can both be 0 and the tolerance degenerates. That is arguably
    # correct -- nothing straddled -- but it prints as a bound of 0, which
    # reads as a bug. Say so instead of implying a precision that is an
    # accident of which spin happened to be last.
    tolerance_note = ("" if tolerance else
                      " (the window's first and last games both awarded "
                      "nothing, so no edge game could straddle the boundary)")
    closed, near = [], []
    span_notes: list[str] = []
    for label, cols in candidates:
        parts = [d(c) for c in cols]
        if any(p is None for p in parts):
            continue
        total = sum(parts)
        a = awarded_for(cols)
        if a is None:
            continue
        own_tol = a["tolerance"] or tolerance
        diff = total - a["total"]
        if a["n"] != len(wins):
            span_notes.append(
                f"    {label}: compared over its OWN reading span "
                f"{a['start'][11:19]}..{a['end'][11:19]}, which holds "
                f"{a['n']} of the window's {len(wins)} game ends")
        if diff == 0:
            closed.append((label, total, diff, a["total"]))
        elif abs(diff) <= own_tol:
            near.append((label, total, diff, a["total"], own_tol))

    # TWO INDEPENDENT METER PATHS AGREEING IS THE STRONGEST STATEMENT HERE,
    # and the first version of this detector buried it. On the lab database
    # total_coin_out + total_jackpot came to 6,115,820 and
    # c7_total_won_credits to 6,114,285 -- agreement to 0.025% between LP 0x0F
    # and a Table C-7 sweep, which pins the metering convention beyond
    # argument. The output instead headlined "closest was total_coin_out
    # alone", a worse fit, because the ranking only looked at distance from
    # the event total. When the events are the thing in doubt, that is
    # exactly backwards.
    agreeing = []
    totals = {}
    for label, cols in candidates:
        parts = [d(c) for c in cols]
        if not any(p is None for p in parts):
            totals[label] = sum(parts)
    labels = list(totals)
    cols_of = {label: set(cols) for label, cols in candidates}
    for i, a in enumerate(labels):
        for b in labels[i + 1:]:
            if totals[a] == 0 or totals[b] == 0:
                continue
            # A METER THAT DID NOT MOVE IS NOT CORROBORATION.
            #
            # "total_coin_out alone" and "total_coin_out + total_jackpot"
            # differ by exactly one meter. On a window with no attendant
            # jackpot that meter is zero, so the two candidates are the
            # SAME NUMBER -- and reporting them as "two independent meter
            # paths agree, off by 0.000%" is one path counted twice.
            #
            # It is not cosmetic: `best` is chosen from the agreeing pair,
            # so the detector anchored on total_coin_out and reported
            # -3,220 unexplained where c7_total_won_credits, a genuinely
            # different poll, sat +520 from the events. Six times the
            # residual, and the wrong sign, from a tautology.
            #
            # The branch below already knew this -- "a meter that did not
            # move cannot tell them apart" -- but only said it about
            # candidates that CLOSED. The ranking never applied it.
            differing = cols_of.get(a, set()) ^ cols_of.get(b, set())
            if differing and all((d(c) or 0) == 0 for c in differing):
                continue
            off = abs(totals[a] - totals[b])
            if off <= 0.01 * max(abs(totals[a]), abs(totals[b])):
                agreeing.append((a, b, totals[a], totals[b], off))
    if agreeing:
        lines.append("")
        lines.append("  TWO INDEPENDENT METER PATHS AGREE on what was awarded:")
        for a, b, ta, tb, off in agreeing:
            pct = 100.0 * off / max(abs(ta), abs(tb))
            lines.append(f"    {ta} ({a})")
            lines.append(f"    {tb} ({b})")
            lines.append(f"    off by {off} -- {pct:.3f}%")
        lines.append("    LP 0x0F and a Table C-7 sweep are different polls "
                     "reading different meters, so their agreeing pins the "
                     "cabinet's metering convention. Where the EVENTS then "
                     "disagree with both, it is the events that are in "
                     "doubt, not the meters.")

    lines.append("")
    if span_notes:
        lines.append("  each meter is compared only against the game ends its "
                     "own first and last readings bracket:")
        lines.extend(span_notes)
        lines.append("")
    if closed:
        label, total, _, own = closed[0]
        lines.append(f"  IDENTITY CLOSES EXACTLY on {label}")
        lines.append(f"    {total} == {own}")
        if len(closed) > 1:
            lines.append(f"    ({len(closed)} candidates close; they agree because "
                         f"a meter that did not move cannot tell them apart)")
        verdict = "PASS"
    elif near:
        label, total, diff, own, own_tol = near[0]
        lines.append(f"  within one game's win of closing on {label}")
        lines.append(f"    {total} vs {own}, off by {diff}, against {own_tol} "
                     f"for that span's first and last games -- the only two a "
                     f"boundary can cut")
        lines.append("    That is exactly what a window edge produces, and it is "
                     "not evidence either way. Narrow with --since/--until.")
        verdict = "INCONCLUSIVE"
    else:
        # Rank by the METER CONSENSUS where two paths agree, not by which
        # single candidate sits closest to the event total. Closeness to a
        # quantity under suspicion is not evidence about it.
        by_label = {label: cols for label, cols in candidates}
        best = None
        if agreeing:
            a, b, ta, tb, _ = agreeing[0]
            best = (f"{a}, corroborated by {b}", ta, by_label.get(a, []))
        else:
            for label, cols in candidates:
                parts = [d(c) for c in cols]
                if any(p is None for p in parts):
                    continue
                total = sum(parts)
                a = awarded_for(cols)
                if a is None:
                    continue
                if best is None or abs(total - a["total"]) < abs(best[1] - awarded):
                    best = (label, total, cols)
        verdict = "FAIL"
        lines.append("  ** NO COMBINATION OF PAYOUT METERS ACCOUNTS FOR THE "
                     "AWARDED CREDITS")
        own_tol = tolerance
        if best:
            # Against the wins inside THAT meter's own reading span, never
            # the window's. Comparing a meter delta to events it was never
            # sampled across is what produced the 2.2% that turned out to be
            # 54 seconds of C-7 cadence.
            a = awarded_for(best[2]) if best[2] else None
            basis = a["total"] if a else awarded
            own_tol = (a["tolerance"] if a and a["tolerance"] else tolerance)
            residual = basis - best[1]
            lines.append(f"    closest was {best[0]}: {best[1]} against {basis}, "
                         f"leaving {residual} unexplained")
            if a and a["n"] != len(wins):
                lines.append(f"    over that meter's own span "
                             f"{a['start'][11:19]}..{a['end'][11:19]} "
                             f"({a['n']} of {len(wins)} game ends)")
            if basis:
                lines.append(f"    that is {100.0 * residual / basis:.1f}% of "
                             f"everything the game said it awarded")
        lines.append(f"    too large for a window edge, which can only cut the "
                     f"first and last games of that span ({own_tol} between "
                     f"them){tolerance_note}")
        lines.append("    READ THE SIGN. Meters exceeding the events means game-end "
                     "events were LOST. Events exceeding the meters means a payout "
                     "meter this tool does not know about is carrying the "
                     "difference -- most likely a progressive meter, which is NOT "
                     "in LP 0x0F and needs its own poll.")

    cash = [c for c in CASHOUT_SIDE_METERS if d(c)]
    if cash:
        lines.append("")
        lines.append("  cash-out side, reported and deliberately NOT summed above:")
        for c in cash:
            lines.append(f"    {c} +{d(c)}")
        lines.append("    A jackpot that was awarded and then handpaid moves an "
                     "award meter AND one of these. Adding them to the win side "
                     "double-counts it, which is how a reconciler ends up "
                     "'passing' on a cabinet that is losing events.")

    if overflowed:
        lines.append("")
        lines.append("  ** AN EXCEPTION BUFFER OVERFLOW OCCURRED IN THIS WINDOW.")
        lines.append("    Lost game-end events mean the awarded total is an "
                     "UNDER-count, so this identity cannot be clean and a PASS "
                     "here would be luck. Fix the overflow first -- detector 1 "
                     "names its source -- then re-run on a window with none.")
        if verdict == "PASS":
            verdict = "INCONCLUSIVE"
    return verdict, lines


def poll_cadence(conn, since, until) -> list[str]:
    """What rate was actually achieved -- the claim's other half."""
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
    if len(rows) < 2:
        return ["fewer than two meter rows: no cadence to measure"]
    from datetime import datetime
    def ts(v):
        return datetime.fromisoformat(v.replace("Z", "+00:00").replace(" ", "T"))
    deltas = []
    for a, b in zip(rows, rows[1:]):
        try:
            deltas.append((ts(b) - ts(a)).total_seconds())
        except ValueError:
            continue
    if not deltas:
        return ["timestamps unparseable"]
    deltas.sort()
    n = len(deltas)
    return [
        f"{len(rows)} cycles over {rows[0]} .. {rows[-1]}",
        f"cycle interval: min {deltas[0]:.2f}s  median {deltas[n // 2]:.2f}s  "
        f"max {deltas[-1]:.2f}s",
        "NOTE: this is the CYCLE interval, which includes the meter sweep. It is "
        "not the general-poll rate, and under RTE it is not the event-collection "
        "rate either -- §12.3 lets the machine report an event in place of ANY "
        "long poll reply, so a 70-poll sweep is 70 further chances to unload the "
        "queue. Events collected that way are stored with source "
        "'rte-interleaved'.",
    ]


def legacy_cutoff(root=None) -> str | None:
    """The date before which this project's own history is legacy.

    Kept in LEGACY-CUTOFF at the repository root -- one line, with the
    reasoning above it -- so the boundary is a reviewable fact in the tree
    rather than a number buried in a tool.

    Nothing is deleted. --all-history reads the whole database, and every
    run prints which cutoff it used, because a scoped number that looks
    like a lifetime one is how "255 gap(s), 74,835 credits" kept being
    quoted back at this project after the bug behind it was fixed.
    """
    import pathlib
    base = pathlib.Path(root) if root else pathlib.Path(__file__).resolve().parent.parent
    path = base / "LEGACY-CUTOFF"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except OSError:
        return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("--since", help="ISO timestamp lower bound")
    ap.add_argument("--until", help="ISO timestamp upper bound")
    ap.add_argument("--all-history", action="store_true",
                    help="ignore LEGACY-CUTOFF and read the whole database. "
                         "Useful for archaeology; misleading as a headline, "
                         "because the totals then span every build this "
                         "database remembers.")
    args = ap.parse_args()

    # The cutoff applies only when no explicit --since was given: a caller
    # who named a window meant that window.
    cutoff_note = None
    if args.since is None and not args.all_history:
        cut = legacy_cutoff()
        if cut:
            args.since = cut
            cutoff_note = cut

    path = Path(args.db)
    if not path.is_file():
        print(f"No such database: {path}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    have = {r[1] for r in conn.execute("PRAGMA table_info(machine_events)")}
    if "rte_fields" not in have:
        print("This database predates --rte: machine_events has no rte_fields "
              "column, so there are no real time events to reconcile.",
              file=sys.stderr)
        return 2

    events = _events(conn, args.since, args.until)
    rte = [e for e in events if e["source"] in RTE_SOURCES]

    print("=" * 72)
    print(f"RTE reconciliation: {path}")
    if cutoff_note:
        print(f"scoped to the LEGACY CUTOFF {cutoff_note} -- nothing before "
              f"it is deleted; --all-history reads the whole database")
    elif args.all_history:
        print("--all-history: WHOLE DATABASE, every build this file "
              "remembers. Read the by-day breakdown, never the total.")
    elif args.since is None:
        # The public mirror ships this tool without LEGACY-CUTOFF, which is
        # a private-repo file. Degrading to the whole database is right, but
        # degrading SILENTLY is not: a reader would take a lifetime total
        # for a current one, which is the exact mistake the cutoff exists
        # to prevent.
        print("no LEGACY-CUTOFF file found, so this is the WHOLE DATABASE. "
              "Totals here span every build it remembers -- read the by-day "
              "breakdown, or pass --since.")
    print("=" * 72)
    print(f"\n{len(events)} machine event(s) in window, {len(rte)} of them from RTE")
    by_source = defaultdict(int)
    for e in events:
        by_source[e["source"]] += 1
    print(f"  by source: {dict(sorted(by_source.items()))}")
    by_code = defaultdict(int)
    for e in rte:
        by_code[e["code"]] += 1
    if by_code:
        print("  by code:   " + ", ".join(
            f"0x{c:02X}×{n}" for c, n in sorted(by_code.items())))
    rte_only = sum(by_code.get(c, 0) for c in (REEL_STOPPED, CARD_HELD))
    if rte_only:
        print(f"  {rte_only} of these are RTE-ONLY events (reel stops, card held) "
              f"that ordinary polling would never have reported at all")

    print("\n--- POLLING RATE ACHIEVED " + "-" * 46)
    for line in poll_cadence(conn, args.since, args.until):
        print(f"  {line}")

    first_meter = conn.execute(
        "SELECT polled_at FROM meters_history ORDER BY id LIMIT 1").fetchone()
    window_start = first_meter[0] if first_meter else None

    boundaries = gateway_restarts(conn)
    if len(boundaries) > 1:
        print(f"  the gateway was started {len(boundaries)} times in this "
              f"database; play during the gaps between runs is excluded below")

    backlog = [e for e in events if e["source"] == "startup-backlog"]
    if backlog:
        print(f"  {len(backlog)} event(s) were queued BEFORE this run and drained "
              f"at startup; they are excluded from the reconciliation below")

    # Detector 5 needs this: a lost game-end event makes the awarded total an
    # under-count, so the payout identity cannot be clean and must not be
    # allowed to report PASS on a window that overflowed.
    overflowed = any(e["code"] == BUFFER_OVERFLOW for e in rte
                     if e["source"] != "startup-backlog")

    results = []
    failed_titles: list[str] = []
    for title, (verdict, lines) in (
        ("1. BUFFER OVERFLOW (the machine's own admission)",
         check_buffer_overflow(rte, window_start)),
        ("2. GAME-START CONTINUITY (the stream audits itself, 12.5.3)",
         check_game_start_continuity(rte, boundaries)),
        ("3. BILL-EVENT CONTINUITY (12.5.1's running count)",
         check_bill_continuity(rte)),
        ("6. REPEATED EVENTS (the sign detector 2 cannot see)",
         check_repeated_events(rte, boundaries)),
        ("4. METER RECONCILIATION (the wager side)",
         check_meters(conn, rte, args.since, args.until, boundaries)),
        ("5. PAYOUT IDENTITY (the award side: coin out + jackpot + progressive)",
         check_payout_identity(conn, rte, args.since, args.until, boundaries,
                               overflowed=overflowed)),
        ("7. CREDIT LEDGER (credits - wagers + wins = credits, per interval)",
         check_credit_ledger(conn, rte, args.since, args.until, boundaries)),
    ):
        results.append(verdict)
        if verdict == "FAIL":
            failed_titles.append(title)
        print(f"\n--- {title} " + "-" * max(0, 68 - len(title)))
        print(f"  [{verdict}]")
        for line in lines:
            print(f"  {line}")

    print("\n" + "=" * 72)
    failed = [v for v in results if v == "FAIL"]
    passed = [v for v in results if v == "PASS"]
    inconclusive = [v for v in results if v == "INCONCLUSIVE"]
    if failed:
        repeated = [t for t in failed_titles if t.startswith("6.")]
        if len(repeated) == len(failed_titles):
            # Detector 6 alone. Saying "events were lost" here would send the
            # reader after a polling-rate problem that is not the defect.
            print("VERDICT: events arrived TWICE. Nothing was lost -- the "
                  "opposite. A game start repeated a coin-in reading the "
                  "meter cannot have un-moved.")
            print("  This is NOT a polling-rate fault. Look at delivery: a "
                  "re-queued long poll whose reply was already collected "
                  "(\u00a712.4), or the same exception stored twice.")
            conn.close()
            return 1
        if repeated:
            print("VERDICT: events were lost AND events arrived twice. Read "
                  "detectors 2 and 6 separately -- they are different faults "
                  "with different causes, and one can mask the other in any "
                  "total.")
        else:
            print("VERDICT: events were lost. At this polling rate this cabinet "
                  "does NOT retain every event.")
        print("  Before blaming the rate: check whether the loss coincides with "
              "poll_errors (a dead link loses events for a different reason) and "
              "re-run with a window inside a quiet period.")
        conn.close()
        return 1
    if inconclusive and not passed:
        print("VERDICT: within window-edge ambiguity on every detector that had "
              "data, and no detector found a loss.")
        print("  Narrow the window to quiet minutes at both ends before calling "
              "it either way.")
        conn.close()
        return 3
    if not passed:
        print("VERDICT: nothing to conclude. No detector had data -- the machine "
              "was idle, so there were no events to lose.")
        print("  This is NOT evidence that 450 ms is safe. Play the machine during "
              "the run and re-run this.")
        conn.close()
        return 3
    extra = (f", with {len(inconclusive)} inside window-edge ambiguity"
             if inconclusive else "")
    print(f"VERDICT: no loss detected, on {len(passed)} of 7 detectors with "
          f"data{extra}.")
    print("  What this supports: at the cycle interval above, this cabinet "
          "retained every event these detectors can see.")
    print("  What it does NOT support: that no rate is ever too slow. §2.2.1 "
          f"guarantees only {SPEC_MIN_QUEUE} queue elements, and RTE's reel-stop "
          "events make a spin cost several of them. A faster game, or a longer "
          "gap between polls, can still overrun it.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
