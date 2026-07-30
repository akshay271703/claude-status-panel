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

        body = json.dumps({
            "session_id": session_id,
            "event": event,
            "claude_pid": claude_pid,
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
