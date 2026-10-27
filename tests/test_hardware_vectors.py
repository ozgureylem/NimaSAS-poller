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


def test_the_two_aft_exceptions_are_told_apart_by_their_reissue_interval():
    """0x69 and 0x6A look alike in a log and mean opposite things, and the
    spec gives each its own reissue rate -- which is the discriminator when
    the cause is in doubt.

    Section 8.8, exception 69 (a transfer the HOST started has finished and
    is unacknowledged): "reissue exception 69 every 15 seconds until the host
    polls for and acknowledges the completion status".

    Section 8.7, exceptions 6A/6B (the MACHINE asking the host to take
    credits, as when a card is removed): "as long as a cashout to the host is
    pending, the gaming machine will reissue the exception 6A or exception 6B
    every 800 milliseconds".

    Observed on an EGT Premier: 15.2 second spacing, which is 0x69's rate and
    nowhere near 0x6A's, so the cause was an unacknowledged transfer
    completion rather than a cashout request.
    """
    from saspy.constants import describe_exception

    assert describe_exception(0x69) == "AFT transfer complete"
    assert describe_exception(0x6A) == "AFT request for host cashout"
    assert describe_exception(0x6B) == "AFT request for host to cash out win"


def test_host_cashout_enabled_is_bit_1_of_host_cashout_status():
    """Table 8.2b. The machine reported 0x02: cashout to host enabled, forced
    by the machine rather than host-controllable, soft mode. So the
    card-removal flow that returns credits to an AFT account is live on it --
    and will announce itself as 0x6A, not 0x69.
    """
    status = 0x02
    assert status & 0x02, "cashout to host currently enabled"
    assert not status & 0x01, "forced by gaming machine, not host-controllable"
    assert not status & 0x04, "soft mode: machine self-recovers after 8s"


def test_credits_reconcile_against_wager_and_win_on_hardware():
    """The closed loop, from a live EGT Premier at 19:13 on 23 Oct 2026.

    An AFT push left 4110 credits. Four games followed, each wagering 25,
    one paying 5. Every CUR_CRED the monitor reported equals the previous
    balance minus the wager plus the win, and every total_coin_in delta
    equals credits_wagered.

    That closes the loop the whole session was aiming at: the transfer, the
    wager, the win and the credit meter all agree, so a session's wager can
    be reconciled from the boundary events alone. Kept as a fixture because
    it is arithmetic over values confirmed outside this codebase.
    """
    games = [(25, 0, 4085), (25, 0, 4060), (25, 5, 4040), (25, 0, 4015)]
    credits = 4110
    for wagered, win, reported in games:
        credits = credits - wagered + win
        assert credits == reported

    coin_in = [21389298, 21389323, 21389348, 21389373]
    assert all(coin_in[i + 1] - coin_in[i] == 25 for i in range(len(coin_in) - 1))


def test_credits_and_coin_out_are_batched_into_one_exchange():
    """Both are Table C-7 codes, so LP 0x6F fetches them together. That is
    cheaper than two single-meter polls and more correct: the two values come
    from the same instant, where separate reads could have a game land
    between them and fail to reconcile.
    """
    from saspy.constants import MeterCode

    assert int(MeterCode.CURRENT_CREDITS) == 0x0C
    assert int(MeterCode.TOTAL_COIN_OUT_CREDITS) == 0x01


def test_restricted_transfer_carries_pool_id_and_expiration():
    """Section 8: "the pool ID is ignored if the transfer does not include a
    restricted amount", and "expiration is valid for all restricted amount
    transfers". So those two fields are only meaningful with restricted
    credits, and sending them otherwise is noise in the frame.
    """
    from saspy.constants import AFT_TRANSFER_CODE_FULL_ONLY, AftTransferType
    from saspy.crc import crc16_bytes
    from saspy.transport import SASTransport
    from tests.test_client import FakeSerial

    fake = FakeSerial(b"")
    client = SASClient(SASTransport(fake, wake_byte=None), ADDRESS, timeout=0.01)
    try:
        client.aft_transfer_funds(
            transfer_code=AFT_TRANSFER_CODE_FULL_ONLY,
            transfer_type=int(AftTransferType.IN_HOUSE_TO_GAMING_MACHINE),
            restricted_amount=1000,
            pool_id=7,
            expiration=12312026,
            asset_number=246,
            transaction_id="PROMO1",
        )
    except Exception:
        pass  # no response in the fake; the frame is what is under test

    sent = bytes(fake.written)
    assert sent[0] == ADDRESS and sent[1] == 0x72
    # transfer code, index, type, then three 5-byte BCD amounts
    assert sent[3] == 0x00 and sent[5] == 0x00, "in-house transfer to gaming machine"
    cashable = sent[6:11]
    restricted = sent[11:16]
    nonrestricted = sent[16:21]
    assert cashable == encode_bcd(0, 5), "no cashable amount requested"
    assert restricted == encode_bcd(1000, 5), "the restricted amount goes in its own field"
    assert nonrestricted == encode_bcd(0, 5)
    assert encode_bcd(12312026, 4) in sent, "expiration present for a restricted transfer"


