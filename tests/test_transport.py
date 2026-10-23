import threading
import time

import pytest

from saspy.exceptions import SASPortCapabilityError, SASTimeoutError
from saspy.transport import PARITY_MARK, PARITY_SPACE, SASTransport


class FakeSerial:
    """A minimal stand-in for pyserial's Serial, for testing SASTransport
    without any real hardware or pyserial dependency.
    """

    def __init__(self, response: bytes = b"", response_delay: float = 0.0):
        self.parity = None
        self.written: list[tuple[bytes, object]] = []
        self._response = bytearray(response)
        self._response_delay = response_delay
        self._start_time = None

    def write(self, data: bytes) -> int:
        self.written.append((bytes(data), self.parity))
        return len(data)

    def read(self, size: int = 1) -> bytes:
        if self._start_time is None:
            self._start_time = time.monotonic()
        if time.monotonic() - self._start_time < self._response_delay:
            return b""
        chunk = bytes(self._response[:size])
        del self._response[:size]
        return chunk

    def reset_input_buffer(self) -> None:
        pass


class ParityRejectingSerial(FakeSerial):
    """Simulates a port/driver that rejects mark/space parity — e.g. a USB-
    serial adapter without CMSPAR support, or (as found in practice) a
    virtual pty, which has no physical UART framing for parity to exist on
    at all.
    """

    def __init__(self):
        super().__init__()

    @property
    def parity(self):
        return self._parity

    @parity.setter
    def parity(self, value):
        if value == PARITY_MARK:
            raise OSError(22, "Invalid argument")
        self._parity = value


def test_write_with_wakeup_wraps_parity_failure():
    fake = ParityRejectingSerial()
    transport = SASTransport(fake)
    with pytest.raises(SASPortCapabilityError):
        transport.write_with_wakeup(bytes([0x81]))


def test_write_with_wakeup_sets_mark_parity_on_first_byte_only():
    fake = FakeSerial()
    transport = SASTransport(fake)
    transport.write_with_wakeup(bytes([0x81, 0x02, 0x03]))
    assert fake.written[0] == (bytes([0x81]), PARITY_MARK)
    assert fake.written[1] == (bytes([0x02, 0x03]), PARITY_SPACE)


def test_write_with_wakeup_single_byte():
    fake = FakeSerial()
    transport = SASTransport(fake)
    transport.write_with_wakeup(bytes([0x81]))
    assert fake.written == [(bytes([0x81]), PARITY_MARK)]


def test_exchange_fixed_reads_exact_length():
    fake = FakeSerial(response=bytes(range(5)))
    transport = SASTransport(fake)
    result = transport.exchange_fixed(bytes([0x01, 0x1F]), response_length=5, timeout=1.0)
    assert result == bytes(range(5))


def test_exchange_fixed_times_out_on_silence():
    fake = FakeSerial(response=b"")
    transport = SASTransport(fake)
    with pytest.raises(SASTimeoutError):
        transport.exchange_fixed(bytes([0x01, 0x1F]), response_length=5, timeout=0.05)


def test_exchange_length_prefixed_reads_header_then_body():
    # address, command, length=3, three data bytes, two CRC bytes
    response = bytes([0x01, 0x73, 0x03, 0xAA, 0xBB, 0xCC, 0x00, 0x00])
    fake = FakeSerial(response=response)
    transport = SASTransport(fake)
    result = transport.exchange_length_prefixed(bytes([0x01, 0x73]), timeout=1.0)
    assert result == response


def test_general_poll_reads_single_byte():
    fake = FakeSerial(response=bytes([0x00]))
    transport = SASTransport(fake)
    result = transport.general_poll(0x01, timeout=1.0)
    assert result == bytes([0x00])
    # general poll writes address ORed with 0x80, wakeup bit set
    assert fake.written == [(bytes([0x81]), PARITY_MARK)]


