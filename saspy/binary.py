"""Multi-byte *binary* (as opposed to BCD/ASCII) field codecs.

SAS 6.02 Section 2.2.3 states the rule plainly: "All data exchanged in the
BCD and ASCII formats are sent most significant byte (MSB) first. All data
exchanged in the binary format are sent least significant byte (LSB)
first." BCD and ASCII fields (handled in bcd.py) are therefore big-endian;
scalar fields the spec tables mark as "binary" (asset number, pool ID,
game options, lock timeout, buffer index, ...) are little-endian.

This is easy to miss by reading individual long-poll tables in isolation
(each table just says "N binary" without repeating the section 2.2.3 rule),
and the legacy implementation never applied it anywhere — every multi-byte
binary field in legacy/sas.py was decoded as if it were big-endian.
"""

from __future__ import annotations


def encode_binary_le(value: int, length: int) -> bytes:
    return value.to_bytes(length, byteorder="little", signed=False)


def decode_binary_le(data: bytes) -> int:
    return int.from_bytes(data, byteorder="little", signed=False)
