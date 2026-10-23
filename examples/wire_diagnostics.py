"""Settle how this machine wants to be polled, before trusting any poll loop.

Read-only. It sends general polls and a handful of read-only long polls.
It never writes a meter, moves funds, prints a ticket, or changes machine
state, so it is safe on a live machine -- unlike poll_test_tool.py.

Three questions, in the order they actually block progress:

  A. Does a plain general poll work on its own?
  B. Does a 0x80 "sync" before each poll help, hurt, or do nothing -- and
     does the gap after it matter?
  C. Do LONG polls work? Every long poll is a multi-byte frame, and a
     multi-byte frame exercises a parity transition that a single-byte
     general poll never touches. A bus that passes A and B can still fail
     every long poll, which is most of what this library does.

Why B is phrased as "help, hurt, or do nothing": sending 0x80 before each
general poll is not something the SAS 6.02 spec asks for, and nothing in
this library does it. It is worth measuring precisely because it is an
assumption, and the only honest way to measure it is to run the identical
harness with it on and off. Every condition below is counted over the
same number of attempts with the same error handling -- a loop that stops
at its first failure and a loop that counts failures are not comparable
evidence, however similar they look.

    python3 examples/wire_diagnostics.py /dev/ttyS0 --address 1
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.client import SASClient
from saspy.exceptions import SASError, SASPortCapabilityError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

CADENCE = 0.2  # section 2.3.3 floor; keeps every condition on equal terms


def run_condition(transport, address, *, sync, gap, n, timeout):
    """Count replies to ``n`` general polls. Never aborts early -- the
    failure rate IS the measurement."""
    ok = 0
    for _ in range(n):
        try:
            if sync:
                transport.write_with_wakeup(bytes([0x80]))
                if gap:
                    time.sleep(gap)
            transport.general_poll(address, timeout=timeout)
            ok += 1
        except SASError:
            pass
        time.sleep(CADENCE)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=0.3)
    ap.add_argument("-n", type=int, default=30, help="attempts per condition (default 30)")
    args = ap.parse_args()

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except SASPortCapabilityError as e:
        print(f"\n{e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"\nCould not open {args.port}: {e}", file=sys.stderr)
        return 2

    transport = SASTransport(port, byte_timeout=args.timeout)
    client = SASClient(transport, args.address, timeout=args.timeout)
    n = args.n

    print(f"{args.port} @ {args.baud}, address {args.address}, {n} attempts per condition")
    print(f"(~{n * CADENCE:.0f}s per condition; read-only throughout)\n")

    # --- A: is the bus healthy with nothing clever at all? -------------
    print("A. Plain general poll, no sync byte")
    baseline = run_condition(transport, args.address, sync=False, gap=0, n=n, timeout=args.timeout)
    print(f"   {baseline}/{n} replies\n")

    if baseline == 0:
        print("Nothing answered a plain general poll. Fix that before "
              "interpreting anything else: wrong address is the usual cause.")
        return 1

    # --- B: does the sync byte earn its place? -------------------------
    print("B. 0x80 sync before each poll, by gap")
    gaps = (0.0, 0.002, 0.005, 0.01, 0.02)
    results = {}
    for gap in gaps:
        got = run_condition(transport, args.address, sync=True, gap=gap, n=n, timeout=args.timeout)
        results[gap] = got
        print(f"   sync + {gap * 1000:5.1f} ms gap : {got}/{n}")
    print()

    # --- C: long polls, the part none of the above proves --------------
    print("C. Long polls (multi-byte frames -- the parity transition)")
    long_polls = [
        ("0x54 SAS version/serial", client.send_sas_version_and_serial),
        ("0x1F gaming machine ID", client.send_gaming_machine_id),
        ("0x0F meters 10-15", client.send_meters_10_through_15),
        ("0xA0 enabled features", client.send_enabled_features),
    ]
    lp_results = {}
    for label, fn in long_polls:
        ok, last_err, sample = 0, None, None
        for _ in range(10):
            try:
                result = fn()
                ok += 1
                sample = sample or result
            except SASError as e:
                last_err = f"{type(e).__name__}: {e}"
            time.sleep(CADENCE)
        lp_results[label] = ok
        detail = f"  <- {sample}" if sample else (f"  ({last_err})" if last_err else "")
        print(f"   {label:26} {ok}/10{detail}")
    print()

    # --- verdict --------------------------------------------------------
    print("=" * 64)
    best_sync = max(results.values())
    no_gap = results[0.0]

    print(f"A. plain general poll ....... {baseline}/{n}")
    if baseline == n and best_sync <= baseline:
        print("   The sync byte buys nothing here. Don't send it: it is not")
        print("   in the spec, and an unnecessary byte per cycle is a cost.")
    elif best_sync > baseline:
        best_gap = max(results, key=lambda g: results[g])
        print(f"   The sync byte DOES help (best {best_sync}/{n} at {best_gap * 1000:.0f} ms).")
        print("   Worth adding -- and worth asking why this machine needs it.")
    if no_gap < best_sync:
        print(f"   Back-to-back sync is worse ({no_gap}/{n}); the gap is doing the work.")
    elif no_gap == best_sync and best_sync > 0:
        print(f"   Gap size made no difference ({no_gap}/{n} with none).")

    worst_lp = min(lp_results.values())
    print(f"\nC. long polls ............... {worst_lp}/10 worst case")
    if worst_lp == 10:
        print("   Multi-byte framing works. This is the result that matters")
        print("   most: it is what the rest of the library depends on.")
    elif worst_lp == 0 and baseline == n:
        print("   General polls fine, long polls dead. That is the signature of")
        print("   the wakeup bit being lost on the first byte of multi-byte")
        print("   frames. Confirm you are running a build that drains the")
        print("   transmitter before lowering parity (saspy/transport.py).")
    else:
        print("   Partial. Record which ones and report the exact bytes.")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
