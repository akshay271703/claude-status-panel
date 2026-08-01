import json
import os
import time
from pathlib import Path

# Events whose meaning doesn't depend on any other payload field.
# `pre_tool_use` and the subagent lifecycle events are handled specially in
# handle_event() -- see there for why.
EVENT_STATE_MAP = {
    "session_start": "WORKING",
    "user_prompt_submit": "WORKING",
    "notification": "BLOCKED",
    "stop": "IDLE",
}

MODULE_NUMBERS = tuple(range(1, 17))

# Valid firmware states, used to validate anything read back off disk.
STATES = frozenset(EVENT_STATE_MAP.values()) | {"WORKING", "DISPATCHED", "BLOCKED", "IDLE", "RUNNING", "OFF"}

DEFAULT_STATE = "WORKING"

# A subagent's slot is keyed separately from its parent session_id so both can
# hold a module at once. The parent's own session_id never contains "#", so
# this can't collide with a real session_id.
_AGENT_MARKER = "#agent:"


def _agent_key(session_id, agent_id):
    return f"{session_id}{_AGENT_MARKER}{agent_id}"


def is_agent_key(key):
    """True for a synthetic subagent claimant key, not a real session_id.

    Used by callers (bridge.py's usage poll) that need to tell a subagent's
    slot apart from a real Claude Code session -- a subagent has no
    transcript of its own to poll by this key, only its parent does.
    """
    return _AGENT_MARKER in key


