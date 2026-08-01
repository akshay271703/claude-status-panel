# bridge/report_event.py
import json
import sys
import urllib.request
from pathlib import Path

BRIDGE_URL = "http://127.0.0.1:8765/event"
TIMEOUT_SECONDS = 0.5


def main():
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from process_utils import find_claude_pid

        event = sys.argv[1]
        payload = json.load(sys.stdin)
        session_id = payload.get("session_id")
        claude_pid = find_claude_pid()

        # agent_id/agent_type are present only when the hook fired inside a
        # subagent call (SubagentStart/SubagentStop, or a PreToolUse caused by
        # a subagent's own tool use). tool_name is what lets the bridge tell a
        # Task dispatch apart from any other tool call.
        body = json.dumps({
            "session_id": session_id,
            "event": event,
            "claude_pid": claude_pid,
            "tool_name": payload.get("tool_name"),
            "agent_id": payload.get("agent_id"),
            "agent_type": payload.get("agent_type"),
        }).encode("utf-8")

        req = urllib.request.Request(
            BRIDGE_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
