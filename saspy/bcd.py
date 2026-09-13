"""Binary-coded decimal helpers used throughout the SAS wire format.

SAS packs most numeric fields as BCD: each byte holds two decimal digits,
most-significant byte first. A field declared as N bytes holds exactly
2*N decimal digits.
"""

from __future__ import annotations

from .exceptions import SASEncodingError


def encode_bcd(value: int, length: int) -> bytes:
    """Encode ``value`` as ``length`` bytes of big-endian BCD.

    Raises SASEncodingError if ``value`` is negative or needs more than
    ``2 * length`` decimal digits. The legacy int_to_bcd() silently dropped
    the high-order digits instead of raising, which is worse than a crash
    for a protocol that moves money.
    """
    if value < 0:
        raise SASEncodingError(f"cannot BCD-encode a negative value: {value}")
    max_value = 10 ** (2 * length) - 1
    if value > max_value:
        raise SASEncodingError(
            f"{value} does not fit in {length} BCD bytes (max {max_value})"
        )

    digits = str(value).zfill(2 * length)
    return bytes(
        (int(digits[i]) << 4) | int(digits[i + 1])
        for i in range(0, len(digits), 2)
    )


def decode_bcd(data: bytes) -> int:
    """Decode big-endian BCD bytes into an int.

    Raises SASEncodingError if any nibble is not a valid decimal digit
    (0-9), rather than silently producing a wrong number.
    """
    digits = []
    for byte in data:
        high, low = byte >> 4, byte & 0x0F
        if high > 9 or low > 9:
            raise SASEncodingError(f"byte 0x{byte:02X} is not valid BCD")
        digits.append(str(high))
        digits.append(str(low))
    return int("".join(digits)) if digits else 0
