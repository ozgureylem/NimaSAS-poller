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

    def __post_init__(self) -> None:
        if not 1 <= self.address <= 127:
            raise SASError(f"address must be 1-127, got {self.address}")
        if self.baudrate <= 0:
            raise SASError(f"baudrate must be positive, got {self.baudrate}")
        if self.timeout <= 0:
            raise SASError(f"timeout must be positive, got {self.timeout}")


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

    return GatewayConfig(
        port=conn["port"],
        address=address,
        baudrate=conn.getint("baudrate", DEFAULT_BAUDRATE),
        timeout=conn.getfloat("timeout", DEFAULT_TIMEOUT),
        sas_version=proto.get("sas_version", ""),
        serial_number=machine.get("serial_number", ""),
        game_id=machine.get("game_id", ""),
    )


def save_config(config: GatewayConfig, path: str | Path) -> None:
    parser = configparser.ConfigParser()
    parser["connection"] = {
        "port": config.port,
        "baudrate": str(config.baudrate),
        "address": str(config.address),
        "timeout": str(config.timeout),
    }
    parser["protocol"] = {"sas_version": config.sas_version}
    if config.serial_number or config.game_id:
        parser["machine"] = {
            "serial_number": config.serial_number,
            "game_id": config.game_id,
        }
    with open(path, "w") as f:
        parser.write(f)


def connect_from_config(path: str | Path):
    """Open the serial port a config file describes and return a ready-to-use
    SASClient. Kept out of client.py to avoid importing pyserial (via
    serial_port.py) anywhere that only needs the protocol logic.
    """
    from .client import SASClient
    from .serial_port import open_serial_port
    from .transport import SASTransport

    config = load_config(path)
    serial_port = open_serial_port(config.port, baudrate=config.baudrate)
    transport = SASTransport(serial_port, byte_timeout=config.timeout)
    return SASClient(transport, config.address, timeout=config.timeout)
