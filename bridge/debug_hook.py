# bridge/debug_hook.py
#
# TEMPORARY diagnostic hook. Logs every invocation (label + full stdin
# payload) to bridge/hook_debug.log so we can see which Claude Code hook
# events actually fire, and what their payloads contain.
#
# Not part of the shipped bridge. Remove once the IDLE/permission-prompt
# investigation is done.
import json
import sys
from datetime import datetime
from pathlib import Path

LOG_PATH = Path(__file__).parent / "hook_debug.log"


def main():
    try:
        label = sys.argv[1] if len(sys.argv) > 1 else "<no-label>"
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw)
            pretty = json.dumps(payload, indent=2, sort_keys=True)
        except Exception:
            pretty = f"<unparseable> {raw!r}"

        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"===== [{stamp}] {label} =====\n{pretty}\n\n")
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
