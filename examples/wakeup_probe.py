"""Find out which byte the machine is actually answering.

Read-only: general polls only. Nothing changes machine state.

An earlier version of this script concluded "the machine really does want
a 0x80 first" from one observation: polls preceded by 0x80 were answered
20/20, polls sent any other way 0/20. That conclusion outran the
evidence, because the test could not tell these two apart:

  * the 0x80 is a preamble the machine needs before answering OUR poll; or
  * the machine is answering the 0x80 ITSELF, and our poll byte is
    ignored. Input is flushed before the 0x80 goes out, so a reply to the
    0x80 lands in the buffer and is read back as though it answered the
    poll that followed.

The second is not exotic. 0x80 is a general poll to address 0, and a
machine that answers it is simply a machine answering a general poll --
which would also mean the address under test is the wrong one.

F and G below separate them, and the address sweep says which address is
real. --watch then gives ground truth no byte-counting can: open the slot
door and the exception byte must change. If it never leaves 0x00 while
the door swings, nothing here is talking to that machine.

    python3 examples/wakeup_probe.py /dev/ttyS0 --address 1
    python3 examples/wakeup_probe.py /dev/ttyS0 --address 1 --watch
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


def trial(port, poll_byte, timeout, setup, send, n):
    """Run one strategy. Returns (replies, set of distinct bytes seen)."""
    setup(port)
    ok, seen = 0, set()
    for _ in range(n):
        port.reset_input_buffer()
        send(port, poll_byte)
        reply = read_reply(port, timeout)
        if reply:
            ok += 1
            seen.add(reply[0])
        time.sleep(CADENCE)
    return ok, seen


def fmt(seen):
    return "{" + " ".join(f"{b:02X}" for b in sorted(seen)) + "}" if seen else "-"


def watch(port, poll_byte, timeout, preamble):
    """Ground truth. Poll continuously and print the exception byte as it
    changes. Open and close the slot door: a machine that is really
    answering us cannot keep reporting 0x00 through that.
    """
    print("Watching. Open and close the slot door, then Ctrl-C.\n")
    port.parity = PARITY_MARK
    last, start = None, time.monotonic()
    try:
        while True:
            port.reset_input_buffer()
            if preamble:
                port.write(bytes([0x80]))
                time.sleep(0.005)
                port.reset_input_buffer()  # never count the 0x80's own reply
            port.write(bytes([poll_byte]))
            reply = read_reply(port, timeout)
            code = reply[0] if reply else None
            if code != last:
                t = time.monotonic() - start
                shown = f"0x{code:02X}" if code is not None else "(no reply)"
                note = " <- activity" if code not in (None, 0x00) else ""
                print(f"  t+{t:6.1f}s  {shown}{note}")
                last = code
            time.sleep(CADENCE)
    except KeyboardInterrupt:
        print("\nStopped.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=0.3)
    ap.add_argument("-n", type=int, default=20)
    ap.add_argument("--watch", action="store_true", help="poll continuously and print exception bytes as they change")
    ap.add_argument("--no-preamble", action="store_true", help="with --watch, do not send 0x80 first")
    args = ap.parse_args()

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    pb = args.address | 0x80
    n = args.n

    if args.watch:
        watch(port, pb, args.timeout, preamble=not args.no_preamble)
        return 0

    print(f"{args.port} @ {args.baud}, address {args.address} (poll byte 0x{pb:02X}), {n} attempts each\n")

    def set_mark(p):
        p.parity = PARITY_MARK

    def set_space(p):
        p.parity = PARITY_SPACE

    res = {}

    def send_b(p, b):
        p.parity = PARITY_MARK
        p.write(bytes([b]))

    res["B poll alone (shipped path)"] = trial(port, pb, args.timeout, set_space, send_b, n)

    def send_c(p, b):
        p.write(bytes([b]))

    res["C poll alone, no parity calls"] = trial(port, pb, args.timeout, set_mark, send_c, n)

    def send_e(p, b):
        p.parity = PARITY_MARK
        p.write(bytes([0x80]))
        time.sleep(0.005)
        p.write(bytes([b]))

    res["E 0x80, 5ms, poll (confounded)"] = trial(port, pb, args.timeout, set_space, send_e, n)

    # F: the 0x80 entirely on its own. If this answers, the machine is
    #    answering a general poll to address 0 -- the 0x80 is not a
    #    preamble to anything, it IS the poll being answered.
    def send_f(p, _b):
        p.parity = PARITY_MARK
        p.write(bytes([0x80]))

    res["F 0x80 alone, no poll after"] = trial(port, pb, args.timeout, set_space, send_f, n)

    # G: E with the confound removed -- discard whatever the 0x80 drew
    #    before sending the real poll, so only a reply to OUR poll counts.
    def send_g(p, b):
        p.parity = PARITY_MARK
        p.write(bytes([0x80]))
        time.sleep(0.005)
        p.reset_input_buffer()
        p.write(bytes([b]))

    res["G 0x80, flush, then poll"] = trial(port, pb, args.timeout, set_space, send_g, n)

    for label, (got, seen) in res.items():
        print(f"  {label:34} {got}/{n}  bytes {fmt(seen)}")

    # --- which address is real? ----------------------------------------
    print("\nAddress sweep (bare general poll, 3 attempts each):")
    port.parity = PARITY_MARK
    answered = []
    for addr in range(0, 32):
        hits = 0
        for _ in range(3):
            port.reset_input_buffer()
            port.write(bytes([addr | 0x80]))
            if read_reply(port, 0.15):
                hits += 1
            time.sleep(0.05)
        if hits:
            answered.append((addr, hits))
    print("  " + (", ".join(f"addr {a} ({h}/3)" for a, h in answered) if answered else "nothing answered any address 0-31"))

    # --- verdict --------------------------------------------------------
    f_ok = res["F 0x80 alone, no poll after"][0]
    g_ok = res["G 0x80, flush, then poll"][0]
    e_ok = res["E 0x80, 5ms, poll (confounded)"][0]
    print("\n" + "=" * 64)
    if f_ok >= n * 0.9 and g_ok < n * 0.5:
        print("The 0x80 is not a preamble -- it is the poll being answered.")
        print(f"  F answered {f_ok}/{n} with no poll byte sent at all, and G")
        print(f"  answered {g_ok}/{n} once the 0x80's own reply was discarded.")
        print(f"  E's {e_ok}/{n} was reading the reply to the 0x80.")
        print("  => Nothing is answering address "
              f"{args.address}. Use the sweep above, and confirm with --watch.")
    elif g_ok >= n * 0.9:
        print("The 0x80 really is a required preamble: G still answers with")
        print("the 0x80's own reply discarded, so the reply belongs to our")
        print("poll. Worth implementing, and worth asking the vendor why --")
        print("SAS 6.02 does not call for it.")
    elif f_ok < n * 0.5 and g_ok < n * 0.5 and e_ok >= n * 0.9:
        print("Odd: only the confounded case answers. Re-run; if it holds,")
        print("report the whole table rather than acting on it.")
    else:
        print("Mixed. Report the whole table -- the pattern is the finding.")
    print("\nThen: --watch, and open the slot door. If the byte never leaves")
    print("0x00 while the door swings, nothing here is talking to that machine.")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
