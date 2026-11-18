"""Per-gateway connection config: one INI file per gateway, since the
project's topology is 1:1 (one micro-PC, one serial port, one EGM). This
is the file the commissioning tool (see commission_gateway.py) writes,
and what a long-running poller loop reads at startup instead of hardcoding
connection details.

INI over TOML/YAML: it's stdlib-only (configparser) on every Python
version this project supports, matches the format the previous vendor
tooling (sastest.ini) used — familiar to anyone who's touched that — and
a gateway config has no nested structure that would need anything richer.

Example file::

    [connection]
    port = /dev/ttyUSB0
    baudrate = 19200
    address = 1
    timeout = 1.0

    [protocol]
    sas_version = 6.02

    [machine]
    serial_number = TROP-0042
    game_id = 01

    [server]
    ip = 192.168.100.137
    port = 8443

``[connection] reconnect`` (default ON) decides whether a poller that loses
the serial port reopens it itself or exits for a supervisor to restart. The
flag lives HERE rather than in ``[machine]`` -- which the operator's first
sketch suggested -- because ``[machine]`` is metadata nothing reads back,
and a behavioural switch buried in a section documented as inert is a trap
for the next reader. ``port``, ``timeout`` and the reconnect behaviour are
all one subject: how this gateway talks to its cabinet.

``[server]`` is where the gateway client finds the server. Optional: a
poller with no server link does not need it, and a gateway that has one
should not need a second file or a command-line flag to say where to
reach it.

``[machine]`` is optional metadata the commissioning tool fills in from
what it discovered (see send_sas_version_and_serial / send_gaming_machine_id)
for an audit trail of what was actually on the other end when the gateway
was set up. Nothing in this module or SASClient reads it back.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path

from .exceptions import SASError

DEFAULT_BAUDRATE = 19200  # fixed by the SAS spec (§1.2), not actually configurable per-machine
DEFAULT_TIMEOUT = 1.0


@dataclass
class GatewayConfig:
    port: str
    address: int
    baudrate: int = DEFAULT_BAUDRATE
    timeout: float = DEFAULT_TIMEOUT
    sas_version: str = ""
    serial_number: str = ""
    game_id: str = ""
    # Default ON, at the operator's instruction (2026-11-18). A gateway on a
    # floor is headless, has no SSH, and a technician cannot be sent to every
    # cabinet that lost a cable for a moment. The reasons NOT to reconnect
    # were weighed and are recorded in MANUAL.md; the one that survived --
    # that an automatic reopen papers over a physical fault -- is answered by
    # making the loop loud rather than by refusing to have one.
    reconnect: bool = True
    server_ip: str = ""
    server_port: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.address <= 127:
            raise SASError(f"address must be 1-127, got {self.address}")
        if self.baudrate <= 0:
            raise SASError(f"baudrate must be positive, got {self.baudrate}")
        if self.timeout <= 0:
            raise SASError(f"timeout must be positive, got {self.timeout}")
        if self.server_port and not 1 <= self.server_port <= 65535:
            raise SASError(f"server port must be 1-65535, got {self.server_port}")
        if self.server_ip and not self.server_port:
            raise SASError(
                "[server] has an ip but no port; half an address is not an "
                "address, and a gateway that silently picked a default would "
                "connect somewhere nobody chose")
        if self.server_port and not self.server_ip:
            raise SASError("[server] has a port but no ip")


def load_config(path: str | Path) -> GatewayConfig:
    parser = configparser.ConfigParser()
    read_files = parser.read(path)
    if not read_files:
        raise SASError(f"config file not found: {path}")

    if not parser.has_section("connection"):
        raise SASError(f"{path}: missing required [connection] section")
    conn = parser["connection"]
    if "port" not in conn:
        raise SASError(f"{path}: [connection] is missing required 'port'")
    if "address" not in conn:
        raise SASError(f"{path}: [connection] is missing required 'address'")

    try:
        address = int(conn["address"])
    except ValueError as e:
        raise SASError(f"{path}: 'address' must be an integer, got {conn['address']!r}") from e

    proto = parser["protocol"] if parser.has_section("protocol") else {}
    machine = parser["machine"] if parser.has_section("machine") else {}
    server = parser["server"] if parser.has_section("server") else {}

    # A MISPLACED reconnect FLAG IS WORSE THAN A MISSING ONE, because the
    # file looks configured and the gateway behaves as though it is not.
    # The operator's first sketch put it under [machine]; accept it there
    # and say so, rather than ignoring it in silence.
    stray = [sec for sec in ("machine", "protocol")
             if parser.has_section(sec) and "reconnect" in parser[sec]]
    try:
        if "reconnect" in conn:
            reconnect = conn.getboolean("reconnect")
        elif stray:
            reconnect = parser[stray[0]].getboolean("reconnect")
            print(f"{path}: 'reconnect' found in [{stray[0]}]; it belongs in "
                  "[connection]. Honouring it, but move it.")
        else:
            reconnect = True
    except ValueError as e:
        raise SASError(
            f"{path}: 'reconnect' must be a boolean (1/0, true/false, yes/no)") from e

    try:
        server_port = int(server["port"]) if server.get("port") else 0
    except ValueError as e:
        raise SASError(f"{path}: [server] 'port' must be an integer, "
                       f"got {server.get('port')!r}") from e

    return GatewayConfig(
        port=conn["port"],
        address=address,
        baudrate=conn.getint("baudrate", DEFAULT_BAUDRATE),
        timeout=conn.getfloat("timeout", DEFAULT_TIMEOUT),
        sas_version=proto.get("sas_version", ""),
        serial_number=machine.get("serial_number", ""),
        game_id=machine.get("game_id", ""),
        reconnect=reconnect,
        server_ip=server.get("ip", ""),
        server_port=server_port,
    )


def save_config(config: GatewayConfig, path: str | Path) -> None:
    parser = configparser.ConfigParser()
    parser["connection"] = {
        "port": config.port,
        "baudrate": str(config.baudrate),
        "address": str(config.address),
        "timeout": str(config.timeout),
        "reconnect": "1" if config.reconnect else "0",
    }
    parser["protocol"] = {"sas_version": config.sas_version}
    if config.serial_number or config.game_id:
        parser["machine"] = {
            "serial_number": config.serial_number,
            "game_id": config.game_id,
        }
    if config.server_ip and config.server_port:
        parser["server"] = {
            "ip": config.server_ip,
            "port": str(config.server_port),
        }
    with open(path, "w") as f:
        parser.write(f)


def connect_from_config(path: str | Path, *, min_poll_interval: float = 0.2):
    """Open the serial port a config file describes and return a ready-to-use
    SASClient. Kept out of client.py to avoid importing pyserial (via
    serial_port.py) anywhere that only needs the protocol logic.

    ``min_poll_interval`` is §2.3.3's floor, passed through to the transport
    where it is enforced. The default is the specification's own 200 ms. See
    SASTransport for why going faster buys nothing and why §12.2's 40 ms
    relaxation is a permission rather than a target.
    """
    from .client import SASClient
    from .serial_port import open_serial_port
    from .transport import SASTransport

    config = load_config(path)
    serial_port = open_serial_port(config.port, baudrate=config.baudrate)
    transport = SASTransport(serial_port, byte_timeout=config.timeout,
                             min_poll_interval=min_poll_interval)
    return SASClient(transport, config.address, timeout=config.timeout)