class StateManager:
    def __init__(self, persistence_path=None, liveness_check=None, time_source=None):
        self._persistence_path = Path(persistence_path) if persistence_path else None
        self._liveness_check = liveness_check
        self._now = time_source or time.time
        self._module_owner = {}   # module number -> claimant key (session_id or "<session_id>#agent:<id>")
        self._session_module = {}  # claimant key -> module number
        self._session_state = {}   # claimant key -> last state string sent
        self._session_pid = {}     # claimant key -> claude_pid
        self._session_since = {}   # claimant key -> when its CURRENT state began
        self._queue = []           # FIFO of waiting claimant keys

    def handle_event(self, session_id, event, claude_pid, tool_name=None, agent_id=None):
        # PreToolUse/PostToolUse fire for tool calls made *inside* a dispatched
        # subagent too, carrying the *parent's* session_id -- these are tagged
        # with agent_id so they can be told apart from the top-level session's
        # own activity. Anything but a genuine subagent lifecycle event that
        # carries an agent_id is subagent-internal noise and must not touch
        # either slot: the top-level session's state didn't change, and the
        # subagent's own slot is driven only by subagent_start/subagent_stop.
        if agent_id is not None and event not in ("subagent_start", "subagent_stop"):
            return []

        if event == "session_end":
            return self._release_module(session_id)
        if event == "subagent_start":
            return self._apply(_agent_key(session_id, agent_id), "RUNNING", claude_pid)
        if event == "subagent_stop":
            return self._release_module(_agent_key(session_id, agent_id))

        if event == "pre_tool_use":
            # A Task dispatch (fanning out a subagent) is visually distinct
            # from any other direct tool call -- see docs/decisions.md.
            state = "DISPATCHED" if tool_name == "Task" else "WORKING"
        else:
            state = EVENT_STATE_MAP.get(event)
            if state is None:
                return []

        return self._apply(session_id, state, claude_pid)

    def _apply(self, key, state, claude_pid):
        self._session_pid[key] = claude_pid
        # Only restart the clock on a real state change, so "how long has this
        # been WORKING" survives the repeated events of a busy session.
        if self._session_state.get(key) != state:
            self._session_since[key] = self._now()
        self._session_state[key] = state

        if key in self._session_module:
            # Already assigned to a module
            module = self._session_module[key]
            self._persist()
            return [f"{module}:{state}"]

        if key in self._queue:
            # Already queued; just remember its latest state for when it's dequeued
            self._persist()
            return []

        # Brand new claimant; try to assign it
        module = self._assign_module(key)
        self._persist()
        if module is None:
            return []
        return [f"{module}:{state}"]

    def _assign_module(self, key):
        for m in MODULE_NUMBERS:
            if m not in self._module_owner:
                self._module_owner[m] = key
                self._session_module[key] = m
                return m
        self._queue.append(key)
        return None

    def _release_module(self, key):
        module = self._session_module.pop(key, None)
        self._session_state.pop(key, None)
        self._session_pid.pop(key, None)
        self._session_since.pop(key, None)
        if key in self._queue:
            self._queue.remove(key)

        if module is None:
            self._persist()
            return []

        del self._module_owner[module]

        if self._queue:
            next_key = self._queue.pop(0)
            self._module_owner[module] = next_key
            self._session_module[next_key] = module
            state = self._session_state.get(next_key, DEFAULT_STATE)
            self._persist()
            return [f"{module}:{state}"]

        self._persist()
        return [f"{module}:OFF"]

    def current_commands(self):
        """Repaint commands for every currently-assigned module.

        Used after a serial reconnect: events kept updating state while the
        board was unreachable, so the panel needs resyncing to match.
        """
        return [
            f"{module}:{self._session_state.get(key, DEFAULT_STATE)}"
            for key, module in self._session_module.items()
        ]

    def _is_alive(self, pid):
        """Liveness with a safe default.

        If the check itself fails we assume the session is still alive: an
        unreadable answer is not proof of death, and wrongly releasing a live
        session's module is worse than briefly holding a dead one's. Crucially
        this must never raise -- check_liveness runs on a background thread
        whose death would silently disable the whole fallback mechanism.
        """
        if self._liveness_check is None:
            return True
        try:
            return self._liveness_check(pid)
        except Exception:
            return True

    def check_liveness(self):
        """Release any claimant (session or subagent) whose process died.

        Subagent claimant keys store the *parent's* claude_pid, so a dead
        parent frees its subagents' slots here too, with no extra code.
        """
        commands = []
        for key, pid in list(self._session_pid.items()):
            if not self._is_alive(pid):
                commands.extend(self._release_module(key))
        return commands

    def load_and_recover(self):
        """Repaint modules for sessions that outlived a bridge restart.

        A corrupt or partially-written file is discarded rather than raised:
        losing recovery is a brief visual blip, but a bridge that refuses to
        start needs manual file deletion to fix.
        """
        if not self._persistence_path or not self._persistence_path.exists():
            return []
        try:
            data = json.loads(self._persistence_path.read_text())
            if not isinstance(data, dict):
                raise ValueError("persisted state is not an object")
        except Exception as e:
            print(f"Ignoring unreadable state file {self._persistence_path}: {e}")
            return []

        commands = []
        for key, entry in data.items():
            if not isinstance(entry, dict):
                continue
            pid = entry.get("claude_pid")
            module = entry.get("module")
            state = entry.get("last_known_state")
            if module not in MODULE_NUMBERS or state not in STATES:
                continue
            if module in self._module_owner:
                continue
            if not self._is_alive(pid):
                continue
            self._module_owner[module] = key
            self._session_module[key] = module
            self._session_state[key] = state
            self._session_pid[key] = pid
            # The original transition time didn't survive the restart, so the
            # clock restarts here rather than claiming a duration we don't know.
            self._session_since[key] = self._now()
            commands.append(f"{module}:{state}")
        self._persist()
        return commands

    def snapshot(self):
        """Read-only view of current state, for the dashboard.

        Pure data only -- no I/O and no name resolution, so this stays unit
        testable. bridge.py enriches each entry with a project name.
        """
        now = self._now()
        modules = []
        for module in MODULE_NUMBERS:
            key = self._module_owner.get(module)
            if key is None:
                modules.append({
                    "module": module,
                    "session_id": None,
                    "state": "OFF",
                    "state_seconds": None,
                    "claude_pid": None,
                })
                continue
            since = self._session_since.get(key)
            modules.append({
                "module": module,
                "session_id": key,
                "state": self._session_state.get(key, DEFAULT_STATE),
                "state_seconds": None if since is None else round(now - since, 1),
                "claude_pid": self._session_pid.get(key),
            })
        return {
            "modules": modules,
            "queue": [
                {"session_id": key, "claude_pid": self._session_pid.get(key)}
                for key in self._queue
            ],
        }

    def _persist(self):
        if not self._persistence_path:
            return
        data = {
            key: {
                "module": self._session_module[key],
                "last_known_state": self._session_state.get(key, DEFAULT_STATE),
                "claude_pid": self._session_pid.get(key),
            }
            for key in self._session_module
        }
        # Write-then-replace: _persist runs on every event, and the documented
        # way to stop the bridge is Ctrl+C, so a truncate-in-place write can
        # leave half a file behind and wedge the next startup.
        tmp = self._persistence_path.with_suffix(self._persistence_path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self._persistence_path)
        except Exception as e:
            print(f"Could not persist state to {self._persistence_path}: {e}")
            try:
                tmp.unlink()
            except OSError:
                pass
