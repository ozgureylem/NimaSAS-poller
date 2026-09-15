"""Client-level tests. Response fixtures are built field-by-field from the
SAS 6.02 tables each method implements (see the table reference in each
test), using the package's own (independently tested) encode_bcd /
encode_binary_le / crc16_bytes helpers — not hand-typed hex — so a fixture
mistake would have to independently agree with a codec bug to hide a real
parsing defect.

Several tests are written specifically to demonstrate a legacy bug is
fixed: they'd fail (or crash) if the old offset/precedence/field-count
mistake were reintroduced.
"""

from __future__ import annotations

import pytest

from saspy.bcd import encode_bcd
from saspy.binary import encode_binary_le
from saspy.client import SASClient
from saspy.constants import LongPoll, MeterCode
from saspy.crc import crc16_bytes
from saspy.exceptions import SASAddressMismatchError, SASCommandNackedError, SASEncodingError, SASError
from saspy.transport import SASTransport

ADDRESS = 0x01


class FakeSerial:
    def __init__(self, response: bytes):
        self.parity = None
        self.written = bytearray()
        self._response = bytearray(response)
        self._deadline = None

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def read(self, size: int = 1) -> bytes:
        chunk = bytes(self._response[:size])
        del self._response[:size]
        return chunk

    def reset_input_buffer(self) -> None:
        pass


def make_client(response_body: bytes) -> SASClient:
    """``response_body`` is address..data (no CRC) — the CRC is appended here."""
    full_response = response_body + crc16_bytes(response_body)
    transport = SASTransport(FakeSerial(full_response))
    return SASClient(transport, ADDRESS, timeout=1.0)


def test_general_poll_returns_exception_code():
    transport = SASTransport(FakeSerial(bytes([0x00])))
    client = SASClient(transport, ADDRESS)
    assert client.general_poll() == 0x00


# -- Enable/disable (0x01/0x02, Table 7.4a/7.4b) -----------------------------


def test_send_shutdown_acked_returns_none():
    # make_client() appends a CRC that exchange_fixed(response_length=1)
    # never reads -- Table 7.4b's response is a bare address byte, no CRC.
    assert make_client(bytes([ADDRESS])).send_shutdown() is None


def test_send_startup_acked_returns_none():
    assert make_client(bytes([ADDRESS])).send_startup() is None


def test_send_shutdown_nacked_raises():
    with pytest.raises(SASCommandNackedError) as exc_info:
        make_client(bytes([ADDRESS | 0x80])).send_shutdown()
    assert exc_info.value.address == ADDRESS


def test_send_startup_nacked_raises():
    with pytest.raises(SASCommandNackedError) as exc_info:
        make_client(bytes([ADDRESS | 0x80])).send_startup()
    assert exc_info.value.address == ADDRESS


def test_send_shutdown_wrong_address_raises_address_mismatch_not_nack():
    """A different address entirely responding (bus contention, wiring
    fault) is a distinct failure from a NACK from *this* machine -- it
    must not be misread as either an ACK or a NACK for our own address.
    """
    other_address = 0x02
    with pytest.raises(SASAddressMismatchError):
        make_client(bytes([other_address])).send_shutdown()


def test_send_meters_10_through_15():
    body = bytes([ADDRESS, 0x0F])
    body += encode_bcd(100, 4) + encode_bcd(200, 4) + encode_bcd(300, 4)
    body += encode_bcd(400, 4) + encode_bcd(500, 4) + encode_bcd(600, 4)
    meters = make_client(body).send_meters_10_through_15()
    assert meters.total_cancelled_credits == 100
    assert meters.total_coin_in == 200
    assert meters.total_coin_out == 300
    assert meters.total_drop == 400
    assert meters.total_jackpot == 500
    assert meters.games_played == 600


def test_send_extended_meters_group():
    body = bytes([ADDRESS, 0x1C])
    values = [11, 12, 13, 14, 15, 16, 17, 18]
    for v in values:
        body += encode_bcd(v, 4)
    meters = make_client(body).send_extended_meters_group()
    assert (
        meters.total_coin_in,
        meters.total_coin_out,
        meters.total_drop,
        meters.total_jackpot,
        meters.games_played,
        meters.games_won,
        meters.slot_door_opened,
        meters.power_reset,
    ) == tuple(values)


