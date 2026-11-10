"""Read the handpay queue the way section 7.8.1 says to, one entry at a time.

Reads only. No LP 0x94, so no handpay is reset and no money moves. But it is
NOT without effect, and the effect is the point: reading an entry and then
acknowledging it is what removes it from the machine's queue. That is the
host's job, and nothing in this toolchain has ever done it.

What 7.8.1 actually specifies, because every symptom follows from it:

  The machine keeps an n-entry FIFO handpay queue, minimum five. On a
  handpay it stores everything LP 0x1B will need and sends exception 51.
  "If the handpay queue is already full, the oldest handpay record will be
  lost." LP 0x1B returns the OLDEST UNREPORTED entry; the entry is marked
  reported "when the gaming machine's response is acknowledged". If LP 0x1B
  does not arrive within fifteen seconds, exception 51 is reissued every
  fifteen seconds "as long as the entry is in the queue".

  And the part that explains the lab cabinet: "Exception 52 is not sent
  until the corresponding 1B long poll has been received, responded to and
  acknowledged, and the associated handpay has been reset."

So a host that never reads LP 0x1B gets exception 51 forever, never sees
exception 52 at all even when a technician clears the lockup at the machine,
and once more than n handpays accumulate the oldest records are destroyed.
On the EGT Premier: 250 exception 51s in sixty-four minutes and not one 52,
with handpays physically cleared during that window.

RECORD BEFORE ACKNOWLEDGE. Each entry is written to the output file and
flushed to disk BEFORE the acknowledging poll goes out, because the
acknowledgement is what makes the entry unreadable again. This is the same
rule as durable-write-before-send on the gateway, for the same reason, and
getting it the wrong way round loses exactly the records the run exists to
recover.

THE ACKNOWLEDGEMENT IS NOT ASSUMED. Section 3.2 makes two consecutive
identical long polls indistinguishable from a NACK -- which is why LP 0x6F
has 0xAF as a twin, and LP 0x1B has no twin at all. This sends a general
poll between reads to serve as the acknowledgement, and then CHECKS whether
the queue advanced. If the same entry comes back, the run stops and says the
acknowledgement was not accepted rather than looping over one record and
reporting it as many.

    python3 examples/handpay_queue_drain.py /dev/ttyS0 --address 1
    python3 examples/handpay_queue_drain.py /dev/ttyS0 --address 1 --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.client import SASClient
from saspy.exceptions import SASError, SASTimeoutError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

POLL_FLOOR = 0.25  # §2.3.3: never poll one machine faster than once per 200 ms
MAX_ENTRIES = 32   # 7.8.1's queue is "minimum of 5"; no maximum is stated

# Table 7.8's level byte, as far as the spec names it.
LEVELS = {
    0x80: "CANCELLED CREDITS (a cashout that became a handpay)",
    0x00: "progressive or non-progressive jackpot",
}


def as_dict(info) -> dict:
    return {"progressive_group": info.progressive_group, "level": info.level,
            "amount": info.amount, "partial_pay": info.partial_pay,
            "reset_id": info.reset_id}


def is_empty(d: dict) -> bool:
    """7.8.1: 'Long poll 1B returns all zeros if a handpay record has been
    reported and acknowledged, and a subsequent exception 51 has not been
    issued and acknowledged.' All zeros is the queue saying it is done."""
    return not any(d.values())


def describe(d: dict, denom_is_cent: bool) -> str:
    money = f" = ${d['amount'] / 100:,.2f}" if denom_is_cent else ""
    return (f"group {d['progressive_group']}  level 0x{d['level']:02X} "
            f"{LEVELS.get(d['level'], 'unnamed in Table 7.8')}\n"
            f"      amount {d['amount']:,}{money}   partial pay {d['partial_pay']:,}"
            f"   reset ID 0x{d['reset_id']:02X}")


def write_now(path: Path, payload: dict) -> None:
    """Flushed and fsynced. The next thing this program does is make the
    entry unreadable, so a buffered write is a lost record."""
    with path.open("w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--out", default=None,
                    help="where the entries are written (default handpay-queue-<UTC>.json "
                         "in the current directory)")
    ap.add_argument("--max-entries", type=int, default=MAX_ENTRIES)
    ap.add_argument("--dry-run", action="store_true",
                    help="read ONE entry and stop, sending no acknowledging poll. "
                         "Shows what is at the head of the queue without advancing it.")
    ap.add_argument("--denom-is-cent", action="store_true", default=True,
                    help="interpret the amount as cents (true on the EGT Premier tested)")
    args = ap.parse_args()

    started = dt.datetime.now(dt.timezone.utc)
    out = Path(args.out or f"handpay-queue-{started:%Y-%m-%dT%H-%M-%SZ}.json")

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(SASTransport(port, byte_timeout=args.timeout),
                       args.address, timeout=args.timeout)

    entries: list[dict] = []
    payload = {"started_utc": started.isoformat(timespec="seconds"),
               "port": args.port, "address": args.address,
               "entries": entries, "complete": False, "stopped_because": None}

    print(f"\nDraining the handpay queue on {args.port} address {args.address}.")
    print(f"Entries are written to {out} before each acknowledgement.\n")

    stopped = None
    for n in range(1, args.max_entries + 1):
        try:
            info = client.send_handpay_information()
        except SASTimeoutError:
            stopped = ("no answer to LP 0x1B. §4.4 lets a machine ignore a long poll "
                       "it does not implement, but this one answered earlier in the "
                       "run, so silence here is a real failure, not 'unsupported'."
                       if entries else
                       "no answer to LP 0x1B at all. Either the machine does not "
                       "implement it (§4.4) or the link is down -- connectivity_check "
                       "tells them apart.")
            break
        except SASError as e:
            stopped = f"LP 0x1B failed: {type(e).__name__}: {e}"
            break

        d = as_dict(info)
        if is_empty(d):
            stopped = "LP 0x1B returned all zeros -- the queue is drained (§7.8.1)."
            payload["complete"] = True
            break

        if entries and d == entries[-1]["record"]:
            stopped = ("the SAME entry came back after an acknowledging poll. The "
                       "queue is not advancing, so the acknowledgement is not being "
                       "accepted (§3.2 -- LP 0x1B has no alternate command code the "
                       "way 0x6F has 0xAF). Stopping rather than counting one record "
                       "as many.")
            break

        entries.append({"index": n,
                        "read_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                        "record": d})
        print(f"  entry {n}: {describe(d, args.denom_is_cent)}")

        # Durable FIRST. The next poll is what makes this unreadable.
        write_now(out, payload)

        if args.dry_run:
            stopped = ("--dry-run: one entry read, no acknowledging poll sent, so "
                       "nothing was removed from the queue.")
            break

        time.sleep(POLL_FLOOR)
        try:
            client.general_poll()  # a different message: the implied acknowledgement
        except SASError:
            pass  # silence to a general poll just means no pending exception
        time.sleep(POLL_FLOOR)

    payload["stopped_because"] = stopped
    write_now(out, payload)

    print(f"\n{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} recovered.")
    print(f"{stopped}")
    if entries and args.denom_is_cent:
        total = sum(e["record"]["amount"] for e in entries)
        print(f"total across recovered entries: {total:,} = ${total / 100:,.2f}")
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
