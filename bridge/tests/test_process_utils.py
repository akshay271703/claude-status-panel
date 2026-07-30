import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from process_utils import find_claude_pid, is_pid_alive


class FakeProcess:
    def __init__(self, pid, name, parent=None):
        self._pid = pid
        self._name = name
        self._parent = parent

    @property
    def pid(self):
        return self._pid

    def name(self):
        return self._name

    def parent(self):
        return self._parent


def test_finds_claude_two_hops_up():
    claude_proc = FakeProcess(pid=100, name="claude.exe")
    shell_proc = FakeProcess(pid=200, name="powershell.exe", parent=claude_proc)
    tool_proc = FakeProcess(pid=300, name="python.exe", parent=shell_proc)

    result = find_claude_pid(start_process=tool_proc)
    assert result == 100


def test_returns_none_if_claude_not_found_within_max_hops():
    top = FakeProcess(pid=1, name="explorer.exe")
    chain = top
    for i in range(10):
        chain = FakeProcess(pid=i + 2, name="cmd.exe", parent=chain)

    result = find_claude_pid(start_process=chain, max_hops=3)
    assert result is None


def test_returns_none_when_ancestry_ends_before_max_hops():
    orphan = FakeProcess(pid=5, name="python.exe", parent=None)
    result = find_claude_pid(start_process=orphan, max_hops=8)
    assert result is None


def test_is_pid_alive_returns_false_for_none():
    # find_claude_pid() returns None when the ancestry walk fails, and that
    # None is posted straight through to the bridge. is_pid_alive must treat
    # it as "not alive" rather than raising -- a raise here kills the
    # liveness thread and then wedges bridge startup permanently.
    assert is_pid_alive(None) is False


def test_is_pid_alive_true_for_current_process():
    import os
    assert is_pid_alive(os.getpid()) is True
