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

from saspy.exceptions import SASPortCapabilityError
from saspy.serial_port import open_serial_port


class FakePort:
    """Stands in for a pyserial Serial instance. ``parity_capable=False``
    models the adapter that cannot produce SAS's 9th bit.
    """

    def __init__(self, parity_capable=True, **kwargs):
        object.__setattr__(self, "parity_capable", parity_capable)
        object.__setattr__(self, "port", kwargs.get("port"))
        object.__setattr__(self, "parity_writes", [])
        object.__setattr__(self, "closed", False)

    def __setattr__(self, name, value):
        if name == "parity":
            if not self.parity_capable:
                raise OSError(22, "Invalid argument")
            self.parity_writes.append(value)
            return
        object.__setattr__(self, name, value)

    def close(self):
        object.__setattr__(self, "closed", True)


class FakeSerialModule(types.ModuleType):
    EIGHTBITS = 8
    PARITY_SPACE = "S"
    PARITY_MARK = "M"
    STOPBITS_ONE = 1

    def __init__(self, parity_capable=True, refuse_parity=False):
        super().__init__("serial")
        self.kwargs = None
        self.parity_capable = parity_capable
        # refuse_parity models the adapter whose driver rejects the parity
        # setting at construction -- the real-hardware shape of this fault,
        # which happens before any per-write probe can run.
        self.refuse_parity = refuse_parity
        self.last_port = None

    PARITY_NONE = "N"

    def Serial(self, **kwargs):  # noqa: N802 - mirrors pyserial's class name
        if self.refuse_parity and kwargs.get("parity") != self.PARITY_NONE:
            raise OSError(22, "Invalid argument")
        self.kwargs = kwargs
        self.last_port = FakePort(parity_capable=self.parity_capable, **kwargs)
        return self.last_port


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


def test_open_serial_port_rejects_a_port_that_cannot_do_mark_space_parity(monkeypatch):
    """The wakeup bit is not optional in SAS -- it frames every message
    (Section 1.2.1). An adapter that cannot toggle mark/space parity used
    to fail per-write, deep inside a poll, and every tool reported it as
    silence: "check wiring, baud, address", none of which is the problem.
    Probing at open turns that into one accurate line.
    """
    module = FakeSerialModule(parity_capable=False)
    monkeypatch.setitem(sys.modules, "serial", module)

    with pytest.raises(SASPortCapabilityError) as excinfo:
        open_serial_port("/dev/ttyUSB0")

    message = str(excinfo.value)
    assert "/dev/ttyUSB0" in message
    assert "mark/space parity" in message
    # It must actively steer the operator away from the wrong suspects.
    assert "wiring" in message and "address" in message
    # A port we refused must not be left open holding TIOCEXCL.
    assert module.last_port.closed is True


def test_open_serial_port_can_opt_out_of_the_wakeup_probe(monkeypatch):
    """Loopback/simulator rigs (see docs/sastest) use a pty, which can
    never carry a 9th bit. They may opt out explicitly -- and only
    explicitly, which is why the default above is True.
    """
    module = FakeSerialModule(parity_capable=False)
    monkeypatch.setitem(sys.modules, "serial", module)

    port = open_serial_port("/dev/pts/9", require_wakeup_capability=False)

    assert port.closed is False


def test_the_wakeup_probe_leaves_parity_at_space(fake_serial):
    """Probing must not leave the port in MARK: SASTransport assumes it
    starts SPACE and only raises it for a message's first byte.
    """
    open_serial_port("/dev/ttyUSB0")
    assert fake_serial.last_port.parity_writes == ["M", "S"]


def test_a_driver_that_refuses_parity_at_open_is_named_as_such(monkeypatch):
    """The port opens with PARITY_SPACE, so an adapter that cannot do space
    parity fails at construction -- before any probe -- with something as
    unhelpful as "(22, 'Invalid argument')". Reopening once without parity
    separates "this driver cannot do SAS parity" from "this port is
    missing, busy or forbidden", which have entirely different fixes.
    """
    module = FakeSerialModule(refuse_parity=True)
    monkeypatch.setitem(sys.modules, "serial", module)

    with pytest.raises(SASPortCapabilityError) as excinfo:
        open_serial_port("/dev/ttyUSB0")

    message = str(excinfo.value)
    assert "mark/space parity" in message
    assert "not the wiring" in message


def test_a_port_that_will_not_open_at_all_is_not_blamed_on_parity(monkeypatch):
    """A missing or busy port must keep its own error. Misreporting it as a
    parity problem would send someone hunting for a new adapter when the
    real cause is a wrong device name or another process holding the port.
    """
    module = FakeSerialModule()

    def always_fail(**kwargs):
        raise OSError(2, "No such file or directory")

    module.Serial = always_fail
    monkeypatch.setitem(sys.modules, "serial", module)

    with pytest.raises(OSError) as excinfo:
        open_serial_port("/dev/ttyGONE")

    assert not isinstance(excinfo.value, SASPortCapabilityError)
    assert "No such file" in str(excinfo.value)


def test_a_port_held_by_another_process_says_what_to_do(monkeypatch):
    """The lock is deliberate -- two processes sharing a SAS port interleave
    bytes mid-frame -- but pyserial's "Could not exclusively lock port ...
    Resource temporarily unavailable" tells nobody what to do about it. On a
    gateway the cause is nearly always a poller left running, and that was
    exactly what happened: an AFT test refused to start because an overnight
    soak still owned the port.
    """
    module = FakeSerialModule()

    def locked(**kwargs):
        raise OSError(11, "Could not exclusively lock port /dev/ttyS0: "
                          "[Errno 11] Resource temporarily unavailable")

    module.Serial = locked
    monkeypatch.setitem(sys.modules, "serial", module)

    with pytest.raises(SASPortCapabilityError) as excinfo:
        open_serial_port("/dev/ttyS0")

    message = str(excinfo.value)
    assert "already held by another process" in message
    assert "pgrep" in message, "it must name the command that finds the holder"
    assert "soak.pid" in message, "and the one that stops the usual culprit"
