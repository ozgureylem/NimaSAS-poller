import pytest

from saspy.crc import crc16_bytes
from saspy.exceptions import SASAddressMismatchError, SASChecksumError
from saspy.framing import build_command, parse_response


def test_build_command_type_r_has_no_crc():
    frame = build_command(0x01, bytes([0x1F]), crc_required=False)
    assert frame == bytes([0x01, 0x1F])


def test_build_command_type_s_appends_crc():
    frame = build_command(0x01, bytes([0x58, 0x00]), crc_required=True)
    body = bytes([0x01, 0x58, 0x00])
    assert frame == body + crc16_bytes(body)


def test_parse_response_strips_address_and_crc():
    body = bytes([0x01, 0x58, 0x00])
    raw = body + crc16_bytes(body)
    parsed = parse_response(raw, expected_address=0x01)
    assert parsed.address == 0x01
    assert parsed.payload == bytes([0x58, 0x00])


def test_parse_response_rejects_address_mismatch():
    body = bytes([0x02, 0x58, 0x00])
    raw = body + crc16_bytes(body)
    with pytest.raises(SASAddressMismatchError):
        parse_response(raw, expected_address=0x01)


def test_parse_response_rejects_bad_crc():
    body = bytes([0x01, 0x58, 0x00])
    raw = body + bytes([0x00, 0x00])  # wrong CRC
    with pytest.raises(SASChecksumError):
        parse_response(raw, expected_address=0x01)