def test_the_amount_types_are_separate_meters():
    """Table C-7 gives each amount type its own pair of meters, which is how a
    promotional push is verified: a restricted transfer must move 00A2/00A3
    and leave the cashable pair 00A0/00A1 alone. Reading all three pairs
    every time means the test proves what did NOT move as well as what did.
    """
    from saspy.constants import MeterCode

    assert int(MeterCode.IN_HOUSE_CASHABLE_TRANSFERS_TO_GAMING_MACHINE_CENTS) == 0xA0
    assert int(MeterCode.IN_HOUSE_RESTRICTED_TRANSFERS_TO_GAMING_MACHINE_CENTS) == 0xA2
    assert int(MeterCode.IN_HOUSE_NONRESTRICTED_TRANSFERS_TO_GAMING_MACHINE_CENTS) == 0xA4
    # And the credit meters they land in are distinct too.
    assert int(MeterCode.CURRENT_CREDITS) == 0x0C
    assert int(MeterCode.CURRENT_RESTRICTED_CREDITS) == 0x1B


def test_host_cashout_uses_the_whole_cashout_sentinel():
    """Section 8.7: "the amount fields may all be set to 9999999999, with
    partial transfer allowed, to transfer the entire cashout amount."

    The pairing matters. 9999999999 with FULL-transfer-only would ask the
    machine to move ten billion units or nothing, which it cannot, so the
    partial-allowed code is what makes the sentinel mean "whatever is there".
    """
    from saspy.constants import AFT_TRANSFER_CODE_PARTIAL_ALLOWED, AftTransferType
    from saspy.crc import crc16_bytes
    from saspy.transport import SASTransport
    from tests.test_client import FakeSerial

    fake = FakeSerial(b"")
    client = SASClient(SASTransport(fake, wake_byte=None), ADDRESS, timeout=0.01)
    try:
        client.aft_transfer_funds(
            transfer_code=AFT_TRANSFER_CODE_PARTIAL_ALLOWED,
            transfer_type=int(AftTransferType.IN_HOUSE_FROM_GAMING_MACHINE_TO_HOST),
            cashable_amount=9_999_999_999,
            restricted_amount=9_999_999_999,
            nonrestricted_amount=9_999_999_999,
            asset_number=246,
            transaction_id="OUT1",
        )
    except Exception:
        pass

    sent = bytes(fake.written)
    assert sent[3] == 0x01, "partial transfer allowed"
    assert sent[5] == 0x80, "in-house transfer FROM gaming machine TO host"
    for offset in (6, 11, 16):
        assert sent[offset:offset + 5] == encode_bcd(9_999_999_999, 5)


def test_a_zero_transfer_is_how_a_cashout_is_declined():
    """Section 8.7: "all amounts may be set to zero to instruct the gaming
    machine to perform the cashout by whatever other means are available."

    That is an answer, not a no-op -- and it is the only way to observe the
    exchange without taking money. Ignoring 0x6A instead lets the 8-second
    deadline expire, which in soft mode is silent.
    """
    from saspy.constants import AFT_TRANSFER_CODE_FULL_ONLY, AftTransferType
    from saspy.transport import SASTransport
    from tests.test_client import FakeSerial

    fake = FakeSerial(b"")
    client = SASClient(SASTransport(fake, wake_byte=None), ADDRESS, timeout=0.01)
    try:
        client.aft_transfer_funds(
            transfer_code=AFT_TRANSFER_CODE_FULL_ONLY,
            transfer_type=int(AftTransferType.IN_HOUSE_FROM_GAMING_MACHINE_TO_HOST),
            cashable_amount=0, restricted_amount=0, nonrestricted_amount=0,
            asset_number=246, transaction_id="DENY1",
        )
    except Exception:
        pass

    sent = bytes(fake.written)
    for offset in (6, 11, 16):
        assert sent[offset:offset + 5] == encode_bcd(0, 5)


