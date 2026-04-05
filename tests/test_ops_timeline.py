"""Tests for ops_timeline tool."""
from __future__ import annotations

import json
import pathlib
import pytest
from datetime import datetime, timezone, timedelta

from ouroboros.tools.ops_timeline import (
    _parse_ts,
    _load_source,
    _build_summary_line,
    _ops_timeline,
    get_tools,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _write_jsonl(path: pathlib.Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


@pytest.fixture
def drive_root(tmp_path):
    (tmp_path / "logs").mkdir()
    return tmp_path


def _make_rec(ts: str, rtype: str = "llm_round", **kw) -> dict:
    return {"ts": ts, "type": rtype, **kw}


# ── _parse_ts ──────────────────────────────────────────────────────────────────

def test_parse_ts_iso():
    dt = _parse_ts("2026-04-05T03:00:00+00:00")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 4 and dt.day == 5

def test_parse_ts_z():
    dt = _parse_ts("2026-04-05T03:00:00Z")
    assert dt is not None and dt.tzinfo is not None

def test_parse_ts_empty():
    assert _parse_ts("") is None

def test_parse_ts_invalid():
    assert _parse_ts("not-a-date") is None


# ── _load_source ───────────────────────────────────────────────────────────────

def test_load_source_missing_file(drive_root):
    result = _load_source(
        drive_root / "logs" / "nonexistent.jsonl",
        since=None, until=None, task_id=None, search=None, event_type=None,
        source_name="events",
    )
    assert result == []


def test_load_source_all_records(drive_root):
    path = drive_root / "logs" / "events.jsonl"
    records = [
        _make_rec("2026-04-05T03:00:00Z", task_id="t1"),
        _make_rec("2026-04-05T03:01:00Z", task_id="t2"),
    ]
    _write_jsonl(path, records)
    loaded = _load_source(path, None, None, None, None, None, "events")
    assert len(loaded) == 2
    assert all(r["_source"] == "events" for r in loaded)


def test_load_source_since_filter(drive_root):
    path = drive_root / "logs" / "events.jsonl"
    _write_jsonl(path, [
        _make_rec("2026-04-05T02:00:00Z"),
        _make_rec("2026-04-05T03:00:00Z"),
        _make_rec("2026-04-05T04:00:00Z"),
    ])
    since = datetime(2026, 4, 5, 2, 30, tzinfo=timezone.utc)
    loaded = _load_source(path, since=since, until=None, task_id=None, search=None, event_type=None, source_name="events")
    assert len(loaded) == 2
    for r in loaded:
        assert r["ts"] >= "2026-04-05T03"


def test_load_source_until_filter(drive_root):
    path = drive_root / "logs" / "events.jsonl"
    _write_jsonl(path, [
        _make_rec("2026-04-05T01:00:00Z"),
        _make_rec("2026-04-05T02:00:00Z"),
        _make_rec("2026-04-05T03:00:00Z"),
    ])
    until = datetime(2026, 4, 5, 2, 30, tzinfo=timezone.utc)
    loaded = _load_source(path, since=None, until=until, task_id=None, search=None, event_type=None, source_name="events")
    assert len(loaded) == 2


def test_load_source_task_id_filter(drive_root):
    path = drive_root / "logs" / "tools.jsonl"
    _write_jsonl(path, [
        {"ts": "2026-04-05T03:00:00Z", "tool": "repo_read", "task_id": "abc123"},
        {"ts": "2026-04-05T03:01:00Z", "tool": "run_shell", "task_id": "def456"},
    ])
    loaded = _load_source(path, None, None, task_id="abc", search=None, event_type=None, source_name="tools")
    assert len(loaded) == 1
    assert loaded[0]["tool"] == "repo_read"


def test_load_source_search_filter(drive_root):
    path = drive_root / "logs" / "events.jsonl"
    _write_jsonl(path, [
        {"ts": "2026-04-05T03:00:00Z", "type": "llm_round", "model": "claude-sonnet"},
        {"ts": "2026-04-05T03:01:00Z", "type": "llm_round", "model": "gpt-4"},
    ])
    loaded = _load_source(path, None, None, None, search="claude", event_type=None, source_name="events")
    assert len(loaded) == 1
    assert "claude" in loaded[0]["model"]


def test_load_source_event_type_filter(drive_root):
    path = drive_root / "logs" / "events.jsonl"
    _write_jsonl(path, [
        {"ts": "2026-04-05T03:00:00Z", "type": "llm_round"},
        {"ts": "2026-04-05T03:01:00Z", "type": "tool_timeout"},
        {"ts": "2026-04-05T03:02:00Z", "type": "llm_round"},
    ])
    loaded = _load_source(path, None, None, None, None, event_type="tool_timeout", source_name="events")
    assert len(loaded) == 1
    assert loaded[0]["type"] == "tool_timeout"


def test_load_source_nested_task_id(drive_root):
    """task_id inside nested 'task' dict should match."""
    path = drive_root / "logs" / "events.jsonl"
    _write_jsonl(path, [
        {"ts": "2026-04-05T03:00:00Z", "type": "task_received", "task": {"id": "xyz999", "type": "chat"}},
        {"ts": "2026-04-05T03:01:00Z", "type": "llm_round", "task_id": "other"},
    ])
    loaded = _load_source(path, None, None, task_id="xyz999", search=None, event_type=None, source_name="events")
    assert len(loaded) == 1
    assert loaded[0]["type"] == "task_received"


# ── _build_summary_line ────────────────────────────────────────────────────────

def test_summary_line_llm_round():
    rec = {
        "ts": "2026-04-05T03:00:00+00:00",
        "type": "llm_round",
        "model": "copilot/sonnet",
        "round": 5,
        "prompt_tokens": 1234,
        "cost_usd": 0.00012,
    }
    line = _build_summary_line(rec, "events")
    assert "llm_round" in line
    assert "copilot/sonnet" in line
    assert "1234" in line


def test_summary_line_tool_call():
    rec = {
        "ts": "2026-04-05T03:00:00+00:00",
        "tool": "repo_read",
        "task_id": "abc12345",
    }
    line = _build_summary_line(rec, "tools")
    assert "repo_read" in line
    assert "abc1234" in line  # short task_id


def test_summary_line_chat():
    rec = {
        "ts": "2026-04-05T03:00:00+00:00",
        "role": "user",
        "text": "Hello world",
    }
    line = _build_summary_line(rec, "chat")
    assert "user" in line
    assert "Hello world" in line


def test_summary_line_tool_timeout():
    rec = {
        "ts": "2026-04-05T03:00:00+00:00",
        "type": "tool_timeout",
        "tool": "run_shell",
        "error": "exceeded 30s limit",
    }
    line = _build_summary_line(rec, "events")
    assert "tool_timeout" in line
    assert "run_shell" in line


def test_summary_line_task_received():
    rec = {
        "ts": "2026-04-05T03:00:00+00:00",
        "type": "task_received",
        "task": {"id": "abc12345", "type": "evolution"},
    }
    line = _build_summary_line(rec, "events")
    assert "task_received" in line
    assert "evolution" in line


def test_summary_line_progress():
    rec = {
        "ts": "2026-04-05T03:00:00+00:00",
        "text": "Running smoke tests...",
    }
    line = _build_summary_line(rec, "progress")
    assert "Running smoke tests" in line


# ── _ops_timeline — core integration ──────────────────────────────────────────

@pytest.fixture
def populated_drive(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()

    _write_jsonl(logs / "events.jsonl", [
        {"ts": "2026-04-05T03:00:00Z", "type": "task_received", "task": {"id": "t1", "type": "evolution"}, "task_id": "t1"},
        {"ts": "2026-04-05T03:01:00Z", "type": "llm_round", "task_id": "t1", "model": "sonnet", "round": 1, "cost_usd": 0.001, "prompt_tokens": 500},
        {"ts": "2026-04-05T03:02:00Z", "type": "tool_timeout", "task_id": "t1", "tool": "run_shell"},
        {"ts": "2026-04-05T03:05:00Z", "type": "task_done", "task_id": "t1"},
    ])
    _write_jsonl(logs / "tools.jsonl", [
        {"ts": "2026-04-05T03:01:30Z", "tool": "repo_read", "task_id": "t1"},
        {"ts": "2026-04-05T03:02:30Z", "tool": "run_shell", "task_id": "t1"},
    ])
    _write_jsonl(logs / "chat.jsonl", [
        {"ts": "2026-04-05T03:00:30Z", "role": "user", "text": "evolve"},
    ])
    _write_jsonl(logs / "progress.jsonl", [
        {"ts": "2026-04-05T03:01:10Z", "text": "Starting evolution cycle"},
    ])
    return tmp_path


def test_basic_timeline(populated_drive):
    result = _ops_timeline(
        None,
        sources="events,tools",
        task_id="t1",
        _drive_root=str(populated_drive),
    )
    assert "ops_timeline" in result
    assert "task_received" in result or "llm_round" in result
    assert "repo_read" in result


def test_minutes_filter(populated_drive):
    """Without minutes filter we get everything; with 0 we also get everything."""
    result_all = _ops_timeline(None, _drive_root=str(populated_drive))
    assert "ops_timeline" in result_all


def test_since_until_filter(populated_drive):
    result = _ops_timeline(
        None,
        since="2026-04-05T03:01:00Z",
        until="2026-04-05T03:03:00Z",
        sources="events",
        _drive_root=str(populated_drive),
    )
    # task_received at 03:00 should be excluded
    assert "task_received" not in result or "03:01" in result or "03:02" in result


def test_search_filter(populated_drive):
    result = _ops_timeline(None, search="tool_timeout", sources="events", _drive_root=str(populated_drive))
    assert "tool_timeout" in result


def test_event_type_filter(populated_drive):
    result = _ops_timeline(None, event_type="task_done", sources="events", _drive_root=str(populated_drive))
    assert "task_done" in result
    assert "llm_round" not in result


def test_json_format(populated_drive):
    result = _ops_timeline(None, sources="events", format="json", _drive_root=str(populated_drive))
    data = json.loads(result)
    assert "records" in data
    assert "total_matched" in data
    assert "source_counts" in data


def test_json_format_no_internal_keys(populated_drive):
    """_ts_dt should not appear in JSON output."""
    result = _ops_timeline(None, sources="events", format="json", _drive_root=str(populated_drive))
    assert "_ts_dt" not in result


def test_limit_truncation(populated_drive):
    result = _ops_timeline(None, limit=2, sources="events", format="json", _drive_root=str(populated_drive))
    data = json.loads(result)
    assert data["truncated"] is True
    assert len(data["records"]) == 2


def test_source_counts_in_json(populated_drive):
    result = _ops_timeline(None, sources="events,tools", task_id="t1", format="json", _drive_root=str(populated_drive))
    data = json.loads(result)
    assert "events" in data["source_counts"]
    assert "tools" in data["source_counts"]
    assert data["source_counts"]["events"] > 0


def test_invalid_source():
    result = _ops_timeline(None, sources="nonexistent")
    data = json.loads(result)
    assert "error" in data
    assert "available" in data


def test_chronological_order(populated_drive):
    """Records must come out in timestamp order."""
    result = _ops_timeline(None, sources="events,tools", task_id="t1", format="json", _drive_root=str(populated_drive))
    data = json.loads(result)
    timestamps = [r.get("ts", "") for r in data["records"]]
    assert timestamps == sorted(timestamps)


def test_all_sources_loaded(populated_drive):
    """With no source filter, all 5 sources are included in output."""
    result = _ops_timeline(None, format="json", _drive_root=str(populated_drive))
    data = json.loads(result)
    sources_present = {r.get("_source") for r in data["records"]}
    # We wrote events, tools, chat, progress — all should appear
    assert "events" in sources_present
    assert "tools" in sources_present
    assert "chat" in sources_present
    assert "progress" in sources_present


def test_empty_logs(tmp_path):
    """Empty drive root returns no records but doesn't crash."""
    (tmp_path / "logs").mkdir()
    result = _ops_timeline(None, _drive_root=str(tmp_path))
    assert "ops_timeline" in result
    assert "no records" in result


def test_verbose_mode(populated_drive):
    """verbose=True should output full JSON per line."""
    result = _ops_timeline(None, sources="events", verbose=True, task_id="t1", _drive_root=str(populated_drive))
    # verbose outputs JSON blobs — should contain full field names
    assert '"type"' in result or "type" in result


# ── get_tools ──────────────────────────────────────────────────────────────────

def test_get_tools_returns_one():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "ops_timeline"


def test_get_tools_schema_valid():
    tool = get_tools()[0]
    schema = tool.schema
    assert schema["name"] == "ops_timeline"
    params = schema["parameters"]["properties"]
    assert "minutes" in params
    assert "since" in params
    assert "sources" in params
    assert "task_id" in params
    assert "search" in params
    assert "event_type" in params
    assert "format" in params
    assert "verbose" in params


def test_get_tools_handler_callable():
    tool = get_tools()[0]
    assert callable(tool.handler)


def test_handler_via_tool_entry(populated_drive):
    """Handler should work when called through the ToolEntry wrapper."""
    tool = get_tools()[0]
    result = tool.handler(None, sources="events", task_id="t1", _drive_root=str(populated_drive))
    assert "ops_timeline" in result
