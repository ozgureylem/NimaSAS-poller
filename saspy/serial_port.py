"""Production serial port factory.

Kept separate from transport.py so the rest of the package (and all of its
tests) never has to import pyserial — SASTransport only needs something
that matches the narrow SerialLike protocol.
"""

from __future__ import annotations

from .transport import SerialLike


def open_serial_port(port: str, baudrate: int = 19200) -> SerialLike:
    """Open a real serial port configured for SAS: 8 data bits, 1 stop bit,
    no flow control. Parity starts at SPACE (wakeup bit clear) and is
    toggled per write by SASTransport.
    """
    import serial  # imported lazily so importing this module doesn't require pyserial

    return serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_SPACE,
        stopbits=serial.STOPBITS_ONE,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
        timeout=0,  # SASTransport does its own deadline-based polling
    )