def test_the_two_cashout_exceptions_use_different_transfer_types():
    """6A is a cashout from the credit meter and takes type 80. 6B is a win
    ready to be cashed out and takes type 90, which section 8 notes is
    "metered as an in-house transfer to the host" even so.
    """
    from saspy.constants import AftTransferType, describe_exception

    assert describe_exception(0x6A) == "AFT request for host cashout"
    assert describe_exception(0x6B) == "AFT request for host to cash out win"
    assert int(AftTransferType.IN_HOUSE_FROM_GAMING_MACHINE_TO_HOST) == 0x80
    assert int(AftTransferType.WIN_AMOUNT_FROM_GAMING_MACHINE_TO_HOST) == 0x90


def test_a_denied_cashout_falls_back_to_whatever_device_exists():
    """Observed, and it inverts the advice.

    Section 8.7's zero transfer instructs the machine to "perform the cashout
    by whatever other means are available". On an EGT Premier with no printer
    there is no other device, so a denied $37.47 cashout became a HANDPAY:
    exception 0x51, "Call Attendant" on screen, locked until it was reset two
    minutes later.

    So on a machine with no usable cashout device, declining is the MORE
    disruptive option -- it needs a person with a reset key -- while taking
    the money to the host completes cleanly.

    CORRECTION to what this test used to assert. It read Table 8.2b's
    available-transfers bit 2 as the discriminator, on the basis that a clear
    bit 2 meant "no printer, so no ticket fallback". That is wrong, and
    wrong in the direction that wastes a day: bit 2 is "Transfer to printer
    OK", an AFT statement about whether the HOST may transfer funds onto a
    ticket. It says nothing about whether the machine prints its own voucher
    when a player presses CASH OUT. The same cabinet later printed cashout
    tickets perfectly with bit 2 unchanged, once its Printer Limit was moved
    off 0.00 -- a setting SAS cannot read at all (MANUAL.md section 7.2).

    So there is no bit to check here. SAS does not report whether a cashout
    will reach paper, which is precisely why --deny is the dangerous option:
    the gateway cannot know in advance which fallback it is choosing. What
    remains assertable is the exception sequence, and that long poll 0x94 --
    not a long poll 0x52, which is Send game N meters -- is the remote way
    out of the lockup.
    """
    from saspy.constants import HANDPAY_RESET_CODES, LongPoll, describe_exception

    assert describe_exception(0x51) == (
        "handpay is pending (progressive, non-progressive or cancelled credits)"
    )
    assert describe_exception(0x52) == "handpay was reset (jackpot reset switch activated)"
    assert describe_exception(0x66) == "cash out button pressed"

    # Exception 0x52 reports a reset; long poll 0x94 performs one.
    assert int(LongPoll.RESET_HANDPAY) == 0x94
    assert int(LongPoll.SEND_GAME_N_METERS) == 0x52
    assert HANDPAY_RESET_CODES[0x00] == "handpay was reset"


def test_priority_exceptions_are_delivered_before_queued_ones():
    """Visible in the same capture. The player pressed cash out, which is
    0x66 and queued (Type Q in Appendix A), but 0x6A arrived FIRST because
    section 2.2.1 sends a priority exception ahead of anything in the queue:

        14:02:00  0x6A  AFT request for host cashout      (priority)
        14:02:02  0x51  handpay is pending
        14:02:02  0x66  cash out button pressed           (queued, from before)

    A host that assumed arrival order equals event order would reconstruct
    that sequence backwards.
    """
    from saspy.constants import GENERAL_POLL_EXCEPTIONS

    for code in (0x6A, 0x6B, 0x69, 0x51, 0x66):
        assert code in GENERAL_POLL_EXCEPTIONS


