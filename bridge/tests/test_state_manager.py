import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json
from state_manager import StateManager


def test_first_session_gets_module_1():
    sm = StateManager()
    commands = sm.handle_event("session-a", "session_start", claude_pid=111)
    assert commands == ["1:THINKING"]


def test_three_sessions_get_three_distinct_modules():
    sm = StateManager()
    c1 = sm.handle_event("session-a", "session_start", claude_pid=111)
    c2 = sm.handle_event("session-b", "session_start", claude_pid=222)
    c3 = sm.handle_event("session-c", "session_start", claude_pid=333)
    assert c1 == ["1:THINKING"]
    assert c2 == ["2:THINKING"]
    assert c3 == ["3:THINKING"]


def test_fourth_session_is_queued_not_assigned():
    sm = StateManager()
    sm.handle_event("session-a", "session_start", claude_pid=111)
    sm.handle_event("session-b", "session_start", claude_pid=222)
    sm.handle_event("session-c", "session_start", claude_pid=333)
    commands = sm.handle_event("session-d", "session_start", claude_pid=444)
    assert commands == []


def test_repeat_event_same_session_reuses_its_module():
    sm = StateManager()
    sm.handle_event("session-a", "session_start", claude_pid=111)
    commands = sm.handle_event("session-a", "pre_tool_use", claude_pid=111)
    assert commands == ["1:WORKING"]


def test_unrecognized_event_returns_no_commands():
    sm = StateManager()
    commands = sm.handle_event("session-a", "some_future_event", claude_pid=111)
    assert commands == []


def test_session_end_frees_module_and_sends_off_when_queue_empty():
    sm = StateManager()
    sm.handle_event("session-a", "session_start", claude_pid=111)
    commands = sm.handle_event("session-a", "session_end", claude_pid=111)
    assert commands == ["1:OFF"]


def test_session_end_assigns_freed_module_to_next_queued_session():
    sm = StateManager()
    sm.handle_event("session-a", "session_start", claude_pid=111)
    sm.handle_event("session-b", "session_start", claude_pid=222)
    sm.handle_event("session-c", "session_start", claude_pid=333)
    sm.handle_event("session-d", "session_start", claude_pid=444)  # queued
    sm.handle_event("session-d", "pre_tool_use", claude_pid=444)   # still queued, updates its remembered state
    commands = sm.handle_event("session-a", "session_end", claude_pid=111)
    assert commands == ["1:WORKING"]  # session-d dequeued onto module 1, with its last known state


def test_session_end_for_unknown_session_is_a_no_op():
    sm = StateManager()
    commands = sm.handle_event("never-seen", "session_end", claude_pid=999)
    assert commands == []


def test_queued_session_receiving_second_event_does_not_duplicate_queue_entry():
    sm = StateManager()
    sm.handle_event("session-a", "session_start", claude_pid=111)
    sm.handle_event("session-b", "session_start", claude_pid=222)
    sm.handle_event("session-c", "session_start", claude_pid=333)
    sm.handle_event("session-d", "session_start", claude_pid=444)  # queued
    sm.handle_event("session-d", "pre_tool_use", claude_pid=444)   # still queued, must NOT duplicate
    sm.handle_event("session-a", "session_end", claude_pid=111)  # frees module 1 -> session-d
    commands = sm.handle_event("session-b", "session_end", claude_pid=222)  # frees module 2
    # module 2 should go OFF (no one left queued) -- if the duplication bug were present,
    # session-d would incorrectly be reassigned to module 2 as well
    assert commands == ["2:OFF"]


def test_queued_session_can_end_before_being_dequeued():
    sm = StateManager()
    sm.handle_event("session-a", "session_start", claude_pid=111)
    sm.handle_event("session-b", "session_start", claude_pid=222)
    sm.handle_event("session-c", "session_start", claude_pid=333)
    sm.handle_event("session-d", "session_start", claude_pid=444)  # queued
    commands = sm.handle_event("session-d", "session_end", claude_pid=444)  # ends while still queued
    assert commands == []
    # module 1 should still be free for session-a's eventual replacement, not haunted by session-d
    commands2 = sm.handle_event("session-a", "session_end", claude_pid=111)
    assert commands2 == ["1:OFF"]


def test_check_liveness_releases_module_for_dead_pid():
    dead_pids = {111}
    sm = StateManager(liveness_check=lambda pid: pid not in dead_pids)
    sm.handle_event("session-a", "session_start", claude_pid=111)
    commands = sm.check_liveness()
    assert commands == ["1:OFF"]


def test_check_liveness_leaves_alive_sessions_untouched():
    sm = StateManager(liveness_check=lambda pid: True)
    sm.handle_event("session-a", "session_start", claude_pid=111)
    commands = sm.check_liveness()
    assert commands == []


def test_load_and_recover_repaints_live_sessions(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "session-a": {"module": 1, "last_known_state": "WORKING", "claude_pid": 111},
    }))
    sm = StateManager(persistence_path=path, liveness_check=lambda pid: True)
    commands = sm.load_and_recover()
    assert commands == ["1:WORKING"]


def test_load_and_recover_drops_dead_sessions(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "session-a": {"module": 1, "last_known_state": "WORKING", "claude_pid": 111},
    }))
    sm = StateManager(persistence_path=path, liveness_check=lambda pid: False)
    commands = sm.load_and_recover()
    assert commands == []


