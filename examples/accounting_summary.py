"""Print the machine's accounting figures as SAS reports them, grouped the
way the machine's own Machine Accounting screen groups them.

Read-only. Meter polls only; nothing changes machine state.

Comparing the gateway's numbers against the machine's own screen has been
this project's one validation that does not come from inside its own code.
Until now that comparison was done by eye, across two screens, with the SAS
side spread over Table C-7 codes that do not resemble the screen's labels.
This does the mapping, and recomputes the screen's subtotals from the SAS
meters so they can be checked rather than trusted.

The mapping that is not obvious -- confirmed on an EGT Premier by pushing
and cashing out each amount type and watching which line moved:

    machine's screen label          SAS / Table C-7
    "Cashable Electronic In"        002E  electronic regular cashable to GM
    "Promotional Electronic In"     0030  electronic NONRESTRICTED to GM
    "Non-cashable Electronic In"    002F  electronic RESTRICTED to GM
    "Cashable Electronic Out"       0032  electronic regular cashable to host
    "Promotional Electronic Out"    0034  electronic NONRESTRICTED to host
    "Non-cashable Electronic Out"   0033  electronic RESTRICTED to host

So the screen's "non-cashable" is SAS's RESTRICTED, and the screen's
"promotional" is SAS's NONRESTRICTED. Reading those the other way round --
which the words invite -- swaps the two promotional categories.

    python3 examples/accounting_summary.py /dev/ttyS0 --address 1
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

# (screen label, Table C-7 code, indent). Subtotals are computed, not read,
# so a mismatch against the machine's own screen is visible rather than
# hidden behind agreement on the components.
IN_LINES = [
    ("Bill In", 0x58, 1),
    ("Cashless In", None, 1),            # computed
    ("Cashable Electronic In", 0x2E, 2),
    ("Promotional Electronic In", 0x30, 2),
    ("Non-cashable Electronic In", 0x2F, 2),
]
OUT_LINES = [
    ("Cancelled Credits Paid by Attendant", 0x03, 1),
    ("Cashless Out", None, 1),           # computed
    ("Cashable Electronic Out", 0x32, 2),
    ("Promotional Electronic Out", 0x34, 2),
    ("Non-cashable Electronic Out", 0x33, 2),
]
CORE = [
    ("Total Bet (Coin In)", 0x00),
    ("Total Won", 0x01),
    ("Total Cancelled Credits", 0x04),
    ("Total Drop", 0x24),
    ("Total Jackpot", 0x02),
]
CURRENT = [
    ("Current credits", 0x0C),
    ("Current restricted credits", 0x1B),
]


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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

    wanted = [c for _l, c, _i in IN_LINES + OUT_LINES if c is not None]
    wanted += [c for _l, c in CORE + CURRENT]
    values = read(client, wanted)

    def show(lines):
        for label, code, indent in lines:
            if code is None:
                continue
            pad = "  " * indent
            print(f"  {pad}{label:<38} {money(values.get(code))}   (C-7 {code:04X})")

    print(f"\n=== {args.port}, address {args.address} ===\n")

    cashless_in = [values.get(c) for c in (0x2E, 0x30, 0x2F)]
    bill_in = values.get(0x58)
    print("Total In")
    print(f"    {'Bill In':<38} {money(bill_in)}   (C-7 0058)")
    sub_in = sum(v for v in cashless_in if v is not None) if any(
        v is not None for v in cashless_in) else None
    print(f"    {'Cashless In  [computed]':<38} {money(sub_in)}")
    for label, code, _ in IN_LINES[2:]:
        print(f"      {label:<36} {money(values.get(code))}   (C-7 {code:04X})")
    total_in = None
    if sub_in is not None and bill_in is not None:
        total_in = sub_in + bill_in
    print(f"  {'TOTAL IN  [computed]':<40} {money(total_in)}")

    cashless_out = [values.get(c) for c in (0x32, 0x34, 0x33)]
    attendant = values.get(0x03)
    print("\nTotal Out")
    print(f"    {'Cancelled Credits Paid by Attendant':<38} {money(attendant)}   (C-7 0003)")
    sub_out = sum(v for v in cashless_out if v is not None) if any(
        v is not None for v in cashless_out) else None
    print(f"    {'Cashless Out  [computed]':<38} {money(sub_out)}")
    for label, code, _ in OUT_LINES[2:]:
        print(f"      {label:<36} {money(values.get(code))}   (C-7 {code:04X})")
    total_out = None
    if sub_out is not None and attendant is not None:
        total_out = sub_out + attendant
    print(f"  {'TOTAL OUT  [computed]':<40} {money(total_out)}")

    print("\nCore meters")
    for label, code in CORE:
        print(f"    {label:<38} {money(values.get(code))}   (C-7 {code:04X})")

    print("\nOn the machine now")
    for label, code in CURRENT:
        print(f"    {label:<38} {money(values.get(code))}   (C-7 {code:04X})")

    print("\n" + "=" * 68)
    print("Compare against the machine's Machine Accounting screen. The two")
    print("subtotals above are COMPUTED from the SAS meters, not read, so if")
    print("they match the screen's own Cashless In/Out the mapping is right")
    print("as well as the individual figures.")
    print("\nReminder on the two promotional lines, which the wording inverts:")
    print('  the screen\'s "Non-cashable Electronic" is SAS RESTRICTED (002F/0033)')
    print('  the screen\'s "Promotional Electronic"  is SAS NONRESTRICTED (0030/0034)')
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