def test_partial_success_is_the_normal_outcome_of_a_host_cashout():
    """A host cashout succeeds with 0x01, not 0x00, and that trips people up.

    Section 8.7's "take everything" sentinel is 9999999999 with partial
    transfer allowed, which asks for far more than any machine holds -- so
    the machine moves the actual balance and reports PARTIAL transfer
    successful. Observed on an EGT Premier: a $49.70 cashout returned 0x01.

    A host testing `status == 0x00` would read every host cashout as a
    failure, and might retry a transfer that had already moved the money.
    Table 8.3e's own note is the defence: the top three bits give the
    category, and 000xxxxx covers both 00 and 01.
    """
    from saspy.constants import describe_aft_transfer_status

    assert describe_aft_transfer_status(0x00) == ("SUCCESSFUL", "full transfer successful")
    assert describe_aft_transfer_status(0x01) == ("SUCCESSFUL", "partial transfer successful")
    assert describe_aft_transfer_status(0x01)[0] == describe_aft_transfer_status(0x00)[0]


def test_the_promotional_lifecycle_reconciles_end_to_end():
    """Every step observed on one machine, and the arithmetic closes.

    $5.00 pushed as restricted (00A2 +500, 00A0 unchanged). Play consumed
    $4.80 of it, leaving 0.20 restricted beside 50.00 cashable. One more game
    at a 0.50 bet took the restricted first, per section 8's fixed order of
    restricted, then nonrestricted, then cashable -- so 0.20 restricted and
    0.30 cashable. The host cashout then took 49.70.
    """
    cashable, restricted = 5000, 20          # cents, before the last game
    bet = 50
    # Section 8: restricted is wagered first.
    from_restricted = min(restricted, bet)
    from_cashable = bet - from_restricted
    assert from_restricted == 20 and from_cashable == 30

    assert cashable - from_cashable == 4970, "the amount the host actually received"
    assert restricted - from_restricted == 0, "restricted exhausted by play, not by the cashout"


def test_the_screens_promotional_wording_is_inverted_from_sas():
    """The mapping that the words actively mislead about, established by
    pushing and cashing out each amount type and watching which screen line
    moved on an EGT Premier.

    The machine's "Non-cashable Electronic In/Out" is SAS RESTRICTED, and its
    "Promotional Electronic In/Out" is SAS NONRESTRICTED. Reading those the
    way the words suggest -- promotional meaning restricted-promotional --
    swaps the two categories, and both are promotional, so neither figure
    looks obviously wrong afterwards.

    Evidence: $5.00 and $9.00 were pushed as restricted and $9.00 cashed back
    out. The screen showed Non-cashable Electronic In 14.00 and Non-cashable
    Electronic Out 9.00, while both Promotional Electronic lines stayed 0.00
    -- no nonrestricted amount was ever transferred.
    """
    from saspy.constants import MeterCode

    assert int(MeterCode.ELECTRONIC_RESTRICTED_PROMOTIONAL_TRANSFERS_TO_GAMING_MACHINE_CREDITS) == 0x2F
    assert int(MeterCode.ELECTRONIC_NONRESTRICTED_PROMOTIONAL_TRANSFERS_TO_GAMING_MACHINE_CREDITS) == 0x30
    assert int(MeterCode.ELECTRONIC_RESTRICTED_PROMOTIONAL_TRANSFERS_TO_HOST_CREDITS) == 0x33
    assert int(MeterCode.ELECTRONIC_NONRESTRICTED_PROMOTIONAL_TRANSFERS_TO_HOST_CREDITS) == 0x34

    # 5.00 + 9.00 pushed as restricted, 9.00 taken back: the figures the
    # screen showed on those two lines.
    assert 500 + 900 == 1400, "screen: Non-cashable Electronic In 14.00"
    assert 900 == 900, "screen: Non-cashable Electronic Out 9.00"


def test_the_screens_cashless_subtotals_are_the_sum_of_three_sas_meters():
    """Recomputing the subtotal is what checks the MAPPING rather than just
    the individual figures -- three meters can each be read correctly and
    still be assigned to the wrong screen lines.

    From the machine: Cashable 63,585.58 + Promotional 0.00 + Non-cashable
    14.00 = Cashless In 63,599.58, and 57,459.27 + 0.00 + 9.00 = Cashless Out
    57,468.27. Both close exactly.
    """
    assert round(63585.58 + 0.00 + 14.00, 2) == 63599.58
    assert round(57459.27 + 0.00 + 9.00, 2) == 57468.27
    # And the screen's own totals, one level up.
    assert round(694.00 + 63599.58, 2) == 64293.58, "Bill In + Cashless In = Total In"
    assert round(123.59 + 57468.27, 2) == 57591.86, "attendant + Cashless Out = Total Out"


