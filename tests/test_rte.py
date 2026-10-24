"""Real time event reporting: long poll 0x0E and the Table 12.5 message.

RTE is not "more exceptions". Section 12.5 replaces the single-byte
exception entirely with address, FF, code, data..., CRC, and Table 12.5
carries no length field -- so a host that does not know each code's data
length cannot frame the response at all. That is what these pin.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.bcd import encode_bcd
from saspy.client import SASClient
from saspy.constants import rte_data_length
from saspy.crc import crc16_bytes
from saspy.exceptions import SASCommandNackedError, SASError
from saspy.transport import SASTransport
from tests.test_client import ADDRESS, FakeSerial


def _client(response: bytes, timeout: float = 1.0) -> SASClient:
    return SASClient(SASTransport(FakeSerial(response), wake_byte=None), ADDRESS, timeout=timeout)


def _event(code: int, data: bytes = b"") -> bytes:
    body = bytes([ADDRESS, 0xFF, code]) + data
    return body + crc16_bytes(body)


def test_enable_real_time_events_sends_the_table_12_1_frame():
    fake = FakeSerial(bytes([ADDRESS]))  # Table 7.4b ACK: bare address, no CRC
    client = SASClient(SASTransport(fake, wake_byte=None), ADDRESS, timeout=1.0)

    client.enable_real_time_events(True)

    sent = bytes(fake.written)
    assert sent[:3] == bytes([ADDRESS, 0x0E, 0x01])
    assert sent[3:] == crc16_bytes(sent[:3]), "0x0E is type S -- it carries a CRC"


def test_disable_real_time_events_sends_zero():
    fake = FakeSerial(bytes([ADDRESS]))
    SASClient(SASTransport(fake, wake_byte=None), ADDRESS, timeout=1.0).enable_real_time_events(False)

    sent = bytes(fake.written)
    assert sent[:3] == bytes([ADDRESS, 0x0E, 0x00])


def test_a_nacked_enable_raises():
    """Table 7.4b: the machine NACKs by returning its address ORed with 0x80."""
    fake = FakeSerial(bytes([ADDRESS | 0x80]))
    client = SASClient(SASTransport(fake, wake_byte=None), ADDRESS, timeout=1.0)

    with pytest.raises(SASCommandNackedError):
        client.enable_real_time_events(True)


def test_no_activity_under_rte_is_silence_not_a_zero_byte():
    """Section 12.6 removes codes 00 and 1F from the response set: "no
    activity is implied when the gaming machine does not send a real time
    event". A timeout is therefore the ordinary idle case here, the opposite
    of what it means for general_poll().
    """
    assert _client(b"", timeout=0.01).general_poll_rte() is None


def test_game_start_event_carries_the_coin_in_meter_it_affected():
    """The field that decides whether RTE is worth enabling. Section 12.5.3
    specifies total coin in *after* the wager, so a session's wager can be
    reconciled from its boundary events rather than from separate meter
    reads -- which matters when exceptions can be lost but meters cannot.
    """
    data = encode_bcd(25, 2) + encode_bcd(21387723, 4) + bytes([0b11000011, 0x00])
    event = _client(_event(0x7E, data)).general_poll_rte()

    assert event is not None
    assert event.is_game_start
    assert event.description == "GAME HAS STARTED"
    assert event.fields["credits_wagered"] == 25
    assert event.fields["total_coin_in"] == 21387723
    assert event.fields["max_bet_wagered"] is True
    assert event.fields["multi_denom"] is True
    assert event.fields["denomination_code"] == 0b000011


def test_game_end_event_carries_the_win():
    event = _client(_event(0x7F, encode_bcd(1500, 4))).general_poll_rte()

    assert event is not None and event.is_game_end
    assert event.fields == {"game_win": 1500}


def test_an_exception_with_no_12_5_layout_still_arrives():
    """Most codes have no extra data. They are still delivered under RTE --
    as a bare code -- and must not be mistaken for a framing error.
    """
    event = _client(_event(0x11)).general_poll_rte()

    assert event is not None
    assert event.code == 0x11
    assert event.description == "slot door was opened"
    assert event.data == b"" and event.fields == {}


def test_a_rom_signature_response_is_not_read_as_an_event():
    """Section 12.7: a ROM signature response may also answer a poll and
    deliberately omits the FF identifier, "which distinguishes a ROM
    signature response from a coin in tilt exception response". Parsing it
    as an event would invent a bogus exception code.
    """
    with pytest.raises(SASError, match="12.7"):
        _client(bytes([ADDRESS, 0x21, 0x00])).general_poll_rte()


def test_data_lengths_come_from_the_tables_not_from_guessing():
    """Table 12.5 has no length field, so these ARE the framing. A wrong
    entry desynchronises the stream rather than producing one bad field.
    """
    assert rte_data_length(0x7E) == 8   # 12.5.3
    assert rte_data_length(0x7F) == 4   # 12.5.4
    assert rte_data_length(0x4F) == 6   # 12.5.1
    assert rte_data_length(0x7C) == 10  # 12.5.2
    assert rte_data_length(0x88) == 2   # 12.5.5
    assert rte_data_length(0x8A) == 4   # 12.5.6
    assert rte_data_length(0x8B) == 1   # 12.5.7
    assert rte_data_length(0x8C) == 2   # 12.5.8
    assert rte_data_length(0x11) == 0   # no 12.5.x table


def test_rte_event_with_code_zero_is_still_a_valid_frame():
    """Observed on the EGT Premier: section 12.6 says a machine in RTE mode
    does not report code 00 and implies idle by sending nothing, but this one
    sends a well-formed five-byte event with code 00 and a valid CRC on
    almost every poll. The parser must accept it -- rejecting it would make a
    chatty-but-idle machine look like a framing fault -- and callers must not
    read "an event arrived" as "something happened".
    """
    event = _client(_event(0x00)).general_poll_rte()

    assert event is not None
    assert event.code == 0x00
    assert event.description == "no activity"
    assert event.fields == {}


def test_a_long_poll_preempted_by_an_event_is_reissued_and_the_event_kept():
    """Sections 12.3 and 12.4. An event arriving in a long poll's place is
    not an error and not the long poll's answer -- the host must reissue the
    poll, and must not lose the event, which is a real exception that simply
    landed in the wrong slot. Dropping it would reintroduce exactly the
    exception loss RTE is being used to avoid.
    """
    from saspy.bcd import encode_bcd

    event = _event(0x7F, encode_bcd(1500, 4))
    # LP 0x1A current credits: address, command, 4 BCD, CRC
    body = bytes([ADDRESS, 0x1A]) + encode_bcd(2110, 4)
    real = body + crc16_bytes(body)

    fake = FakeSerial(event + real)
    client = SASClient(SASTransport(fake, wake_byte=None), ADDRESS, timeout=1.0)
    client._rte_active = True

    from saspy.constants import LongPoll

    credits = client.send_meter(LongPoll.SEND_CURRENT_CREDITS)

    assert credits == 2110, "the reissued poll's answer, not the event"
    captured = client.drain_rte_events()
    assert len(captured) == 1
    assert captured[0].code == 0x7F
    assert captured[0].fields == {"game_win": 1500}
    assert client.drain_rte_events() == [], "draining is destructive"


def test_rte_is_only_marked_active_after_the_machine_acks():
    """Flipping the flag before the ACK would mis-frame the very next poll,
    since until the machine accepts 0x0E its response format is unchanged.
    """
    fake = FakeSerial(bytes([ADDRESS | 0x80]))  # NACK
    client = SASClient(SASTransport(fake, wake_byte=None), ADDRESS, timeout=1.0)

    with pytest.raises(SASCommandNackedError):
        client.enable_real_time_events(True)

    assert client._rte_active is False, "a NACKed enable must not change framing"


def test_repeated_preemption_eventually_gives_up_rather_than_looping():
    """A machine with more queued events than the poll has retries must not
    hang the caller forever."""
    from saspy.bcd import encode_bcd

    many = _event(0x7F, encode_bcd(0, 4)) * 10
    client = SASClient(SASTransport(FakeSerial(many), wake_byte=None), ADDRESS, timeout=1.0)
    client._rte_active = True

    from saspy.constants import LongPoll

    with pytest.raises(SASError, match="12.3"):
        client.send_meter(LongPoll.SEND_CURRENT_CREDITS)

    # The events seen along the way are still kept, not thrown away.
    assert len(client.drain_rte_events()) >= 1
