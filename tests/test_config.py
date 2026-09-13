import pytest

from saspy.config import GatewayConfig, load_config, save_config
from saspy.exceptions import SASError


def test_round_trip(tmp_path):
    original = GatewayConfig(
        port="/dev/ttyUSB0",
        address=1,
        baudrate=19200,
        timeout=1.5,
        sas_version="602",
        serial_number="TROP-0042",
        game_id="01",
    )
    path = tmp_path / "gateway.ini"
    save_config(original, path)
    loaded = load_config(path)
    assert loaded == original


def test_defaults(tmp_path):
    path = tmp_path / "gateway.ini"
    path.write_text("[connection]\nport = /dev/ttyUSB0\naddress = 5\n")
    config = load_config(path)
    assert config.port == "/dev/ttyUSB0"
    assert config.address == 5
    assert config.baudrate == 19200
    assert config.timeout == 1.0
    assert config.sas_version == ""


def test_missing_file_raises():
    with pytest.raises(SASError):
        load_config("/nonexistent/gateway.ini")


def test_missing_connection_section_raises(tmp_path):
    path = tmp_path / "gateway.ini"
    path.write_text("[protocol]\nsas_version = 602\n")
    with pytest.raises(SASError):
        load_config(path)


def test_missing_port_raises(tmp_path):
    path = tmp_path / "gateway.ini"
    path.write_text("[connection]\naddress = 1\n")
    with pytest.raises(SASError):
        load_config(path)


def test_missing_address_raises(tmp_path):
    path = tmp_path / "gateway.ini"
    path.write_text("[connection]\nport = /dev/ttyUSB0\n")
    with pytest.raises(SASError):
        load_config(path)


def test_invalid_address_raises(tmp_path):
    path = tmp_path / "gateway.ini"
    path.write_text("[connection]\nport = /dev/ttyUSB0\naddress = not-a-number\n")
    with pytest.raises(SASError):
        load_config(path)


def test_address_out_of_range_rejected():
    with pytest.raises(SASError):
        GatewayConfig(port="/dev/ttyUSB0", address=128)
    with pytest.raises(SASError):
        GatewayConfig(port="/dev/ttyUSB0", address=0)


def test_save_omits_machine_section_when_empty(tmp_path):
    config = GatewayConfig(port="/dev/ttyUSB0", address=1)
    path = tmp_path / "gateway.ini"
    save_config(config, path)
    assert "[machine]" not in path.read_text()