def test_send_gaming_machine_id_zero_denomination_does_not_crash():
    """Legacy: bytearray(data[6]) where data[6] is the int 0 allocates a
    bytearray of length 0 rather than wrapping the byte — this specific
    all-zero-denomination case is exactly where that would misbehave.
    """
    body = bytes([ADDRESS, 0x1F])
    body += b"01"  # game id (ASCII)
    body += b"000"  # additional id (ASCII)
    body += bytes([0x00])  # denomination = 0
    body += bytes([0x05])  # max bet
    body += bytes([0x02])  # progressive group
    body += encode_binary_le(0x00FF, 2)  # game options, binary LSB-first
    body += b"PAYTBL"  # paytable id (6 ASCII)
    body += b"9200"  # base percentage (4 ASCII)
    info = make_client(body).send_gaming_machine_id()
    assert info.denomination == 0x00
    assert info.max_bet == 0x05
    assert info.progressive_group == 0x02
    assert info.game_options == 0x00FF
    assert info.game_id == "01"
    assert info.paytable_id == "PAYTBL"
    assert info.base_percentage == "9200"


def test_send_gaming_machine_id_game_options_byte_order():
    """0x1234 must come back as 0x1234, not 0x3412 — proves the LSB-first
    binary-field decoding is actually being applied, not skipped.
    """
    body = bytes([ADDRESS, 0x1F])
    body += b"01" + b"000"
    body += bytes([0x01, 0x05, 0x00])
    body += encode_binary_le(0x1234, 2)
    body += b"PAYTBL" + b"9200"
    info = make_client(body).send_gaming_machine_id()
    assert info.game_options == 0x1234


def test_send_sas_version_and_serial():
    version = b"602"
    serial = b"TROP-0042"
    body = bytes([ADDRESS, 0x54, 3 + len(serial)]) + version + serial
    info = make_client(body).send_sas_version_and_serial()
    assert info.sas_version == "602"
    assert info.serial_number == "TROP-0042"


def test_send_sas_version_and_serial_empty_serial():
    version = b"601"
    body = bytes([ADDRESS, 0x54, 3]) + version
    info = make_client(body).send_sas_version_and_serial()
    assert info.sas_version == "601"
    assert info.serial_number == ""


def test_send_enabled_features_validation_style_precedence_bug_fixed():
    """features1 = 0b01000001: bit0 (jackpot_multiplier) set, bits 5-6 = 0b10
    (secure enhanced = 2). The legacy expression `data[3]&0b01100000>>5`
    evaluates (due to operator precedence) as `data[3] & 0b11`, which for
    this byte gives 1 — the *wrong* validation style. The fixed
    `(data[3] & 0b01100000) >> 5` gives the correct value, 2.
    """
    features1 = 0b01000001
    body = bytes([ADDRESS, 0xA0])
    body += encode_bcd(7, 2)  # game number
    body += bytes([features1, 0x00, 0x00])
    body += bytes([0x00, 0x00, 0x00])  # reserved
    features = make_client(body).send_enabled_features()
    assert features.jackpot_multiplier is True
    assert features.validation_style == 2  # would be 1 under the legacy bug
    assert features.game_number == 7


def test_aft_register_gaming_machine_full_response():
    body = bytes([ADDRESS, 0x73, 0x1D])
    body += bytes([0x01])  # registration status
    body += encode_binary_le(0xAABBCCDD, 4)  # asset number
    body += bytes(range(20))  # registration key (opaque)
    body += encode_binary_le(0x11223344, 4)  # POS ID
    status = make_client(body).aft_register_gaming_machine(registration_code=0xFF)
    assert status.registration_status == 0x01
    assert status.asset_number == 0xAABBCCDD
    assert status.pos_id == 0x11223344
    assert status.registration_key == bytes(range(20))


def test_aft_game_lock_and_status_includes_transfer_limit_field():
    """Legacy dropped the 5-byte 'gaming machine transfer limit' field
    entirely, which shifted restricted_expiration/pool_id to read the wrong
    bytes and silently discarded the real trailing fields. This fixture
    gives transfer_limit, restricted_expiration and restricted_pool_id all
    different, distinguishable values to prove none of them bleed into
    each other.
    """
    body = bytes([ADDRESS, 0x74, 0x23])
    body += encode_binary_le(0x01020304, 4)  # asset number
    body += bytes([0x00, 0b00011111, 0b00000111, 0xFF, 0x7F])  # lock/avail/cashout/aft/max-buf
    body += encode_bcd(1000, 5)  # current cashable
    body += encode_bcd(2000, 5)  # current restricted
    body += encode_bcd(3000, 5)  # current non-restricted
    body += encode_bcd(4000, 5)  # gaming machine transfer limit
    body += encode_bcd(12312099, 4)  # restricted expiration (MMDDYYYY-shaped)
    body += encode_binary_le(0x0203, 2)  # restricted pool id
    status = make_client(body).aft_game_lock_and_status_request()
    assert status.current_cashable_amount == 1000
    assert status.current_restricted_amount == 2000
    assert status.current_non_restricted_amount == 3000
    assert status.gaming_machine_transfer_limit == 4000
    assert status.restricted_expiration == 12312099
    assert status.restricted_pool_id == 0x0203


