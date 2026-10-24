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

from .constants import RTE_EVENT_IDENTIFIER
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

    def __init__(
        self,
        serial_port: SerialLike,
        *,
        byte_timeout: float = 1.0,
        wake_byte: int | None = 0x80,
        wake_gap: float = 0.005,
        wake_after_idle: float = 0.0,
    ):
        """``wake_byte`` is a throwaway byte sent before a message when the
        line has been idle, to absorb the loss of the first byte after idle.

        Measured on an EGT machine: a general poll sent after ~500ms of
        idle was answered 0/20, and the identical poll preceded by 0x80 and
        5ms was answered 20/20 -- with the preamble's own reply discarded,
        so the reply provably belonged to the real poll. 0x80 is a general
        poll to the broadcast address, which that machine does not answer
        (0/20 sent alone), making it a safe byte to throw away.

        This is an empirical workaround, not a SAS requirement -- the spec
        asks for nothing of the kind, and the real cause is more likely the
        receiving end of the link needing the line driven before it reads
        reliably. Set ``wake_byte=None`` to switch it off on a link that
        does not need it.

        ``wake_after_idle`` is how long the line must have been quiet
        before the wake byte is sent again. The default of 0.0 sends it
        before every message, which is the configuration actually measured
        at 20/20; raise it to skip the byte on back-to-back exchanges once
        examples/preamble_probe.py has established the real threshold for
        a given machine.
        """
        self._serial = serial_port
        self._byte_timeout = byte_timeout
        self._wake_byte = wake_byte
        self._wake_gap = wake_gap
        self._wake_after_idle = wake_after_idle
        self._last_tx: float | None = None
        self._lock = threading.Lock()

    def _wake_line(self) -> None:
        """Send the throwaway byte if the line has been idle long enough.

        Any reply it draws is discarded: the callers above flush input
        before calling in, so a reply arriving after that flush would
        otherwise be read back as the answer to the real message.
        """
        if self._wake_byte is None:
            return
        now = time.monotonic()
        if self._last_tx is not None and (now - self._last_tx) < self._wake_after_idle:
            return
        self._serial.parity = PARITY_MARK
        self._serial.write(bytes([self._wake_byte]))
        self._drain()
        if self._wake_gap:
            time.sleep(self._wake_gap)
        self._serial.reset_input_buffer()

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

        Deliberately scoped to multi-byte frames only. A single-byte
        general poll leaves parity at MARK, so the next one issues no
        tcsetattr before its write at all -- and that exact path is what
        real machines have already answered. Restoring SPACE after every
        frame is tidier and was tried; it puts a port reconfiguration
        immediately before every general poll, on the one path that was
        known good. See examples/wakeup_probe.py, which measures whether
        a tcsetattr before a write costs this machine the byte.

        Note this cannot be caught by a fake serial port (an attribute
        assignment has no timing) nor by a pty (which accepts parity and
        ignores it). It needs either a drain or real hardware with a
        second listener.
        """
        if not frame:
            return
        try:
            self._wake_line()
            self._serial.parity = PARITY_MARK
            self._serial.write(frame[:1])
            if len(frame) > 1:
                # The ONLY place the race lives. Draining here keeps the
                # single-byte path (every general poll) byte-for-byte
                # identical to what real machines have already answered
                # 60/60 -- in steady state parity is already MARK, so
                # pyserial issues no tcsetattr at all before the write.
                self._drain()
                self._serial.parity = PARITY_SPACE
                self._serial.write(frame[1:])
                # Drain the tail so the NEXT frame raising parity to MARK
                # cannot catch these bytes still queued and stamp a
                # spurious message start on them.
                self._drain()
            self._last_tx = time.monotonic()
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

    def exchange_rte_aware(
        self,
        frame: bytes,
        *,
        timeout: float,
        data_len_for,
        on_event,
        response_length: int | None = None,
        header_length: int = 3,
        crc_length: int = 2,
        max_requeue: int = 4,
    ) -> bytes:
        """A long poll that survives being pre-empted by a real time event.

        Section 12.3: "Event reporting takes priority over long poll
        responses. If a gaming machine has any outstanding events to report
        when it receives a long poll, it reports the event." Section 12.4:
        "When the host receives an event response to a long poll, it
        considers the long poll NACKed and reinserts the long poll into its
        transmit queue."

        So while RTE is on, any long poll may come back as an event instead
        of its own answer, and the host has to notice and ask again. Without
        this a meter read under RTE silently decodes an event as meter data.

        The event is handed to ``on_event`` rather than discarded. Dropping
        it would reintroduce exactly the exception loss RTE is being used to
        avoid -- it is a real event, it simply arrived in the wrong slot.

        Telling the two apart is unambiguous: an event's second byte is the
        FF identifier (Table 12.5) and no long poll command is FF, so a
        response whose second byte is FF is an event. Section 12.7's ROM
        signature response deliberately omits the identifier for the same
        reason, so it is not confused with one either.

        Pass ``response_length`` for a fixed-length reply, or leave it None
        for the length-prefixed shape.
        """
        with self._lock:
            deadline = time.monotonic() + timeout
            for _ in range(max_requeue):
                self._serial.reset_input_buffer()
                self.write_with_wakeup(frame)
                head = self._read_exact(2, deadline)
                if head[1] == RTE_EVENT_IDENTIFIER:
                    code = self._read_exact(1, deadline)[0]
                    body = self._read_exact(data_len_for(code) + crc_length, deadline)
                    on_event(head + bytes([code]) + body)
                    continue  # 12.4: treat as a NACK and reissue
                if response_length is not None:
                    return head + self._read_exact(response_length - 2, deadline)
                header = head + self._read_exact(header_length - 2, deadline)
                return header + self._read_exact(header[-1] + crc_length, deadline)
            raise SASTimeoutError(
                f"long poll pre-empted by a real time event {max_requeue} times "
                f"(section 12.3); the machine has more events queued than this "
                f"poll has retries"
            )

    def general_poll_rte(
        self, address: int, *, timeout: float, data_len_for
    ) -> bytes:
        """General poll while the machine is in real time event mode.

        Returns b"" for no activity, which under RTE is silence rather than
        a 0x00 byte: section 12.6 removes exception codes 00 and 1F from the
        response set entirely and says "no activity on the gaming machine is
        implied when the gaming machine does not send a real time event".
        So a timeout here is the normal idle case, not a fault -- which is
        the opposite of what it means in the default polling mode, and the
        reason this is a separate method rather than a flag on general_poll.

        Table 12.5 has no length field, so the frame cannot be read in one
        go: the first three bytes give address, the FF identifier and the
        exception code, and only then is the remaining length known.
        ``data_len_for`` supplies it per code, keeping the table in
        constants rather than in the transport.

        A response whose second byte is not FF is returned as-is after those
        three bytes. Section 12.7: a ROM signature response may also arrive
        in reply to a poll and deliberately omits the identifier, "which
        distinguishes a ROM signature response from a coin in tilt exception
        response" -- so the caller, not the transport, decides what it is.
        """
        with self._lock:
            self._serial.reset_input_buffer()
            self.write_with_wakeup(bytes([address | 0x80]))
            deadline = time.monotonic() + timeout
            try:
                head = self._read_exact(3, deadline)
            except SASTimeoutError:
                return b""
            if head[1] != RTE_EVENT_IDENTIFIER:
                return head
            return head + self._read_exact(data_len_for(head[2]) + 2, deadline)

    def general_poll(self, address: int, *, timeout: float) -> bytes:
        """Send a general poll (address ORed with 0x80) and read the single
        exception byte response.
        """
        with self._lock:
            self._serial.reset_input_buffer()
            self.write_with_wakeup(bytes([address | 0x80]))
            deadline = time.monotonic() + timeout
            return self._read_exact(1, deadline)
