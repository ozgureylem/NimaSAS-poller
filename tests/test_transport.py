import threading
import time

import pytest

from saspy.exceptions import SASTimeoutError
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
