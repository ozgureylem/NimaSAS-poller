"""Which meters this machine's current values can and cannot tell apart.

Read-only. Meter polls only.

The verified-label table grows by matching a meter against the machine's own
screen. That only works when the match is UNAMBIGUOUS, and the commonest way
for it not to be is two meters holding the same number. A zero matching a zero
proves nothing, and neither does 25.00 matching 25.00 when three meters all
read 25.00 -- any of them could be the screen's line.

So this reports the ambiguity directly: every group of meters sharing a value,
and what it would take to separate them. The answer is almost always to put a
DIFFERENT, distinctive amount through each path, which is what makes a
deliberate fingerprinting run worth doing.

Picking those amounts by hand is harder than it looks, because every total
meter is a SUM of component meters. Round numbers fail immediately: push 25.00
down two paths and the total reads 50.00, which is itself a thoroughly
plausible meter value. The obvious repunit set fails too -- 111.11 + 222.22 is
exactly 333.33, so the total collides with the third component. That is why the
amounts below are generated against the machine's own current values and
against each other's sums, rather than typed out from habit.

The amounts suit the paths the host controls to the cent: AFT transfers and
vouchers the gateway itself writes. Bills cannot be chosen freely -- there the
separation has to come from distinct COUNTS of distinct denominations instead.

    python3 examples/meter_fingerprint.py /dev/ttyS0 --address 1
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.client import SASClient
from saspy.constants import MeterCode
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

CHUNK = 12   # LP 0x6F's own per-request limit (§7.21)

# Meters whose value is a count, not money. Mixing them into the collision
# report would pair "7 games won" with "7 cents" as if that were meaningful.
def is_count(code: int) -> bool:
    name = MeterCode(code).name
    return (name.endswith("_QUANTITY") or "NUMBER_OF" in name
            or name.startswith("GAMES_"))


def is_percent(code: int) -> bool:
    return MeterCode(code) is MeterCode.WEIGHTED_AVERAGE_THEORETICAL_PAYBACK_PERCENTAGE


def label(code: int) -> str:
    return MeterCode(code).name.lower().replace("_", " ")


def read_all(client) -> dict[int, int | None]:
    codes = list(MeterCode)
    out: dict[int, int | None] = {}
    for i in range(0, len(codes), CHUNK):
        chunk = codes[i:i + CHUNK]
        alternate = (i // CHUNK) % 2 == 1
        poll = (client.send_extended_meters_alternate if alternate
                else client.send_extended_meters)
        try:
            result = poll([int(c) for c in chunk])
        except SASError as e:
            print(f"  (chunk {i // CHUNK + 1} failed: {type(e).__name__}: {e})",
                  file=sys.stderr)
            out.update({int(c): None for c in chunk})
            continue
        out.update({int(c): result.meters.get(int(c)) for c in chunk})
    return out


# The identities Table C-7 states outright, in the meter's own description.
# Eight carry a literal "[same as meter ...]" annotation; the last two say
# "sum of" in prose. These are not coincidences to be broken apart -- they
# are the same number BY DEFINITION, so no amount, count or event will ever
# separate them, and a labelling run that tries is wasted.
#
# They are worth checking rather than merely asserting: the arithmetic is a
# genuine cross-check of the machine's metering, and a machine that fails one
# is reporting something inconsistent.
# The identities Table C-7 states outright, in the meter's own description.
# Eight carry a literal "[same as meter ...]" annotation; the last two say
# "sum of" in prose.
#
# The distinction between a one-part and a multi-part identity matters, and
# it is the difference between a wasted test and a useful one:
#
#   000F = 0082 is PERMANENT. The two are the same number for good, so no
#   amount, count or event will ever separate them, and a labelling run that
#   tries is wasted effort.
#
#   000D = 0080 + 0084 is CONDITIONAL. 000D equals 0080 only while 0084 is
#   zero -- so the collision is real today and breaks the moment a
#   nonrestricted ticket is redeemed. That one IS worth a test, and the test
#   is naming the path rather than the amount.
#
# They are worth checking rather than merely asserting: the arithmetic is a
# genuine cross-check of the machine's metering, and a machine that fails one
# is contradicting the spec's own definition of its meter.
SPEC_IDENTITIES = (
    (0x000D, (0x0080, 0x0084), "total SAS cashable ticket in"),
    (0x000E, (0x0086, 0x008A), "total SAS cashable ticket out"),
    (0x000F, (0x0082,),        "total SAS restricted ticket in"),
    (0x0010, (0x0088,),        "total SAS restricted ticket out"),
    (0x0011, (0x0081, 0x0085), "total SAS cashable ticket in (quantity)"),
    (0x0012, (0x0087, 0x008B), "total SAS cashable ticket out (quantity)"),
    (0x0013, (0x0083,),        "total SAS restricted ticket in (quantity)"),
    (0x0014, (0x0089,),        "total SAS restricted ticket out (quantity)"),
    (0x0022, (0x0001, 0x0002), "total won"),
    (0x0023, (0x0003, 0x0002), "total hand paid"),
)


def sas_only(code: int) -> bool:
    """Whether this meter counts only what arrived through SAS.

    The note under Table C-7's validation block: "Meter codes 0080 through
    0093 and 00A0 through 00BD include only amounts accumulated using the SAS
    protocol. They do not include amounts from any other process or
    protocol."

    That is the whole difference between those two blocks and their
    all-sources counterparts in 0015-0039. On a cabinet where a gateway is
    the only electronic route in or out -- a lab machine, and most production
    machines -- the two hold identical numbers forever, and no choice of
    amount changes it. Only an amount arriving by some NON-SAS process would
    separate them, which is not a thing a SAS host can arrange.
    """
    return 0x0080 <= code <= 0x0093 or 0x00A0 <= code <= 0x00BD


def explain(codes: list[int], values: dict[int, int]) -> tuple[list[str], str]:
    """(notes about this group, what to do about what is left of it).

    A declared identity explains part of a group, never the whole of it: at a
    value of 1, "games won" sits beside half a dozen ticket quantities for no
    better reason than that each happened once. Calling the group as a whole
    "defined equal" would assert a relationship between those that the spec
    does not state -- so identities are pulled out as their own notes and the
    verdict speaks only for what remains genuinely ambiguous.
    """
    members = set(codes)
    notes: list[str] = []
    accounted: set[int] = set()

    for total, parts, name in SPEC_IDENTITIES:
        present = members & set(parts)
        if total not in members or not present:
            continue
        if len(parts) == 1:
            notes.append(
                f"{total:04X} and {parts[0]:04X} are the SAME METER: Table C-7 "
                f"defines {total:04X} as '[same as meter {parts[0]:04X}]'. "
                f"Permanently equal -- record the identity, do not try to "
                f"break it.")
            accounted |= {total, parts[0]}
        else:
            missing = [p for p in parts if values.get(p, 0) == 0]
            others = " + ".join(f"{p:04X}" for p in parts)
            if missing:
                notes.append(
                    f"{total:04X} = {others} per Table C-7, and it equals "
                    f"{', '.join(f'{p:04X}' for p in present)} here only because "
                    f"{', '.join(f'{p:04X}' for p in missing)} is still zero. "
                    f"Put something down that path and they separate.")
                accounted |= {total} | present
            else:
                notes.append(f"{total:04X} = {others} per Table C-7.")

    remaining = sorted(members - accounted)
    if len(remaining) < 2:
        return notes, ("Nothing else in this group is ambiguous once those "
                       "identities are accounted for.")
    if all(is_count(c) for c in remaining):
        return notes, (
            f"The other {len(remaining)} count events, not money, so distinct "
            f"AMOUNTS do nothing for them. Separate them with distinct event "
            f"COUNTS -- one ticket down one path, two down another.")
    pair = [c for c in remaining if sas_only(c)]
    if pair and len(pair) != len(remaining):
        return notes, (
            "The rest pair a SAS-only meter with an all-sources one (C-7's "
            "note on 0080-0093 / 00A0-00BD). Equal while the gateway is the "
            "only route in or out, and no amount changes that -- only a "
            "non-SAS transfer would, which a SAS host cannot arrange.")
    return notes, (f"The other {len(remaining)} are simply equal by chance. "
                   f"Distinct amounts will separate them.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--min-group", type=int, default=2,
                    help="only report groups of at least this many meters "
                         "(default 2)")
    args = ap.parse_args()

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(
        SASTransport(port, byte_timeout=args.timeout), args.address, timeout=args.timeout)

    print(f"\n=== {args.port}, address {args.address} ===\n")
    values = read_all(client)

    answered = {c: v for c, v in values.items() if v is not None}
    unsupported = [c for c, v in values.items() if v is None]
    zero = [c for c, v in answered.items() if v == 0]
    nonzero = {c: v for c, v in answered.items() if v}

    print(f"{len(answered)} of {len(values)} meters answered; "
          f"{len(unsupported)} not implemented")
    print(f"{len(zero)} read zero, {len(nonzero)} hold a value\n")

    # --- the identities the spec declares, checked rather than assumed ----
    print("--- Identities Table C-7 declares -------------------------------\n")
    print("  Each of these is stated in the meter's own description, so the")
    print("  arithmetic is a cross-check of the machine's metering rather than")
    print("  an assumption about it.\n")
    identity_failures = []
    for total, parts, name in SPEC_IDENTITIES:
        if total not in answered or any(p not in answered for p in parts):
            continue
        lhs = answered[total]
        rhs = sum(answered[p] for p in parts)
        parts_text = " + ".join(f"{p:04X}" for p in parts)
        mark = "OK  " if lhs == rhs else "FAIL"
        print(f"  {mark} {total:04X} = {parts_text:<15} {lhs:>12,} vs {rhs:>12,}   {name}")
        if lhs != rhs:
            identity_failures.append((total, parts, lhs, rhs))
    if identity_failures:
        print()
        print("  A FAIL is the machine contradicting Table C-7's own definition of")
        print("  the meter. Worth confirming against the screen before trusting")
        print("  either side of it -- and worth reporting, since a CMS reconciling")
        print("  on the declared identity would be reconciling on a false premise.")
    print()

    print("--- Meters that cannot be distinguished from each other ----------\n")
    print("  Two meters holding the same number cannot both be matched to a")
    print("  screen line with any confidence. But only some of these can be")
    print("  broken apart by choosing better amounts, so each group carries the")
    print("  verdict: pushing money at the others achieves nothing.\n")

    # Bucket by (kind, value), not value alone: 7 games won and 7 cents are
    # not the same number for this purpose, and pairing them would invent an
    # ambiguity that does not exist.
    groups: dict[tuple[str, int], list[int]] = defaultdict(list)
    for code, value in nonzero.items():
        kind = "count" if is_count(code) else "percent" if is_percent(code) else "cents"
        groups[(kind, value)].append(code)

    collisions = {k: v for k, v in groups.items() if len(v) >= args.min_group}
    if not collisions:
        print("  None. Every non-zero meter on this machine holds a value no")
        print("  other non-zero meter holds, so each one can be matched to a")
        print("  screen line on its own.\n")
    else:
        for (kind, value), codes in sorted(
                collisions.items(), key=lambda kv: -len(kv[1])):
            if kind == "cents":
                shown = f"{value:,} ({value / 100:,.2f})"
            elif kind == "percent":
                shown = f"{value:,} ({value / 100:,.2f}%)"
            else:
                shown = f"{value:,}"
            print(f"  {len(codes)} meters all read {shown}:")
            for code in sorted(codes):
                flag = "  (SAS only)" if sas_only(code) else ""
                print(f"      {code:04X}  {label(code)}{flag}")
            notes, remedy = explain(sorted(codes), nonzero)
            for note in notes:
                for i, line in enumerate(textwrap.wrap(note, 62)):
                    print(f"      = {line}" if i == 0 else f"        {line}")
            for i, line in enumerate(textwrap.wrap(remedy, 62)):
                print(f"      -> {line}" if i == 0 else f"         {line}")
            print()

    if zero:
        print(f"--- {len(zero)} meters read zero --------------------------------\n")
        print("  A zero matching a zero on screen is not a correspondence, so")
        print("  none of these can be labelled from this machine's current")
        print("  state however many times it is read. They need activity down")
        print("  that path, not another reading.\n")

    print("--- What to put through next ------------------------------------\n")
    print("  Pick amounts that collide with nothing already here, including")
    print("  with sums of each other -- a total meter equal to a component")
    print("  meter is the same ambiguity in a different place.\n")
    # Only cents meters can collide with a chosen transfer amount; a games
    # counter reading 1,111 is no reason to avoid 11.11.
    taken = {v for c, v in nonzero.items()
             if not is_count(c) and not is_percent(c)}
    suggestions, candidate = [], 1111
    while len(suggestions) < 6:
        # Repunit-style amounts: no two sums of them collide with a third,
        # and none is the kind of round number a machine is likely to be
        # holding already.
        amount = candidate
        if amount not in taken and not any(
                amount == a + b for a in suggestions for b in suggestions):
            suggestions.append(amount)
        candidate += 1111
    for amount in suggestions:
        print(f"      {amount / 100:>10,.2f}   ({amount:,} cents)")
    print()
    print("  None of these equals any cents meter this machine currently")
    print("  holds, and none is the sum of two others, so no total meter can")
    print("  come out equal to a component meter.\n")
    print("  Put a different one down each path -- cashable electronic in,")
    print("  promotional electronic in, non-cashable electronic in, and the")
    print("  three voucher-in types -- then re-read the screen. Every meter")
    print("  and every screen line will then carry a number belonging to")
    print("  exactly one path.")
    print()
    print("  Bills are the exception: the denominations are fixed, so separate")
    print("  those meters with distinct counts instead -- one 10, two 20s,")
    print("  three 50s -- which leaves each quantity meter unique even though")
    print("  the amounts are round.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
