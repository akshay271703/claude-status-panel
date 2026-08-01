import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json
from state_manager import StateManager, MODULE_NUMBERS


def fill_slots(sm, n, start_pid=100):
    """Claim n slots with session_start events, session ids sess-0..sess-{n-1}.

    The pool is 16 wide, so exercising the queue path by hand (as the old
    3-slot tests did) would mean writing out 17 session ids per test. This
    is the shared way to get there economically.
    """
    return [sm.handle_event(f"sess-{i}", "session_start", claude_pid=start_pid + i) for i in range(n)]


def test_first_session_gets_module_1():
    sm = StateManager()
    commands = sm.handle_event("session-a", "session_start", claude_pid=111)
    assert commands == ["1:WORKING"]


def test_three_sessions_get_three_distinct_modules():
    sm = StateManager()
    c1 = sm.handle_event("session-a", "session_start", claude_pid=111)
    c2 = sm.handle_event("session-b", "session_start", claude_pid=222)
    c3 = sm.handle_event("session-c", "session_start", claude_pid=333)
    assert c1 == ["1:WORKING"]
    assert c2 == ["2:WORKING"]
    assert c3 == ["3:WORKING"]


def test_seventeenth_claimant_is_queued_not_assigned():
    sm = StateManager()
    fill_slots(sm, len(MODULE_NUMBERS))
    commands = sm.handle_event("sess-overflow", "session_start", claude_pid=999)
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
    fill_slots(sm, len(MODULE_NUMBERS))
    sm.handle_event("sess-overflow", "session_start", claude_pid=999)  # queued
    sm.handle_event("sess-overflow", "pre_tool_use", claude_pid=999)   # still queued, updates its remembered state
    commands = sm.handle_event("sess-0", "session_end", claude_pid=100)
    assert commands == ["1:WORKING"]  # sess-overflow dequeued onto module 1, with its last known state


def test_session_end_for_unknown_session_is_a_no_op():
    sm = StateManager()
    commands = sm.handle_event("never-seen", "session_end", claude_pid=999)
    assert commands == []


def test_queued_session_receiving_second_event_does_not_duplicate_queue_entry():
    sm = StateManager()
    fill_slots(sm, len(MODULE_NUMBERS))
    sm.handle_event("sess-overflow", "session_start", claude_pid=999)  # queued
    sm.handle_event("sess-overflow", "pre_tool_use", claude_pid=999)   # still queued, must NOT duplicate
    sm.handle_event("sess-0", "session_end", claude_pid=100)  # frees module 1 -> sess-overflow
    commands = sm.handle_event("sess-1", "session_end", claude_pid=101)  # frees module 2
    # module 2 should go OFF (no one left queued) -- if the duplication bug were present,
    # sess-overflow would incorrectly be reassigned to module 2 as well
    assert commands == ["2:OFF"]


def test_queued_session_can_end_before_being_dequeued():
    sm = StateManager()
    fill_slots(sm, len(MODULE_NUMBERS))
    commands = sm.handle_event("sess-overflow", "session_end", claude_pid=999)  # ends while still queued
    assert commands == []
    # module 1 should still be free for sess-0's eventual replacement, not haunted by sess-overflow
    commands2 = sm.handle_event("sess-0", "session_end", claude_pid=100)
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
        "session-a": {"module": 2, "last_known_state": "IDLE", "claude_pid": 111},
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
    assert sorted(sm.current_commands()) == ["1:WORKING", "2:WORKING"]


def test_current_commands_empty_when_nothing_assigned():
    assert StateManager().current_commands() == []


def test_snapshot_reports_all_sixteen_modules_with_unclaimed_as_off():
    sm = StateManager()
    snap = sm.snapshot()
    assert [m["module"] for m in snap["modules"]] == list(range(1, 17))
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
    sm.handle_event("session-a", "stop", claude_pid=111)  # WORKING -> IDLE
    clock[0] = 1008.0
    m1 = sm.snapshot()["modules"][0]
    assert m1["state"] == "IDLE"
    assert m1["state_seconds"] == 3.0


def test_snapshot_lists_queued_sessions():
    sm = StateManager()
    fill_slots(sm, len(MODULE_NUMBERS))
    sm.handle_event("sess-overflow", "session_start", claude_pid=999)
    snap = sm.snapshot()
    assert [q["session_id"] for q in snap["queue"]] == ["sess-overflow"]
    assert snap["queue"][0]["claude_pid"] == 999


