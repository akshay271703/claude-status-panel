# bridge/usage.py
#
# Per-session token totals, read from Claude Code's own session transcripts.
#
# A session with subagents writes two kinds of file:
#
#   ~/.claude/projects/<mangled-cwd>/<session_id>.jsonl
#       the main thread -- your prompts and Claude's direct replies
#
#   ~/.claude/projects/<mangled-cwd>/<session_id>/subagents/agent-<id>.jsonl
#       one per subagent, each with a sibling agent-<id>.meta.json naming its
#       type and purpose. Nested subagents land in that same flat directory.
#
# Reading only the first undercounts. Measured on two real sessions in this
# repo, subagents were ~19% of output tokens and tens of millions of cache-read
# tokens that appeared nowhere at all. So both are read -- and reported apart,
# because "I typed a lot" and "I fanned out 29 agents" are different facts and
# only the split tells you which one you are looking at.
#
# Session ids are UUIDs, so a session's files can be found by globbing for its
# id -- which is why none of this needed a change to the hook script.
import json
from pathlib import Path

TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"

FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

AGENT_PREFIX = "agent-"


def _zero():
    return {f: 0 for f in FIELDS}


class UsageTracker:
    """Accumulates token totals per session, split main vs. subagents.

    Reads incrementally -- each poll consumes only the bytes appended to each
    file since the last one. Transcripts reach several MB and a busy session
    has dozens of them, so re-parsing from the top every few seconds would be
    wasteful.
    """

    def __init__(self, root=None):
        self._root = Path(root) if root is not None else TRANSCRIPT_ROOT
        self._paths = {}        # session_id -> main transcript Path
        self._files = {}        # session_id -> set of Paths seen for it
        self._offsets = {}      # Path -> bytes already consumed
        self._totals = {}       # Path -> {field: count}
        self._meta = {}         # Path -> {"type", "description", "model"}

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

    def _subagent_paths(self, main_path):
        """Subagent transcripts for a main transcript, or [] if there are none.

        Re-globbed every poll rather than cached: subagents appear while the
        session runs, and a cached empty list would hide every one of them.
        """
        try:
            return sorted((main_path.parent / main_path.stem / "subagents")
                          .glob(f"{AGENT_PREFIX}*.jsonl"))
        except OSError:
            return []

    def totals_for(self, session_id):
        """Usage for a session, or None if it has no readable transcript.

        Shape:
            {"main": {...}, "subagents": {...}, "total": {...},
             "subagent_count": int, "agents": [{...}, ...]}

        `agents` is ordered by output tokens, heaviest first -- the cheapest
        way to see which dispatch actually cost you something.
        """
        if not session_id:
            return None
        main_path = self._find(session_id)
        if main_path is None:
            return None

        self._consume(main_path)
        main = dict(self._totals.get(main_path) or _zero())

        subs = _zero()
        agents = []
        sub_paths = self._subagent_paths(main_path)
        for path in sub_paths:
            self._consume(path)
            counts = self._totals.get(path) or _zero()
            for field in FIELDS:
                subs[field] += counts[field]
            meta = self._meta_for(path)
            agents.append({
                "id": path.stem[len(AGENT_PREFIX):],
                "type": meta.get("agentType"),
                "description": meta.get("description"),
                "model": meta.get("model"),
                "output_tokens": counts["output_tokens"],
            })
        agents.sort(key=lambda a: a["output_tokens"], reverse=True)

        self._files[session_id] = {main_path, *sub_paths}

        return {
            "main": main,
            "subagents": subs,
            "total": {f: main[f] + subs[f] for f in FIELDS},
            "subagent_count": len(sub_paths),
            "agents": agents,
        }

    def _consume(self, path):
        """Fold any newly appended bytes of one file into its running totals."""
        totals = self._totals.setdefault(path, _zero())
        offset = self._offsets.get(path, 0)
        try:
            size = path.stat().st_size
            if size < offset:
                # File shrank -- rotated or rewritten. Start over rather than
                # carry totals that no longer correspond to the file.
                totals = _zero()
                self._totals[path] = totals
                offset = 0
                self._offsets[path] = 0
            if size > offset:
                # Binary mode: text-mode iteration disables tell(), and we need
                # an exact byte offset to resume from.
                with path.open("rb") as f:
                    f.seek(offset)
                    chunk = f.read()
                # The file is being appended to live, so the tail may be a
                # partial line. Consume only through the last newline.
                cut = chunk.rfind(b"\n")
                if cut != -1:
                    self._offsets[path] = offset + cut + 1
                    for raw in chunk[:cut].split(b"\n"):
                        self._add_line(totals, raw)
        except OSError:
            pass

    def _meta_for(self, path):
        """Sidecar description of one subagent. Empty dict if not readable.

        Cached only on success: the sidecar and the transcript are written
        separately, so a miss can simply mean "not yet".
        """
        cached = self._meta.get(path)
        if cached is not None:
            return cached
        try:
            meta = json.loads(path.with_name(path.stem + ".meta.json")
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        if not isinstance(meta, dict):
            return {}
        self._meta[path] = meta
        return meta

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
        """Drop a finished session so its bookkeeping doesn't accumulate.

        A long-lived bridge sees many sessions, each with any number of
        subagent files; keeping every offset forever is a slow leak.
        """
        self._paths.pop(session_id, None)
        for path in self._files.pop(session_id, ()):
            self._offsets.pop(path, None)
            self._totals.pop(path, None)
            self._meta.pop(path, None)
