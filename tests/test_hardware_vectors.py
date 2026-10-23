"""Fixtures built from one real machine, cross-checked against its own screen.

Every other test in this suite is built from the spec tables: they prove the
code matches somebody's reading of the document. These prove it matches a
gaming machine, because each value here was read off an EGT Premier over SAS
and then found on that machine's own Machine Accounting display, to the cent:

    SAS total_coin_in           21387723  ->  Total Bet (Coin In)   213,877.23 USD
    SAS total_coin_out          20718334  ->  Total Won             207,183.34 USD
    SAS total_drop               6408958  ->  Total In               64,089.58 USD
    SAS total_cancelled_credits  5739569  ->  Total Cancelled Cr.    57,395.69 USD
    SAS games_played              665524  ->  Total Games Played        665,524

That agreement is what makes these worth keeping. Field ORDER especially: a
transcription slip that swapped two meters would still decode to plausible
numbers and pass every spec-derived test, and would only be caught by values
that are known-correct from outside this codebase. Machine: SAS version 602,
serial 000106719, paytable MGDJE3.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.bcd import encode_bcd
from saspy.client import SASClient
from saspy.crc import crc16_bytes
from saspy.transport import SASTransport
from tests.test_client import ADDRESS, FakeSerial

# As read from the machine, in SAS field order (Table 7.1a).
METERS_0F = (
    ("total_cancelled_credits", 5739569),
    ("total_coin_in", 21387723),
    ("total_coin_out", 20718334),
    ("total_drop", 6408958),
    ("total_jackpot", 0),
    ("games_played", 665524),
)


def _client(response_body: bytes) -> SASClient:
    full = response_body + crc16_bytes(response_body)
    return SASClient(SASTransport(FakeSerial(full), wake_byte=None), ADDRESS, timeout=1.0)


def test_meters_10_through_15_matches_the_machines_own_accounting_screen():
    body = bytes([ADDRESS, 0x0F]) + b"".join(encode_bcd(v, 4) for _n, v in METERS_0F)
    result = _client(body).send_meters_10_through_15()

    for name, expected in METERS_0F:
        assert getattr(result, name) == expected, f"{name} decoded wrong"

    # The cross-check that gives these numbers their authority: SAS meters are
    # in cents, and these are the machine's own displayed dollars.
    assert result.total_coin_in / 100 == 213877.23
    assert result.total_coin_out / 100 == 207183.34
    assert result.total_drop / 100 == 64089.58
    assert result.total_cancelled_credits / 100 == 57395.69


def test_sas_version_and_serial_round_trip():
    """602 / 000106719, as reported by the machine.

    The length byte covers the version and serial together: 3 + 9 = 12.
    """
    body = bytes([ADDRESS, 0x54, 12]) + b"602" + b"000106719"
    result = _client(body).send_sas_version_and_serial()

    assert result.sas_version == "602"
    assert result.serial_number == "000106719"


def test_gaming_machine_id_round_trip():
    """The 0x1F fields this machine reported.

    max_bet is a single byte and came back 255, which is both a legitimate
    value and 0xFF -- the saturation value a too-narrow or unsupported field
    also produces. It is pinned here as observed, not as verified; confirm it
    against the machine's own max-bet setting before trusting it.
    """
    body = (
        bytes([ADDRESS, 0x1F])
        + b"EG"
        + b"3CO"
        + bytes([1, 255, 0])
        + bytes([0x00, 0x00])
        + b"MGDJE3"
        + b"9546"
    )
    result = _client(body).send_gaming_machine_id()

    assert result.game_id == "EG"
    assert result.additional_id == "3CO"
    assert result.denomination == 1
    assert result.max_bet == 255
    assert result.paytable_id == "MGDJE3"
    assert result.base_percentage == "9546"  # 95.46%
