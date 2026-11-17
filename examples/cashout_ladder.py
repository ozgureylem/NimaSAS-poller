#!/usr/bin/env python3
"""How long does a cash out take, and where does the time go?

READ-ONLY on the database, never opens the serial port. Safe during a soak.

`unwanted_handpays.py` counts the cashouts that FAILED. This one times the
ones that succeeded, because "the handpay stopped happening" and "the
customer is still standing there" are different outcomes and only the
second one is visible from the floor.

THE LADDER. A cash out on a cabinet with AFT enabled is not one step:

    0x6A  AFT request for host cashout   <- 8.7 stage 1, every time
    0x66  cash out button pressed
    0x6A  ...repeated while nothing answers
    0x57  system validation request      <- 15.7, ten seconds on the clock
    0x3D  a cash out ticket HAS BEEN PRINTED

Stage 1 runs whether or not the host does AFT. If nothing answers it, the
cabinet works through its own timeout before it ever asks for a validation
number, and that timeout is dead time the player watches. The poller's
`--decline-aft-cashout` exists to answer it immediately with a refusal,
which is the ONLY thing that shortens stage 1. This tool measures whether
it is working, in seconds, against a run where it was not.

0x6A COMES BEFORE 0x66, AND THAT IS NOT A BUG IN THE CLOCK. Measured twice
on 2026-11-15, each time exactly one poll interval ahead: the cabinet
queues the AFT request BEFORE the button-pressed exception. Both arrive
through the same drain in queue order, one event per poll, so the order is
the cabinet's and not an artefact. An attempt therefore claims any 0x6A in
the `--lead` seconds before its press.

WHAT THESE TIMES ARE, AND WHAT THEY ARE NOT. `occurred_at` is the moment
the GATEWAY filed the event, not the moment the cabinet raised it, and
events reach the database through several paths -- 'rte', 'rte-interleaved',
'heartbeat', 'cycle' -- which are drained at different moments. WITHIN one
path the order is the cabinet's queue order and the spacing is a poll
interval. ACROSS two paths, a few hundred milliseconds is collection noise:
on 2026-11-15 a 0x57 filed from 'rte' at +3.83 s carried a HIGHER id than
one filed from 'rte-interleaved' at +1.42 s. So the tool prints the source
beside every event and will not report a cross-path gap finer than a
second without saying which paths it spans. Seconds are real. Milliseconds
are not.

ORDER BY occurred_at, id -- never by id alone. id is insert order, which is
arrival order within one drain, but a later drain of a slower path files
older events with newer ids. Sorting by id alone reorders the ladder and
every timing computed off it is wrong. This cost a wrong measurement on
2026-11-15 and is the reason the tool exists in the repository instead of
being retyped from memory each time.
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

AFT_CASHOUT_REQUEST = 0x6A      # 106  8.7 stage 1
CASHOUT_PRESSED = 0x66          # 102
VALIDATION_REQUEST = 0x57       # 87   15.7 starts here
TICKET_PRINTED = 0x3D           # 61   the good ending
HANDPAY_PENDING = 0x51          # 81   the bad one

NAME = {
    AFT_CASHOUT_REQUEST: "0x6A AFT cashout request",
    CASHOUT_PRESSED: "0x66 cash out PRESSED",
    VALIDATION_REQUEST: "0x57 validation request",
    TICKET_PRINTED: "0x3D TICKET PRINTED",
    HANDPAY_PENDING: "0x51 HANDPAY PENDING",
}
LADDER_CODES = tuple(NAME)

# Wider than 15.7's ten seconds on purpose: these are capture times and a
# full Table C-7 sweep can delay one event by a whole cycle. A window that
# matched the spec would split one slow cashout into an attempt with no
# outcome plus an orphan ticket, which reads as a failure that did not
# happen -- the wrong direction for a tool whose job is to time successes.
DEFAULT_WINDOW_SECONDS = 120.0

# One poll interval is 0.2-0.3 s in the lab. 1.5 s allows for a sweep
# landing between the two without reaching back to a previous attempt.
DEFAULT_LEAD_SECONDS = 1.5

# The 0x3D and the LP 0x4D buffer row were filed 2 ms apart on 2026-11-15
# -- the poller files both in the same cycle -- so this is generous.
DEFAULT_TICKET_TOLERANCE = 5.0


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00").replace(" ", "T"))
    except (ValueError, AttributeError):
        return None


def read_events(conn, since=None, until=None) -> list[dict]:
    """Every ladder event in the window, in CABINET order as best we know it.

    ORDER BY occurred_at, id. See the module docstring on why id alone is
    wrong and timestamp alone is undefined within a tie.
    """
    where = [f"exception_code IN ({','.join('?' * len(LADDER_CODES))})"]
    params: list = list(LADDER_CODES)
    if since:
        where.append("occurred_at >= ?")
        params.append(since)
    if until:
        where.append("occurred_at <= ?")
        params.append(until)
    rows = conn.execute(
        "SELECT occurred_at, exception_code, id, source FROM machine_events "
        "WHERE " + " AND ".join(where) + " ORDER BY occurred_at, id",
        params).fetchall()
    out = []
    for occurred_at, code, row_id, source in rows:
        at = _parse(occurred_at)
        if at is None:
            continue
        out.append({"at": at, "code": code, "id": row_id,
                    "source": source or "?", "occurred_at": occurred_at})
    return out


def segment(events: list[dict],
            window: float = DEFAULT_WINDOW_SECONDS,
            lead: float = DEFAULT_LEAD_SECONDS) -> dict:
    """Split the stream into one attempt per button press.

    An attempt opens on 0x66 and closes on the first 0x3D or 0x51 after it,
    on the next 0x66, or when `window` expires -- whichever comes first.
    A 0x6A within `lead` seconds BEFORE the press belongs to that press
    (see the docstring: the cabinet queues it first).

    Returns {"attempts": [...], "orphan_tickets": [...], "orphan_handpays": [...]}.
    An orphan is an ending with no press in front of it: a jackpot lockup
    raises 0x51 and never presses anything, and a ticket can print from a
    press that fell outside the window.
    """
    attempts: list[dict] = []
    orphan_tickets: list[dict] = []
    orphan_handpays: list[dict] = []
    open_attempt: dict | None = None
    recent_aft: list[dict] = []

    def close(reason: str) -> None:
        nonlocal open_attempt
        if open_attempt is not None:
            open_attempt.setdefault("outcome", reason)
            attempts.append(open_attempt)
            open_attempt = None

    for ev in events:
        if open_attempt is not None and \
                (ev["at"] - open_attempt["press"]["at"]).total_seconds() > window:
            close("no outcome within the window")

        code = ev["code"]
        if code == CASHOUT_PRESSED:
            close("no outcome before the next press")
            claimed = [a for a in recent_aft
                       if 0 <= (ev["at"] - a["at"]).total_seconds() <= lead]
            open_attempt = {"press": ev, "events": claimed + [ev],
                            "aft": list(claimed), "validations": []}
            recent_aft = []
        elif code == AFT_CASHOUT_REQUEST:
            if open_attempt is not None:
                open_attempt["events"].append(ev)
                open_attempt["aft"].append(ev)
            else:
                recent_aft = [a for a in recent_aft
                              if (ev["at"] - a["at"]).total_seconds() <= lead]
                recent_aft.append(ev)
        elif code == VALIDATION_REQUEST:
            if open_attempt is not None:
                open_attempt["events"].append(ev)
                open_attempt["validations"].append(ev)
        elif code in (TICKET_PRINTED, HANDPAY_PENDING):
            if open_attempt is not None:
                open_attempt["events"].append(ev)
                open_attempt["end"] = ev
                close("ticket" if code == TICKET_PRINTED else "handpay")
            elif code == TICKET_PRINTED:
                orphan_tickets.append(ev)
            else:
                orphan_handpays.append(ev)

    close("no outcome within the window")
    for a in attempts:
        _time_attempt(a)
    return {"attempts": attempts, "orphan_tickets": orphan_tickets,
            "orphan_handpays": orphan_handpays}


def _gap(a: dict | None, b: dict | None) -> dict | None:
    """Seconds between two events, with the paths they came through.

    `cross_path` is the caller's warning that the number is not reliable
    below about a second.
    """
    if a is None or b is None:
        return None
    return {"seconds": (b["at"] - a["at"]).total_seconds(),
            "cross_path": a["source"] != b["source"],
            "paths": (a["source"], b["source"])}


def _time_attempt(attempt: dict) -> None:
    press = attempt["press"]
    first_v = attempt["validations"][0] if attempt["validations"] else None
    end = attempt.get("end")
    attempt["press_to_validation"] = _gap(press, first_v)
    attempt["validation_to_end"] = _gap(first_v, end)
    attempt["press_to_end"] = _gap(press, end)
    attempt["aft_count"] = len(attempt["aft"])
    attempt["validation_count"] = len(attempt["validations"])
    attempt["aft_before_press"] = sum(
        1 for a in attempt["aft"] if a["at"] < press["at"])


def match_tickets(conn, attempts: list[dict],
                  tolerance: float = DEFAULT_TICKET_TOLERANCE) -> None:
    """Attach each ticketed attempt's LP 0x4D buffer row, when one is there.

    This is the step that settles "was that one cash out or two?" -- two
    presses mean two rows in ticket_out_history, with different validation
    numbers and consecutive buffer slots. The timings alone cannot say it,
    because a duplicate delivery of one event looks like a second cashout
    until you go and look at the buffer.
    """
    try:
        rows = conn.execute(
            "SELECT id, captured_at, buffer_index, validation_number, "
            "amount_cents FROM ticket_out_history ORDER BY captured_at"
        ).fetchall()
    except sqlite3.OperationalError:
        return
    parsed = [(_parse(r[1]), r) for r in rows]
    parsed = [(t, r) for t, r in parsed if t is not None]
    for attempt in attempts:
        end = attempt.get("end")
        if end is None or end["code"] != TICKET_PRINTED:
            continue
        best = None
        for t, row in parsed:
            delta = abs((t - end["at"]).total_seconds())
            if delta <= tolerance and (best is None or delta < best[0]):
                best = (delta, row)
        if best is not None:
            attempt["ticket_row"] = best[1]
            attempt["ticket_match_seconds"] = best[0]


def _fmt_gap(gap: dict | None) -> str:
    if gap is None:
        return "      -"
    text = f"{gap['seconds']:7.2f}s"
    return text + ("~" if gap["cross_path"] else " ")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--window", type=float, default=DEFAULT_WINDOW_SECONDS,
                    help="seconds an attempt may run before it is reported "
                         "as having no outcome (default: %(default)s)")
    ap.add_argument("--lead", type=float, default=DEFAULT_LEAD_SECONDS,
                    help="seconds before a press in which a 0x6A still "
                         "belongs to it (default: %(default)s)")
    ap.add_argument("--units-per-currency", type=int, default=100)
    ap.add_argument("--show-validation-tail", action="store_true",
                    help="print the last 6 digits of each validation "
                         "number, to match a ticket in your hand against "
                         "the database. OFF by default: validation numbers "
                         "do not belong in anything that gets pasted into "
                         "a report or an issue")
    args = ap.parse_args()

    path = Path(args.db)
    if not path.is_file():
        print(f"No such database: {path}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    print("=" * 72)
    print(f"Cash out ladder: {path}")
    print("=" * 72)
    try:
        events = read_events(conn, args.since, args.until)
    except sqlite3.OperationalError as e:
        print(f"cannot read machine_events: {e}", file=sys.stderr)
        return 2

    if not events:
        print("\nNo cash out events in this window.")
        conn.close()
        return 3

    found = segment(events, window=args.window, lead=args.lead)
    attempts = found["attempts"]
    match_tickets(conn, attempts)

    print(f"\n  {len(events)} ladder events, {len(attempts)} button press(es)")
    if found["orphan_tickets"]:
        print(f"  {len(found['orphan_tickets'])} ticket(s) with no press in "
              f"front of them -- a press outside the window, or a reprint")
    if found["orphan_handpays"]:
        print(f"  {len(found['orphan_handpays'])} handpay(s) with no press -- "
              f"a jackpot raises no 0x66 and no 0x57")

    for n, a in enumerate(attempts, 1):
        print(f"\n--- press {n} {'-' * 58}")
        print(f"  pressed {a['press']['occurred_at']}")
        base = a["press"]["at"]
        for ev in sorted(a["events"], key=lambda e: (e["at"], e["id"])):
            offset = (ev["at"] - base).total_seconds()
            print(f"    {offset:+8.2f}s  {NAME[ev['code']]:<26} {ev['source']}")
        if a["aft_before_press"]:
            print(f"  {a['aft_before_press']} of the {a['aft_count']} 0x6A "
                  f"were queued AHEAD of the button exception (expected)")
        print(f"  0x6A x{a['aft_count']}   0x57 x{a['validation_count']}   "
              f"outcome: {a['outcome']}")
        print(f"    press -> 0x57   {_fmt_gap(a['press_to_validation'])}"
              f"   8.7 stage 1, what --decline-aft-cashout shortens")
        print(f"    0x57  -> end    {_fmt_gap(a['validation_to_end'])}"
              f"   15.7 and the print path")
        print(f"    press -> end    {_fmt_gap(a['press_to_end'])}"
              f"   what the player waits")
        row = a.get("ticket_row")
        if row is not None:
            _id, _at, slot, vnum, cents = row
            money = "unknown" if cents is None else \
                f"{cents / args.units_per_currency:,.2f}"
            tail = f", validation ...{str(vnum)[-6:]}" \
                if args.show_validation_tail else ""
            print(f"    LP 0x4D buffer slot {slot}, {money}{tail} "
                  f"(matched {a['ticket_match_seconds']:.3f}s away)")
        elif a["outcome"] == "ticket":
            print("    NO ticket_out_history row within tolerance -- the "
                  "0x3D arrived but the buffer read has not caught up")

    ticketed = [a for a in attempts if a["outcome"] == "ticket"]
    waits = [a["press_to_end"]["seconds"] for a in ticketed
             if a["press_to_end"] is not None]
    print(f"\n--- SUMMARY {'-' * 60}")
    print(f"  {len(ticketed)} of {len(attempts)} press(es) ended in a ticket")
    print(f"  {sum(1 for a in attempts if a['outcome'] == 'handpay')} "
          f"ended in a handpay")
    if waits:
        print(f"  press -> ticket: median {statistics.median(waits):.2f}s, "
              f"min {min(waits):.2f}s, max {max(waits):.2f}s")
    slots = [a["ticket_row"][2] for a in ticketed if a.get("ticket_row")]
    if len(slots) > 1:
        print(f"  LP 0x4D buffer slots {slots} -- distinct slots mean "
              f"distinct prints, not one event delivered twice")
    print("\n  A '~' on a gap means the two events came through DIFFERENT")
    print("  collection paths, where anything finer than a second is")
    print("  collection noise rather than a measurement of the cabinet.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
