"""Does the wake byte matter, and does its value matter -- measured so the
answer cannot be an artefact of when each condition happened to run.

Read-only: general polls only. Nothing changes machine state.

This script replaces one that ran each condition as a block: twenty
attempts of 0x80, then twenty of 0x00, and so on. That design cannot tell
a condition effect from anything drifting over the minute it takes to run
-- machine state after a door or attendant-menu visit, link supervision,
warm-up. And on this machine something IS drifting: a bare poll measured
0/20 in one script and 13/20 in another, for the same operation.

So every condition here is sampled once per round, with the order rotated
each round, interleaved across the whole run. A condition that only works
early, or only once the line has been busy a while, can no longer hide as
a property of the byte.

Section 1 varies the throwaway byte, including a genuine no-preamble
control. Section 2 sends NO preamble at all and varies the silence before
the poll -- the previous version wrote a 0x80 every iteration while
titled "no preamble", and then advised raising wake_after_idle on the
strength of it. It was measuring the gap a preamble still works across,
which is a different question and not the one the setting answers.

    python3 examples/preamble_probe.py /dev/ttyS0 --address 1
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.serial_port import open_serial_port
from saspy.transport import PARITY_MARK


def read_reply(port, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = port.read(1)
        if chunk:
            return chunk
    return b""


def interleave(port, poll_byte, timeout, conditions, rounds, attempt):
    """Sample every condition once per round, rotating the order.

    ``conditions`` is a list of (label, payload); ``attempt`` performs one
    and returns True if the poll was answered. Returns {label: (ok, half)}
    where ``half`` is how many of those successes fell in the first half
    of the run -- a condition that is genuinely about the byte should sit
    near half, and drift shows up as a lopsided split.
    """
    ok = Counter()
    first_half = Counter()
    for r in range(rounds):
        order = conditions[r % len(conditions):] + conditions[: r % len(conditions)]
        for label, payload in order:
            if attempt(port, poll_byte, timeout, payload):
                ok[label] += 1
                if r < rounds / 2:
                    first_half[label] += 1
    return {label: (ok[label], first_half[label]) for label, _ in conditions}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=0.3)
    ap.add_argument("-n", type=int, default=20, help="rounds per condition")
    args = ap.parse_args()

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    pb = args.address | 0x80
    n = args.n
    port.parity = PARITY_MARK
    print(f"{args.port} @ {args.baud}, address {args.address} (poll 0x{pb:02X})")
    print(f"{n} rounds, conditions interleaved and rotated\n")

    # --- 1. does the identity of the throwaway byte matter? ------------
    def attempt_byte(p, poll, timeout, value):
        time.sleep(0.3)  # let the line go quiet before each attempt
        p.reset_input_buffer()
        if value is not None:
            p.write(bytes([value]))
            if hasattr(p, "flush"):
                p.flush()
            time.sleep(0.005)
            p.reset_input_buffer()
        p.write(bytes([poll]))
        return bool(read_reply(p, timeout))

    byte_conditions = [("0x80 broadcast poll", 0x80), ("0x00 null", 0x00),
                       ("0xFF all ones", 0xFF), ("none (control)", None)]
    print("1. Preamble byte, 0.3s quiet before each attempt")
    by_byte = interleave(port, pb, args.timeout, byte_conditions, n, attempt_byte)
    for label, _ in byte_conditions:
        got, half = by_byte[label]
        print(f"   {label:22} {got:2}/{n}   (first half {half}, second {got - half})")

    # --- 2. silence before a GENUINELY bare poll ------------------------
    def attempt_idle(p, poll, timeout, idle):
        time.sleep(idle)  # nothing is written during this -- that is the point
        p.reset_input_buffer()
        p.write(bytes([poll]))
        return bool(read_reply(p, timeout))

    idle_conditions = [(f"{int(ms * 1000)} ms", ms) for ms in (0.005, 0.05, 0.2, 0.5, 1.0)]
    print("\n2. NO preamble; silence before the poll (nothing written during it)")
    by_idle = interleave(port, pb, args.timeout, idle_conditions, n, attempt_idle)
    for label, _ in idle_conditions:
        got, half = by_idle[label]
        print(f"   {label:>8} quiet        {got:2}/{n}   (first half {half}, second {got - half})")

    # --- verdict --------------------------------------------------------
    hi = n * 0.9
    lo = n * 0.5
    c80 = by_byte["0x80 broadcast poll"][0]
    c00 = by_byte["0x00 null"][0]
    cff = by_byte["0xFF all ones"][0]
    ctl = by_byte["none (control)"][0]
    print("\n" + "=" * 68)
    if min(c80, c00, cff) >= hi and ctl < lo:
        print("Any byte works, none does not: the preamble is a throwaway, not")
        print("protocol. The far end needs the line driven before it reads --")
        print("a link fault, worth raising with whoever built the interface.")
    elif c80 >= hi and max(c00, cff) < lo:
        print("Only 0x80 works. That is protocol, and the vendor should be")
        print("asked why -- SAS 6.02 does not call for it.")
    elif ctl >= hi:
        print("The control answers too: on this run a bare poll needs no")
        print("preamble at all. Earlier 0/20 runs were measuring something")
        print("else. Do NOT conclude the wake byte is required from this.")
    else:
        lopsided = [l for l, _ in byte_conditions
                    if by_byte[l][0] >= 4 and (by_byte[l][1] == by_byte[l][0] or by_byte[l][1] == 0)]
        print("Mixed. Report the table as-is rather than acting on it.")
        if lopsided:
            print(f"  All successes for {', '.join(lopsided)} fell in one half of")
            print("  the run, so something is drifting and the byte is not the")
            print("  variable. Re-run before drawing any conclusion.")

    bare_ok = [ms for (label, ms) in idle_conditions if by_idle[label][0] >= hi]
    print()
    if not bare_ok:
        print("No bare poll was reliable at any silence: keep wake_byte=0x80")
        print("and wake_after_idle=0.0 (a wake byte before every message).")
    elif len(bare_ok) == len(idle_conditions):
        print("A bare poll was reliable at EVERY silence tested, up to")
        print(f"{max(bare_ok) * 1000:.0f} ms. On this evidence the wake byte is not needed")
        print("here at all -- but it also never hurt, so leaving it on costs")
        print("~5.6ms per exchange and nothing else. Decide on a soak, not this.")
    else:
        print(f"Bare polls reliable up to {max(bare_ok) * 1000:.0f} ms of silence.")
        print(f"  => SASTransport(wake_after_idle={max(bare_ok):.3f}) is supported by this")
        print("     run: below that gap the line is warm enough without a byte.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
