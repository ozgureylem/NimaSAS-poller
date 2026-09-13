from saspy.binary import decode_binary_le, encode_binary_le


def test_round_trip():
    assert decode_binary_le(encode_binary_le(0x1234, 2)) == 0x1234


def test_encode_is_least_significant_byte_first():
    # Section 2.2.3: binary fields are transmitted LSB first.
    assert encode_binary_le(0x1234, 2) == bytes([0x34, 0x12])


def test_decode_treats_first_byte_as_least_significant():
    assert decode_binary_le(bytes([0x34, 0x12])) == 0x1234
