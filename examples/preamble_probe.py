"""Is it the 0x80, or is it just that the line was quiet? And for how long?

Read-only: general polls only. Nothing changes machine state.

Established on an EGT machine: a general poll sent on its own after about
half a second of silence is answered 0/20, and the same poll preceded by
0x80 and a 5ms gap is answered 20/20 -- with the preamble's own reply
discarded, so the reply provably belongs to the real poll. The machine
does not answer the 0x80 itself (0/20 when sent alone), and opening the
slot door moves the exception byte to 0x11/0x12, so the link is real.

What that does NOT establish is why. Two readings remain, and they lead
to different code:

  1. SAS on this machine wants a 0x80 before a poll. Then the byte is
     protocol, and only 0x80 will do.
  2. The first byte sent after the line goes quiet is lost, whatever it
     is -- the far end needs the line driven before it reads reliably.
     Then the byte is a throwaway, any value works, and what actually
     matters is how long "quiet" has to be.

Reading 2 is favoured by something already in hand: during the address
sweep in wakeup_probe.py, address 1 answered 2 of 3 bare polls with no
0x80 anywhere near them -- the only difference being that those polls
followed the previous byte by ~50ms instead of ~500ms.

Section 1 sends a different throwaway byte each time. If 0x00 and 0xFF
work as well as 0x80, the byte is not protocol.
Section 2 drops the preamble entirely and varies the silence before the
poll, to find where it stops being answered.

Both answers go straight into SASTransport(wake_byte=..., wake_after_idle=...).

    python3 examples/preamble_probe.py /dev/ttyS0 --address 1
"""

from __future__ import annotations

import argparse
import sys
import time
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=0.3)
    ap.add_argument("-n", type=int, default=20)
    args = ap.parse_args()

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    pb = args.address | 0x80
    n = args.n
    port.parity = PARITY_MARK
    print(f"{args.port} @ {args.baud}, address {args.address} (poll 0x{pb:02X}), {n} attempts each\n")

    # --- 1. does the identity of the throwaway byte matter? ------------
    print("1. Preamble byte (0.5s of silence before each attempt, 5ms gap after)")
    by_byte = {}
    for name, value in (("0x80 broadcast poll", 0x80), ("0x00 null", 0x00),
                        ("0xFF all ones", 0xFF), ("none (control)", None)):
        ok = 0
        for _ in range(n):
            time.sleep(0.5)  # force the line quiet
            port.reset_input_buffer()
            if value is not None:
                port.write(bytes([value]))
                if hasattr(port, "flush"):
                    port.flush()
                time.sleep(0.005)
                port.reset_input_buffer()
            port.write(bytes([pb]))
            if read_reply(port, args.timeout):
                ok += 1
        by_byte[name] = ok
        print(f"   {name:22} {ok}/{n}")

    # --- 2. how long does the line have to be quiet to break it? -------
    print("\n2. No preamble; silence before the poll")
    by_idle = {}
    for idle in (0.005, 0.02, 0.05, 0.1, 0.2, 0.5):
        ok = 0
        for _ in range(n):
            # A byte to warm the line, then the chosen silence, then poll.
            port.reset_input_buffer()
            port.write(bytes([0x80]))
            if hasattr(port, "flush"):
                port.flush()
            time.sleep(idle)
            port.reset_input_buffer()
            port.write(bytes([pb]))
            if read_reply(port, args.timeout):
                ok += 1
            time.sleep(0.05)
        by_idle[idle] = ok
        print(f"   {idle * 1000:6.0f} ms quiet      {ok}/{n}")

    # --- verdict --------------------------------------------------------
    any_byte = min(by_byte["0x80 broadcast poll"], by_byte["0x00 null"], by_byte["0xFF all ones"])
    control = by_byte["none (control)"]
    print("\n" + "=" * 66)
    if any_byte >= n * 0.9 and control < n * 0.5:
        print("Any byte works, none does not: the preamble is a throwaway, not")
        print("protocol. The far end needs the line driven before it reads --")
        print("a link-layer fault, so worth raising with whoever built the")
        print("interface rather than living with forever.")
    elif by_byte["0x80 broadcast poll"] >= n * 0.9 and any_byte < n * 0.5:
        print("Only 0x80 works. That is protocol, not a line effect, and the")
        print("vendor should be asked why -- SAS 6.02 does not call for it.")
    else:
        print("Section 1 is mixed; report it as-is.")

    good = [ms for ms, got in by_idle.items() if got >= n * 0.9]
    if good:
        print(f"\nLongest silence still answered: {max(good) * 1000:.0f} ms.")
        print(f"  => SASTransport(wake_after_idle={max(good):.3f}) skips the wake byte")
        print("     on back-to-back exchanges, which a meter sweep is dozens of.")
        print("     Leave it at 0.0 if you would rather spend the byte than the risk.")
    else:
        print("\nNo silence was reliably answered without a preamble: keep")
        print("wake_after_idle at 0.0, so every message carries the wake byte.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
