#!/usr/bin/env python3
"""Ask the machine what polling rate it declares, and read back the spec.

One long poll, read-only, changes nothing. Written to settle a specific
recurring claim: that a host MUST poll faster than 40 ms or real time events
are lost.

What the specification actually says, in four places:

  §2.3.3 Polling Rate
    "The host may not issue general polls or long polls to any single gaming
     machine at a rate faster than once per 200 ms. The slowest allowable
     polling rate is 5000 ms (five seconds)."
    "Note that some SAS features, such as RTE and ticketing, require THE
     GAMING MACHINE TO SUPPORT a 40 ms polling rate."

  §12.2 Polling Method
    "the polling rate CAN be increased to 40 ms in order to BETTER
     APPROXIMATE REAL TIME reporting."

  Table 7.14e, Feature Codes 3, bit 0
    "Maximum polling rate: 0 = Not specified, 1 = 40 milliseconds"

  §2.2.1 General Polls
    "It is possible for a gaming machine to generate a series of exceptions
     at a rate that is faster than the polling cycle of the host. To
     accommodate this, gaming machines must maintain a first in/first out
     (FIFO) exception queue of at least 20 elements in non-volatile memory."

Read together: the 40 ms obligation falls on the MACHINE -- it must tolerate
being polled that fast -- and is advertised as a capability bit. The host's
obligation is the opposite one, a 200 ms floor. §12.2 makes the fast rate
optional and says what it buys: lower latency, not fewer lost events.
Retention is the FIFO queue's job, and §12 does not modify it.

Where the confusion comes from, fairly: §2.3.3's sentence is easy to read as
"RTE requires a 40 ms polling rate" if the words "the gaming machine to
support" are skipped. That is a genuine trap in the text, not an invention.

What this tool CANNOT tell you: whether a given cabinet actually retains
events at a slower rate. A declared capability is a promise about tolerance,
not about queue depth under load. For that, run a soak with --rte and then
examples/rte_reconcile.py.

    python3 examples/polling_capability.py /dev/ttyS0 --address 1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.client import SASClient
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

METER_MODEL = {
    0: "not specified",
    1: "won credits metered when won",
    2: "won credits metered when played or paid",
    3: "reserved",
}
VALIDATION_STYLE = {0: "standard or none", 1: "system", 2: "secure enhanced", 3: "reserved"}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=0.5)
    args = ap.parse_args()

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(SASTransport(port, byte_timeout=args.timeout),
                       args.address, timeout=args.timeout)

    try:
        features = client.send_enabled_features()
    except SASError as e:
        print(f"LP 0xA0 failed: {type(e).__name__}: {e}", file=sys.stderr)
        print("\nA machine that does not answer 0xA0 declares nothing either way. "
              "That is not evidence about its polling tolerance.", file=sys.stderr)
        return 1

    print(f"\nLP 0xA0 enabled features, game {features.game_number}\n")
    print("  Table 7.14e -- Feature Codes 3")
    if features.max_polling_rate_40ms:
        print("    maximum polling rate ........ 40 ms DECLARED (bit 0 = 1)")
        print("      This machine says it tolerates being polled every 40 ms.")
        print("      It does NOT say the host has to. §2.3.3 forbids the host from")
        print("      polling a single machine faster than once per 200 ms, and")
        print("      §12.2 makes the fast rate optional: it 'can be increased to")
        print("      40 ms in order to better approximate real time reporting'.")
    else:
        print("    maximum polling rate ........ NOT SPECIFIED (bit 0 = 0)")
        print("      7.14e's own caveat: machines older than SAS 6.01 may support")
        print("      40 ms without setting this bit, so this means 'not declared',")
        print("      never 'not capable'.")
        print("      But note what it does NOT mean either: a machine that declares")
        print("      nothing here is not thereby excused from §2.2.1's queue, which")
        print("      is what actually retains events between polls.")
    print(f"    multiple SAS progressive win  {features.multiple_sas_progressive_win_reporting}")

    print("\n  Table 7.14d -- Feature Codes 2")
    print(f"    meter model ................. {METER_MODEL.get(features.meter_model, '?')}")
    print(f"    tickets in total drop ....... {features.tickets_in_total_drop}")
    print(f"    extended meters (LP 0x6F) ... {features.extended_meters}")
    print(f"    component authentication .... {features.component_authentication}")
    print(f"    advanced funds transfer ..... {features.advanced_funds_transfer}")
    print(f"    multi-denom extensions ...... {features.multi_denom_extensions}")

    print("\n  Table 7.14c -- Feature Codes 1")
    print(f"    jackpot multiplier .......... {features.jackpot_multiplier}")
    print(f"    AFT bonus awards ............ {features.aft_bonus_awards}")
    print(f"    legacy bonus awards ......... {features.legacy_bonus_awards}")
    print(f"    tournament .................. {features.tournament}")
    print(f"    validation extensions ....... {features.validation_extensions}")
    print(f"    validation style ............ "
          f"{VALIDATION_STYLE.get(features.validation_style, '?')}")
    print(f"    ticket redemption ........... {features.ticket_redemption}")

    print("\n  Whether THIS cabinet keeps every event at a slower rate is an")
    print("  empirical question this poll cannot answer. Run a soak with --rte")
    print("  and then examples/rte_reconcile.py.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
