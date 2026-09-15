"""open_serial_port() is the one place this package touches pyserial, so
these tests stand in a fake ``serial`` module rather than requiring the
real one (or a real port). They exist to pin the port settings that are
easy to drop in a refactor and impossible to notice until real hardware
misbehaves.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.serial_port import open_serial_port


class FakeSerialModule(types.ModuleType):
    EIGHTBITS = 8
    PARITY_SPACE = "S"
    STOPBITS_ONE = 1

    def __init__(self):
        super().__init__("serial")
        self.kwargs = None

    def Serial(self, **kwargs):  # noqa: N802 - mirrors pyserial's class name
        self.kwargs = kwargs
        return object()


@pytest.fixture
def fake_serial(monkeypatch):
    module = FakeSerialModule()
    monkeypatch.setitem(sys.modules, "serial", module)
    return module


def test_open_serial_port_requests_exclusive_access(fake_serial):
    """SASTransport's single-flight lock is a threading lock -- it only
    serializes callers within one process. Two processes sharing a port
    (sql_poll_logger.py still running while connectivity_check.py is
    pointed at the same device) would interleave bytes mid-frame, and the
    symptom is garbled responses and checksum failures rather than an
    obvious "port busy". TIOCEXCL makes the second open fail loudly
    instead.
    """
    open_serial_port("/dev/ttyUSB0")
    assert fake_serial.kwargs["exclusive"] is True


def test_open_serial_port_uses_the_sas_wire_settings(fake_serial):
    open_serial_port("/dev/ttyUSB0", baudrate=19200)
    kwargs = fake_serial.kwargs
    assert kwargs["port"] == "/dev/ttyUSB0"
    assert kwargs["baudrate"] == 19200
    assert kwargs["bytesize"] == FakeSerialModule.EIGHTBITS
    assert kwargs["stopbits"] == FakeSerialModule.STOPBITS_ONE
    # Parity starts SPACE (wakeup bit clear); SASTransport toggles it per byte.
    assert kwargs["parity"] == FakeSerialModule.PARITY_SPACE


def test_open_serial_port_disables_flow_control_and_blocking_reads(fake_serial):
    """Flow control off: SAS is a bare 2-wire protocol with no RTS/CTS or
    XON/XOFF. timeout=0 because SASTransport runs its own deadline-based
    read loop and must never block inside pyserial.
    """
    open_serial_port("/dev/ttyUSB0")
    kwargs = fake_serial.kwargs
    assert kwargs["xonxoff"] is False
    assert kwargs["rtscts"] is False
    assert kwargs["dsrdtr"] is False
    assert kwargs["timeout"] == 0