def test_aft_transfer_funds_round_trip_has_no_phantom_field():
    """The command this builds must NOT contain a lock_timeout field (that
    belongs to long poll 0x74, not 0x72) and its length byte must match the
    real body length. We inspect the bytes actually written to the wire.
    """
    data = bytes([0x00, 0x00, 0x00, 0x00])  # buffer pos, transfer status, receipt status, transfer type
    data += encode_bcd(500, 5) + encode_bcd(0, 5) + encode_bcd(0, 5)  # amounts
    data += bytes([0x00])  # transfer flags
    data += encode_binary_le(0x0A0B0C0D, 4)  # asset number
    tid = b"TXN1"
    data += bytes([len(tid)]) + tid
    data += encode_bcd(1122099, 4)  # transaction date
    data += encode_bcd(153000, 3)  # transaction time
    data += encode_bcd(0, 4)  # expiration
    data += encode_binary_le(0, 2)  # pool id
    data += bytes([0])  # cumulative cashable meter size = 0
    data += bytes([0])  # cumulative restricted meter size = 0
    data += bytes([0])  # cumulative nonrestricted meter size = 0
    body_with_length = bytes([ADDRESS, 0x72, len(data)]) + data

    fake = FakeSerial(body_with_length + crc16_bytes(body_with_length))
    transport = SASTransport(fake)
    client = SASClient(transport, ADDRESS)

    result = client.aft_transfer_funds(
        transfer_code=0x00,
        transaction_index=0x00,
        transfer_type=0x00,
        cashable_amount=500,
        asset_number=0x0A0B0C0D,
        transaction_id="1",
    )

    # Inspect what was actually written: address, command, length, then the body.
    written = bytes(fake.written)
    length_byte = written[2]
    declared_body = written[3:3 + length_byte]
    # Fixed portion is 1+1+1+5+5+5+1+4+20 = 43 bytes before the transaction-ID-length byte.
    fixed_prefix_len = 43
    after_asset_and_key = declared_body[fixed_prefix_len:]
    tid_len = after_asset_and_key[0]
    assert tid_len == 1  # "1" is one ASCII byte
    remainder_after_tid = after_asset_and_key[1 + tid_len:]
    # expiration(4) + pool_id(2) + receipt_data_length(1) is exactly what should follow —
    # no extra 2-byte lock_timeout field.
    assert len(remainder_after_tid) == 4 + 2 + 1
    assert length_byte == len(declared_body)
    assert result.transaction_id == "TXN1"
    assert result.asset_number == 0x0A0B0C0D


def test_send_ticket_validation_data_no_ticket_in_escrow():
    body = bytes([ADDRESS, 0x70, 0x01, 0xFF])
    data = make_client(body).send_ticket_validation_data()
    assert data.ticket_in_escrow is False
    assert data.amount_cents == 0


def test_send_ticket_validation_data_ticket_present():
    validation_data = bytes([0x00] + list(encode_bcd(1234567890123456, 8)))
    body = bytes([ADDRESS, 0x70])
    length = 1 + 5 + 1 + len(validation_data)
    body += bytes([length, 0x00])  # status = ticket in escrow
    body += encode_bcd(4750, 5)
    body += bytes([0x00])  # parsing code
    body += validation_data
    data = make_client(body).send_ticket_validation_data()
    assert data.ticket_in_escrow is True
    assert data.amount_cents == 4750
    assert data.validation_data == validation_data


