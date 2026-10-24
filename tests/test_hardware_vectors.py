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


def test_exception_table_names_the_codes_seen_on_hardware():
    """0x11, 0x12 and 0x84 were observed live on the EGT machine: the slot
    door was opened and closed, and the operator menu was entered. Pinning
    their descriptions keeps the Appendix A transcription honest about the
    three codes we have independent confirmation for.
    """
    from saspy.constants import describe_exception

    assert describe_exception(0x11) == "slot door was opened"
    assert describe_exception(0x12) == "slot door was closed"
    assert describe_exception(0x84) == "self test or operator menu has been entered"


def test_rte_only_codes_are_flagged_as_such():
    """These four are sent only when real time event reporting is enabled
    (long poll 0x0E, Section 12). Their absence in the default polling mode
    is correct behaviour, and a monitor that does not say so invites someone
    to chase a fault that is not there.
    """
    from saspy.constants import (
        GENERAL_POLL_EXCEPTIONS_RTE_ONLY,
        describe_exception,
    )

    assert GENERAL_POLL_EXCEPTIONS_RTE_ONLY == {0x87, 0x88, 0x89, 0x8B}
    for code in GENERAL_POLL_EXCEPTIONS_RTE_ONLY:
        assert "RTE only" in describe_exception(code)


def test_game_start_and_end_are_not_rte_only():
    """The point of the distinction. 0x7E/0x7F are queued exceptions in the
    ordinary polling mode, so watching play does not require RTE -- which
    matters because enabling RTE changes the response format library-wide
    (Section 12.5) and nothing here parses that yet.
    """
    from saspy.constants import (
        GENERAL_POLL_EXCEPTIONS_RTE_ONLY,
        describe_exception,
    )

    assert 0x7E not in GENERAL_POLL_EXCEPTIONS_RTE_ONLY
    assert 0x7F not in GENERAL_POLL_EXCEPTIONS_RTE_ONLY
    assert describe_exception(0x7E) == "GAME HAS STARTED"
    assert describe_exception(0x7F) == "GAME HAS ENDED"


def test_aft_transfer_status_categories_come_from_the_top_three_bits():
    """Table 8.3e's own note: the 3 MSbits give the category. A host that
    only compares against 0x00 reads "partial transfer successful" as a
    failure, and an undocumented code as nothing at all.
    """
    from saspy.constants import describe_aft_transfer_status

    assert describe_aft_transfer_status(0x00)[0] == "SUCCESSFUL"
    assert describe_aft_transfer_status(0x01)[0] == "SUCCESSFUL"
    assert describe_aft_transfer_status(0x40)[0] == "PENDING"
    for failed in (0x81, 0x84, 0x87, 0x93, 0x9F):
        assert describe_aft_transfer_status(failed)[0] == "FAILED"
    assert describe_aft_transfer_status(0xC1)[0] == "UNSUPPORTED POLL"
    assert describe_aft_transfer_status(0xFF)[0] == "NO INFORMATION"


def test_the_machines_own_aft_status_byte_decodes_as_observed():
    """0xB2, read from the EGT Premier: AFT on, in-house and bonus transfers
    enabled, debit off, not registered. Registration being absent is not a
    blocker for in-house transfers -- section 8.1 requires it for debit only.
    """
    status = 0xB2
    assert status & 0x80, "any AFT enabled"
    assert status & 0x10, "in-house transfers enabled"
    assert not status & 0x40, "debit transfers disabled"
    assert not status & 0x08, "not registered"


def test_the_exception_queue_is_specified_as_at_least_twenty_deep():
    """Section 2.2.1 requires "a first in/first out (FIFO) exception queue of
    at least 20 elements in non-volatile memory", drops the OLDEST on
    overrun, and requires exception 70 to report any loss.

    Recorded here because a project design document attributed observed
    exception loss to SAS holding only one pending exception per machine.
    The spec says the opposite, and the difference changes the diagnosis: a
    gateway losing exceptions without ever seeing 0x70 is not losing them to
    queue overrun.
    """
    from saspy.constants import describe_exception

    assert describe_exception(0x70) == "exception buffer overflow"
    # Both door exceptions are Type Q (queued), which is what makes them a
    # usable generator for measuring the queue -- priority exceptions are
    # never queued at all.
    assert describe_exception(0x11) == "slot door was opened"
    assert describe_exception(0x12) == "slot door was closed"


def test_interrogation_poll_is_table_8_3b_not_a_full_transfer_body():
    """The poll that stops exception 0x69. Table 8.3b is length 02 -- transfer
    code plus transaction index, nothing else. Sending a full Table 8.3a body
    with transfer code FF would be a different message entirely.

    Observed on an EGT Premier: a transfer answered with pending status 0x40
    completed, and the machine then reissued exception 0x69 every 15.2 seconds
    for minutes, exactly as section 8.8 requires, because nothing had sent
    this. It is mandatory for any host that transfers funds, not optional.
    """
    from saspy.crc import crc16_bytes
    from saspy.transport import SASTransport
    from tests.test_client import FakeSerial

    # Table 8.3c response, enough of it to parse: command, length, then the
    # fixed head and the three length-prefixed meters at the tail.
    data = (
        bytes([0x01, 0x00, 0x00, 0x00])            # buffer position, status, receipt, type
        + encode_bcd(2000, 5) + encode_bcd(0, 5) + encode_bcd(0, 5)
        + bytes([0x00]) + bytes([246, 0, 0, 0])    # flags, asset number (LE)
        + bytes([4]) + b"TEST"                     # transaction ID
        + encode_bcd(10232026, 4) + encode_bcd(184500, 3)
        + encode_bcd(0, 4) + bytes([0x00, 0x00])   # expiration, pool id
        + bytes([5]) + encode_bcd(6341558, 5)
        + bytes([5]) + encode_bcd(0, 5)
        + bytes([5]) + encode_bcd(0, 5)
    )
    # The length byte counts everything after it, excluding the CRC -- get it
    # wrong and exchange_length_prefixed reads the wrong number of bytes.
    body = bytes([ADDRESS, 0x72, len(data)]) + data
    fake = FakeSerial(body + crc16_bytes(body))
    client = SASClient(SASTransport(fake, wake_byte=None), ADDRESS, timeout=1.0)

    client.aft_interrogate_transfer(0x00, acknowledge=True)

    sent = bytes(fake.written)
    assert sent[:4] == bytes([ADDRESS, 0x72, 0x02, 0xFF]), "length 02, transfer code FF"
    assert sent[4] == 0x00, "transaction index 00 = current/most recent"
    assert sent[5:] == crc16_bytes(sent[:5])


def test_interrogation_can_read_without_acknowledging():
    """Table 8.3b: transfer code FE is "identical to FF, except a response to
    an FE interrogation does not in any way affect the current transfer
    cycle". So FE reads status but does NOT stop the 0x69 reissue, and
    machines without FE support answer with status C1.
    """
    from saspy.crc import crc16_bytes
    from saspy.transport import SASTransport
    from tests.test_client import FakeSerial

    fake = FakeSerial(b"")
    client = SASClient(SASTransport(fake, wake_byte=None), ADDRESS, timeout=0.01)
    try:
        client.aft_interrogate_transfer(0x00, acknowledge=False)
    except Exception:
        pass  # no response in the fake; the frame is what is under test

    assert bytes(fake.written)[:4] == bytes([ADDRESS, 0x72, 0x02, 0xFE])
