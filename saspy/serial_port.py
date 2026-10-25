"""Production serial port factory.

Kept separate from transport.py so the rest of the package (and all of its
tests) never has to import pyserial — SASTransport only needs something
that matches the narrow SerialLike protocol.
"""

from __future__ import annotations

from .exceptions import SASPortCapabilityError
from .transport import SerialLike


def open_serial_port(
    port: str, baudrate: int = 19200, *, require_wakeup_capability: bool = True
) -> SerialLike:
    """Open a real serial port configured for SAS: 8 data bits, 1 stop bit,
    no flow control. Parity starts at SPACE (wakeup bit clear) and is
    toggled per write by SASTransport.

    ``require_wakeup_capability`` probes that toggle at open time and
    refuses a port that cannot do it. Pass False only for a loopback or
    simulator rig where you knowingly accept that the wakeup bit is not
    being exercised — never against a real machine, where a port that
    silently drops the 9th bit looks exactly like a machine that is not
    answering.

    Opened with ``exclusive=True``: SASTransport's single-flight lock is a
    threading lock, so it only serializes callers inside ONE process. Two
    processes on the same port — the obvious one being sql_poll_logger.py
    left running while connectivity_check.py is pointed at the same
    device — would interleave their bytes mid-frame with nothing to stop
    them, and the symptom is not "port busy" but garbled responses,
    checksum failures and phantom protocol bugs. TIOCEXCL turns that into
    an immediate, obvious SerialException on the second open instead. See
    MANUAL.md §4.5.
    """
    import serial  # imported lazily so importing this module doesn't require pyserial

    try:
        port_obj = _open(serial, port, baudrate, serial.PARITY_SPACE)
    except Exception as e:
        # TIOCEXCL refused: another process already owns this port. pyserial
        # reports it as "Could not exclusively lock port ... Resource
        # temporarily unavailable", which is accurate and tells nobody what to
        # do. On a gateway the answer is nearly always a poller left running,
        # and the fix is one command -- so say which command.
        if "exclusively lock" in str(e) or "Resource temporarily unavailable" in str(e):
            raise SASPortCapabilityError(
                f"{port} is already held by another process, so this one cannot open it. "
                f"That lock is deliberate: two processes sharing a SAS port interleave "
                f"bytes mid-frame, and the symptom is garbled responses rather than an "
                f"obvious conflict.\n\n"
                f"Find it with:   pgrep -af 'sql_poll_logger|exception_monitor|poll_test_tool'\n"
                f"Stop a soak:    kill \"$(cat soak.pid)\"\n\n"
                f"Original error: {e}"
            ) from e
        # The port opens with PARITY_SPACE (wakeup bit clear), so an adapter
        # whose driver cannot do space parity fails here, at construction,
        # before the probe below ever runs -- and reports something as
        # unhelpful as "(22, 'Invalid argument')". Reopening once with no
        # parity distinguishes "this driver cannot do SAS parity" from
        # "this port is missing/busy//denied", which are different problems
        # with different fixes.
        if require_wakeup_capability and _opens_without_parity(serial, port, baudrate):
            raise SASPortCapabilityError(
                f"{port} rejected the space parity SAS needs for the wakeup bit "
                f"({e}), but opens fine without parity. The adapter or its driver "
                f"cannot do mark/space parity. This is not the wiring, the baud "
                f"rate or the SAS address."
            ) from e
        raise
    if require_wakeup_capability:
        _assert_wakeup_bit_capable(port_obj)
    return port_obj


def _open(serial, port: str, baudrate: int, parity):
    return serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=serial.EIGHTBITS,
        parity=parity,
        stopbits=serial.STOPBITS_ONE,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
        timeout=0,  # SASTransport does its own deadline-based polling
        exclusive=True,  # POSIX TIOCEXCL; accepted and ignored on Windows, where COM ports are already exclusive
    )


def _opens_without_parity(serial, port: str, baudrate: int) -> bool:
    """True if the port opens with parity disabled -- i.e. the port itself
    is fine and only the parity setting was refused."""
    try:
        probe = _open(serial, port, baudrate, serial.PARITY_NONE)
    except Exception:
        return False
    try:
        probe.close()
    except Exception:
        pass
    return True


def _assert_wakeup_bit_capable(port_obj) -> None:
    """Fail at open time if the adapter cannot toggle mark/space parity.

    Without this the failure surfaces per-write, deep inside a poll, and
    every tool reports it the same way it reports a machine that isn't
    there: silence. The operator is then told to check wiring, baud and
    address -- none of which is wrong -- while the real cause is that the
    adapter's UART cannot produce the 9th bit SAS frames every message
    with (Section 1.2.1). Cheap USB bridges frequently cannot; a virtual
    pty never can. Catching it here costs one parity round-trip at open
    and turns an hour at a machine into one line of output.

    What this does NOT catch, and it matters: a driver that *accepts* the
    parity setting and then ignores it. Linux ptys do exactly that, and
    some USB bridges do too. Nothing on this side of the wire can tell
    that apart from a working port -- confirming the wakeup bit is really
    on the wire needs a second listener or a scope. So a clean open means
    "the driver did not refuse", not "the 9th bit is reaching the
    machine": if every poll still times out, the adapter is not cleared
    of suspicion. See MANUAL.md and POLL_TEST_TOOL_MANUAL.md section 8.
    """
    import serial

    try:
        port_obj.parity = serial.PARITY_MARK
        port_obj.parity = serial.PARITY_SPACE
    except Exception as e:
        try:
            port_obj.close()
        except Exception:
            pass
        raise SASPortCapabilityError(
            f"{port_obj.port} cannot set mark/space parity, which SAS needs for "
            f"the wakeup bit on the first byte of every message ({e}). "
            f"This is the adapter/driver, not the wiring, the baud rate or the "
            f"SAS address. Use an adapter whose UART supports mark/space parity "
            f"(many cheap USB bridges do not); a virtual tty never can."
        ) from e