def test_redeem_ticket_encodes_nine_byte_validation_number():
    """Legacy encoded only 8 bytes (16 digits) for the validation number,
    dropping the mandatory 2-digit system-ID prefix that table 15.11b
    requires for an 18-digit BCD validation number. Assert the actual
    command bytes on the wire carry all 9 bytes.
    """
    ack_body = bytes([ADDRESS, 0x71, 0x01, 0x00])
    fake = FakeSerial(ack_body + crc16_bytes(ack_body))
    transport = SASTransport(fake)
    client = SASClient(transport, ADDRESS)

    validation_number = int("00" + "1234567890123456")  # system id 00 + 16-digit number
    client.redeem_ticket(
        transfer_code=0x00,
        transfer_amount_cents=4750,
        validation_number=validation_number,
    )

    written = bytes(fake.written)
    length_byte = written[2]
    # transfer_code(1) + amount(5) + parsing_code(1) + validation_data(9) + expiration(4) + pool_id(2) = 22
    assert length_byte == 22
    validation_data_field = written[3 + 1 + 5 + 1: 3 + 1 + 5 + 1 + 9]
    assert validation_data_field == encode_bcd(validation_number, 9)


def test_send_validation_number_status():
    body = bytes([ADDRESS, 0x58, 0x00])
    status = make_client(body).send_validation_number(validation_system_id=1, validation_number=1234567890123456)
    assert status == 0x00


def test_send_pending_cashout_info():
    body = bytes([ADDRESS, 0x57, 0x00]) + encode_bcd(4750, 5)
    info = make_client(body).send_pending_cashout_info()
    assert info.cashout_type == 0x00
    assert info.amount_cents == 4750


def test_send_pending_cashout_info_not_waiting():
    body = bytes([ADDRESS, 0x57, 0x80]) + encode_bcd(0, 5)
    info = make_client(body).send_pending_cashout_info()
    assert info.cashout_type == 0x80


# -- Selected / extended meters (0x2F, 0x6F) --------------------------------


def test_send_selected_meters_reads_ticket_meters():
    """This is the exact gap the review found: LP 0x0F/0x1C cannot report
    ticket meters at all (Table 7.2a/7.2c only cover the six core meters).
    Cashable Tickets In (the meter D-01's testable prediction depends on)
    is only reachable via LP 2F/6F.
    """
    data = encode_bcd(0, 2)  # game number
    data += bytes([MeterCode.CASHABLE_TICKET_IN_CENTS]) + encode_bcd(4750, 5)
    data += bytes([MeterCode.CASHABLE_TICKET_IN_QUANTITY]) + encode_bcd(12, 4)
    body = bytes([ADDRESS, 0x2F, len(data)]) + data
    result = make_client(body).send_selected_meters(
        [MeterCode.CASHABLE_TICKET_IN_CENTS, MeterCode.CASHABLE_TICKET_IN_QUANTITY]
    )
    assert result.game_number == 0
    assert result.meters[MeterCode.CASHABLE_TICKET_IN_CENTS] == 4750
    assert result.meters[MeterCode.CASHABLE_TICKET_IN_QUANTITY] == 12


def test_send_selected_meters_request_uses_bcd_game_number_and_binary_codes():
    data = encode_bcd(0, 2) + bytes([MeterCode.CASHABLE_TICKET_IN_CENTS]) + encode_bcd(0, 5)
    body = bytes([ADDRESS, 0x2F, len(data)]) + data
    fake = FakeSerial(body + crc16_bytes(body))
    transport = SASTransport(fake)
    client = SASClient(transport, ADDRESS)
    client.send_selected_meters([MeterCode.CASHABLE_TICKET_IN_CENTS], game_number=12)

    written = bytes(fake.written)
    length_byte = written[2]
    request_data = written[3:3 + length_byte]
    assert request_data[0:2] == encode_bcd(12, 2)
    assert request_data[2] == MeterCode.CASHABLE_TICKET_IN_CENTS


def test_send_selected_meters_rejects_unknown_meter_code():
    transport = SASTransport(FakeSerial(b""))
    client = SASClient(transport, ADDRESS)
    with pytest.raises(SASEncodingError):
        client.send_selected_meters([0x7E])  # not in METER_CODE_SIZES_BCD


def test_send_selected_meters_rejects_too_many_codes():
    transport = SASTransport(FakeSerial(b""))
    client = SASClient(transport, ADDRESS)
    with pytest.raises(SASError):
        client.send_selected_meters([MeterCode.CASHABLE_TICKET_IN_CENTS] * 11)