def test_released_session_clears_its_clock():
    sm = StateManager()
    sm.handle_event("session-a", "session_start", claude_pid=111)
    sm.handle_event("session-a", "session_end", claude_pid=111)
    assert sm.snapshot()["modules"][0]["state_seconds"] is None


# --- New in v2: Task dispatch (DISPATCHED/purple) ---

def test_pre_tool_use_for_task_tool_sets_dispatched():
    sm = StateManager()
    sm.handle_event("session-a", "session_start", claude_pid=111)
    commands = sm.handle_event("session-a", "pre_tool_use", claude_pid=111, tool_name="Task")
    assert commands == ["1:DISPATCHED"]


def test_pre_tool_use_for_other_tools_sets_working():
    sm = StateManager()
    sm.handle_event("session-a", "session_start", claude_pid=111)
    commands = sm.handle_event("session-a", "pre_tool_use", claude_pid=111, tool_name="Bash")
    assert commands == ["1:WORKING"]


def test_pre_tool_use_with_no_tool_name_defaults_to_working():
    sm = StateManager()
    sm.handle_event("session-a", "session_start", claude_pid=111)
    commands = sm.handle_event("session-a", "pre_tool_use", claude_pid=111)
    assert commands == ["1:WORKING"]


# --- New in v2: subagents claim/release their own slot ---

def test_subagent_start_claims_a_distinct_slot_from_its_parent():
    sm = StateManager()
    sm.handle_event("session-a", "session_start", claude_pid=111)  # module 1
    commands = sm.handle_event("session-a", "subagent_start", claude_pid=111, agent_id="agent-1")
    assert commands == ["2:RUNNING"]


def test_subagent_stop_releases_its_slot_immediately():
    sm = StateManager()
    sm.handle_event("session-a", "subagent_start", claude_pid=111, agent_id="agent-1")
    commands = sm.handle_event("session-a", "subagent_stop", claude_pid=111, agent_id="agent-1")
    assert commands == ["1:OFF"]


def test_subagent_stop_for_unknown_agent_is_a_no_op():
    sm = StateManager()
    commands = sm.handle_event("session-a", "subagent_stop", claude_pid=111, agent_id="never-started")
    assert commands == []


def test_two_subagents_of_the_same_session_get_distinct_slots():
    sm = StateManager()
    c1 = sm.handle_event("session-a", "subagent_start", claude_pid=111, agent_id="agent-1")
    c2 = sm.handle_event("session-a", "subagent_start", claude_pid=111, agent_id="agent-2")
    assert c1 == ["1:RUNNING"]
    assert c2 == ["2:RUNNING"]


def test_dead_parent_pid_frees_its_subagents_slot_too():
    dead_pids = {111}
    sm = StateManager(liveness_check=lambda pid: pid not in dead_pids)
    sm.handle_event("session-a", "subagent_start", claude_pid=111, agent_id="agent-1")
    commands = sm.check_liveness()
    assert commands == ["1:OFF"]


# --- New in v2: agent_id is a noise guard for everything except subagent lifecycle ---

def test_pre_tool_use_carrying_agent_id_does_not_touch_the_parent_slot():
    sm = StateManager()
    sm.handle_event("session-a", "session_start", claude_pid=111)  # module 1, WORKING
    sm.handle_event("session-a", "pre_tool_use", claude_pid=111, tool_name="Task")  # module 1, DISPATCHED
    # A subagent's own internal tool call carries the parent's session_id
    # too, but is tagged with agent_id -- it must not flip the parent back
    # to WORKING, or "purple while waiting on subagents" could never hold.
    commands = sm.handle_event(
        "session-a", "pre_tool_use", claude_pid=111, tool_name="Bash", agent_id="agent-1"
    )
    assert commands == []
    assert sm.snapshot()["modules"][0]["state"] == "DISPATCHED"


def test_stop_carrying_agent_id_is_ignored():
    sm = StateManager()
    sm.handle_event("session-a", "session_start", claude_pid=111)
    commands = sm.handle_event("session-a", "stop", claude_pid=111, agent_id="agent-1")
    assert commands == []
    assert sm.snapshot()["modules"][0]["state"] == "WORKING"
