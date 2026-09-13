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

from saspy.bcd import encode_bcd
from saspy.binary import encode_binary_le
from saspy.client import SASClient
from saspy.crc import crc16_bytes
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


def test_send_meters_11_through_15_extended():
    body = bytes([ADDRESS, 0x1C])
    values = [11, 12, 13, 14, 15, 16, 17, 18]
    for v in values:
        body += encode_bcd(v, 4)
    meters = make_client(body).send_meters_11_through_15_extended()
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