def test_send_selected_meters_ticket_in_meter_rollover():
    """Cashable Tickets In (Table C-7, 5 BCD bytes -> max 9,999,999,999
    cents) rolling over mid-session. Concrete numbers: the meter sits at
    $99,999,950.00, a $105.00 ticket is redeemed, and the field wraps
    (mod 10**10) to $55.00 -- an ordinary BCD field overflow per Technical
    v3 §8.2, which the spec is explicit is corrected server-side, never by
    the device reading the meter. decode_bcd() has no notion of "value
    went down" -- it only fails on an invalid nibble -- so both the
    pre-rollover and post-rollover reads must decode cleanly, and the
    client must return the raw wrapped value with no "helpful" correction
    of its own.
    """
    pre_rollover_cents = 9_999_995_000
    ticket_cents = 10_500
    post_rollover_cents = (pre_rollover_cents + ticket_cents) % 10 ** 10
    assert post_rollover_cents == 5_500  # not 0 -- rules out a hidden "looks like a reset" special case

    def read_meter(cents: int) -> int:
        data = encode_bcd(0, 2) + bytes([MeterCode.CASHABLE_TICKET_IN_CENTS]) + encode_bcd(cents, 5)
        body = bytes([ADDRESS, 0x2F, len(data)]) + data
        result = make_client(body).send_selected_meters([MeterCode.CASHABLE_TICKET_IN_CENTS])
        return result.meters[MeterCode.CASHABLE_TICKET_IN_CENTS]

    before = read_meter(pre_rollover_cents)
    after = read_meter(post_rollover_cents)
    assert before == pre_rollover_cents
    assert after == post_rollover_cents
    assert after < before  # the raw wrapped wire value, not a corrected/monotonic one


def test_send_extended_meters_handles_arbitrary_code_via_self_describing_size():
    """Unlike LP 2F, LP 6F's response carries its own size byte per meter
    (Table 7.21b), so any code works here even without a METER_CODE_SIZES_BCD
    entry.
    """
    data = encode_bcd(0, 2)  # game number
    data += encode_binary_le(0x1234, 2) + bytes([3]) + encode_bcd(567, 3)
    body = bytes([ADDRESS, 0x6F, len(data)]) + data
    result = make_client(body).send_extended_meters([0x1234])
    assert result.meters[0x1234] == 567


def test_send_extended_meters_skips_unsupported_meter():
    data = encode_bcd(0, 2)
    data += encode_binary_le(0x9999, 2) + bytes([0])  # size 0 = unsupported, no value bytes
    body = bytes([ADDRESS, 0x6F, len(data)]) + data
    result = make_client(body).send_extended_meters([0x9999])
    assert result.meters == {}


def test_send_extended_meters_reads_table_c7_codes_beyond_the_ticket_range():
    """MeterCode now covers essentially all of Table C-7 (0x00-0x0C,
    0x15-0x93, 0xA0-0xBD), not just the 8 ticket meters -- spot-check one
    code from each of the three ranges added: a core meter (0x00, size 4),
    an AFT-specific meter (0xA0, size 5), and a gauge (0x0C current
    credits, size 4), all reachable via LP 6F exactly like the ticket
    meters already were.
    """
    data = encode_bcd(0, 2)
    data += encode_binary_le(MeterCode.TOTAL_COIN_IN_CREDITS, 2) + bytes([4]) + encode_bcd(112233, 4)
    data += encode_binary_le(MeterCode.CURRENT_CREDITS, 2) + bytes([4]) + encode_bcd(4500, 4)
    data += encode_binary_le(MeterCode.IN_HOUSE_CASHABLE_TRANSFERS_TO_GAMING_MACHINE_CENTS, 2) + bytes([5]) + encode_bcd(9900, 5)
    body = bytes([ADDRESS, 0x6F, len(data)]) + data
    result = make_client(body).send_extended_meters(
        [MeterCode.TOTAL_COIN_IN_CREDITS, MeterCode.CURRENT_CREDITS, MeterCode.IN_HOUSE_CASHABLE_TRANSFERS_TO_GAMING_MACHINE_CENTS]
    )
    assert result.meters[MeterCode.TOTAL_COIN_IN_CREDITS] == 112233
    assert result.meters[MeterCode.CURRENT_CREDITS] == 4500
    assert result.meters[MeterCode.IN_HOUSE_CASHABLE_TRANSFERS_TO_GAMING_MACHINE_CENTS] == 9900


# -- Secure enhanced validation ID (0x4C) -----------------------------------


def test_set_secure_enhanced_validation_id_round_trip():
    body = bytes([ADDRESS, 0x4C]) + encode_binary_le(0xABCDEF, 3) + encode_binary_le(0x001234, 3)
    result = make_client(body).set_secure_enhanced_validation_id(machine_id=0xABCDEF, sequence_number=0x001234)
    assert result.machine_id == 0xABCDEF
    assert result.sequence_number == 0x001234


