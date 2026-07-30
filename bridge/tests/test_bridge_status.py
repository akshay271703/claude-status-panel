import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from bridge import parse_status


def test_parses_a_well_formed_status_line():
    line = "STATUS up=15897 dim=5 stale=0 buzz=1 ram=1561 m1=OFF m2=NEED_INPUT m3=WORKING"
    hw = parse_status(line)
    assert hw["uptime_seconds"] == 15.9
    assert hw["brightness_pct"] == 5
    assert hw["stale"] is False
    assert hw["buzzer"] is True
    assert hw["free_ram_bytes"] == 1561
    assert hw["modules"] == ["OFF", "NEED_INPUT", "WORKING"]


def test_stale_flag_is_read_as_a_boolean():
    hw = parse_status("STATUS up=1 dim=5 stale=1 buzz=0 ram=100 m1=OFF m2=OFF m3=OFF")
    assert hw["stale"] is True
    assert hw["buzzer"] is False


def test_unparseable_lines_return_none_rather_than_raising():
    # Old firmware answering ERR, a truncated line, or plain garbage must all
    # degrade to "no telemetry" instead of breaking the dashboard.
    assert parse_status("ERR: STATUS") is None
    assert parse_status("STATUS up=notanumber dim=5 stale=0 buzz=0 ram=1 m1=OFF") is None
    assert parse_status("STATUS up=1") is None
    assert parse_status("") is None
