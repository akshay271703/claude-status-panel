import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from usage import UsageTracker


def write_transcript(root, session_id, entries, project="E--proj"):
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return p


def write_subagent(root, session_id, agent_id, entries, meta=None, project="E--proj"):
    d = root / project / session_id / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"agent-{agent_id}.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    if meta is not None:
        (d / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return p


def usage_entry(inp=0, out=0, cw=0, cr=0):
    return {"message": {"usage": {
        "input_tokens": inp, "output_tokens": out,
        "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr,
    }}}


def test_sums_usage_across_messages(tmp_path):
    write_transcript(tmp_path, "sess-a", [
        usage_entry(inp=10, out=100, cw=5, cr=1000),
        usage_entry(inp=2, out=50, cw=1, cr=2000),
    ])
    t = UsageTracker(root=tmp_path)
    assert t.totals_for("sess-a")["main"] == {
        "input_tokens": 12, "output_tokens": 150,
        "cache_creation_input_tokens": 6, "cache_read_input_tokens": 3000,
    }


def test_returns_none_when_no_transcript_exists(tmp_path):
    assert UsageTracker(root=tmp_path).totals_for("nope") is None


def test_reads_incrementally_without_double_counting(tmp_path):
    p = write_transcript(tmp_path, "sess-a", [usage_entry(out=100)])
    t = UsageTracker(root=tmp_path)
    assert t.totals_for("sess-a")["main"]["output_tokens"] == 100
    # polling again with no new data must not re-add anything
    assert t.totals_for("sess-a")["main"]["output_tokens"] == 100
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(usage_entry(out=25)) + "\n")
    assert t.totals_for("sess-a")["main"]["output_tokens"] == 125


def test_ignores_a_partially_written_trailing_line(tmp_path):
    p = write_transcript(tmp_path, "sess-a", [usage_entry(out=100)])
    with p.open("a", encoding="utf-8") as f:
        f.write('{"message": {"usage": {"output_tok')  # torn mid-write, no newline
    t = UsageTracker(root=tmp_path)
    assert t.totals_for("sess-a")["main"]["output_tokens"] == 100
    # once the line is completed it gets counted, exactly once
    with p.open("a", encoding="utf-8") as f:
        f.write('ens": 7}}}\n')
    assert t.totals_for("sess-a")["main"]["output_tokens"] == 107


def test_skips_lines_without_usage_and_malformed_json(tmp_path):
    write_transcript(tmp_path, "sess-a", [
        {"message": {"role": "user", "content": "hi"}},
        usage_entry(out=42),
    ])
    p = tmp_path / "E--proj" / "sess-a.jsonl"
    with p.open("a", encoding="utf-8") as f:
        f.write("not json at all\n")
    assert UsageTracker(root=tmp_path).totals_for("sess-a")["main"]["output_tokens"] == 42


def test_truncated_file_restarts_counting(tmp_path):
    write_transcript(tmp_path, "sess-a", [usage_entry(out=100)])
    t = UsageTracker(root=tmp_path)
    assert t.totals_for("sess-a")["main"]["output_tokens"] == 100
    write_transcript(tmp_path, "sess-a", [usage_entry(out=5)])  # rewritten smaller
    assert t.totals_for("sess-a")["main"]["output_tokens"] == 5


def test_forget_drops_session_bookkeeping(tmp_path):
    write_transcript(tmp_path, "sess-a", [usage_entry(out=100)])
    write_subagent(tmp_path, "sess-a", "aaa", [usage_entry(out=7)])
    t = UsageTracker(root=tmp_path)
    t.totals_for("sess-a")
    t.forget("sess-a")
    # recounted from scratch, subagent offsets included
    again = t.totals_for("sess-a")
    assert again["main"]["output_tokens"] == 100
    assert again["subagents"]["output_tokens"] == 7


# --- main vs. subagent split ------------------------------------------------

def test_session_without_subagents_reports_zeroed_split(tmp_path):
    write_transcript(tmp_path, "sess-a", [usage_entry(inp=1, out=100)])
    u = UsageTracker(root=tmp_path).totals_for("sess-a")
    assert u["subagent_count"] == 0
    assert u["agents"] == []
    assert u["subagents"]["output_tokens"] == 0
    assert u["total"] == u["main"]


def test_subagent_tokens_are_counted_and_kept_separate(tmp_path):
    write_transcript(tmp_path, "sess-a", [usage_entry(inp=10, out=100, cw=5, cr=1000)])
    write_subagent(tmp_path, "sess-a", "aaa", [usage_entry(inp=1, out=20, cw=2, cr=300)])
    write_subagent(tmp_path, "sess-a", "bbb", [usage_entry(inp=3, out=5, cw=0, cr=700)])

    u = UsageTracker(root=tmp_path).totals_for("sess-a")
    assert u["main"]["output_tokens"] == 100
    assert u["subagents"] == {
        "input_tokens": 4, "output_tokens": 25,
        "cache_creation_input_tokens": 2, "cache_read_input_tokens": 1000,
    }
    assert u["total"] == {
        "input_tokens": 14, "output_tokens": 125,
        "cache_creation_input_tokens": 7, "cache_read_input_tokens": 2000,
    }
    assert u["subagent_count"] == 2


