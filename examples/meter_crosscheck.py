"""Which of the gateway's figures the machine already reports natively, and
which are only ever arithmetic the gateway does for itself.

Read-only. Meter polls plus one query-only 0x74; nothing changes machine state.

The gateway prints several figures it computes by adding meters together --
"Cashless In", "Cashless Out", "Total In", "Total Out", credits on the
machine. A computed figure is a liability in an accounting system: it is
right only as long as the component list is right, and a component the
machine does not implement turns the whole sum into a silent n/a. Where SAS
already has a meter for the same figure, the meter is the value to keep and
the sum is worth keeping only as a cross-check against it.

Table C-7 does have natives for most of them, and the spec's own wording
says what each native includes -- which matters, because two of them are
NOT simply the sum of the parts the gateway adds up:

    0017  "Total electronic transfers to gaming machine, including cashable,
          nonrestricted, restricted and debit, WHETHER TRANSFER IS TO CREDIT
          METER OR TO TICKET (credits)"
    0018  "Total electronic transfers to host, including cashable,
          nonrestricted, restricted AND WIN AMOUNTS (credits)"

So 0017 legitimately exceeds 002E+002F+0030+0031 by the host's
transfers-to-ticket, and 0018 legitimately exceeds 0032+0033+0034 by win
amounts moved to the host. A delta is not automatically a fault, and this
prints what each delta should be made of rather than calling it an error.

Two other natives the spec defines AS a sum, so the sum is a true check:

    0022  "Total won credits (sum of total coin out and total jackpot)"
    0023  "Total hand paid credits (sum of total hand paid cancelled credits
          and total jackpot)"

One figure has no Table C-7 meter at all: current NONRESTRICTED credits.
C-7 has 000C (current credits) and 001B (current restricted credits) and
stops there, so 000C - 001B is cashable AND nonrestricted together, with no
meter that splits them. LP 0x74's game-lock-and-status response is the only
native source for the third balance: it reports cashable, restricted and
nonrestricted as three separate fields in one poll.

What 000C itself means -- the cashable balance alone, or the whole balance --
the spec does not say, and this measures it rather than assuming. The test is
only decisive while there IS non-cashable credit on the machine; with
restricted and nonrestricted both zero, both readings give the same number
and the run says so instead of guessing.

On the EGT Premier this was built against, 000C is the WHOLE balance: it read
57.00 against a machine displaying 57.00 (50.00 cashable + 7.00 restricted),
and 60.00 after 3.00 nonrestricted was pushed. A separate report of "50.00"
on the same machine came from a tool printing 0x74's cashable field alone, not
from any meter disagreeing -- which is the kind of thing this tool exists to
tell apart.

    python3 examples/meter_crosscheck.py /dev/ttyS0 --address 1
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.client import SASClient
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport


@dataclass(frozen=True)
class Identity:
    """A figure the gateway computes, next to the meter that already has it."""

    label: str
    native: int
    native_wording: str          # Table C-7's own text, abbreviated
    parts: tuple[int, ...]
    parts_wording: str
    delta_should_be: str         # what a non-zero delta ought to consist of


# Natives whose scope is WIDER than the parts the gateway adds up, per the
# C-7 wording quoted in the module docstring. Both deltas should be >= 0.
WIDER_NATIVES = [
    Identity(
        label="Cashless In",
        native=0x17,
        native_wording="total electronic transfers to gaming machine, to credit meter OR to ticket",
        parts=(0x2E, 0x2F, 0x30, 0x31),
        parts_wording="cashable + restricted + nonrestricted + debit, to the credit meter",
        delta_should_be="the host's transfers to a TICKET (00A8+00AA+00AC, which are in cents)",
    ),
    Identity(
        label="Cashless Out",
        native=0x18,
        native_wording="total electronic transfers to host, including win amounts",
        parts=(0x32, 0x33, 0x34),
        parts_wording="cashable + restricted + nonrestricted, to host",
        delta_should_be="win amounts transferred to the host",
    ),
]

# Natives the spec defines AS a sum, so a delta here is a real disagreement.
EXACT_NATIVES = [
    Identity(
        label="Total Won",
        native=0x22,
        native_wording="total won credits (sum of total coin out and total jackpot)",
        parts=(0x01, 0x02),
        parts_wording="total coin out + total jackpot",
        delta_should_be="nothing -- the spec defines 0022 as exactly this sum",
    ),
    Identity(
        label="Total Hand Paid",
        native=0x23,
        native_wording="total hand paid credits (sum of hand paid cancelled credits and total jackpot)",
        parts=(0x03, 0x02),
        parts_wording="total hand paid cancelled credits + total jackpot",
        delta_should_be="nothing -- the spec defines 0023 as exactly this sum",
    ),
]

# Live balances. 000C/001B are meters; the three native amounts come from
# 0x74, which is the only native source for the nonrestricted balance.
CURRENT_METERS = [
    ("Current credits", 0x0C),
    ("Current restricted credits", 0x1B),
]

# The screen's two headline totals. Whether a native meter covers these is a
# property of the MACHINE, not of SAS: 0024 is "total drop, including but not
# limited to coins to drop, bills to drop, tickets to drop, and electronic in",
# so on a machine with a coin acceptor or a ticket drop it counts more than the
# screen's Total In -- and on a cashless machine with neither, it counts exactly
# the same things. Likewise 0004 (total cancelled credits) equals the screen's
# Total Out only where every way out of the machine is a cancelled credit.
#
# On the EGT Premier both matched exactly, twice, across a 3.00 transfer:
# 0024 = 64,350.58 then 64,353.58 against the screen's Total In, and 0004 =
# 57,591.86 then 57,651.46 against its Total Out. So these are measured here,
# not assumed either way.
CANDIDATE_NATIVES = [
    Identity(
        label="Total In (screen's headline)",
        native=0x24,
        native_wording="total drop -- coins, bills, tickets to drop AND electronic in",
        parts=(0x0B, 0x17),
        parts_wording="bills accepted + total electronic in",
        delta_should_be="coins or tickets to drop, which a cashless machine has none of",
    ),
    Identity(
        label="Total Out (screen's headline)",
        native=0x04,
        native_wording="total cancelled credits",
        parts=(0x03, 0x18),
        parts_wording="attendant-paid cancelled credits + total electronic out",
        delta_should_be="any way out of the machine that is not a cancelled credit",
    ),
]

# What genuinely has no native meter anywhere. Saying so is the point: it marks
# the one place the gateway's arithmetic is load-bearing rather than decorative.
NO_NATIVE = [
    ("Current nonrestricted credits", (),
     "no Table C-7 code exists -- 000C and 001B cannot be subtracted to get it,"
     " since 000C - 001B is cashable and nonrestricted together. Native only"
     " from LP 0x74 -- see above."),
]

# The accounting unit. Everything above is in credits; 00A0 is the same
# traffic in cents. If they are equal, one credit is one cent on this machine,
# which is what lets every other figure here be printed as currency.
#
# Caveat the C-7 note states: codes 0080-0093 and 00A0-00BD "include only
# amounts accumulated using the SAS protocol", while 002E counts all electronic
# regular cashable transfers. On a machine whose only cashless path is AFT the
# two cover the same traffic; on one with another cashless protocol they do not.
UNIT_PROBE = (0xA0, 0x2E,
              "in-house cashable transfers to gaming machine (cents)",
              "electronic regular cashable transfers to gaming machine (credits)")


def read(client, codes: list[int]) -> dict[int, int | None]:
    """Read in batches: LP 0x6F takes 12 codes per exchange."""
    out: dict[int, int | None] = {}
    for i in range(0, len(codes), 12):
        chunk = codes[i:i + 12]
        try:
            result = client.send_extended_meters(chunk)
        except SASError as e:
            print(f"  (chunk {i // 12 + 1} failed: {type(e).__name__}: {e})", file=sys.stderr)
            out.update({c: None for c in chunk})
            continue
        out.update({c: result.meters.get(c) for c in chunk})
    return out


def money(value: int | None) -> str:
    return "       n/a" if value is None else f"{value / 100:>10,.2f}"


def bullet(text: str, *, indent: str = "    ") -> None:
    """Print a wrapped "-> ..." verdict. Verdicts are the part of this report
    someone acts on, so they are not allowed to run off the side of a terminal.
    """
    lines = textwrap.wrap(text, 66) or [""]
    print(f"{indent}-> {lines[0]}")
    for line in lines[1:]:
        print(f"{indent}   {line}")


def sum_parts(values: dict[int, int | None], codes: tuple[int, ...]) -> tuple[int | None, list[int]]:
    """Sum the components, naming any the machine did not report."""
    missing = [c for c in codes if values.get(c) is None]
    if missing:
        return None, missing
    return sum(values[c] for c in codes), []


def show_identity(
    values: dict[int, int | None],
    ident: Identity,
    *,
    kind: str,
    delta_note: str | None = None,
    delta_explained: bool = True,
) -> None:
    """Print one native/computed pair and a verdict on any difference.

    ``kind`` says what relationship the spec promises between the two, which
    is what makes a delta meaningful or not:

      "exact"      the spec defines the native AS that sum, so any delta is a
                   disagreement between two of the machine's own meters.
      "wider"      the native's wording covers more than the parts, so a
                   positive delta is expected and a negative one is not.
      "candidate"  no documented relationship -- a different meter that may or
                   may not cover the same ground on this machine. A delta
                   either way just means it does not, on this one.

    ``delta_note`` lets the caller override the stock "expected difference"
    line with something it has measured. Without that, a report can say a
    delta is explained by transfers-to-ticket in one section and that
    transfers-to-ticket are zero in the next -- arguing with itself in a
    document someone is meant to act on.
    """
    native = values.get(ident.native)
    computed, missing = sum_parts(values, ident.parts)
    parts_list = "+".join(f"{c:04X}" for c in ident.parts)

    print(f"  {ident.label}")
    print(f"    native   {ident.native:04X}                {money(native)}   {ident.native_wording}")
    print(f"    computed {parts_list:<19} {money(computed)}   {ident.parts_wording}")

    if native is None:
        print("    -> the machine does not report this meter. The sum is all there is;")
        print("       keep computing it, and note the gap in the machine's support.")
    elif missing:
        print(f"    -> components {' '.join(f'{c:04X}' for c in missing)} not reported, so the sum"
              " cannot be formed.")
        print("       Use the native meter; there is nothing to cross-check it against.")
    elif native == computed:
        if kind == "candidate":
            bullet("agree, so on this machine this meter does cover exactly the same"
                   " ground. Read it instead of computing the sum, and keep the sum as"
                   " the cross-check.")
        else:
            print("    -> agree. Use the native meter; keep the sum as the cross-check.")
    else:
        delta = native - computed
        print(f"    delta    {'':<19} {money(delta)}   native minus computed")
        if kind == "candidate":
            bullet("they differ, so on this machine this meter does NOT cover the same"
                   " ground as that sum. Nothing is wrong -- it is simply not a"
                   " substitute here, so keep computing the sum.")
        elif delta < 0:
            print("    -> native is SMALLER than its own components. That is not a legitimate")
            print("       difference under either wording -- investigate before trusting either.")
        elif kind == "exact":
            print("    -> they disagree, and the spec defines this native AS that sum, so one of")
            print("       the two is wrong on this machine. Worth raising with the manufacturer.")
        elif delta_note is not None:
            bullet(delta_note)
            if delta_explained:
                print("       Use the native meter for the total and the parts for the breakdown.")
        else:
            print(f"    -> expected difference: {ident.delta_should_be}.")
            print("       Use the native meter for the total and the parts for the breakdown.")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=1.0)
    args = ap.parse_args()

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(
        SASTransport(port, byte_timeout=args.timeout), args.address, timeout=args.timeout
    )

    wanted: list[int] = []
    for ident in WIDER_NATIVES + EXACT_NATIVES + CANDIDATE_NATIVES:
        wanted.append(ident.native)
        wanted.extend(ident.parts)
    wanted += [c for _l, c in CURRENT_METERS]
    wanted += [0xA8, 0xAA, 0xAC, UNIT_PROBE[0], UNIT_PROBE[1]]
    wanted += [c for _l, codes, _n in NO_NATIVE for c in codes]
    wanted = list(dict.fromkeys(wanted))   # de-duplicate, keep order

    values = read(client, wanted)

    print(f"\n=== {args.port}, address {args.address} ===")

    print("\n--- Natives wider than the sum the gateway computes -------------\n")

    # Measured before the verdicts, not after, so the 0017 verdict can say
    # whether the one difference its wording permits is actually present.
    to_ticket, missing_tt = sum_parts(values, (0xA8, 0xAA, 0xAC))
    print("  Transfers to a ticket, which 0017 counts and 002E-0031 do not")
    print(f"    computed 00A8+00AA+00AC {money(to_ticket)}   (in cents, per the C-7 note"
          " on 00A0-00BD)"
          + (f"\n    missing {' '.join(f'{c:04X}' for c in missing_tt)}" if missing_tt else ""))
    print()

    cashless_in_explained = True
    if missing_tt:
        cashless_in_note = (
            f"expected difference: {WIDER_NATIVES[0].delta_should_be}. This machine does"
            " not report those meters, so the delta cannot be confirmed against them."
        )
    elif to_ticket == 0:
        cashless_in_explained = False
        cashless_in_note = (
            "transfers to a ticket are zero on this machine, and that is the only"
            " difference 0017's wording allows over 002E-0031. So this delta is NOT"
            " explained -- find out what it is before treating either figure as the"
            " total."
        )
    else:
        cashless_in_note = (
            f"transfers to a ticket are {money(to_ticket).strip()} (cents) above, which is"
            " what this delta should consist of. 0017 is in credits, so the two match"
            " directly only where one credit is one cent -- see the accounting unit below."
        )

    for ident in WIDER_NATIVES:
        is_cashless_in = ident.native == 0x17
        show_identity(
            values, ident, kind="wider",
            delta_note=cashless_in_note if is_cashless_in else None,
            delta_explained=cashless_in_explained if is_cashless_in else True,
        )

    print("--- Natives the spec defines as exactly that sum ----------------\n")
    for ident in EXACT_NATIVES:
        show_identity(values, ident, kind="exact")

    print("--- Credits on the machine now ----------------------------------\n")
    for label, code in CURRENT_METERS:
        print(f"    {label:<34} {money(values.get(code))}   (C-7 {code:04X})")

    lock = None
    try:
        lock = client.aft_game_lock_and_status_request(lock_code=0xFF)
    except SASError as e:
        print(f"\n    0x74 did not answer ({type(e).__name__}: {e}).")
        print("    Without it there is no native source for the nonrestricted balance,")
        print("    and the gateway cannot report what the machine's screen shows.")

    if lock is not None:
        cash = lock.current_cashable_amount
        restr = lock.current_restricted_amount
        nonr = lock.current_non_restricted_amount
        total = cash + restr + nonr
        print("\n    From LP 0x74 -- three native fields, one poll, no arithmetic:")
        print(f"      {'cashable':<32} {money(cash)}")
        print(f"      {'restricted':<32} {money(restr)}")
        print(f"      {'nonrestricted':<32} {money(nonr)}")
        print(f"      {'their total [computed]':<32} {money(total)}"
              "   <- what the screen shows")

        current = values.get(0x0C)
        print("\n    What 000C means on this machine:")
        if current is None:
            print("      000C did not answer, so there is nothing to compare.")
        elif restr == 0 and nonr == 0:
            print("      restricted and nonrestricted are both zero, so 'cashable only' and")
            print("      'everything' give the same number. Indeterminate -- re-run this while")
            print("      there is non-cashable credit on the machine.")
        elif current == cash and current != total:
            print("      000C == 0x74's cashable and != the total, so 000C is the CASHABLE")
            print("      balance alone. Reporting it as 'credits on the machine' understates")
            print(f"      the screen by {money(total - current).strip()}. Use 0x74's three fields.")
        elif current == total and current != cash:
            print("      000C == the total of all three, so on this machine 000C already is")
            print("      the whole balance and matches the screen on its own.")
        else:
            print(f"      000C ({money(current).strip()}) matches neither 0x74's cashable"
                  f" ({money(cash).strip()}) nor the total ({money(total).strip()}).")
            print("      Something is off -- do not report either as the screen balance until")
            print("      this is explained.")

    print("--- The screen's two headline totals ----------------------------\n")
    print("  Whether a native meter covers these depends on the machine, not on")
    print("  SAS. A match here means the gateway can stop computing them.\n")
    for ident in CANDIDATE_NATIVES:
        show_identity(values, ident, kind="candidate")

    print("--- No native meter exists: this stays computed -----------------\n")
    for label, codes, note in NO_NATIVE:
        parts_list = "+".join(f"{c:04X}" for c in codes) if codes else "--"
        value, missing = sum_parts(values, codes) if codes else (None, [])
        print(f"    {label:<34} {money(value) if codes else '          '}"
              f"   {parts_list}")
        for line in textwrap.wrap(note, 64):
            print(f"      {line}")
        if missing:
            print(f"      (not reported: {' '.join(f'{c:04X}' for c in missing)})")
    print()

    print("--- The accounting unit -----------------------------------------\n")
    cents_code, credits_code, cents_name, credits_name = UNIT_PROBE
    cents = values.get(cents_code)
    credits = values.get(credits_code)
    print(f"    {cents_code:04X}  {money(cents)}   {cents_name}")
    print(f"    {credits_code:04X}  {money(credits)}   {credits_name}")
    if cents is None or credits is None:
        print("    -> one of the two is not reported, so the unit is not established here.")
    elif cents == credits == 0:
        print("    -> both zero. Nothing has moved through this path, so the unit is not")
        print("       established. Re-run after an AFT transfer.")
    elif cents == credits:
        print("    -> equal, so one credit is one cent on this machine, and printing every")
        print("       figure above as value/100 is correct.")
    elif credits and cents % credits == 0:
        print(f"    -> cents = {cents // credits} x credits, so one credit is"
              f" {cents // credits} cents on this machine.")
        print("       Every figure above printed as value/100 is then WRONG by that factor.")
    else:
        print("    -> not a whole multiple. Either another cashless protocol also feeds 002E")
        print("       (the C-7 note says 00A0 counts SAS-protocol amounts only), or the two")
        print("       meters are not covering the same traffic. Do not infer a unit from this.")

    print("\n" + "=" * 68)
    print("What this run establishes: which of 0017, 0018, 0022, 0023, 0024 and")
    print("0004 this machine answers, and whether each matches the sum the")
    print("gateway would otherwise compute. Every one that matches is a figure")
    print("to read rather than calculate, with the sum kept as its cross-check.")
    print("The one figure with no meter behind it anywhere is the nonrestricted")
    print("balance, which comes from 0x74 or not at all.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