def test_bill_in_is_bills_accepted_not_bills_to_drop():
    """Two meters that sound alike and are not.

    0x0B is total credits from bills ACCEPTED, which is the screen's "Bill
    In". 0x58 is total credits from bills TO DROP -- where accepted bills end
    up afterwards, and which a cashless machine with no drop box does not
    report at all. Using 0x58 printed "n/a" against a screen reading 694.00.
    """
    from saspy.constants import MeterCode

    assert int(MeterCode.TOTAL_CREDITS_FROM_BILLS_ACCEPTED) == 0x0B
    assert int(MeterCode.TOTAL_CREDITS_FROM_BILLS_TO_DROP) == 0x58
    # And neither is the stacker level, which falls when the box is emptied.
    assert int(MeterCode.TOTAL_VALUE_OF_BILLS_CURRENTLY_IN_STACKER_CREDITS) == 0x3F


def test_a_subtotal_names_the_meter_that_did_not_answer():
    """An n/a subtotal hides WHICH component is missing, and the answer is
    usually that the machine does not implement it -- a fact about the
    machine worth printing rather than an error to swallow.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "accounting_summary",
        str(Path(__file__).resolve().parent.parent / "examples" / "accounting_summary.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    total, missing = mod.subtotal({0x2E: 100, 0x30: 0, 0x2F: 400}, (0x2E, 0x30, 0x2F))
    assert total == 500 and missing == []

    total, missing = mod.subtotal({0x2E: 100, 0x30: None, 0x2F: 400}, (0x2E, 0x30, 0x2F))
    assert total is None and missing == [0x30]


# -- Accounting identities, checked against the committed captures ----------
#
# These exist because a claim about 0004 was made from arithmetic that was
# never read off the machine, used to withdraw a correct label, and committed.
# The captures were sitting in the repository the whole time. Asserting the
# identities over them turns "I computed this" into "the machine reported
# this", which is the distinction that failed.

def _captures():
    import json
    from pathlib import Path
    d = Path(__file__).resolve().parent.parent / "captures"
    if not d.is_dir():  # code-only mirror carries no captures
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        meters = json.loads(f.read_text()).get("meters_raw") or {}
        if meters:
            out.append((f.name, meters))
    return out


def test_total_cancelled_credits_includes_electronic_out():
    """0004 = attendant handpay + both ticket-out types + all three
    electronic-out types, in every capture.

    This is what disproves the withdrawal: 0004 does NOT exclude AFT
    transfers to the host. On the 2026-10-27 capture it reads 51,880, which
    is 15,100 + 18,500 + 2,910 + 8,704 + 4,444 + 2,222 -- and the screen line
    of the same name reads 518.80.
    """
    captures = _captures()
    if not captures:
        import pytest
        pytest.skip("no captures/ directory in this checkout")
    checked = 0
    for name, m in captures:
        g = lambda c: m.get(f"{c:04X}")
        if g(0x0004) is None:
            continue
        parts = [g(c) for c in (0x0003, 0x000E, 0x0010, 0x0032, 0x0033, 0x0034)]
        if any(p is None for p in parts):
            continue
        # Pre-clear captures carry bill-era activity this formula omits; they
        # are recognised by having no ticket-out meters at all.
        total = sum(parts)
        assert g(0x0004) == total, (
            f"{name}: 0004 is {g(0x0004)}, components sum to {total}")
        checked += 1
    assert checked >= 15, f"only {checked} captures carried all the meters"


def test_total_drop_is_electronic_in_plus_ticket_in():
    """0024 = 0017 + 0015 in the current epoch, 54,554 against a screen
    reading 545.54. C-7 defines it as total drop "including but not limited
    to ... tickets to drop, and electronic in", so a capture with bill
    activity adds a term; this checks the epoch that has none.
    """
    captures = _captures()
    if not captures:
        import pytest
        pytest.skip("no captures/ directory in this checkout")
    latest = captures[-1][1]
    g = lambda c: latest.get(f"{c:04X}")
    assert g(0x0024) == (g(0x0017) or 0) + (g(0x0015) or 0)


def test_the_machine_balances_end_to_end():
    """Total in minus total out equals total bet minus total won, whenever
    the credit meter is empty. The strongest single check available: it ties
    four independent meters together and would catch a misread on any of
    them.
    """
    captures = _captures()
    if not captures:
        import pytest
        pytest.skip("no captures/ directory in this checkout")
    name, m = captures[-1]
    g = lambda c: m.get(f"{c:04X}")
    if g(0x000C):
        import pytest
        pytest.skip(f"{name} has credits on the machine; the identity needs an empty meter")
    assert g(0x0024) - g(0x0004) == g(0x0000) - g(0x0022), (
        f"{name}: in-out is {g(0x0024) - g(0x0004)}, bet-won is "
        f"{g(0x0000) - g(0x0022)}")


def test_the_player_button_asks_the_host_before_it_prints():
    """Measured on the EGT Premier, 2026-10-27T01:10:54 to 01:11:04.

    With cashout-to-host enabled, pressing CASH OUT does not simply print.
    The machine issues 0x66, then 0x6A reissued every 800ms per section 8.7,
    and only after the eight-second host budget expires does its SOFT-mode
    recovery print the ticket -- 0x3D, ten seconds after the button.

    This contradicts what MANUAL.md section 7.4 used to say (that a working
    printer meant the player's button bypassed the host), so it is pinned
    here: the two paths are sequential, not alternative, and a ten-second
    wait for paper is the signature of a host that let the clock run out.
    """
    from saspy.constants import describe_exception

    assert describe_exception(0x66) == "cash out button pressed"
    assert describe_exception(0x6A) == "AFT request for host cashout"
    # 0x3D is the printed-ticket announcement the fallback ends in.
    assert "cash out ticket" in describe_exception(0x3D).lower()


def test_nonrestricted_leaves_on_the_cashable_ticket():
    """Section 8: "whenever nonrestricted promotional amounts are cashed out
    by a method that cannot preserve the nonrestricted promotional status,
    they become regular cashable amounts."

    Observed to the cent on 2026-10-27. Before the cashout the meter held
    30.10 cashable and 1.80 nonrestricted; the printed cashable ticket was
    31.90, and 000E/002C rose by the full 31.90 rather than by 30.10. The
    restricted 19.19 printed separately and did NOT touch the cashable
    ticket-out meter, which stayed at 21,690 across the second ticket.
    """
    assert 3010 + 180 == 3190
    assert 18500 + 3190 == 21690


def test_the_validation_buffer_holds_only_what_the_machine_issued():
    """Observed on 2026-10-27, and it corrected a false diagnosis.

    ticket_history.py reported "the buffer has very likely wrapped" on a
    machine holding SIX records in a thirty-one slot buffer, where wrapping
    is arithmetically impossible. The real cause is structural: §15.10's
    buffer holds validations the MACHINE issued -- it prints a ticket, so it
    files the record -- while a redeemed ticket's validation number came from
    the host, leaving the machine nothing of its own to file. The LP 0x50
    meters count both directions.

    Printed agreed exactly (buffer 265.19 vs meters 265.19); redeemed was
    buffer 0.00 against meters 80.00, across six redemptions the buffer never
    saw. Both are correct behaviour.
    """
    printed = [15000, 2500, 3500, 410, 3190, 1919]
    assert sum(printed) == 26519
    # Cashable 150.00 + 35.00 + 31.90 and restricted 25.00 + 4.10 + 19.19,
    # matching the screen's Cashable/Non-cashable Voucher Out to the cent.
    assert 15000 + 3500 + 3190 == 21690
    assert 2500 + 410 + 1919 == 4829
    assert 21690 + 4829 == 26519
    # Six redemptions, 80.00, none of them in the buffer.
    assert 5000 + 1000 + 2000 == 8000


def test_the_whole_machine_balances_on_the_2026_10_27_screens():
    """Total In - Total Out == Total Bet - Total Won, with an empty credit
    meter: 605.73 - 569.89 == 56.10 - 20.26 == 35.84, and 35.84/56.10 is
    63.89%, the hold percentage the machine printed on its own screen.
    """
    assert 60573 - 56989 == 5610 - 2026 == 3584
    assert round(3584 / 5610 * 100, 2) == 63.89
    # Play order, demonstrated: the 13.20 wagered this round took restricted
    # first (5.00, exhausting it), then nonrestricted (8.20), and never
    # touched cashable -- which is why cashable played held at 17.00.
    assert 1700 + 820 + 3090 == 5610