def test_concurrent_exchanges_do_not_interleave():
    """Two threads issuing exchanges must never have their writes interleaved
    on the wire — this is the ACK-atomicity requirement from the project's
    scheduler rule (an in-flight poll is never interrupted).
    """
    fake = FakeSerial(response=bytes([0xAA]) * 100, response_delay=0.02)
    transport = SASTransport(fake)
    results = []

    def worker(tag: bytes):
        r = transport.exchange_fixed(tag, response_length=1, timeout=1.0)
        results.append(r)

    threads = [threading.Thread(target=worker, args=(bytes([i]),)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2.0)

    # each write must be a single, uninterrupted tag byte (never merged with another thread's)
    write_bytes = [w for w, _parity in fake.written]
    assert all(len(w) == 1 for w in write_bytes)
    assert len(results) == 5


class RacyFakeSerial:
    """A fake that models the thing the other fakes cannot: transmission
    takes time, and a parity change applies to bytes still queued.

    ``FakeSerial`` records the parity in effect at the moment ``write()``
    is called, which silently assumes a byte is on the wire the instant it
    is handed over. Real UARTs do not work that way, and pyserial changes
    parity with ``TCSANOW`` -- immediately, without draining -- so a byte
    still sitting in the transmit buffer goes out under the NEW setting.
    Here, bytes stay pending until ``flush()``, and a parity change
    restamps everything still pending. That is what turns the race into a
    visible assertion instead of a heisenbug on real hardware.
    """

    def __init__(self):
        self._parity = "S"
        self._pending = []
        self.on_wire = []  # (byte, parity it was actually transmitted with)
        self.to_read = b""
        self.reconfigs = 0  # tcsetattr calls that actually changed the line

    @property
    def parity(self):
        return self._parity

    @parity.setter
    def parity(self, value):
        if value == self._parity:
            return  # pyserial issues no tcsetattr when nothing changed
        self.reconfigs += 1
        # TCSANOW: the new setting catches anything not yet shifted out.
        self._pending = [(b, value) for b, _ in self._pending]
        self._parity = value

    def write(self, data: bytes) -> int:
        self._pending.extend((b, self._parity) for b in bytes(data))
        return len(data)

    def flush(self) -> None:
        self.settle()

    def settle(self) -> None:
        """Bytes reach the wire, stamped with the parity in force now --
        by a drain, or just by time passing between polls."""
        self.on_wire.extend(self._pending)
        self._pending.clear()

    def read(self, size: int = 1) -> bytes:
        chunk, self.to_read = self.to_read[:size], self.to_read[size:]
        return chunk

    def reset_input_buffer(self) -> None:
        pass


def test_wakeup_bit_survives_a_multi_byte_frame():
    """The regression that matters on real hardware.

    Every long poll is a multi-byte frame, so every long poll used to hand
    the first byte to the kernel and lower parity to SPACE before the UART
    had shifted it. The machine then sees the command byte as a mid-message
    continuation, never registers a message start, and ignores the poll --
    a timeout indistinguishable from an unimplemented command (4.4), while
    single-byte general polls keep working because nothing lowers parity
    after them.
    """
    fake = RacyFakeSerial()
    SASTransport(fake).write_with_wakeup(bytes([0x01, 0x3D]))

    assert fake.on_wire[0] == (0x01, "M"), (
        "first byte must reach the wire with MARK parity -- the wakeup bit"
    )
    assert [p for _b, p in fake.on_wire[1:]] == ["S"], "data bytes must be SPACE"


def test_frame_tail_cannot_be_restamped_by_the_next_frame():
    """Back-to-back frames: if the tail of one is still queued when the
    next raises parity to MARK, those bytes go out flagged as message
    starts. Draining the tail is what prevents it.
    """
    fake = RacyFakeSerial()
    transport = SASTransport(fake)
    transport.write_with_wakeup(bytes([0x01, 0x3D]))
    transport.write_with_wakeup(bytes([0x81]))
    fake.settle()

    assert fake.on_wire == [(0x01, "M"), (0x3D, "S"), (0x81, "M")]


def test_single_byte_frames_leave_parity_at_mark_on_purpose():
    """Deliberate, and the opposite of tidy.

    A general poll is one byte, so nothing has to lower parity afterwards.
    Leaving it at MARK means the next general poll's ``parity = MARK`` is
    a no-op and pyserial issues no tcsetattr at all -- the write goes out
    with no port reconfiguration anywhere near it. That is the exact path
    real machines have already answered 60/60, so it is pinned here.

    Restoring SPACE after every frame reads better and was tried. It puts
    a reconfiguration immediately before every general poll, which is a
    change to the one path known to work; examples/wakeup_probe.py exists
    to measure whether a machine can tell the difference.
    """
    fake = RacyFakeSerial()
    transport = SASTransport(fake)
    transport.write_with_wakeup(bytes([0x81]))
    transport.write_with_wakeup(bytes([0x81]))
    fake.settle()

    assert fake.parity == "M"
    assert fake.reconfigs == 1, "only the first poll may reconfigure the port"
    assert fake.on_wire == [(0x81, "M"), (0x81, "M")]
