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
    assert t.totals_for("sess-a") == {
        "input_tokens": 12, "output_tokens": 150,
        "cache_creation_input_tokens": 6, "cache_read_input_tokens": 3000,
    }


def test_returns_none_when_no_transcript_exists(tmp_path):
    assert UsageTracker(root=tmp_path).totals_for("nope") is None


def test_reads_incrementally_without_double_counting(tmp_path):
    p = write_transcript(tmp_path, "sess-a", [usage_entry(out=100)])
    t = UsageTracker(root=tmp_path)
    assert t.totals_for("sess-a")["output_tokens"] == 100
    # polling again with no new data must not re-add anything
    assert t.totals_for("sess-a")["output_tokens"] == 100
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(usage_entry(out=25)) + "\n")
    assert t.totals_for("sess-a")["output_tokens"] == 125


def test_ignores_a_partially_written_trailing_line(tmp_path):
    p = write_transcript(tmp_path, "sess-a", [usage_entry(out=100)])
    with p.open("a", encoding="utf-8") as f:
        f.write('{"message": {"usage": {"output_tok')  # torn mid-write, no newline
    t = UsageTracker(root=tmp_path)
    assert t.totals_for("sess-a")["output_tokens"] == 100
    # once the line is completed it gets counted, exactly once
    with p.open("a", encoding="utf-8") as f:
        f.write('ens": 7}}}\n')
    assert t.totals_for("sess-a")["output_tokens"] == 107


def test_skips_lines_without_usage_and_malformed_json(tmp_path):
    write_transcript(tmp_path, "sess-a", [
        {"message": {"role": "user", "content": "hi"}},
        usage_entry(out=42),
    ])
    p = tmp_path / "E--proj" / "sess-a.jsonl"
    with p.open("a", encoding="utf-8") as f:
        f.write("not json at all\n")
    assert UsageTracker(root=tmp_path).totals_for("sess-a")["output_tokens"] == 42


def test_truncated_file_restarts_counting(tmp_path):
    p = write_transcript(tmp_path, "sess-a", [usage_entry(out=100)])
    t = UsageTracker(root=tmp_path)
    assert t.totals_for("sess-a")["output_tokens"] == 100
    write_transcript(tmp_path, "sess-a", [usage_entry(out=5)])  # rewritten smaller
    assert t.totals_for("sess-a")["output_tokens"] == 5


def test_forget_drops_session_bookkeeping(tmp_path):
    write_transcript(tmp_path, "sess-a", [usage_entry(out=100)])
    t = UsageTracker(root=tmp_path)
    t.totals_for("sess-a")
    t.forget("sess-a")
    assert t.totals_for("sess-a")["output_tokens"] == 100  # recounted from scratch
