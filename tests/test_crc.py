"""CRC-16 tests.

The four worked-example cases come directly from SAS 6.02 Section 15.9
(System Validation Examples) — real address/command/data bytes with the
spec's own stated CRC result, not synthetic data. These confirm the CRC
*value* (and, incidentally, MSB-first BCD packing), but the spec examples
alone don't actually pin down wire transmission byte order for the CRC
itself — see test_crc16_bytes_wire_order_matches_sastest_reference below,
which is the test that actually settles it.
"""

from saspy.crc import crc16, crc16_bytes


def test_crc16_kermit_standard_check_value():
    # Catalog check value for CRC-16/KERMIT: CRC of ASCII "123456789" is 0x2189.
    assert crc16(b"123456789") == 0x2189


def test_spec_example_15_9a_pending_cashout_response():
    # Address=01 Command=57 CashoutType=00 Amount(5 BCD)=0000004750
    msg = bytes([0x01, 0x57, 0x00, 0x00, 0x00, 0x00, 0x47, 0x50])
    assert crc16(msg) == 0x6D83
    assert crc16_bytes(msg) == bytes([0x83, 0x6D])  # wire order: LSB first


def test_spec_example_15_9b_validation_number_command():
    # Address=01 Command=58 SysID(1 BCD)=01 ValidationNumber(8 BCD)=1234567890123456
    msg = bytes([0x01, 0x58, 0x01, 0x12, 0x34, 0x56, 0x78, 0x90, 0x12, 0x34, 0x56])
    assert crc16(msg) == 0x349C


def test_spec_example_15_9c_and_15_9e_ack_response():
    # Address=01 Command=58 Status=00 (both 15.9c and 15.9e use this exact frame)
    msg = bytes([0x01, 0x58, 0x00])
    assert crc16(msg) == 0x47EB


def test_spec_example_15_9d_validation_denied_command():
    # Address=01 Command=58 SysID=00 ValidationNumber=0000000000000000
    msg = bytes([0x01, 0x58, 0x00] + [0x00] * 8)
    assert crc16(msg) == 0xBF91


def test_crc16_bytes_wire_order_matches_sastest_reference():
    """Each entry is address(01) + command(52, Send Game N Meters) + a 2-byte
    BCD game number + CRC, read directly off IGT's own SAS Simulator
    (sastest.exe, "Supports SAS Version 6.01") quick-command list — a table
    of precomputed frames the tool itself will transmit verbatim for each
    game number. This is what actually settles the CRC wire byte order
    (LSB first): the four spec worked examples above only confirm the CRC
    *value*, since a table showing "CRC 6D83" doesn't by itself say which
    byte goes on the wire first. All 15 entries here matched immediately
    and exactly once the byte order was corrected to LSB-first — before
    that fix, none of the non-zero entries matched in either byte order
    with the naive (non-BCD) game-number encoding, and 10/15 matched only
    by accident once BCD encoding was fixed but before the byte-order fix
    was found. Getting this wrong means CRC fails on every frame, in both
    directions, against real hardware.
    """
    from saspy.bcd import encode_bcd

    game_number_to_crc_hex = {
        0: "E02A", 1: "693B", 2: "F209", 3: "7B18", 4: "C46C",
        5: "4D7D", 6: "D64F", 7: "5F5E", 8: "A8A6", 9: "21B7",
        10: "613A", 11: "E82B", 12: "7319", 13: "FA08", 14: "457C",
    }
    for game_number, crc_hex in game_number_to_crc_hex.items():
        msg = bytes([0x01, 0x52]) + encode_bcd(game_number, 2)
        assert crc16_bytes(msg) == bytes.fromhex(crc_hex), game_number