def test_set_secure_enhanced_validation_id_query_form_uses_zero():
    body = bytes([ADDRESS, 0x4C]) + encode_binary_le(0, 3) + encode_binary_le(0, 3)
    fake = FakeSerial(body + crc16_bytes(body))
    transport = SASTransport(fake)
    client = SASClient(transport, ADDRESS)
    client.set_secure_enhanced_validation_id()  # defaults: query, don't set
    written = bytes(fake.written)
    assert written[2:5] == encode_binary_le(0, 3)  # machine_id field


# -- Enhanced validation information / ticket-out history (0x4D) -----------


def test_send_enhanced_validation_information_parses_ticket_out_record():
    body = bytes([ADDRESS, 0x4D])
    body += bytes([0x00, 0x03])  # validation type, index number
    body += encode_bcd(9142026, 4)  # date MMDDYYYY (09142026)... zero-padded via BCD width
    body += encode_bcd(153045, 3)  # time HHMMSS
    body += encode_bcd(1234567890123456, 8)  # validation number
    body += encode_bcd(4750, 5)  # amount cents
    body += encode_binary_le(0x0007, 2)  # ticket number
    body += encode_bcd(0, 1)  # validation system id
    body += encode_bcd(9999, 4)  # expiration
    body += encode_binary_le(0, 2)  # pool id
    info = make_client(body).send_enhanced_validation_information(function_code=0x03)
    assert info.validation_type == 0x00
    assert info.index_number == 3
    assert info.validation_number == 1234567890123456
    assert info.amount_cents == 4750
    assert info.ticket_number == 7
    assert info.pool_id == 0


def test_send_enhanced_validation_information_default_function_code_is_peek():
    """Default must be the non-destructive peek (0xFF), not the
    mark-as-read form (0x00) — boot reconciliation and casual reads must
    never accidentally consume buffer state.
    """
    body = bytes([ADDRESS, 0x4D]) + bytes(31)  # all-zero record (no data)
    fake = FakeSerial(body + crc16_bytes(body))
    transport = SASTransport(fake)
    client = SASClient(transport, ADDRESS)
    client.send_enhanced_validation_information()
    written = bytes(fake.written)
    assert written[2] == 0xFF


# -- Extended validation status (0x7B) --------------------------------------


def test_extended_validation_status_inquiry_round_trip():
    data = encode_binary_le(0x0A0B0C0D, 4)  # asset number
    data += encode_binary_le(0x00FF, 2)  # status bits
    data += encode_bcd(30, 2)  # cashable expiration days
    data += encode_bcd(60, 2)  # restricted expiration days
    body = bytes([ADDRESS, 0x7B, len(data)]) + data
    status = make_client(body).extended_validation_status()
    assert status.asset_number == 0x0A0B0C0D
    assert status.status_bits == 0x00FF
    assert status.cashable_ticket_expiration_days == 30
    assert status.restricted_ticket_expiration_days == 60


def test_extended_validation_status_default_call_does_not_change_anything():
    data = bytes(12)
    body = bytes([ADDRESS, 0x7B, len(data)]) + data
    fake = FakeSerial(body + crc16_bytes(body))
    transport = SASTransport(fake)
    client = SASClient(transport, ADDRESS)
    client.extended_validation_status()
    written = bytes(fake.written)
    length_byte = written[2]
    request_data = written[3:3 + length_byte]
    # control_mask all zero -> "no bit may be changed" (§15.2)
    assert request_data[0:2] == encode_binary_le(0, 2)


# -- Redeem ticket status query (0x71/FF short form) ------------------------


def test_redeem_ticket_status_sends_the_short_form_request():
    """Appendix D Step 7: `2A 71 01 FF cc cc` — a 1-byte body, not the full
    transfer request. This is what reading completion status safely
    depends on; sending the full redeem_ticket() shape instead risks the
    machine treating it as a new redemption attempt.
    """
    ack_body = bytes([ADDRESS, 0x71, 0x01, 0x00])
    fake = FakeSerial(ack_body + crc16_bytes(ack_body))
    transport = SASTransport(fake)
    client = SASClient(transport, ADDRESS)
    client.redeem_ticket_status()
    written = bytes(fake.written)
    request = bytes([ADDRESS, 0x71, 0x01, 0xFF])
    assert written == request + crc16_bytes(request)


def test_redeem_ticket_status_no_previous_cycle():
    body = bytes([ADDRESS, 0x71, 0x01, 0xFF])
    result = make_client(body).redeem_ticket_status()
    assert result.machine_status == 0xFF
    assert result.amount_cents == 0


