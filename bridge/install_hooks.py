#!/usr/bin/env python3
"""Wire this checkout's report_event.py into Claude Code's hook config.

Hook commands need absolute paths, and the right Python, so they can't be
committed to the repo -- they're machine-specific. Run this once per machine
after cloning:

    python3 bridge/install_hooks.py          # add or update
    python3 bridge/install_hooks.py --remove # take them back out
    python3 bridge/install_hooks.py --dry-run

It merges into ~/.claude/settings.json, leaving every other setting alone,
and backs the file up first.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
REPORT_EVENT = Path(__file__).resolve().parent / "report_event.py"

# hook event -> the internal event name report_event.py expects.
EVENTS = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "user_prompt_submit",
    "PreToolUse": "pre_tool_use",
    "PermissionRequest": "notification",
    "Notification": "notification",
    "Stop": "stop",
    "SessionEnd": "session_end",
    "SubagentStart": "subagent_start",
    "SubagentStop": "subagent_stop",
}

# Notification is a delayed nudge (~6s after a prompt goes unanswered), so it
# is only useful filtered to permission prompts; PermissionRequest is the one
# that fires immediately. Both map to the same IDLE state.
MATCHERS = {"Notification": "permission_prompt"}


def command_for(event_name):
    # sys.executable rather than "python": it resolves python3 vs python, and
    # keeps hooks pointed at the interpreter this was installed with.
    return f'"{sys.executable}" "{REPORT_EVENT}" {event_name}'


def is_ours(hook):
    return isinstance(hook, dict) and "report_event.py" in str(hook.get("command", ""))


def build():
    config = {}
    for hook_event, internal in EVENTS.items():
        config[hook_event] = [{
            "matcher": MATCHERS.get(hook_event, "*"),
            "hooks": [{"type": "command", "command": command_for(internal)}],
        }]
    return config


def strip_ours(hooks):
    """Remove only this project's entries, preserving anything else."""
    cleaned = {}
    for event, groups in (hooks or {}).items():
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            kept = [h for h in group.get("hooks", []) if not is_ours(h)]
            if kept:
                kept_groups.append({**group, "hooks": kept})
        if kept_groups:
            cleaned[event] = kept_groups
    return cleaned


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--remove", action="store_true", help="uninstall the hooks")
    ap.add_argument("--dry-run", action="store_true", help="print the result, write nothing")
    args = ap.parse_args()

    if not REPORT_EVENT.exists():
        print(f"Cannot find {REPORT_EVENT} -- run this from a full checkout.")
        return 1

    settings = {}
    if SETTINGS.exists():
        try:
            settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except ValueError as e:
            print(f"{SETTINGS} is not valid JSON ({e}); fix it before running this.")
            return 1
        if not isinstance(settings, dict):
            print(f"{SETTINGS} does not contain a JSON object; refusing to touch it.")
            return 1

    hooks = strip_ours(settings.get("hooks"))
    if not args.remove:
        for event, groups in build().items():
            hooks.setdefault(event, []).extend(groups)

    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)

    rendered = json.dumps(settings, indent=2) + "\n"
    if args.dry_run:
        print(rendered)
        return 0

    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    if SETTINGS.exists():
        backup = SETTINGS.with_suffix(".json.bak")
        shutil.copy2(SETTINGS, backup)
        print(f"Backed up existing settings to {backup}")
    SETTINGS.write_text(rendered, encoding="utf-8")

    if args.remove:
        print(f"Removed status-panel hooks from {SETTINGS}")
    else:
        print(f"Installed status-panel hooks into {SETTINGS}")
        print(f"  interpreter: {sys.executable}")
        print(f"  script:      {REPORT_EVENT}")
        print("\nStart a NEW Claude Code session to pick them up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
