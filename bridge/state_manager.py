import json
import os
import time
from pathlib import Path

EVENT_STATE_MAP = {
    "session_start": "THINKING",
    "user_prompt_submit": "THINKING",
    "pre_tool_use": "WORKING",
    "notification": "IDLE",
    "stop": "NEED_INPUT",
}

MODULE_NUMBERS = (1, 2, 3)

# Valid firmware states, used to validate anything read back off disk.
STATES = frozenset(EVENT_STATE_MAP.values()) | {"OFF"}


class StateManager:
    def __init__(self, persistence_path=None, liveness_check=None, time_source=None):
        self._persistence_path = Path(persistence_path) if persistence_path else None
        self._liveness_check = liveness_check
        self._now = time_source or time.time
        self._module_owner = {}   # module number -> session_id
        self._session_module = {}  # session_id -> module number
        self._session_state = {}   # session_id -> last state string sent
        self._session_pid = {}     # session_id -> claude_pid
        self._session_since = {}   # session_id -> when its CURRENT state began
        self._queue = []           # FIFO of waiting session_ids

    def handle_event(self, session_id, event, claude_pid):
        if event == "session_end":
            return self._release_module(session_id)

        state = EVENT_STATE_MAP.get(event)
        if state is None:
            return []

        self._session_pid[session_id] = claude_pid
        # Only restart the clock on a real state change, so "how long has this
        # been WORKING" survives the repeated events of a busy session.
        if self._session_state.get(session_id) != state:
            self._session_since[session_id] = self._now()
        self._session_state[session_id] = state

        if session_id in self._session_module:
            # Already assigned to a module
            module = self._session_module[session_id]
            self._persist()
            return [f"{module}:{state}"]

        if session_id in self._queue:
            # Already queued; just remember its latest state for when it's dequeued
            self._persist()
            return []

        # Brand new session; try to assign it
        module = self._assign_module(session_id)
        self._persist()
        if module is None:
            return []
        return [f"{module}:{state}"]

    def _assign_module(self, session_id):
        for m in MODULE_NUMBERS:
            if m not in self._module_owner:
                self._module_owner[m] = session_id
                self._session_module[session_id] = m
                return m
        self._queue.append(session_id)
        return None

    def _release_module(self, session_id):
        module = self._session_module.pop(session_id, None)
        self._session_state.pop(session_id, None)
        self._session_pid.pop(session_id, None)
        self._session_since.pop(session_id, None)
        if session_id in self._queue:
            self._queue.remove(session_id)

        if module is None:
            self._persist()
            return []

        del self._module_owner[module]

        if self._queue:
            next_session = self._queue.pop(0)
            self._module_owner[module] = next_session
            self._session_module[next_session] = module
            state = self._session_state.get(next_session, "THINKING")
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
            f"{module}:{self._session_state.get(session_id, 'THINKING')}"
            for session_id, module in self._session_module.items()
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
        commands = []
        for session_id, pid in list(self._session_pid.items()):
            if not self._is_alive(pid):
                commands.extend(self._release_module(session_id))
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
        for session_id, entry in data.items():
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
            self._module_owner[module] = session_id
            self._session_module[session_id] = module
            self._session_state[session_id] = state
            self._session_pid[session_id] = pid
            # The original transition time didn't survive the restart, so the
            # clock restarts here rather than claiming a duration we don't know.
            self._session_since[session_id] = self._now()
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
            session_id = self._module_owner.get(module)
            if session_id is None:
                modules.append({
                    "module": module,
                    "session_id": None,
                    "state": "OFF",
                    "state_seconds": None,
                    "claude_pid": None,
                })
                continue
            since = self._session_since.get(session_id)
            modules.append({
                "module": module,
                "session_id": session_id,
                "state": self._session_state.get(session_id, "THINKING"),
                "state_seconds": None if since is None else round(now - since, 1),
                "claude_pid": self._session_pid.get(session_id),
            })
        return {
            "modules": modules,
            "queue": [
                {"session_id": sid, "claude_pid": self._session_pid.get(sid)}
                for sid in self._queue
            ],
        }

    def _persist(self):
        if not self._persistence_path:
            return
        data = {
            session_id: {
                "module": self._session_module[session_id],
                "last_known_state": self._session_state.get(session_id, "THINKING"),
                "claude_pid": self._session_pid.get(session_id),
            }
            for session_id in self._session_module
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