def test_load_and_recover_with_no_file_returns_no_commands(tmp_path):
    path = tmp_path / "does_not_exist.json"
    sm = StateManager(persistence_path=path, liveness_check=lambda pid: True)
    commands = sm.load_and_recover()
    assert commands == []


def test_recovered_session_frees_its_module_normally_afterward(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "session-a": {"module": 2, "last_known_state": "NEED_INPUT", "claude_pid": 111},
    }))
    sm = StateManager(persistence_path=path, liveness_check=lambda pid: True)
    sm.load_and_recover()
    commands = sm.handle_event("session-a", "session_end", claude_pid=111)
    assert commands == ["2:OFF"]


def test_check_liveness_survives_a_raising_liveness_check():
    # A liveness check that raises must not propagate: in bridge.py this runs
    # on the background poll thread, and an escaping exception retires that
    # thread permanently and silently.
    def boom(pid):
        raise TypeError("bad pid")

    sm = StateManager(liveness_check=boom)
    sm.handle_event("session-a", "session_start", claude_pid=111)
    assert sm.check_liveness() == []
    # the session must survive -- an unreadable liveness answer is not proof of death
    assert sm.handle_event("session-a", "pre_tool_use", claude_pid=111) == ["1:WORKING"]


def test_load_and_recover_discards_corrupt_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"broken": ')  # truncated mid-write, e.g. Ctrl+C during _persist
    sm = StateManager(persistence_path=path, liveness_check=lambda pid: True)
    assert sm.load_and_recover() == []


def test_load_and_recover_skips_malformed_entries(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "good": {"module": 1, "last_known_state": "WORKING", "claude_pid": 111},
        "missing-keys": {"module": 2},
        "bad-module": {"module": 99, "last_known_state": "IDLE", "claude_pid": 222},
    }))
    sm = StateManager(persistence_path=path, liveness_check=lambda pid: True)
    assert sm.load_and_recover() == ["1:WORKING"]


def test_persist_is_atomic_leaving_no_partial_file(tmp_path):
    path = tmp_path / "state.json"
    sm = StateManager(persistence_path=path, liveness_check=lambda pid: True)
    sm.handle_event("session-a", "session_start", claude_pid=111)
    # whatever is on disk must always be complete, parseable JSON
    assert json.loads(path.read_text())["session-a"]["module"] == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_current_commands_repaints_all_assigned_modules():
    sm = StateManager()
    sm.handle_event("session-a", "session_start", claude_pid=111)
    sm.handle_event("session-b", "pre_tool_use", claude_pid=222)
    assert sorted(sm.current_commands()) == ["1:THINKING", "2:WORKING"]


def test_current_commands_empty_when_nothing_assigned():
    assert StateManager().current_commands() == []


def test_snapshot_reports_all_three_modules_with_unclaimed_as_off():
    sm = StateManager()
    snap = sm.snapshot()
    assert [m["module"] for m in snap["modules"]] == [1, 2, 3]
    assert all(m["state"] == "OFF" and m["session_id"] is None for m in snap["modules"])
    assert snap["queue"] == []


def test_snapshot_reports_state_and_elapsed_for_claimed_module():
    clock = [1000.0]
    sm = StateManager(time_source=lambda: clock[0])
    sm.handle_event("session-a", "pre_tool_use", claude_pid=111)
    clock[0] = 1012.5
    snap = sm.snapshot()
    m1 = snap["modules"][0]
    assert m1["session_id"] == "session-a"
    assert m1["state"] == "WORKING"
    assert m1["state_seconds"] == 12.5
    assert m1["claude_pid"] == 111


def test_repeated_same_state_does_not_reset_the_clock():
    clock = [1000.0]
    sm = StateManager(time_source=lambda: clock[0])
    sm.handle_event("session-a", "pre_tool_use", claude_pid=111)
    clock[0] = 1005.0
    sm.handle_event("session-a", "pre_tool_use", claude_pid=111)  # same state again
    clock[0] = 1010.0
    assert sm.snapshot()["modules"][0]["state_seconds"] == 10.0


def test_changing_state_does_reset_the_clock():
    clock = [1000.0]
    sm = StateManager(time_source=lambda: clock[0])
    sm.handle_event("session-a", "pre_tool_use", claude_pid=111)
    clock[0] = 1005.0
    sm.handle_event("session-a", "stop", claude_pid=111)  # WORKING -> NEED_INPUT
    clock[0] = 1008.0
    m1 = sm.snapshot()["modules"][0]
    assert m1["state"] == "NEED_INPUT"
    assert m1["state_seconds"] == 3.0


def test_snapshot_lists_queued_sessions():
    sm = StateManager()
    for i, sid in enumerate(["a", "b", "c", "d"]):
        sm.handle_event(sid, "session_start", claude_pid=100 + i)
    snap = sm.snapshot()
    assert [q["session_id"] for q in snap["queue"]] == ["d"]
    assert snap["queue"][0]["claude_pid"] == 103


def test_released_session_clears_its_clock():
    sm = StateManager()
    sm.handle_event("session-a", "session_start", claude_pid=111)
    sm.handle_event("session-a", "session_end", claude_pid=111)
    assert sm.snapshot()["modules"][0]["state_seconds"] is None
