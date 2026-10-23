"""Why does a bare general poll get no answer, when a 0x80 before it works?

Read-only: general polls only. Nothing here changes machine state.

Two explanations fit "0x80 first = 10/10, nothing first = 0/30", and they
lead to opposite fixes, so guessing between them is not an option.

  1. The machine wants the 0x80. Then a poll loop must send it, and the
     gap after it is a real protocol parameter.

  2. The 0x80 is a sacrificial byte. Changing parity calls tcsetattr with
     TCSANOW, which reconfigures the port *immediately*. If this driver
     or UART disturbs the line doing that -- a FIFO reset, a settling
     delay, a mangled first byte -- then whichever byte goes out right
     after the reconfiguration is the one that gets damaged. Send 0x80
     first and it absorbs the damage; the real poll follows on a settled
     line and is answered. Nothing to do with SAS at all.

Explanation 2 predicts something 1 does not: pre-set parity to MARK once,
then poll repeatedly with NO parity calls in the loop, and every poll
should be answered with no sync byte anywhere. Strategy C does exactly
that, and is the whole point of this script.

    python3 examples/wakeup_probe.py /dev/ttyS0 --address 1
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.serial_port import open_serial_port
from saspy.transport import PARITY_MARK, PARITY_SPACE

CADENCE = 0.2


def read_reply(port, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = port.read(1)
        if chunk:
            return chunk
    return b""


def trial(port, poll_byte, timeout, setup, send):
    """Run one strategy: ``setup`` once, then ``send`` per attempt."""
    setup(port)
    ok = 0
    for _ in range(trial.n):
        port.reset_input_buffer()
        send(port, poll_byte)
        if read_reply(port, timeout):
            ok += 1
        time.sleep(CADENCE)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=0.3)
    ap.add_argument("-n", type=int, default=20)
    args = ap.parse_args()
    trial.n = args.n

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    pb = args.address | 0x80
    n = args.n
    print(f"{args.port} @ {args.baud}, address {args.address}, {n} attempts each\n")

    def set_mark(p):
        p.parity = PARITY_MARK

    def set_space(p):
        p.parity = PARITY_SPACE

    results = {}

    # A: reconfigure before every poll (what restoring SPACE each frame does)
    def send_a(p, b):
        p.parity = PARITY_MARK
        p.write(bytes([b]))
        if hasattr(p, "flush"):
            p.flush()
        p.parity = PARITY_SPACE

    results["A reconfigure before every poll"] = trial(port, pb, args.timeout, set_space, send_a)

    # B: current shipped behaviour -- MARK sticks, so no tcsetattr after the first
    def send_b(p, b):
        p.parity = PARITY_MARK
        p.write(bytes([b]))

    results["B parity sticks at MARK (shipped)"] = trial(port, pb, args.timeout, set_space, send_b)

    # C: THE DISCRIMINATOR -- parity set once, never touched in the loop
    def send_c(p, b):
        p.write(bytes([b]))

    results["C no parity calls at all"] = trial(port, pb, args.timeout, set_mark, send_c)

    # D: reconfigure, then let the line settle before the poll byte
    def send_d(p, b):
        p.parity = PARITY_SPACE
        p.parity = PARITY_MARK
        time.sleep(0.002)
        p.write(bytes([b]))

    results["D reconfigure + 2ms settle"] = trial(port, pb, args.timeout, set_space, send_d)

    # E: the 0x80 sacrificial byte, as originally tested
    def send_e(p, b):
        p.parity = PARITY_MARK
        p.write(bytes([0x80]))
        time.sleep(0.005)
        p.write(bytes([b]))

    results["E 0x80 then 5ms then poll"] = trial(port, pb, args.timeout, set_space, send_e)

    for label, got in results.items():
        print(f"  {label:36} {got}/{n}")

    c = results["C no parity calls at all"]
    a = results["A reconfigure before every poll"]
    d = results["D reconfigure + 2ms settle"]
    e = results["E 0x80 then 5ms then poll"]

    print("\n" + "=" * 62)
    if c >= n * 0.9 and a < n * 0.5:
        print("C works without any sync byte, A does not.")
        print("=> The 0x80 was never a sync. It absorbed the disturbance from")
        print("   reconfiguring the port. The fix is to stop reconfiguring on")
        print("   the hot path, not to add a byte to every cycle.")
        if d >= n * 0.9:
            print("   D also works, so a settle delay after tcsetattr is the")
            print("   alternative fix where a reconfiguration is unavoidable")
            print("   -- which matters for multi-byte frames.")
    elif c < n * 0.5 and e >= n * 0.9:
        print("C fails without the 0x80 even with no parity calls at all.")
        print("=> The machine really does want the 0x80 first. That is a")
        print("   genuine protocol finding: worth sending, and worth asking")
        print("   the vendor about, since SAS 6.02 does not ask for it.")
    else:
        print("Mixed. Report the whole table -- the pattern is the finding.")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