def test_agents_are_described_and_ordered_by_output(tmp_path):
    write_transcript(tmp_path, "sess-a", [usage_entry(out=1)])
    write_subagent(tmp_path, "sess-a", "small", [usage_entry(out=5)],
                   meta={"agentType": "claude-code-guide", "description": "check hooks"})
    write_subagent(tmp_path, "sess-a", "big", [usage_entry(out=500)],
                   meta={"agentType": "general-purpose", "description": "final review",
                         "model": "sonnet"})

    agents = UsageTracker(root=tmp_path).totals_for("sess-a")["agents"]
    assert [a["id"] for a in agents] == ["big", "small"]
    assert agents[0] == {
        "id": "big", "type": "general-purpose", "description": "final review",
        "model": "sonnet", "output_tokens": 500,
    }
    assert agents[1]["model"] is None  # absent in that sidecar


def test_subagent_without_meta_is_still_counted(tmp_path):
    write_transcript(tmp_path, "sess-a", [usage_entry(out=1)])
    write_subagent(tmp_path, "sess-a", "aaa", [usage_entry(out=9)])  # no sidecar

    u = UsageTracker(root=tmp_path).totals_for("sess-a")
    assert u["subagents"]["output_tokens"] == 9
    assert u["agents"][0]["type"] is None
    assert u["agents"][0]["description"] is None


def test_meta_written_after_the_transcript_is_picked_up_later(tmp_path):
    write_transcript(tmp_path, "sess-a", [usage_entry(out=1)])
    write_subagent(tmp_path, "sess-a", "aaa", [usage_entry(out=9)])
    t = UsageTracker(root=tmp_path)
    assert t.totals_for("sess-a")["agents"][0]["type"] is None
    # a missing sidecar must not be cached as "no meta" forever
    (tmp_path / "E--proj" / "sess-a" / "subagents" / "agent-aaa.meta.json").write_text(
        json.dumps({"agentType": "general-purpose"}), encoding="utf-8")
    assert t.totals_for("sess-a")["agents"][0]["type"] == "general-purpose"


def test_subagent_appearing_later_is_discovered(tmp_path):
    write_transcript(tmp_path, "sess-a", [usage_entry(out=100)])
    t = UsageTracker(root=tmp_path)
    assert t.totals_for("sess-a")["subagent_count"] == 0
    # a subagent is dispatched mid-session; the directory did not exist before
    write_subagent(tmp_path, "sess-a", "aaa", [usage_entry(out=9)])
    u = t.totals_for("sess-a")
    assert u["subagent_count"] == 1
    assert u["subagents"]["output_tokens"] == 9


def test_subagent_transcripts_are_read_incrementally(tmp_path):
    write_transcript(tmp_path, "sess-a", [usage_entry(out=1)])
    p = write_subagent(tmp_path, "sess-a", "aaa", [usage_entry(out=10)])
    t = UsageTracker(root=tmp_path)
    assert t.totals_for("sess-a")["subagents"]["output_tokens"] == 10
    assert t.totals_for("sess-a")["subagents"]["output_tokens"] == 10  # no re-add
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(usage_entry(out=4)) + "\n")
    assert t.totals_for("sess-a")["subagents"]["output_tokens"] == 14


def test_nested_subagents_land_in_the_same_directory(tmp_path):
    # A subagent that spawns its own subagent writes to the session's flat
    # subagents/ directory, not a nested one -- so one glob catches every depth.
    write_transcript(tmp_path, "sess-a", [usage_entry(out=1)])
    write_subagent(tmp_path, "sess-a", "parent", [usage_entry(out=10)],
                   meta={"agentType": "general-purpose", "spawnDepth": 1})
    write_subagent(tmp_path, "sess-a", "child", [usage_entry(out=20)],
                   meta={"agentType": "claude-code-guide", "spawnDepth": 2,
                         "parentAgentId": "parent"})

    u = UsageTracker(root=tmp_path).totals_for("sess-a")
    assert u["subagent_count"] == 2
    assert u["subagents"]["output_tokens"] == 30


def test_one_sessions_subagents_do_not_leak_into_another(tmp_path):
    write_transcript(tmp_path, "sess-a", [usage_entry(out=1)])
    write_transcript(tmp_path, "sess-b", [usage_entry(out=2)])
    write_subagent(tmp_path, "sess-a", "aaa", [usage_entry(out=99)])

    t = UsageTracker(root=tmp_path)
    assert t.totals_for("sess-a")["subagents"]["output_tokens"] == 99
    assert t.totals_for("sess-b")["subagents"]["output_tokens"] == 0
    assert t.totals_for("sess-b")["subagent_count"] == 0
