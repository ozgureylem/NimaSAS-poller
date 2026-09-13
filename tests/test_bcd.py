import pytest

from saspy.bcd import decode_bcd, encode_bcd
from saspy.exceptions import SASEncodingError


def test_encode_matches_spec_example_amount():
    # Table 15.9a: 0000004750 packed as 5 bytes -> 00 00 00 47 50
    assert encode_bcd(4750, 5) == bytes([0x00, 0x00, 0x00, 0x47, 0x50])


def test_encode_matches_spec_example_validation_number():
    assert encode_bcd(1234567890123456, 8) == bytes.fromhex("1234567890123456")


def test_round_trip():
    for value, length in [(0, 1), (99, 1), (4750, 5), (1234567890123456, 8)]:
        assert decode_bcd(encode_bcd(value, length)) == value


def test_encode_rejects_negative():
    with pytest.raises(SASEncodingError):
        encode_bcd(-1, 2)


def test_encode_rejects_overflow():
    with pytest.raises(SASEncodingError):
        encode_bcd(100, 1)  # 1 byte only holds 2 digits (0-99)


def test_decode_rejects_invalid_nibble():
    with pytest.raises(SASEncodingError):
        decode_bcd(bytes([0xAB]))  # A and B are not decimal digits
