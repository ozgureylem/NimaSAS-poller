"""What the pending handpay is, and clearing it without a reset key.

A handpay USUALLY locks the machine, and exception 0x51 is not proof that
this one has. Section 7.8.1 gives the machine a handpay queue and makes 0x51
a report of an unreported ENTRY in it, reissued every fifteen seconds until
a 0x1B read acknowledges it. An entry outlives the lockup: a handpay reset
at the cabinet leaves its record in the queue, so a machine that is unlocked
and happily taking bets can still be shouting 0x51. Observed exactly that on
an EGT Premier -- 0x51 every fifteen seconds through a run of normal play.
Read 0x1B before concluding anything from the exception alone.

When a handpay IS live, it locks the machine, and nothing else works: Table
8.2b's "transfer from gaming machine OK" bit goes clear, §8.2 forbids the
machine from entering the AFT lock state at all ("waiting for handpay" is on
its list), and so an AFT cashout, a push, and a ticket-out all refuse. The
refusals name their own symptom rather than the cause, which is how a
pending handpay comes to look like six unrelated faults.

Two long polls, and they answer different questions.

  LP 0x1B (§7.8, Table 7.8) says WHAT is pending: the amount, whether a
  progressive contributed, and -- the useful field -- the Reset ID, which
  says whether this machine offers a reset to the credit meter as well as a
  standard reset. It moves no money, so it is safe to run at any time.

  It is not, however, without effect, and §7.8.1 is the reason. 0x51
  announces an entry in a handpay QUEUE, not necessarily a live lockup, and
  "when the gaming machine's response is acknowledged, this entry is marked
  as reported or removed from the queue". So reading 0x1B is itself the
  acknowledgement: it is what stops the machine reissuing 0x51 every fifteen
  seconds, and a second read of the same entry answers all zeros rather than
  repeating it. Two consecutive runs of this tool showing 15,000 and then 0
  are not a machine changing its mind -- they are the queue draining,
  correctly, because the first run read it.

  LP 0x94 (§7.9, Table 7.9a/7.9b) CLEARS it. That is a money-moving
  operation, so it only happens with --reset.

The honest limits of the remote reset, both from §7.9:

  A machine not configured for remote handpay reset "must ignore long poll
  94". Ignoring is silence, so an unconfigured machine produces a timeout,
  not a refusal. The two mean different things and this prints them
  differently: a timeout says the feature is off at the cabinet, reset code
  01 says the feature is on and the machine declined.

  Where the W2-G Reset To Credit Meter function has been enabled by long
  poll 0xA8, §7.9 says 0x94 "will reset the handpay to the credit meter" --
  the money returns to the machine as credits instead of being paid out.
  Reset code 00 is returned either way, so the reset code alone does not say
  where the money went. LP 0x1B's Reset ID is read first for exactly this
  reason, and the meters are read before and after so the answer comes from
  the machine rather than from an assumption.

    python3 examples/handpay_clear.py /dev/ttyS0 --address 1
    python3 examples/handpay_clear.py /dev/ttyS0 --address 1 --reset
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.client import SASClient
from saspy.constants import HANDPAY_RESET_CODES, MeterCode
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

# Table 7.8's Level field. 0x80 is the one a stuck cashout produces.
LEVEL = {
    0x00: "non-progressive win",
    0x40: "non-progressive TOP win",
    0x80: "CANCELLED CREDITS (a cashout that became a handpay)",
}

# Where a handpay can land. Read before and after a reset, because the reset
# code says it happened and not where the money went.
WATCH_METERS = (
    (MeterCode.CURRENT_CREDITS, "current credits"),
    (MeterCode.TOTAL_HAND_PAID_CANCELLED_CREDITS, "total hand paid cancelled credits"),
    (MeterCode.TOTAL_CANCELLED_CREDITS, "total cancelled credits"),
    (MeterCode.TOTAL_HAND_PAID_CREDITS, "total hand paid credits"),
    (MeterCode.TOTAL_JACKPOT_CREDITS, "total jackpot credits"),
)


def read_meters(client) -> dict:
    out = {}
    for code, label in WATCH_METERS:
        try:
            result = client.send_extended_meters([int(code)])
            out[int(code)] = (label, result.meters.get(int(code)))
        except SASError as e:
            out[int(code)] = (label, f"({type(e).__name__})")
    return out


def show_deltas(before: dict, after: dict) -> None:
    print("\nmeters before -> after:")
    for code, (label, old) in before.items():
        new = after.get(code, (label, None))[1]
        delta = f"   delta {new - old:+}" if isinstance(old, int) and isinstance(new, int) else ""
        print(f"  {label:38} {old} -> {new}{delta}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--reset", action="store_true",
                    help="send LP 0x94 and clear the handpay. Moves money")
    ap.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    args = ap.parse_args()

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(
        SASTransport(port, byte_timeout=args.timeout), args.address, timeout=args.timeout)

    # --- what is pending (LP 0x1B, read-only) ---------------------------
    print("Handpay information (LP 0x1B, read-only):")
    try:
        info = client.send_handpay_information()
    except SASError as e:
        print(f"  0x1B failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    level_text = LEVEL.get(info.level, f"progressive level {info.level} (01 highest, 20 lowest)")
    progressive = info.level not in (0x00, 0x40, 0x80)
    units = "cents" if progressive else "SAS accounting denom units"

    print(f"  progressive group  {info.progressive_group}")
    print(f"  level              0x{info.level:02X}  {level_text}")
    print(f"  amount             {info.amount:,}  ({units})")
    if not progressive:
        # Table 7.8 is explicit that the units differ, and the machine does
        # not say which. Guessing cents here would misreport the figure on
        # any machine whose accounting denom is not one cent.
        print(f"                     = {info.amount / 100:,.2f} only if this machine's")
        print(f"                     accounting denomination is one cent (it is on the")
        print(f"                     EGT Premier tested; confirm before trusting it)")
    print(f"  partial pay        {info.partial_pay:,}")
    print(f"  reset ID           0x{info.reset_id:02X}  "
          f"{'standard reset AND reset to credit meter available' if info.reset_id == 0x01 else 'only the standard reset is available'}")

    if info.amount == 0 and info.level == 0x00:
        print("\nNothing looks pending. A machine with no handpay still answers")
        print("0x1B -- with zeros -- so this is 'probably idle', not proof. The")
        print("general poll is what reports 0x51 while a handpay is actually")
        print("waiting; exception_monitor.py watches for it.")

    if not args.reset:
        print("\nRead-only run. Nothing was sent that changes anything.")
        print("Add --reset to clear the handpay with LP 0x94.")
        return 0

    # --- clearing it (LP 0x94) ------------------------------------------
    print("\nAbout to send LP 0x94, reset handpay.")
    if info.reset_id == 0x01:
        print("  Reset ID 01: this machine can reset the handpay TO THE CREDIT")
        print("  METER. Section 7.9 says 0x94 does exactly that when the W2-G")
        print("  Reset To Credit Meter function has been enabled by LP 0xA8, which")
        print("  this tool does not send. So the money may be paid out OR returned")
        print("  to the credit meter, and the reset code is 00 either way. The")
        print("  meters below are what settles it.")
    else:
        print("  Reset ID 00: standard reset only, so this records the handpay as")
        print("  paid. The money is accounted as having left the machine by hand --")
        print("  make sure that matches physical reality before sending it.")

    if not args.yes:
        if input("\nType RESET to proceed: ").strip() != "RESET":
            print("Aborted. Nothing sent.")
            return 0

    before = read_meters(client)
    try:
        code = client.reset_handpay()
    except SASError as e:
        print(f"\nNo answer to 0x94 ({type(e).__name__}: {e}).")
        print("Section 7.9: a machine not configured for remote handpay reset")
        print("'must ignore long poll 94'. Silence is therefore the documented")
        print("behaviour of a cabinet with the feature switched off: not a fault,")
        print("and not a refusal either. This one needs the reset key, or the")
        print("setting enabled in its operator menu.")
        return 1

    print(f"\nreset code 0x{code:02X}  "
          f"{HANDPAY_RESET_CODES.get(code, 'undocumented code')}")
    after = read_meters(client)
    show_deltas(before, after)

    print()
    if code == 0x00:
        credits_before = before.get(int(MeterCode.CURRENT_CREDITS), (None, None))[1]
        credits_after = after.get(int(MeterCode.CURRENT_CREDITS), (None, None))[1]
        if (isinstance(credits_before, int) and isinstance(credits_after, int)
                and credits_after > credits_before):
            print(f"The handpay was reset TO THE CREDIT METER: current credits rose")
            print(f"by {credits_after - credits_before:,}. The money is back on the")
            print("machine, not paid out, so it still has to leave by some route --")
            print("aft_cashout.py or a ticket.")
        else:
            print("The handpay was reset and current credits did not rise, so it was")
            print("recorded as PAID. The hand-paid meters above should have moved by")
            print("the handpay amount; if none of them did, the machine metered it")
            print("somewhere this list is not watching.")
        print()
        print("The lock should now be gone, which means an AFT transfer can go")
        print("through: Table 8.2b's 'transfer from gaming machine OK' bit is set")
        print("only when the machine would let a player cash out.")
    elif code == 0x01:
        print("The machine is configured for remote reset and DECLINED this one.")
        print("That is different from silence: the feature is on. Something about")
        print("the machine's current state refused it -- a door open, an operator")
        print("menu, or a jurisdictional rule requiring the key for this amount.")
    elif code == 0x02:
        print("The machine says it is not in a handpay condition, so there was")
        print("nothing to clear. If the general poll is still returning 0x51, the")
        print("two disagree and the exception queue is the one to trust -- it is")
        print("non-volatile (§2.2.1) and may be reporting a handpay already")
        print("cleared at the cabinet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