def test_redeem_ticket_status_reports_current_cycle():
    validation_data = bytes([0x00] + list(encode_bcd(1234567890123456, 8)))
    data = bytes([0x00]) + encode_bcd(4750, 5) + bytes([0x00]) + validation_data
    body = bytes([ADDRESS, 0x71, len(data)]) + data
    result = make_client(body).redeem_ticket_status()
    assert result.machine_status == 0x00
    assert result.amount_cents == 4750


# -- Simple single-meter reads (Table 7.1) -----------------------------------


def test_send_meter_reads_a_four_byte_bcd_meter():
    body = bytes([ADDRESS, 0x11]) + encode_bcd(987654, 4)
    value = make_client(body).send_meter(LongPoll.SEND_TOTAL_COIN_IN_METER)
    assert value == 987654


def test_send_meter_reads_a_two_byte_bcd_meter():
    body = bytes([ADDRESS, 0x51]) + encode_bcd(12, 2)
    value = make_client(body).send_meter(LongPoll.SEND_TOTAL_GAMES_IMPLEMENTED)
    assert value == 12


def test_send_meter_rejects_a_non_meter_poll():
    transport = SASTransport(FakeSerial(b""))
    client = SASClient(transport, ADDRESS)
    with pytest.raises(SASError):
        client.send_meter(LongPoll.REDEEM_TICKET)


def test_send_games_since_power_up_and_door_closure():
    body = bytes([ADDRESS, 0x18]) + encode_bcd(42, 2) + encode_bcd(7, 2)
    result = make_client(body).send_games_since_power_up_and_door_closure()
    assert result.games_since_power_up == 42
    assert result.games_since_door_closure == 7


def test_send_meters_11_through_15_is_the_real_0x19_poll():
    body = bytes([ADDRESS, 0x19])
    for v in (100, 200, 300, 400, 500):
        body += encode_bcd(v, 4)
    fake = FakeSerial(body + crc16_bytes(body))
    transport = SASTransport(fake)
    client = SASClient(transport, ADDRESS)
    meters = client.send_meters_11_through_15()
    assert meters.total_coin_in == 100
    assert meters.games_played == 500
    written = bytes(fake.written)
    assert written[1] == 0x19  # not 0x1C


def test_send_handpay_information():
    body = bytes([ADDRESS, 0x1B, 0x02, 0x05])
    body += encode_bcd(47500, 5)
    body += encode_bcd(1200, 2)
    body += bytes([0x01])
    body += bytes(10)  # unused
    result = make_client(body).send_handpay_information()
    assert result.progressive_group == 0x02
    assert result.level == 0x05
    assert result.amount == 47500
    assert result.partial_pay == 1200
    assert result.reset_id == 1


def test_send_total_bill_meters():
    body = bytes([ADDRESS, 0x1E])
    for v in (1, 5, 10, 20, 50, 100):
        body += encode_bcd(v, 4)
    result = make_client(body).send_total_bill_meters()
    assert (result.bills_1, result.bills_5, result.bills_10, result.bills_20, result.bills_50, result.bills_100) == (
        1,
        5,
        10,
        20,
        50,
        100,
    )


def test_send_total_hand_paid_cancelled_credits():
    body = bytes([ADDRESS, 0x2D]) + encode_bcd(1, 2) + encode_bcd(3300, 4)
    value = make_client(body).send_total_hand_paid_cancelled_credits(game_number=1)
    assert value == 3300


def test_send_cash_out_ticket_information():
    body = bytes([ADDRESS, 0x3D]) + encode_bcd(1234, 4) + encode_bcd(4750, 5)
    result = make_client(body).send_cash_out_ticket_information()
    assert result.ticket_number == 1234
    assert result.amount_cents == 4750


def test_send_current_hopper_status_with_level():
    body = bytes([ADDRESS, 0x4F, 0x06, 0x00, 0x64]) + encode_bcd(500, 4)
    result = make_client(body).send_current_hopper_status()
    assert result.status == 0x00
    assert result.percent_full == 0x64
    assert result.level == 500


def test_send_current_hopper_status_without_level():
    body = bytes([ADDRESS, 0x4F, 0x02, 0x00, 0xFF])
    result = make_client(body).send_current_hopper_status()
    assert result.percent_full == 0xFF
    assert result.level is None


def test_send_last_accepted_bill_information():
    body = bytes([ADDRESS, 0x48, 0x01, 0x02]) + encode_bcd(37, 4)
    result = make_client(body).send_last_accepted_bill_information()
    assert result.country_code == 1
    assert result.denomination_code == 2
    assert result.bill_meter == 37


