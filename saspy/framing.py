"""Pure frame construction/parsing — no I/O, so it's unit-testable without
a serial port or a gaming machine attached.

Frame shapes (SAS 6.02 Section 2.2.2):
  Type R command : address byte only, no CRC.
  Type S/M/G cmd : address + command (+ optional length/game-number) + data + CRC.
  Any response   : address + command (+ optional length) + data + CRC.

Section 2.2.3: for variable-length messages, the length byte counts bytes
following the length byte itself, and does not include address, command,
length, or CRC.
"""

from __future__ import annotations

from dataclasses import dataclass

from .crc import crc16, crc16_bytes
from .exceptions import SASAddressMismatchError, SASChecksumError


def build_command(address: int, command_and_data: bytes, *, crc_required: bool) -> bytes:
    """Build the bytes to write for a long poll (Type R has no CRC; S/M/G do)."""
    frame = bytes([address]) + command_and_data
    if crc_required:
        frame += crc16_bytes(frame)
    return frame


@dataclass(frozen=True)
class ParsedFrame:
    address: int
    payload: bytes  # command byte onward, CRC and address stripped


def parse_response(raw: bytes, expected_address: int) -> ParsedFrame:
    """Validate address and CRC on a complete response frame (the caller is
    responsible for having read exactly the right number of bytes, whether
    that was a fixed count or driven by a length byte) and return the
    payload with the address and CRC stripped off.
    """
    if len(raw) < 4:  # address + command + 2 CRC bytes, minimum possible frame
        raise SASChecksumError(expected=0, actual=0)

    address = raw[0]
    if address != expected_address:
        raise SASAddressMismatchError(expected=expected_address, actual=address)

    body, claimed_crc_bytes = raw[:-2], raw[-2:]
    claimed_crc = claimed_crc_bytes[0] | (claimed_crc_bytes[1] << 8)  # wire order is LSB first
    actual_crc = crc16(body)
    if claimed_crc != actual_crc:
        raise SASChecksumError(expected=claimed_crc, actual=actual_crc)

    return ParsedFrame(address=address, payload=raw[1:-2])
