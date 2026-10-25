"""Per-wager-category theoretical payback and Coin In, read raw from LP 0xB4.

Read-only. Nothing changes machine state.

Meter 007F (weighted average theoretical payback) is optional -- section 7.24.1
lets a machine "choose to not support meter 007F for that paytable" -- and the
EGT Premier in the lab does exactly that: 007F does not answer, while the
machine's own screen shows 95.82%. LP 0xB4 is the other route section 7.24
describes: the payback percentage and Coin In meter for each wager category,
from which a weighted average CAN be formed.

This tool reports those raw figures and does NOT form that average by default,
which is deliberate. A weighted average computed at the gateway is only correct
until a Coin In meter rolls over: past that point the meter has lost its
high-order digits, the gateway has no record of how many times it wrapped, and
the average silently drifts. Reconstructing the missing digits is the CMS's
job, because the CMS is what holds the history to do it with. A gateway that
publishes a derived percentage anyway is publishing a figure that is right for
a while and then quietly is not.

So the useful thing a gateway can contribute here is not the percentage. It is
the raw per-category figures plus each meter's declared width, which is what
says how much headroom is left before the reconstruction becomes necessary --
LP 0xB4 carries a Coin In meter size byte (0-9 bytes BCD), so the ceiling is
the machine's own statement, not an assumption.

--weighted-average computes it anyway, for cross-checking against a figure
from somewhere else. It prints with the caveat attached, and refuses to print
a bare number.

    python3 examples/wager_categories.py /dev/ttyS0 --address 1
    python3 examples/wager_categories.py /dev/ttyS0 --address 1 --weighted-average
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.client import SASClient
from saspy.constants import LongPoll
from saspy.exceptions import SASError
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

# "If the requested wager category is not supported for the requested game
# number, the payback percentage will be all nulls (0) and the Coin In meter
# size will be 0" (Table 7.24b). The client hands those four NULs back as a
# str, so a tool that prints it unchecked prints control characters.
UNSUPPORTED_PAYBACK = "\x00\x00\x00\x00"

# At what fraction of a meter's ceiling to start saying so. A meter past this
# point is one the CMS will shortly have to reconstruct digits for, and any
# percentage derived from it at the gateway is about to go wrong quietly.
NEAR_ROLLOVER_PERCENT = 90.0


def parse_payback(raw: str) -> float | None:
    """4 ASCII digits, "??.??" with the decimal implied and not transmitted."""
    if raw == UNSUPPORTED_PAYBACK or not raw.strip("\x00"):
        return None
    digits = raw.strip()
    if len(digits) != 4 or not digits.isdigit():
        return None
    return int(digits) / 100


def ceiling_for(size_bytes: int) -> int | None:
    """A BCD meter of n bytes holds 2n digits, so it wraps at 10**(2n)."""
    return 10 ** (2 * size_bytes) - 1 if size_bytes else None


def describe_game(client, game_number: int):
    """LP 0xB5 -- the paytable's name and how many wager categories it has."""
    try:
        return client.send_extended_game_n_information(game_number)
    except SASError:
        return None


