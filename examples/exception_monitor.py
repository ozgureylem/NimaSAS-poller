"""Watch a machine's exceptions in plain language while somebody plays it.

Read-only. It general-polls and names what comes back. It never enables
real time event reporting, never changes a setting, never moves money.

Appendix A has 114 exception codes and the interesting ones during play are
0x7E (game has started) and 0x7F (game has ended), plus doors, bills, the
cashout button and handpays. The whole table is in saspy.constants, so this
prints "self test or operator menu has been entered" where the raw byte was
0x84 -- which is the part anyone actually wanted.

    python3 examples/exception_monitor.py /dev/ttyS0 --address 1

ABOUT RTE -- READ THIS BEFORE ENABLING IT
Several codes are marked "(RTE only)": reel stops, cards held, draw card
requests, and coin/credit wagered. Those are only sent when real time event
reporting is on, which the host turns on with long poll 0x0E (Section 12.1).

This library does not implement 0x0E, and that is deliberate for now, because
Section 12.5 says that once RTE is on "gaming machines no longer report
exceptions as single byte codes": every exception comes back as
address, FF, code, data..., CRC. Section 12.3 adds that an event pre-empts a
long poll response, and 12.4 that the host must treat such a reply as a NACK
and re-queue the long poll.

So enabling RTE against this client breaks it everywhere at once -- general
poll reads exactly one byte, and every long poll can come back as an event
instead of its own response. If someone enables it anyway (poll_test_tool
will happily send 0x0E 01), the machine looks dead and the fix is to send
0x0E 00, or power cycle: the spec requires machines to default to
non-RTE polling on power up.

Everything 0x7E/0x7F gives you works today, without any of that.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.client import SASClient
from saspy.constants import (
    GENERAL_POLL_EXCEPTIONS_RTE_ONLY,
    describe_exception,
)
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

# Worth making visually obvious as they scroll past during play.
HIGHLIGHT = {0x7E, 0x7F, 0x66, 0x4F, 0x51, 0x52, 0x3D, 0x3E, 0x67, 0x68, 0x8C}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=0.3)
    ap.add_argument("--interval", type=float, default=0.2,
                    help="seconds between polls (spec floor is 0.2; Section 12.2 allows "
                         "0.04 under RTE, which this tool does not use)")
    ap.add_argument("--show-idle", action="store_true",
                    help="also print 0x00 no-activity polls (noisy; off by default)")
    args = ap.parse_args()

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(
        SASTransport(port, byte_timeout=args.timeout), args.address, timeout=args.timeout
    )

    print(f"{args.port} @ {args.baud}, address {args.address}, polling every {args.interval}s")
    print("Play the machine. Ctrl-C to stop.\n")
    print(f"{'time':12} {'code':6} description")
    print("-" * 78)

    seen = Counter()
    polls = failures = 0
    started = time.monotonic()
    try:
        while True:
            polls += 1
            try:
                code = client.general_poll()
            except SASError as e:
                failures += 1
                print(f"{datetime.now():%H:%M:%S.%f}"[:12] + f" {'--':6} poll failed: {type(e).__name__}: {e}")
                time.sleep(args.interval)
                continue

            seen[code] += 1
            if code == 0x00 and not args.show_idle:
                time.sleep(args.interval)
                continue

            mark = " *" if code in HIGHLIGHT else "  "
            note = ""
            if code in GENERAL_POLL_EXCEPTIONS_RTE_ONLY:
                # Should be impossible without 0x0E; if it shows up, that is a
                # finding about the machine, not about this tool.
                note = "   [RTE-only code seen WITHOUT RTE enabled -- report this]"
            print(f"{datetime.now():%H:%M:%S.%f}"[:12]
                  + f" 0x{code:02X}{mark} {describe_exception(code)}{note}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass

    elapsed = time.monotonic() - started
    print("\n" + "=" * 78)
    print(f"{polls} polls in {elapsed:.0f}s, {failures} failed "
          f"({failures / polls * 100:.2f}%)" if polls else "no polls")
    events = [(c, n) for c, n in seen.items() if c != 0x00]
    if events:
        print("\nexceptions seen:")
        for code, n in sorted(events, key=lambda kv: -kv[1]):
            print(f"   0x{code:02X}  x{n:<5} {describe_exception(code)}")
        if not any(c in (0x7E, 0x7F) for c, _ in events):
            print("\nNo 0x7E/0x7F (game start/end). If games were played, that is")
            print("the finding to chase -- those are queued exceptions and should")
            print("appear in the default polling mode without RTE.")
    else:
        print("\nNothing but 0x00 the whole time.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
