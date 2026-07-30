# bridge/usage.py
#
# Per-session token totals, read from Claude Code's own session transcripts.
#
# Transcripts live at ~/.claude/projects/<mangled-cwd>/<session_id>.jsonl and
# every assistant message carries a `usage` block. Session ids are UUIDs, so a
# session's file can be found by globbing for its id -- which means the hook
# script needs no changes at all to make this work.
import json
from pathlib import Path

TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"

FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


class UsageTracker:
    """Accumulates token totals per session.

    Reads incrementally -- each poll consumes only the bytes appended since the
    last one. Transcripts reach several MB in a long session, and this is polled
    every few seconds, so re-parsing from the top each time would be wasteful.
    """

    def __init__(self, root=None):
        self._root = Path(root) if root is not None else TRANSCRIPT_ROOT
        self._paths = {}    # session_id -> resolved transcript Path
        self._offsets = {}  # session_id -> bytes already consumed
        self._totals = {}   # session_id -> {field: count}

    def _find(self, session_id):
        cached = self._paths.get(session_id)
        if cached is not None:
            return cached
        try:
            for path in self._root.glob(f"*/{session_id}.jsonl"):
                # Only cache a hit. A miss may just mean the session hasn't
                # written its transcript yet, so we retry on the next poll.
                self._paths[session_id] = path
                return path
        except OSError:
            pass
        return None

    def totals_for(self, session_id):
        """Current totals for a session, or None if it has no readable transcript."""
        if not session_id:
            return None
        path = self._find(session_id)
        if path is None:
            return None

        totals = self._totals.setdefault(session_id, {f: 0 for f in FIELDS})
        offset = self._offsets.get(session_id, 0)

        try:
            size = path.stat().st_size
            if size < offset:
                # File shrank -- rotated or rewritten. Start over rather than
                # carry totals that no longer correspond to the file.
                totals = {f: 0 for f in FIELDS}
                self._totals[session_id] = totals
                offset = 0
            if size > offset:
                # Binary mode: text-mode iteration disables tell(), and we need
                # an exact byte offset to resume from.
                with path.open("rb") as f:
                    f.seek(offset)
                    chunk = f.read()
                # The session is being appended to live, so the tail may be a
                # partial line. Consume only through the last newline.
                cut = chunk.rfind(b"\n")
                if cut != -1:
                    self._offsets[session_id] = offset + cut + 1
                    for raw in chunk[:cut].split(b"\n"):
                        self._add_line(totals, raw)
        except OSError:
            pass

        return dict(totals)

    @staticmethod
    def _add_line(totals, raw):
        if not raw.strip():
            return
        try:
            entry = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return  # a torn or non-JSON line is skipped, not fatal
        usage = (entry.get("message") or {}).get("usage")
        if not isinstance(usage, dict):
            return
        for field in FIELDS:
            value = usage.get(field)
            if isinstance(value, int):
                totals[field] += value

    def forget(self, session_id):
        """Drop a finished session so its bookkeeping doesn't accumulate."""
        self._paths.pop(session_id, None)
        self._offsets.pop(session_id, None)
        self._totals.pop(session_id, None)