def read_category(client, game_number: int, category: int):
    try:
        return client.send_wager_category_information(game_number, category)
    except SASError as e:
        return e


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--game", type=int, action="append", default=None,
                    help="only sweep this game number (repeatable). Default: game 0 "
                         "plus every game LP 0x51 says is implemented.")
    ap.add_argument("--max-category", type=int, default=None,
                    help="sweep categories 1..N even where LP 0xB5 does not say how "
                         "many there are. Unsupported ones are reported as such.")
    ap.add_argument("--weighted-average", action="store_true",
                    help="also compute the weighted average, with its caveat. Off by "
                         "default: see this tool's docstring.")
    args = ap.parse_args()

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(
        SASTransport(port, byte_timeout=args.timeout), args.address, timeout=args.timeout
    )

    print(f"\n=== {args.port}, address {args.address} ===\n")

    if args.game is not None:
        games = list(dict.fromkeys(args.game))
    else:
        games = [0]
        try:
            implemented = client.send_meter(LongPoll.SEND_TOTAL_GAMES_IMPLEMENTED)
        except SASError as e:
            print(f"LP 0x51 (total games implemented) did not answer: "
                  f"{type(e).__name__}: {e}")
            print("Sweeping game 0 (the gaming machine) only. Use --game to name others.\n")
        else:
            print(f"LP 0x51: {implemented} games implemented\n")
            games += list(range(1, implemented + 1))

    # (game, category, payback, coin_in, size) for everything that answered.
    rows: list[tuple[int, int, float | None, int, int]] = []
    near_rollover: list[tuple[int, int, int, int]] = []
    any_answer = False

    for game in games:
        info = describe_game(client, game)
        if info is None:
            label = f"game {game}"
            declared = None
        else:
            label = (f"game {game}: {info.game_name.strip()!r}"
                     f" paytable {info.paytable_name.strip()!r}")
            declared = info.wager_categories

        print(label)
        if declared is not None:
            print(f"    LP 0xB5 declares {declared} wager categories, max bet {info.max_bet}")

        if args.max_category is not None:
            categories = range(0, args.max_category + 1)
        elif declared:
            categories = range(0, declared + 1)
        else:
            categories = range(0, 1)   # category 0 is always meaningful
            if declared == 0:
                print("    (0xB5 says no wager categories, so only category 0 -- the"
                      " paytable's max-bet theoretical and total Coin In -- applies)")

        print(f"    {'cat':>4}  {'payback':>8}  {'Coin In':>18}  {'size':>4}  headroom")
        for category in categories:
            result = read_category(client, game, category)
            if isinstance(result, SASError):
                print(f"    {category:>4}  {'--':>8}  {'no answer':>18}"
                      f"        ({type(result).__name__})")
                continue

            any_answer = True
            payback = parse_payback(result.payback_percentage)
            size = getattr(result, "coin_in_meter_size", None)
            coin_in = result.coin_in_meter

            if payback is None and coin_in == 0:
                print(f"    {category:>4}  {'n/a':>8}  {'not supported':>18}"
                      "     (Table 7.24b: null payback, size 0)")
                continue

            size_text = "?" if size is None else str(size)
            ceiling = ceiling_for(size) if size else None
            if ceiling:
                used = 100 * coin_in / ceiling
                # Printed to 4 places, but flagged on the real value: 99.9999%
                # of a ceiling rounds to "100.0000%" and would otherwise read as
                # already wrapped. Which it is not -- and the distinction is the
                # whole point of the column.
                headroom = f"{used:.4f}% of {ceiling:,}"
                if used >= NEAR_ROLLOVER_PERCENT:
                    headroom += "  <- NEAR ROLLOVER"
                    near_rollover.append((game, category, coin_in, ceiling))
            else:
                headroom = "size not reported, so no ceiling"

            payback_text = "n/a" if payback is None else f"{payback:.2f}%"
            print(f"    {category:>4}  {payback_text:>8}  {coin_in:>18,}"
                  f"  {size_text:>4}  {headroom}")
            rows.append((game, category, payback, coin_in, size or 0))
        print()

    if not any_answer:
        print("Nothing answered LP 0xB4. Section 4.4 lets a machine ignore long polls it")
        print("does not support, so this machine very likely does not implement wager")
        print("categories at all -- which, with 007F also unsupported, means its screen's")
        print("payback figure has no SAS route on this machine.")
        return 0

    if near_rollover:
        print("=" * 68)
        print(f"{len(near_rollover)} Coin In meter(s) are past"
              f" {NEAR_ROLLOVER_PERCENT:.0f}% of their declared ceiling:")
        for game, category, coin_in, ceiling in near_rollover:
            print(f"    game {game} category {category}: {coin_in:,} of {ceiling:,}"
                  f"  ({ceiling - coin_in:,} to go)")
        print("When one of these wraps, it does so silently -- SAS sends the wrapped")
        print("value with no indication that it wrapped. Anything derived from it")
        print("downstream needs the rollover count the CMS keeps, not this reading.")
        print("=" * 68)
        print()

    if args.weighted_average:
        print("=" * 68)
        # Categories above 0 are the breakdown; category 0 is the paytable total
        # and would double-count if mixed in.
        parts = [(p, c) for game, cat, p, c, _s in rows
                 if cat != 0 and p is not None and c > 0]
        total_coin_in = sum(c for _p, c in parts)
        if not parts or total_coin_in == 0:
            print("Cannot form a weighted average: no wager category above 0 reported")
            print("both a payback percentage and a non-zero Coin In. Category 0 alone is")
            print("the paytable's max-bet theoretical, which is not an average of anything.")
        else:
            average = sum(p * c for p, c in parts) / total_coin_in
            print(f"Weighted average over {len(parts)} categories: {average:.2f}%")
            print(f"  from Coin In totalling {total_coin_in:,} accounting units")
            print()
            print("  CROSS-CHECK ONLY. This is correct only while no Coin In meter above")
            print("  has rolled over. Past a rollover the meter has lost its high-order")
            print("  digits, nothing here records how many times it wrapped, and this")
            print("  number drifts without any sign that it has. The headroom column says")
            print("  how close each meter is; reconstructing wrapped digits belongs to the")
            print("  CMS, which holds the history to do it with. Do not store this figure")
            print("  as a meter, and do not reconcile against it.")
        print("=" * 68)
    else:
        print("=" * 68)
        print("No weighted average computed. The raw figures above are what a gateway")
        print("can report without going stale at the first meter rollover; the average")
        print("belongs where the rollover history is. --weighted-average computes it")
        print("anyway, for cross-checking against a figure from elsewhere.")
        print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
