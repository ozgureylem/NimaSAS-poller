"""SAS CRC-16 (CCITT/Kermit variant), per SAS 6.02 Section 5.

This is a direct transliteration of the C routine published in the spec
(Figure 5.1), not a call into a generic third-party CRC library — the
legacy code depended on PyCRC's CRC16Kermit class, which meant trusting
that library's byte/bit conventions matched SAS's without ever checking.
The CRC value itself is verified against the four worked examples in spec
Section 15.9 (see tests/test_crc.py).

Wire transmission order for the two CRC bytes is LEAST significant byte
first. The spec's own prose never states this explicitly (Section 5.1
describes internal bit-processing order within the algorithm, not output
byte order), and the §15.9 worked examples read ambiguously either way
since they just quote the CRC as a plain hex number. This was only
settled by cross-checking against IGT's own SAS Simulator (sastest.exe,
"Supports SAS Version 6.01") — its quick-command table of precomputed
`address+command+game_number+CRC` frames for long poll 0x52 matches this
implementation exactly, and only, when the CRC bytes are read low byte
first (verified across 15 independent game-number entries). Getting this
backwards means every single frame fails CRC on a real gaming machine, in
both directions — this is one of the highest-value things to have gotten
right before real hardware is involved.
"""

from __future__ import annotations

_MAGIC = 0x1081  # 010201 octal in the spec's C source, derived from poly x^16+x^12+x^5+1


def crc16(data: bytes, seed: int = 0) -> int:
    """Compute the SAS CRC-16 over ``data``, starting from ``seed``.

    The host and gaming machine both seed with 0 for ordinary long polls;
    a non-zero seed is only used for ROM signature verification (Section 6).
    """
    crcval = seed
    for byte in data:
        q = (crcval ^ byte) & 0x0F
        crcval = (crcval >> 4) ^ (q * _MAGIC)
        q = (crcval ^ (byte >> 4)) & 0x0F
        crcval = (crcval >> 4) ^ (q * _MAGIC)
    return crcval & 0xFFFF


def crc16_bytes(data: bytes, seed: int = 0) -> bytes:
    """Compute the SAS CRC-16 and return it as the two wire bytes (LSB first)."""
    value = crc16(data, seed)
    return bytes([value & 0xFF, (value >> 8) & 0xFF])
