"""Find what makes exception 0x70, buffer overflow, happen.

Read-only: meter polls and general polls. Nothing changes machine state.

Measured on an EGT Premier, playing in turbo mode: about 2.4 exceptions per
second (0x7E and 0x7F per game, ~0.83s per game), against a monitor polling
4.5 times a second. Zero overflow, zero failed polls. So a fast player does
NOT outrun a poll loop that is actually polling -- the event rate is not the
problem, and raising the poll rate is not the fix.

The queue is what sets the budget. Section 2.2.1 requires at least 20
elements; this machine returned about 35 before going idle. At 2.4
exceptions a second, 35 slots is roughly FIFTEEN SECONDS of not polling.
Overflow therefore means something stopped the general poll for that long,
and the usual candidate on a gateway is its own long polls: one general poll
per cycle followed by a meter sweep of dozens of exchanges, during which no
general poll goes out at all and the queue fills unattended.

This measures exactly that gap, by running the real sweep -- it imports
sql_poll_logger's own _poll_all_meters rather than imitating it, so the
number cannot drift from what the poller actually does -- and converts the
duration into exceptions accumulated.

    python3 examples/poll_gap_probe.py /dev/ttyS0 --address 1
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from saspy.client import SASClient
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

from sql_poll_logger import _MeterPollFailure, _poll_all_meters  # the real sweep, not a copy

# Measured, not assumed. Override both if your machine differs.
DEFAULT_EVENT_RATE = 2.4   # exceptions/sec at turbo play, measured
DEFAULT_QUEUE_DEPTH = 35   # measured on an EGT Premier; spec floor is 20
SPEC_MINIMUM_QUEUE = 20


def time_sweep(client, **flags) -> tuple[float, int, int, str | None]:
    """Run one sweep; return (seconds, values obtained, values NULL, abort).

    A sweep that aborts is still a measurement -- the gap it leaves in the
    general poll is however long it ran before giving up -- so the duration
    is returned either way, with the reason it stopped.
    """
    started = time.monotonic()
    try:
        values = _poll_all_meters(client, **flags)
    except _MeterPollFailure as failure:
        elapsed = time.monotonic() - started
        return elapsed, 0, 0, f"{failure.poll_name}: {failure.original}"
    elapsed = time.monotonic() - started
    got = sum(1 for v in values.values() if v is not None)
    return elapsed, len(values) - got, got, None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--event-rate", type=float, default=DEFAULT_EVENT_RATE,
                    help=f"exceptions/sec to assume for the budget (default {DEFAULT_EVENT_RATE}, "
                         "measured at turbo play)")
    ap.add_argument("--queue-depth", type=int, default=DEFAULT_QUEUE_DEPTH,
                    help=f"queue depth in exceptions (default {DEFAULT_QUEUE_DEPTH}, measured; "
                         f"exception_buffer_probe.py measures yours)")
    args = ap.parse_args()

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(
        SASTransport(port, byte_timeout=args.timeout), args.address, timeout=args.timeout
    )

    budget = args.queue_depth / args.event_rate
    print(f"{args.port}, address {args.address}")
    print(f"queue depth {args.queue_depth} exceptions, event rate {args.event_rate}/s")
    print(f"=> the general poll may stop for {budget:.1f}s before the queue overflows\n")

    # Each configuration the poller can be run in, so the flags that matter
    # are visible as numbers rather than guessed at.
    configs = [
        ("full sweep (all defaults)", dict()),
        ("--skip-table-c7-sweep", dict(table_c7_sweep=False)),
        ("--skip-validation-meters-sweep", dict(validation_meters_sweep=False)),
        ("--skip-full-meter-sweep", dict(full_sweep=False)),
        ("both C-7 and validation skipped",
         dict(table_c7_sweep=False, validation_meters_sweep=False)),
        ("minimal (core meters only)",
         dict(full_sweep=False, table_c7_sweep=False,
              validation_meters_sweep=False, last_accepted_bill_poll=False)),
    ]

    results = []
    for label, flags in configs:
        try:
            elapsed, nulls, got, abort = time_sweep(client, **flags)
        except SASError as e:
            print(f"  {label:34} link failure: {type(e).__name__}: {e}")
            continue
        lost = elapsed * args.event_rate
        verdict = "OVERFLOWS" if elapsed > budget else "ok"
        results.append((label, elapsed, got, nulls, lost, verdict))
        detail = f"{got:3} meters" if abort is None else "ABORTED"
        print(f"  {label:34} {elapsed:6.2f}s  {detail}  "
              f"~{lost:4.0f} exceptions queued  {verdict}")
        if abort:
            print(f"       aborted on {abort}")
        # Keep the machine answered between configurations so this probe is
        # not itself the thing that overflows the queue.
        for _ in range(5):
            try:
                client.general_poll()
            except SASError:
                pass

    print("\n" + "=" * 72)
    if not results:
        print("No sweep completed. Fix the link before reading anything into this.")
        return 1

    aborted = [r for r in results if r[2] == 0 and r[3] == 0]
    if aborted:
        print("\nSome sweeps aborted before finishing. On a build from before the")
        print("unsupported-meter fix that is expected on any machine missing an")
        print("optional meter -- a cashless machine has no hopper, so LP 0x2C never")
        print("answers and the whole cycle used to abort there. Pull and re-run.")

    worst = max(results, key=lambda r: r[1])
    print(f"Longest gap without a general poll: {worst[1]:.2f}s  ({worst[0]})")
    print(f"Budget before the queue overflows:  {budget:.1f}s")

    if worst[1] > budget:
        print("\n=> THIS IS A SOURCE OF 0x70. A sweep this long leaves the exception")
        print("   queue unattended past its capacity, so a player active during the")
        print("   sweep loses events and the machine reports buffer overflow. It is")
        print("   not a poll-rate problem and polling faster between sweeps will not")
        print("   fix it -- the gap is inside the cycle, not between cycles.")
        print("\n   Two fixes, in order of preference:")
        print("   1. Interleave general polls INTO the sweep, so no long poll run")
        print("      ever exceeds the budget. Costs nothing but ordering.")
        print("   2. Sweep less: the flags above show what each one buys back.")
        print("      Table C-7 and the validation-meter sweep are the expensive")
        print("      ones and are both explicitly redundant with other reads.")
    else:
        print("\n=> The sweep fits inside the budget, so on these numbers it is not")
        print("   the source of 0x70. Look instead for anything else that stops the")
        print("   general poll for longer: a database write stall, a server sync on")
        print("   the same thread, a port reopen, or a process restart. Measure the")
        print("   gap, do not assume it -- that is what this tool is for.")
    print("\nNote both inputs are assumptions you can check: --queue-depth comes")
    print("from exception_buffer_probe.py, and --event-rate from watching")
    print("exception_monitor.py during the fastest play the floor actually sees.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
