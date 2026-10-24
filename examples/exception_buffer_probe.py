"""Measure how many exceptions this machine really buffers.

Read-only: general polls only. Nothing changes machine state.

Section 2.2.1 is explicit about what must happen, and it is worth quoting
because it contradicts a widely-held belief that SAS holds only one pending
exception per machine:

    "gaming machines must maintain a first in/first out (FIFO) exception
    queue of at least 20 elements in non-volatile memory. In the event of
    exception queue overrun, the oldest exception is lost... If one or more
    exceptions have been lost from the queue, exception 70, buffer overflow,
    should be reported at the next opportunity. Exception 70 is not added to
    the queue."

So the floor is 20, not 1; overflow discards the OLDEST, keeping the most
recent; and a machine that has lost anything is required to say so with
exception 0x70. That last point is the useful one: if a gateway is losing
exceptions and 0x70 never appears, the loss is not queue overrun and the
cause is somewhere else.

How this measures it: a general poll pulls exactly one exception, so the
queue can only be filled while nobody is polling. The tool drains the queue,
stops polling while you generate a known number of events by hand, then
polls as fast as it can and counts what comes back. Opening and closing the
slot door is the cleanest generator -- two exceptions per cycle, 0x11 then
0x12, both queued (Type Q in Appendix A) rather than priority.

    python3 examples/exception_buffer_probe.py /dev/ttyS0 --address 1 --cycles 15

One caveat worth knowing before you run it: the accumulation phase stops
polling entirely, which after long enough is a link-down condition
(section 4.3). The queue itself is specified as non-volatile so it should
survive, but keep the phase brisk -- generate the events and continue.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.client import SASClient
from saspy.constants import describe_exception
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

BUFFER_OVERFLOW = 0x70
SPEC_MINIMUM = 20


def drain(client, label, max_polls=200):
    """Poll until the queue is empty -- two consecutive 0x00."""
    drained, idle_run = [], 0
    for _ in range(max_polls):
        try:
            code = client.general_poll()
        except SASError:
            idle_run = 0
            continue
        if code == 0x00:
            idle_run += 1
            if idle_run >= 2:
                return drained
        else:
            idle_run = 0
            drained.append(code)
    print(f"  {label}: still returning exceptions after {max_polls} polls", file=sys.stderr)
    return drained


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=0.3)
    ap.add_argument("--cycles", type=int, default=15,
                    help="door open/close cycles to ask for; each is 2 exceptions, so the "
                         "default 15 generates 30 -- comfortably over the spec's minimum 20")
    args = ap.parse_args()

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(
        SASTransport(port, byte_timeout=args.timeout), args.address, timeout=args.timeout
    )
    expected = args.cycles * 2

    print(f"{args.port}, address {args.address}\n")
    print("1. Draining whatever is already queued...")
    pre = drain(client, "pre-drain")
    print(f"   {len(pre)} exception(s) cleared. Queue is empty.\n")

    print(f"2. NOT POLLING from here. Open and close the slot door {args.cycles} times")
    print(f"   ({expected} exceptions: 0x11 and 0x12 per cycle). Be brisk -- no polls")
    print("   are going out, which is a link-down condition if it runs long.")
    input("\n   Press Enter the moment you have finished... ")

    print("\n3. Polling as fast as the port allows...")
    started = time.monotonic()
    collected, idle_run, polls = [], 0, 0
    while idle_run < 3 and polls < 500:
        polls += 1
        try:
            code = client.general_poll()
        except SASError:
            continue
        if code == 0x00:
            idle_run += 1
        else:
            idle_run = 0
            collected.append(code)
    elapsed = time.monotonic() - started

    overflow = [c for c in collected if c == BUFFER_OVERFLOW]
    events = [c for c in collected if c != BUFFER_OVERFLOW]

    print(f"   {len(collected)} returned in {elapsed:.1f}s over {polls} polls\n")
    print("   sequence:")
    for i in range(0, len(collected), 10):
        chunk = collected[i:i + 10]
        print(f"     {i + 1:3}-{i + len(chunk):<3} " + " ".join(f"{c:02X}" for c in chunk))

    print("\n" + "=" * 70)
    print(f"generated   {expected}  ({args.cycles} door cycles x 2)")
    print(f"recovered   {len(events)} queued exception(s)"
          + (f" + {len(overflow)} x 0x70 buffer overflow" if overflow else ""))

    if overflow:
        print(f"\nThe machine reported buffer overflow, so it dropped the oldest")
        print(f"events and kept the newest. Its queue therefore holds about")
        print(f"{len(events)} exceptions -- against the spec minimum of {SPEC_MINIMUM}.")
        if len(events) >= SPEC_MINIMUM:
            print("That meets 2.2.1.")
        else:
            print(f"That is BELOW the {SPEC_MINIMUM} section 2.2.1 requires. Worth raising")
            print("with the manufacturer, and worth designing around meanwhile.")
    elif len(events) >= expected:
        print(f"\nEverything came back and no overflow was reported, so the queue")
        print(f"holds AT LEAST {len(events)}. Re-run with a larger --cycles to find")
        print("the real ceiling -- this run did not reach it.")
    else:
        print(f"\n{expected - len(events)} exception(s) are missing and the machine did NOT")
        print("report 0x70. Section 2.2.1 requires overflow to be reported, so")
        print("either the door generated fewer events than assumed (check the")
        print("sequence above for a clean 11/12 alternation), or exceptions were")
        print("lost somewhere that is not the queue -- which is the more")
        print("interesting answer, and not a gateway-side poll-rate problem.")

    if events:
        from collections import Counter
        print("\nwhat came back:")
        for code, n in Counter(events).most_common():
            print(f"   0x{code:02X}  x{n:<4} {describe_exception(code)}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
