"""Exceptions for the SAS host client.

The legacy implementation (see legacy/sas.py) signaled failure with a mix of
``None``, ``False`` and ``''`` return values that were never actually checked
correctly by any caller (see the review notes in the repo history). This
module makes failure modes explicit types instead, so callers cannot
accidentally treat a failure as data.
"""

from __future__ import annotations


class SASError(Exception):
    """Base class for all SAS protocol/transport errors."""


class SASTimeoutError(SASError):
    """No response was received from the gaming machine within the timeout."""


class SASChecksumError(SASError):
    """The response's CRC-16/Kermit did not match its payload."""

    def __init__(self, expected: int, actual: int):
        super().__init__(
            f"CRC mismatch: frame claims 0x{expected:04X}, computed 0x{actual:04X}"
        )
        self.expected = expected
        self.actual = actual


class SASAddressMismatchError(SASError):
    """The response's address byte did not match the address we polled."""

    def __init__(self, expected: int, actual: int):
        super().__init__(
            f"Address mismatch: polled 0x{expected:02X}, response claims 0x{actual:02X}"
        )
        self.expected = expected
        self.actual = actual


class SASEncodingError(SASError):
    """A value could not be encoded into the wire format requested.

    Raised instead of the legacy behavior of silently truncating a value
    that doesn't fit in the requested number of BCD bytes.
    """


class SASCommandNackedError(SASError):
    """The gaming machine explicitly rejected a type S/M/G command (Table
    7.4b's NACK: the address byte it returns is the polled address ORed
    with 0x80) rather than failing to respond at all. The spec doesn't
    say why beyond "the message CRC and data" being invalid -- an ACK
    here only means the command was accepted, not that whatever it asked
    for (e.g. a shutdown) has actually finished happening yet.
    """

    def __init__(self, address: int):
        super().__init__(f"gaming machine at address 0x{address:02X} NACKed the command")
        self.address = address


class SASPortCapabilityError(SASError):
    """The underlying serial port/driver rejected something the wakeup-bit
    scheme needs (typically setting mark/space parity).

    Not every USB-serial adapter reliably supports mark/space parity, and
    virtual ttys (ptys) never do — there's no physical UART framing for a
    parity bit to exist on. Raised instead of letting the underlying
    termios/pyserial exception crash the caller uncaught.
    """
