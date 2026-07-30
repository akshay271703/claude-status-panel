import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from serial_port import choose_port


class FakePort:
    def __init__(self, device, vid=None, description=""):
        self.device = device
        self.vid = vid
        self.description = description


def test_env_override_wins_outright():
    ports = [FakePort("/dev/ttyUSB0", vid=0x1A86)]
    assert choose_port(ports, override="/dev/ttyACM9") == "/dev/ttyACM9"


def test_picks_the_single_board_by_vendor_id():
    ports = [FakePort("/dev/ttyS0"), FakePort("/dev/ttyUSB0", vid=0x1A86)]
    assert choose_port(ports) == "/dev/ttyUSB0"


def test_windows_and_linux_device_names_both_work():
    assert choose_port([FakePort("COM3", vid=0x2341)]) == "COM3"
    assert choose_port([FakePort("/dev/ttyACM0", vid=0x2341)]) == "/dev/ttyACM0"


def test_falls_back_to_the_only_port_when_vendor_is_unknown():
    assert choose_port([FakePort("/dev/ttyUSB0", vid=0x9999)]) == "/dev/ttyUSB0"


def test_refuses_to_guess_between_several_boards():
    ports = [FakePort("COM3", vid=0x1A86), FakePort("COM4", vid=0x2341)]
    with pytest.raises(RuntimeError, match="STATUS_PANEL_PORT"):
        choose_port(ports)


def test_refuses_to_guess_between_several_unidentified_ports():
    ports = [FakePort("COM1"), FakePort("COM2")]
    with pytest.raises(RuntimeError, match="STATUS_PANEL_PORT"):
        choose_port(ports)


def test_no_ports_mentions_the_linux_permissions_gotcha():
    with pytest.raises(RuntimeError, match="dialout"):
        choose_port([])
