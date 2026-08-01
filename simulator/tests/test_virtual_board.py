import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from virtual_board import VirtualBoard, NUM_SLOTS


def test_ping_answers_ok():
    board = VirtualBoard()
    assert board.handle_line("PING") == "OK PING"


def test_valid_state_command_is_accepted():
    board = VirtualBoard()
    assert board.handle_line("1:WORKING") == "OK 1:WORKING"
    assert board.slots[0] == "WORKING"


def test_slot_out_of_range_is_rejected():
    board = VirtualBoard()
    assert board.handle_line("17:WORKING") == "ERR: 17:WORKING"
    assert board.handle_line("0:WORKING") == "ERR: 0:WORKING"


def test_unknown_state_is_rejected():
    board = VirtualBoard()
    assert board.handle_line("1:SLEEPING") == "ERR: 1:SLEEPING"


def test_missing_colon_is_rejected():
    board = VirtualBoard()
    assert board.handle_line("nonsense") == "ERR: nonsense"


def test_malformed_slot_number_is_rejected():
    board = VirtualBoard()
    assert board.handle_line("abc:WORKING") == "ERR: abc:WORKING"


def test_overlong_line_is_rejected():
    board = VirtualBoard()
    line = "1:" + "W" * 40
    assert board.handle_line(line) == "ERR: <line too long>"


def test_dim_sets_brightness_and_clamps():
    board = VirtualBoard()
    assert board.handle_line("DIM:40") == "OK DIM:40"
    assert board.brightness_pct == 40
    board.handle_line("DIM:500")
    assert board.brightness_pct == 100
    board.handle_line("DIM:-5")
    assert board.brightness_pct == 0


def test_dim_with_non_numeric_value_is_rejected():
    board = VirtualBoard()
    assert board.handle_line("DIM:bright") == "ERR: DIM:bright"


def test_status_line_reports_packed_ring_and_version():
    board = VirtualBoard()
    board.handle_line("1:WORKING")
    board.handle_line("2:BLOCKED")
    reply = board.handle_line("STATUS")
    assert reply.startswith("STATUS ")
    assert "ver=2" in reply
    assert f"ring=WB{'O' * (NUM_SLOTS - 2)}" in reply


def test_setting_a_slot_to_its_current_state_is_harmless():
    board = VirtualBoard()
    board.handle_line("1:WORKING")
    assert board.handle_line("1:WORKING") == "OK 1:WORKING"
    assert board.slots[0] == "WORKING"


def test_board_starts_stale():
    # Mirrors docs/decisions.md#the-board-starts-stale: a board with no host
    # attached yet must never look falsely healthy.
    clock = [1000.0]
    board = VirtualBoard(time_source=lambda: clock[0])
    assert board.stale is True


def test_any_parsed_line_clears_staleness():
    clock = [1000.0]
    board = VirtualBoard(time_source=lambda: clock[0])
    assert board.stale is True
    board.handle_line("PING")
    assert board.stale is False


def test_becomes_stale_again_after_timeout():
    clock = [1000.0]
    board = VirtualBoard(time_source=lambda: clock[0])
    board.handle_line("PING")
    assert board.stale is False
    clock[0] = 1000.0 + 10.1
    assert board.stale is True


def test_blocked_does_not_alarm_before_the_timeout():
    clock = [1000.0]
    board = VirtualBoard(time_source=lambda: clock[0])
    board.handle_line("1:BLOCKED")
    clock[0] = 1004.9
    assert board.buzzing is False


def test_blocked_alarms_after_the_timeout():
    clock = [1000.0]
    board = VirtualBoard(time_source=lambda: clock[0])
    board.handle_line("1:BLOCKED")
    clock[0] = 1005.1
    assert board.buzzing is True


def test_buzzing_is_continuous_not_gated_by_chirp_phase():
    # buzz= on STATUS mirrors the firmware's buzzerActive (set for the whole
    # alarming duration) -- NOT the ~150ms-every-3s audible chirp window. A
    # regression here would make the dashboard's buzzer indicator flicker
    # off almost all the time instead of staying lit while alarming.
    clock = [1000.0]
    board = VirtualBoard(time_source=lambda: clock[0])
    board.handle_line("1:BLOCKED")
    clock[0] = 1005.1  # just past the timeout, likely NOT in a chirp window
    assert board.chirp_phase is False
    assert board.buzzing is True


def test_repeated_blocked_does_not_restart_the_alarm_timer():
    clock = [1000.0]
    board = VirtualBoard(time_source=lambda: clock[0])
    board.handle_line("1:BLOCKED")
    clock[0] = 1003.0
    board.handle_line("1:BLOCKED")  # already BLOCKED -- must not reset blockedSince
    clock[0] = 1005.1
    assert board.buzzing is True


def test_transitioning_through_another_state_does_restart_the_alarm_timer():
    clock = [1000.0]
    board = VirtualBoard(time_source=lambda: clock[0])
    board.handle_line("1:BLOCKED")
    clock[0] = 1004.0
    board.handle_line("1:IDLE")
    board.handle_line("1:BLOCKED")  # fresh transition into BLOCKED
    clock[0] = 1004.0 + 4.9
    assert board.buzzing is False


def test_silence_button_stops_an_alarming_slot():
    clock = [1000.0]
    board = VirtualBoard(time_source=lambda: clock[0])
    board.handle_line("1:BLOCKED")
    clock[0] = 1005.1
    assert board.buzzing is True
    board.press_button()
    assert board.buzzing is False


def test_silence_re_arms_only_on_a_fresh_blocked_transition():
    clock = [1000.0]
    board = VirtualBoard(time_source=lambda: clock[0])
    board.handle_line("1:BLOCKED")
    clock[0] = 1005.1
    board.press_button()
    assert board.buzzing is False
    board.handle_line("1:BLOCKED")  # still BLOCKED, resending -- must stay silenced
    assert board.buzzing is False
    board.handle_line("1:IDLE")
    board.handle_line("1:BLOCKED")  # fresh transition -- re-arms
    clock[0] = 1005.1 + 5.1
    assert board.buzzing is True


def test_silence_button_ignored_while_stale():
    # "A press is ignored entirely while contact is stale, since the user
    # cannot see or hear what they would be silencing" (docs/protocol.md).
    clock = [1000.0]
    board = VirtualBoard(time_source=lambda: clock[0])
    board.handle_line("1:BLOCKED")   # contact at t=1000
    clock[0] = 1010.1                # >10s since last contact -- now stale
    board.press_button()             # must be a no-op
    board.handle_line("PING")        # contact resumes, no longer stale
    clock[0] = 1010.1 + 0.1
    assert board.buzzing is True     # never actually silenced


def test_buzzer_is_silent_while_stale_even_if_alarming():
    clock = [1000.0]
    board = VirtualBoard(time_source=lambda: clock[0])
    board.handle_line("1:BLOCKED")
    clock[0] = 1005.1 + 10.1
    assert board.buzzing is False


def test_two_slots_alarm_independently():
    clock = [1000.0]
    board = VirtualBoard(time_source=lambda: clock[0])
    board.handle_line("1:BLOCKED")
    clock[0] = 1002.0
    board.handle_line("2:BLOCKED")
    clock[0] = 1005.1  # slot 1 past timeout, slot 2 not yet
    assert board.buzzing is True
    board.press_button()  # silences only slot 1
    assert board.buzzing is False
    clock[0] = 1007.1  # slot 2 now past its own timeout
    assert board.buzzing is True
