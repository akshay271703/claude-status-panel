import os

import psutil


def find_claude_pid(start_process=None, max_hops=8):
    """Walk up the process ancestry looking for the Claude Code CLI.

    Returns its pid, or None if it isn't found within max_hops. We search by
    process name rather than assuming a fixed depth, since the hook runner's
    ancestry depth is not guaranteed to match any one measurement.
    """
    try:
        proc = start_process if start_process is not None else psutil.Process()
        for _ in range(max_hops):
            proc = proc.parent()
            if proc is None:
                return None
            if proc.name().lower() in ("claude", "claude.exe"):
                return proc.pid
        return None
    except psutil.Error:
        return None


_project_name_cache = {}


def project_name_for_pid(pid):
    """Best-effort project name for a session, from its process's cwd.

    Claude Code runs in the project directory, so the leaf of its cwd is a
    good label. Reading it from the PID means the hook script -- which runs
    on every single tool call -- needs no changes at all.

    Cached because this is polled once per second by the dashboard, and
    returns None rather than raising if the process is gone or unreadable.
    """
    if pid is None:
        return None
    if pid in _project_name_cache:
        return _project_name_cache[pid]
    try:
        name = os.path.basename(psutil.Process(pid).cwd()) or None
    except Exception:
        name = None
    _project_name_cache[pid] = name
    return name


def is_pid_alive(pid):
    """True only if pid names a live process.

    find_claude_pid returns None on failure and that None is posted straight
    through to the bridge, so None must answer False here rather than raising:
    an exception on this path retires the liveness thread and then wedges
    bridge startup permanently.
    """
    if pid is None:
        return False
    try:
        return psutil.pid_exists(pid)
    except (psutil.Error, TypeError, ValueError, OverflowError):
        return False
