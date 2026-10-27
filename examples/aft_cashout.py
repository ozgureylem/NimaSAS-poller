"""Take the credits off the machine electronically, without a ticket.

THIS MOVES MONEY, off the machine. Lab machines only.

This is the mirror image of aft_push_credits.py and the host-initiated
sibling of host_cashout.py. The difference between the two cashout tools is
who starts it, and it matters:

  host_cashout.py waits for the PLAYER. Section 8.7: the player presses cash
  out, the machine issues exception 6A and waits up to 8 seconds for the host
  to take the credits. It only happens if the machine is configured for
  cashout to host.

  THIS tool starts it from the HOST. Section 8.4: "the gaming machine may
  perform transfers from the gaming machine to the host at any time it would
  otherwise normally allow the player to cash out." No exception, no player,
  no 8-second clock, no dependence on the host-cashout configuration. It is
  what a CMS does when a session ends and the balance goes back to the
  account.

Pressing cash out on this cabinet does BOTH, in that order. An earlier note
here said a working printer means the player's button simply prints, and
that is wrong: the machine asks the host first and prints only when nobody
answers. Observed on 2026-10-27 -- 0x66 cash out button pressed, then
exception 0x6A reissued four times over the next ten seconds, then the
ticket. Section 8.7's eight-second host budget, run to expiry, and then
section 8.7's SOFT-mode recovery doing its job quietly.

So the player's button is not a way to avoid the host; it is a way to make
the host race a clock. This tool is the way to avoid the clock: the host
decides, long poll 72 with transfer type 80 goes out immediately, and no
paper is produced.

How "take everything" is expressed (section 8.4): all three amounts set to
9999999999 with transfer code 01, partial transfer allowed. The machine then
transfers what it actually has and reports status 01, PARTIAL SUCCESS. That
01 is the success code for this operation -- asking for 9999999999 and
receiving the real balance is partial by definition, and a host testing for
00 alone would read every cashout as a failure.

Two spec details that look like bugs and are not:

  A zero-amount success. Section 8.4: "If there are no credits on the gaming
  machine when the host requests a transfer of all available credits to the
  host, if possible the gaming machine should perform a successful transfer
  for a total amount of zero." An empty machine answering 00 for 0.00 is
  correct behaviour, not a failed transfer.

  Partial refusal. Section 8.4: "A gaming machine is not required to allow
  cashouts from the credit meter of less than the full available amount for
  each type", and Table 8.2b AFT status bit 1 reports whether this machine
  does. So --amount is checked against that bit before anything is sent,
  because on a machine with the bit clear a partial request is refused
  outright rather than rounded down.

What this tool deliberately does NOT set is transfer flag bit 3, "cashout
from gaming machine request". Section 8.4 describes it as a way for the host
to "effectively press the cashout button on the gaming machine": any credits
above the amounts requested get cashed out by the machine's own means, which
on this cabinet now means a printed ticket. Setting it would produce exactly
the paper this tool exists to avoid.

No game lock is taken. Section 8.2 says the host "may request a gaming
machine lock prior to performing an AFT transfer, to hold the gaming machine
in a state where it is reasonably expected to be able to perform the
transfer" -- may, not must, and section 8.4 allows the transfer whenever the
player would be allowed to cash out. A lock buys nothing on a lab machine
nobody else is touching, and costs a timeout to get wrong, so this reads the
state with lock code FF and leaves the lock alone. A busy floor is the case
where locking earns its keep.

One state blocks all of this and is easy to miss: a pending handpay. Section
8.2 lists "waiting for handpay" among the conditions in which the machine
must never enter the locked state, and the same conditions clear Table 8.2b's
"transfer from gaming machine OK" bit. So a machine sitting on exception 0x51
will refuse the cashout, and the pre-flight below says so rather than letting
long poll 72 fail obscurely.

    python3 examples/aft_cashout.py /dev/ttyS0 --address 1
    python3 examples/aft_cashout.py /dev/ttyS0 --address 1 --amount 11.11
    python3 examples/aft_cashout.py /dev/ttyS0 --address 1 --cashable-only
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.client import SASClient
from saspy.constants import (
    AFT_TRANSFER_CODE_FULL_ONLY,
    AFT_TRANSFER_CODE_PARTIAL_ALLOWED,
    AftTransferType,
    MeterCode,
    describe_aft_transfer_status,
)
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

# Section 8.4's sentinel for "all of it".
TAKE_EVERYTHING = 9_999_999_999

# Table C-7, the to-host side. Identical to host_cashout.py's list on purpose:
# the two tools reach the same meters by different routes, and a side-by-side
# comparison of their output is only meaningful if they report the same rows.
VERIFY_METERS = (
    (MeterCode.CURRENT_CREDITS, "current credits"),
    (MeterCode.CURRENT_RESTRICTED_CREDITS, "current RESTRICTED credits"),
    (0x00B8, "cashable transfers to HOST (cents)"),
    (0x00B9, "cashable transfers to HOST (quantity)"),
    (0x00BA, "RESTRICTED transfers to HOST (cents)"),
    (0x00BB, "RESTRICTED transfers to HOST (quantity)"),
    (0x00BC, "NONRESTRICTED transfers to HOST (cents)"),
    (0x00BD, "NONRESTRICTED transfers to HOST (quantity)"),
    (0x0032, "screen 'Cashable Electronic Out'"),
    (0x0034, "screen 'Promotional Electronic Out'  (SAS NONrestricted)"),
    (0x0033, "screen 'Non-cashable Electronic Out' (SAS RESTRICTED)"),
    (MeterCode.TOTAL_ELECTRONIC_TRANSFERS_TO_HOST_CREDITS,
     "total electronic transfers to host"),
    # Play meters, so a game running between the two reads cannot look like
    # money going missing.
    (MeterCode.TOTAL_COIN_IN_CREDITS, "total bet (coin in)"),
    (MeterCode.TOTAL_RESTRICTED_AMOUNT_PLAYED_CREDITS,
     "screen 'Total Non-cashable Played'  (SAS RESTRICTED)"),
    (MeterCode.TOTAL_NONRESTRICTED_AMOUNT_PLAYED_CREDITS,
     "screen 'Total Promotional Played'   (SAS NONrestricted)"),
)


def read_meters(client) -> dict:
    out = {}
    for code, label in VERIFY_METERS:
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
        print(f"  {label:44} {old} -> {new}{delta}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--amount", type=float, default=None,
                    help="cashable amount to take, in currency units. Omitted "
                         "means take the whole balance of every type")
    ap.add_argument("--cashable-only", action="store_true",
                    help="take the whole cashable balance and leave restricted "
                         "and nonrestricted on the machine")
    ap.add_argument("--transaction-id", default=None,
                    help="1-20 ASCII characters (default NIMAOUT<unix time>)")
    ap.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    args = ap.parse_args()

    if args.amount is not None and args.cashable_only:
        print("--amount and --cashable-only are different requests; pass one.",
              file=sys.stderr)
        return 2
    if args.amount is not None and args.amount <= 0:
        print("--amount must be positive. To take everything, omit it.", file=sys.stderr)
        return 2

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(
        SASTransport(port, byte_timeout=args.timeout), args.address, timeout=args.timeout)

    # --- pre-flight (0x74 with lock code FF: a query, the lock is untouched) --
    print("Pre-flight (0x74, query only -- lock untouched):")
    try:
        lock = client.aft_game_lock_and_status_request(lock_code=0xFF)
    except SASError as e:
        print(f"  0x74 failed: {type(e).__name__}: {e}", file=sys.stderr)
        print("  Cannot establish the machine's state. Not sending a transfer.",
              file=sys.stderr)
        return 1

    aft_on = bool(lock.aft_status & 0x80)
    in_house = bool(lock.aft_status & 0x10)
    partial_ok = bool(lock.aft_status & 0x02)   # Table 8.2b AFT status bit 1
    from_machine = bool(lock.available_transfers & 0x02)  # available transfers bit 1

    cashable = lock.current_cashable_amount
    restricted = lock.current_restricted_amount
    nonrestricted = lock.current_non_restricted_amount
    on_machine = cashable + restricted + nonrestricted

    print(f"  asset number             {lock.asset_number}")
    print(f"  AFT enabled              {aft_on}")
    print(f"  in-house enabled         {in_house}")
    print(f"  transfer FROM GM now OK  {from_machine}   (available transfers bit 1)")
    print(f"  partial to-host allowed  {partial_ok}   (AFT status bit 1)")
    print(f"  credits on machine       {cashable / 100:,.2f} cashable"
          f"  + {restricted / 100:,.2f} restricted"
          f"  + {nonrestricted / 100:,.2f} nonrestricted"
          f"  = {on_machine / 100:,.2f}")

    blockers = []
    if not aft_on:
        blockers.append("AFT is disabled on the machine (0x74 AFT status bit 7)")
    if not in_house:
        blockers.append("in-house transfers are disabled (AFT status bit 4)")
    if not from_machine:
        blockers.append(
            "transfer FROM the gaming machine is not currently available "
            "(Table 8.2b available transfers bit 1). The machine sets that bit "
            "only when it is in a state where it would let the player cash out, "
            "so door open, operator menu, tilt, a pending handpay or a transfer "
            "already in progress all clear it. Sending anyway returns status 87"
        )
    if lock.asset_number == 0:
        blockers.append("asset number is zero, which returns status 93")

    requested_cents = None
    if args.amount is not None:
        requested_cents = int(round(args.amount * 100))
        if not partial_ok and requested_cents != cashable:
            blockers.append(
                f"this machine does not allow a to-host transfer of less than "
                f"the full available amount (AFT status bit 1 is clear), and "
                f"{requested_cents / 100:,.2f} is not the whole {cashable / 100:,.2f} "
                f"cashable balance. Section 8.4 permits the machine to refuse "
                f"this outright. Omit --amount to take the whole balance"
            )
        if requested_cents > cashable:
            blockers.append(
                f"{requested_cents / 100:,.2f} is more cashable than the machine "
                f"holds ({cashable / 100:,.2f})"
            )

    # Not a blocker, because Table 8.2b bit 1 says "less than full available
    # amount" and section 8.4 adds "for each type" -- so a machine with the
    # bit clear may still allow a full transfer of one type on its own. Which
    # reading this cabinet takes is a fact about the cabinet, so it is worth
    # warning about and then finding out rather than refusing to try.
    if (args.cashable_only and not partial_ok
            and (restricted or nonrestricted)):
        print("\n  Warning: this machine does not allow partial to-host transfers")
        print("  (AFT status bit 1), and --cashable-only leaves"
              f" {(restricted + nonrestricted) / 100:,.2f} behind.")
        print("  Section 8.4 words that limit as 'the full available amount for each")
        print("  type', which permits taking cashable alone -- but a machine reading")
        print("  it as the whole balance will refuse with status 87 or 8x. If it")
        print("  does, run without --cashable-only.")

    if blockers:
        print("\nNot sending. Resolve these first:")
        for b in blockers:
            print(f"  - {b}")
        return 1

    # --- what is about to be asked for ---------------------------------
    if requested_cents is not None:
        take = (requested_cents, 0, 0)
        transfer_code = AFT_TRANSFER_CODE_FULL_ONLY
        description = f"{requested_cents / 100:,.2f} cashable (exact amount)"
    elif args.cashable_only:
        take = (TAKE_EVERYTHING, 0, 0)
        transfer_code = AFT_TRANSFER_CODE_PARTIAL_ALLOWED
        description = "the whole CASHABLE balance, leaving the rest on the machine"
    else:
        take = (TAKE_EVERYTHING, TAKE_EVERYTHING, TAKE_EVERYTHING)
        transfer_code = AFT_TRANSFER_CODE_PARTIAL_ALLOWED
        description = "the whole balance of all three types"

    transaction_id = args.transaction_id or f"NIMAOUT{int(time.time())}"
    if not 1 <= len(transaction_id) <= 20:
        print("Transaction ID must be 1-20 ASCII characters.", file=sys.stderr)
        return 2

    print(f"\nAbout to take {description} from address {args.address}, to the host.")
    print(f"  transfer type  0x{int(AftTransferType.IN_HOUSE_FROM_GAMING_MACHINE_TO_HOST):02X} "
          f"(in-house, gaming machine -> host)")
    print(f"  transfer code  0x{transfer_code:02X} "
          f"({'full transfer only' if transfer_code == AFT_TRANSFER_CODE_FULL_ONLY else 'partial transfer allowed'})")
    print(f"  transfer flags 0x00 (bit 3 NOT set -- nothing is cashed out to paper)")
    print(f"  transaction ID {transaction_id}")
    print("  No ticket is printed. The credits leave as an electronic transfer.")
    if restricted and take[1]:
        # Worth saying plainly: restricted may not leave on a cash ticket, but
        # to the host is exactly how it is allowed to leave.
        print(f"  {restricted / 100:,.2f} RESTRICTED is included. Restricted credits may")
        print("  never leave on a cash ticket, from the hopper or by handpay, but")
        print("  returning them to the host preserves their restricted status, so")
        print("  this is a legitimate route for them -- unlike a ticket-out.")
    if on_machine == 0:
        print("  The machine holds nothing. Section 8.4 says it should answer with a")
        print("  SUCCESSFUL transfer of 0.00, so that is the expected result here --")
        print("  not a failure.")

    if not args.yes:
        if input("\nType CASHOUT to proceed: ").strip() != "CASHOUT":
            print("Aborted. Nothing sent.")
            return 0

    before = read_meters(client)

    # --- the transfer ---------------------------------------------------
    try:
        result = client.aft_transfer_funds(
            transfer_code=transfer_code,
            transaction_index=0x00,
            transfer_type=int(AftTransferType.IN_HOUSE_FROM_GAMING_MACHINE_TO_HOST),
            cashable_amount=take[0],
            restricted_amount=take[1],
            nonrestricted_amount=take[2],
            # Section 8: when restricted amounts move TO the host, the host
            # leaves pool ID and expiration zero -- the machine reports the
            # values currently associated with them in its response.
            pool_id=0,
            expiration=0,
            transfer_flags=0x00,
            asset_number=lock.asset_number,
            registration_key=bytes(20),  # zero = registration not required
            transaction_id=transaction_id,
        )
    except SASError as e:
        print(f"\nTransfer failed: {type(e).__name__}: {e}", file=sys.stderr)
        print("The machine's meters below say whether anything moved anyway.",
              file=sys.stderr)
        show_deltas(before, read_meters(client))
        return 1

    category, status_text = describe_aft_transfer_status(result.transfer_status)
    print(f"\nstatus 0x{result.transfer_status:02X}  {category}: {status_text}")
    if result.transfer_status == 0x01 and transfer_code == AFT_TRANSFER_CODE_PARTIAL_ALLOWED:
        print("  (0x01 is the SUCCESS code for this request: asking for")
        print("   9999999999 and receiving the actual balance is partial by")
        print("   definition, per section 8.4)")

    # --- a pending transfer has to be interrogated, not assumed ---------
    if category == "PENDING":
        print("\nPending. Interrogating (0x72 index 00) until it settles:")
        for _ in range(12):
            time.sleep(1.0)
            try:
                final = client.aft_interrogate_transfer(0x00, acknowledge=True)
            except SASError as e:
                print(f"  interrogation failed: {type(e).__name__}: {e}", file=sys.stderr)
                break
            cat, desc = describe_aft_transfer_status(final.transfer_status)
            if cat != "PENDING":
                print(f"  final  0x{final.transfer_status:02X}  {cat}: {desc}")
                result, category = final, cat
                break
        else:
            print("  still pending after 12 seconds. The meters below are the")
            print("  only statement of what actually happened.")

    print(f"\n  transferred      {result.cashable_amount / 100:,.2f} cashable"
          f"  + {result.restricted_amount / 100:,.2f} restricted"
          f"  + {result.nonrestricted_amount / 100:,.2f} nonrestricted")
    moved = result.cashable_amount + result.restricted_amount + result.nonrestricted_amount
    print(f"  total            {moved / 100:,.2f}")
    print(f"  transaction ID   {result.transaction_id}")
    print(f"  date/time        {result.transaction_date} {result.transaction_time}")
    print(f"  buffer position  {result.transaction_buffer_position}")

    after = read_meters(client)
    show_deltas(before, after)

    # --- did the money actually leave? ---------------------------------
    print()
    # The category already comes from the top three bits per Table 8.3e's own
    # note, so there is no second condition to keep in step with it.
    if category == "SUCCESSFUL":
        if moved == 0 and on_machine == 0:
            print("Zero moved from an empty machine, reported as a success. That is")
            print("section 8.4's defined behaviour, and it is how a host opens or")
            print("closes a cashless session without any credits being involved.")
        elif moved == 0:
            print(f"The machine reported success but moved 0.00, while 0x74 had said")
            print(f"{on_machine / 100:,.2f} was on it. The meters above are the arbiter:")
            print("if none of them moved either, the credits are still on the machine")
            print("and something refused the transfer silently.")
        else:
            print(f"{moved / 100:,.2f} left the machine electronically. No ticket was")
            print("printed and no attendant was involved. Check the machine's credit")
            print("meter on screen: it should have dropped by exactly this much.")
            print()
            print("The to-host cents meters (00B8/00BA/00BC) should have risen by the")
            print("per-type amounts, and their quantity meters (00B9/00BB/00BD) by one")
            print("each for the types that moved. If an amount moved but its quantity")
            print("did not, the machine is metering by a different split than it")
            print("reported -- worth knowing before a CMS reconciles against it.")
    else:
        print("The transfer did not succeed. The credits should still be on the")
        print("machine -- the meters above say whether that is true, and the status")
        print("code says why it was refused.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
