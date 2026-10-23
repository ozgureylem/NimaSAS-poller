"""Serial transport for the SAS wire protocol.

Two things the legacy implementation got wrong that this module exists to
fix:

1. **No wakeup-bit handling at all.** SAS 6.02 Section 1.2.1: the physical
   frame is 1 start bit, 8 data bits, a 9th "wakeup" bit, 1 stop bit. The
   host sets the wakeup bit on the first byte of every message and clears
   it on the rest; the gaming machine uses this to know where a new
   message starts. Section 1.2.1's own note says the parity bit can stand
   in for the wakeup bit on UARTs that don't support it directly (which is
   effectively all commodity UART hardware) — so this transport toggles
   parity per byte: MARK (forces the parity/9th bit to 1) for the first
   byte of a poll, SPACE (forces it to 0) for the rest, and for reading
   responses (the gaming machine clears the bit on every response byte
   except the loop-break edge case, which this client does not yet handle).
   legacy/sas.py opened the port with default parity and never touched it,
   which means every poll it ever sent was almost certainly missing the
   frame-start marker a real gaming machine relies on.

   This has NOT been validated against real hardware — that validation is
   exactly the "one gateway against one live EGM for a gaming day" gate the
   project's own rollout plan calls for before this goes near a floor.

2. **No atomicity guarantee.** The project's own scheduler rule (see the
   dev-team brief) is that an in-flight poll must never be interrupted —
   a naive bypass "reintroduces the mid-poll gap this project exists to
   remove". This transport enforces that with a lock around each full
   send-then-receive exchange, so two callers (e.g. a routine meter poll
   and an on-demand cashout check) can never interleave their bytes on the
   wire; the second simply waits for the first exchange to finish.

3. **No principled frame-complete detection.** legacy/sas.py read one byte
   at a time and re-checked the CRC after each one, which means "frame
   complete" was decided by CRC accident rather than by knowing how many
   bytes to expect. This transport reads exactly the number of bytes the
   caller specifies (fixed, or length-byte-driven), and only then runs CRC
   validation.
"""

from __future__ import annotations

import threading
import time
from typing import Protocol

from .exceptions import SASPortCapabilityError, SASTimeoutError


class SerialLike(Protocol):
    """The subset of pyserial's Serial interface this module needs.

    Kept as a narrow protocol so tests can supply a fake without pyserial
    (or real hardware) being involved at all.
    """

    parity: str

    def write(self, data: bytes) -> int: ...
    def read(self, size: int = 1) -> bytes: ...
    def reset_input_buffer(self) -> None: ...

    # Optional: block until everything written is physically out of the
    # UART (POSIX tcdrain). write_with_wakeup() needs it to change parity
    # safely; a fake that omits it still works, it just cannot exhibit the
    # race the drain exists to prevent.
    def flush(self) -> None: ...


PARITY_MARK = "M"
PARITY_SPACE = "S"


class SASTransport:
    """One serial connection to one SAS address bus, with wakeup-bit framing
    and single-flight (ACK-atomic) send/receive exchanges.
    """

    def __init__(self, serial_port: SerialLike, *, byte_timeout: float = 1.0):
        self._serial = serial_port
        self._byte_timeout = byte_timeout
        self._lock = threading.Lock()

    def _drain(self) -> None:
        """Block until bytes already written are physically on the wire.

        This is what makes the parity changes around it safe; see
        write_with_wakeup(). A fake serial object need not provide
        flush(), so its absence is not an error.
        """
        flush = getattr(self._serial, "flush", None)
        if flush is not None:
            flush()

    def write_with_wakeup(self, frame: bytes) -> None:
        """Write ``frame``, setting the wakeup bit only on the first byte.

        The drains are load-bearing, not hygiene. ``write()`` returns once
        the kernel has accepted the bytes, not once the UART has shifted
        them out, and pyserial applies a parity change with ``TCSANOW`` --
        immediately, without waiting for the transmit buffer to empty. So
        setting parity straight after writing the first byte races that
        byte: at 19200 baud it occupies the wire for ~573us, while the
        write and the parity change together return in microseconds. Lose
        that race and the first byte of the frame goes out with SPACE
        parity -- the wakeup bit silently cleared.

        That breaks every multi-byte frame, which means every long poll:
        the machine sees the command byte as a mid-message continuation,
        never registers a message start, and ignores the poll. The symptom
        is a timeout indistinguishable from a machine that does not
        implement the command (section 4.4), while single-byte general
        polls -- which change parity only before the byte, never after --
        keep working perfectly. Draining first makes the ordering explicit
        instead of leaving it to the scheduler.

        Note this cannot be caught by a fake serial port (an attribute
        assignment has no timing) nor by a pty (which accepts parity and
        ignores it). It needs either a drain or real hardware with a
        second listener.
        """
        if not frame:
            return
        try:
            self._serial.parity = PARITY_MARK
            self._serial.write(frame[:1])
            self._drain()  # the wakeup byte must be on the wire before parity moves
            # Safe to lower now, and it restores the documented invariant
            # that the port sits at SPACE between frames -- pyserial skips
            # the tcsetattr entirely when the value is unchanged.
            self._serial.parity = PARITY_SPACE
            if len(frame) > 1:
                self._serial.write(frame[1:])
                # Drain the tail too, so the NEXT frame raising parity to
                # MARK cannot catch these bytes still queued and stamp a
                # spurious message start on them.
                self._drain()
        except Exception as e:
            # Not every USB-serial adapter honors mark/space parity, and a
            # virtual pty never does (see the module docstring) — surface
            # this as a typed, catchable failure rather than letting a raw
            # termios/pyserial exception crash the caller.
            raise SASPortCapabilityError(
                f"port does not support the wakeup-bit parity scheme: {e}"
            ) from e

    def _read_exact(self, count: int, deadline: float) -> bytes:
        buf = bytearray()
        while len(buf) < count:
            if time.monotonic() >= deadline:
                raise SASTimeoutError(
                    f"got {len(buf)}/{count} bytes before timeout"
                )
            chunk = self._serial.read(count - len(buf))
            if chunk:
                buf.extend(chunk)
        return bytes(buf)

    def exchange_fixed(self, frame: bytes, response_length: int, *, timeout: float) -> bytes:
        """Send ``frame`` and read back exactly ``response_length`` bytes.

        The whole exchange (write + read) holds the transport lock, so it
        can never be interleaved with another poll on the same connection.
        """
        with self._lock:
            self._serial.reset_input_buffer()
            self.write_with_wakeup(frame)
            deadline = time.monotonic() + timeout
            return self._read_exact(response_length, deadline)

    def exchange_length_prefixed(
        self, frame: bytes, *, header_length: int = 3, crc_length: int = 2, timeout: float
    ) -> bytes:
        """Send ``frame`` and read a response shaped address, command, length,
        data[length bytes], CRC — i.e. the length byte is the last byte of
        ``header_length`` (default 3: address, command, length).
        """
        with self._lock:
            self._serial.reset_input_buffer()
            self.write_with_wakeup(frame)
            deadline = time.monotonic() + timeout
            header = self._read_exact(header_length, deadline)
            data_length = header[-1]
            rest = self._read_exact(data_length + crc_length, deadline)
            return header + rest

    def general_poll(self, address: int, *, timeout: float) -> bytes:
        """Send a general poll (address ORed with 0x80) and read the single
        exception byte response.
        """
        with self._lock:
            self._serial.reset_input_buffer()
            self.write_with_wakeup(bytes([address | 0x80]))
            deadline = time.monotonic() + timeout
            return self._read_exact(1, deadline)
