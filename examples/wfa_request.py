"""Ask the gateway what a ticket was worth — the server side of a WfA pull.

WfA is "Waiting for Amount": the server holds a ticket's validation number
(it came from the pool it issued) but not its value, because the
dispense-detail drain never arrived. Dispense is deliberately local-only so
it keeps working when the micro-PC-to-server LAN is down, which is exactly
when this state is created. A cashier then scans the ticket at the cage and
somebody has to ask the EGM, with a customer waiting.

This is a CLIENT. It writes a row into host_requests and waits for the
poller to answer it. It never opens the serial port: one port, one master,
and the poller is holding it.

    python3 examples/wfa_request.py gateway.sqlite3 000000000000000001
    python3 examples/wfa_request.py gateway.sqlite3 <number> --json

The number may be given exactly as printed on the ticket face, grouped with
dashes; only the digits are read. No grouped example is written here because
a dashed eighteen-digit string is the SHAPE of a real validation number and
the mirror scanner refuses it on sight, which is the behaviour we want.

The poller must be running with --serve-host-requests or nothing will
answer, and this will say so rather than hang to its deadline.

EXIT CODES, because a cage integration reads them and not prose:

    0  answered, with an amount
    3  answered: the cabinet's buffer does not contain this number. That is
       a real answer and the server's next step is the physical visit, not a
       retry
    4  the deadline passed with no answer
    5  nobody is serving requests
    2  usage or database error
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DEFAULT_DEADLINE_MS = 1000
# A gateway that has not touched the table at all within this many seconds of
# being asked is not serving requests. Deliberately longer than the deadline:
# a busy sweep can be slow to reach the slot, and "slow" must not be reported
# as "absent".
SERVICE_SILENCE_S = 5.0


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def normalise(number: str) -> int:
    """Accept the number as printed on the ticket. The face carries it
    grouped -- 00-0000-0000-0000-0001 -- and a cashier reading it aloud or a
    scanner emitting it raw should not be two different inputs."""
    digits = "".join(c for c in str(number) if c.isdigit())
    if not digits:
        raise ValueError(f"no digits in {number!r}")
    return int(digits)


def submit(conn: sqlite3.Connection, validation_number: int,
           deadline_ms: int = DEFAULT_DEADLINE_MS) -> int:
    cur = conn.execute(
        "INSERT INTO host_requests (requested_at, kind, validation_number, "
        " deadline_ms) VALUES (?, 'wfa_pull', ?, ?)",
        (_utc_now(), int(validation_number), int(deadline_ms)))
    conn.commit()
    return cur.lastrowid


def wait(conn: sqlite3.Connection, req_id: int, *, deadline_ms: int,
         poll_s: float = 0.02, clock=time.monotonic, sleep=time.sleep) -> dict:
    """Poll the row until the poller resolves it.

    The wait is bounded by the request's own deadline plus a grace margin,
    because the poller marks a request expired at the deadline and this must
    outlive that by enough to read the verdict rather than time out first and
    report a different one.
    """
    started = clock()
    limit = (deadline_ms / 1000.0) + 1.0
    while True:
        row = conn.execute(
            "SELECT state, amount_cents, result_json, error, served_from, "
            "       polls_spent, latency_ms, claimed_at "
            "  FROM host_requests WHERE id = ?", (req_id,)).fetchone()
        if row is None:
            return {"state": "lost", "error": "the request row disappeared"}
        state = row[0]
        if state in ("done", "failed", "expired"):
            return {"state": state, "amount_cents": row[1],
                    "result": json.loads(row[2]) if row[2] else None,
                    "error": row[3], "served_from": row[4],
                    "polls_spent": row[5], "latency_ms": row[6]}
        waited = clock() - started
        if state == "pending" and row[7] is None and waited > SERVICE_SILENCE_S:
            return {"state": "unserved", "error":
                    "no poller claimed this request. Is sql_poll_logger "
                    "running with --serve-host-requests?"}
        if waited > limit:
            return {"state": "timeout", "error":
                    f"no answer within {limit:.1f}s"}
        sleep(poll_s)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("database", help="the gateway's SQLite file")
    ap.add_argument("validation_number",
                    help="as printed on the ticket; dashes are fine")
    ap.add_argument("--deadline-ms", type=int, default=DEFAULT_DEADLINE_MS,
                    help="hard budget (default: %(default)s). The spec's "
                         "end-to-end figure is 500 ms p95, 1 s hard")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output for a cage integration")
    args = ap.parse_args(argv)

    if not os.path.exists(args.database):
        print(f"no database at {args.database}", file=sys.stderr)
        return 2
    try:
        number = normalise(args.validation_number)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.database, timeout=5.0)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        req_id = submit(conn, number, args.deadline_ms)
    except sqlite3.Error as e:
        print(f"could not write the request: {e}", file=sys.stderr)
        return 2
    answer = wait(conn, req_id, deadline_ms=args.deadline_ms)
    answer["request_id"] = req_id
    answer["validation_number"] = number

    if args.json:
        print(json.dumps(answer, indent=2))
    else:
        state = answer["state"]
        if state == "done" and answer.get("amount_cents") is not None:
            print(f"{answer['amount_cents'] / 100:,.2f}  "
                  f"(from {answer['served_from']}, "
                  f"{answer['polls_spent']} poll slots, "
                  f"{answer['latency_ms']} ms)")
        elif state == "done":
            print("NOT IN THE CABINET'S BUFFER. The machine holds 31 ticket "
                  "records and a busy cabinet wraps it, so this is an "
                  "answer rather than a fault: the ticket is real and its "
                  "value is no longer on the machine. Next step is the "
                  "physical visit -- read the EGM's own history, and a "
                  "privileged SLOTS user types the value in.")
        else:
            print(f"{state.upper()}: {answer.get('error')}", file=sys.stderr)

    if answer["state"] == "done":
        return 0 if answer.get("amount_cents") is not None else 3
    if answer["state"] == "unserved":
        return 5
    if answer["state"] in ("timeout", "expired"):
        return 4
    return 2


if __name__ == "__main__":
    sys.exit(main())