def test_send_last_accepted_bill_information_never_accepted_a_bill():
    body = bytes([ADDRESS, 0x48, 0x00, 0x00]) + encode_bcd(0, 4)
    result = make_client(body).send_last_accepted_bill_information()
    assert result.country_code == 0
    assert result.denomination_code == 0
    assert result.bill_meter == 0


def test_send_validation_meters():
    body = bytes([ADDRESS, 0x50, 0x80]) + encode_bcd(12, 4) + encode_bcd(4750, 5)
    result = make_client(body).send_validation_meters(0x80)
    assert result.validation_type == 0x80
    assert result.total_validations == 12
    assert result.cumulative_amount_cents == 4750


def test_send_game_n_meters():
    body = bytes([ADDRESS, 0x52]) + encode_bcd(1, 2)
    for v in (10, 20, 30, 40):
        body += encode_bcd(v, 4)
    result = make_client(body).send_game_n_meters(game_number=1)
    assert result.game_number == 1
    assert result.total_coin_in == 10
    assert result.games_played == 40


def test_send_game_n_configuration():
    body = bytes([ADDRESS, 0x53]) + encode_bcd(1, 2)
    body += b"AB" + b"001"  # game id + additional id
    body += bytes([0x04, 0x05, 0x00])  # denom, max bet, progressive group
    body += encode_binary_le(0x0203, 2)  # game options
    body += b"PAYTBL" + b"9500"  # paytable id + base pct
    result = make_client(body).send_game_n_configuration(game_number=1)
    assert result.game_number == 1
    assert result.game_id == "AB"
    assert result.paytable_id == "PAYTBL"
    assert result.game_options == 0x0203


def test_send_selected_game_number():
    body = bytes([ADDRESS, 0x55]) + encode_bcd(3, 2)
    assert make_client(body).send_selected_game_number() == 3


def test_send_enabled_game_numbers():
    data = bytes([0x02]) + encode_bcd(1, 2) + encode_bcd(3, 2)
    body = bytes([ADDRESS, 0x56, len(data)]) + data
    result = make_client(body).send_enabled_game_numbers()
    assert result == [1, 3]


def test_send_current_date_and_time():
    body = bytes([ADDRESS, 0x7E]) + encode_bcd(9142026, 4) + encode_bcd(153045, 3)
    result = make_client(body).send_current_date_and_time()
    assert result.date == "09142026"
    assert result.time == "153045"


def test_send_physical_reel_stop_information():
    stops = bytes(range(1, 10))
    body = bytes([ADDRESS, 0x8F]) + stops
    result = make_client(body).send_physical_reel_stop_information()
    assert result == stops


def test_send_extended_meters_alternate_uses_0xaf_not_0x6f():
    data = encode_bcd(0, 2) + encode_binary_le(0x1234, 2) + bytes([2]) + encode_bcd(99, 2)
    body = bytes([ADDRESS, 0xAF, len(data)]) + data
    fake = FakeSerial(body + crc16_bytes(body))
    transport = SASTransport(fake)
    client = SASClient(transport, ADDRESS)
    result = client.send_extended_meters_alternate([0x1234])
    assert result.meters[0x1234] == 99
    written = bytes(fake.written)
    assert written[1] == 0xAF


def test_send_token_denomination():
    body = bytes([ADDRESS, 0xB3, 0x04])
    assert make_client(body).send_token_denomination() == 0x04


def test_send_wager_category_information():
    data = encode_bcd(0, 2) + encode_bcd(0, 2) + b"9500" + bytes([4]) + encode_bcd(123456, 4)
    body = bytes([ADDRESS, 0xB4, len(data)]) + data
    result = make_client(body).send_wager_category_information()
    assert result.payback_percentage == "9500"
    assert result.coin_in_meter == 123456


def test_send_extended_game_n_information():
    data = encode_bcd(0, 2)  # game number
    data += encode_bcd(500, 2)  # max bet
    data += bytes([0x01])  # progressive group
    data += encode_binary_le(0x00000003, 4)  # progressive levels bitfield
    data += bytes([3]) + b"ABC"  # game name
    data += bytes([2]) + b"XY"  # paytable name
    data += encode_bcd(1, 2)  # wager categories
    body = bytes([ADDRESS, 0xB5, len(data)]) + data
    result = make_client(body).send_extended_game_n_information()
    assert result.max_bet == 500
    assert result.game_name == "ABC"
    assert result.paytable_name == "XY"
    assert result.wager_categories == 1
