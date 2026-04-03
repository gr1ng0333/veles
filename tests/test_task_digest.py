"""Tests for task_digest tool."""
from __future__ import annotations

import json
import pathlib
import pytest

from ouroboros.tools.task_digest import (
    _resolve_task_id,
    _collect_events,
    _collect_tool_calls,
    _collect_reflection,
    _build_digest,
    _format_text,
    get_tools,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def drive_root(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    return tmp_path


def _write_jsonl(path: pathlib.Path, records):
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ── resolve_task_id ───────────────────────────────────────────────────────────

def test_resolve_passthrough(drive_root):
    """Non-'last' ids are returned as-is."""
    resolved = _resolve_task_id("abc12345", drive_root)
    assert resolved == "abc12345"


def test_resolve_last(drive_root):
    _write_jsonl(drive_root / "logs" / "events.jsonl", [
        {"ts": "2026-01-01T00:00:00+00:00", "type": "llm_round", "task_id": "task1"},
        {"ts": "2026-01-01T00:01:00+00:00", "type": "llm_round", "task_id": "task2"},
        {"ts": "2026-01-01T00:02:00+00:00", "type": "llm_round", "task_id": "task3"},
    ])
    assert _resolve_task_id("last", drive_root) == "task3"


def test_resolve_last_n(drive_root):
    _write_jsonl(drive_root / "logs" / "events.jsonl", [
        {"ts": "2026-01-01T00:00:00+00:00", "type": "llm_round", "task_id": "t1"},
        {"ts": "2026-01-01T00:01:00+00:00", "type": "llm_round", "task_id": "t2"},
        {"ts": "2026-01-01T00:02:00+00:00", "type": "llm_round", "task_id": "t3"},
    ])
    assert _resolve_task_id("last:2", drive_root) == "t2"


def test_resolve_empty_logs(drive_root):
    _write_jsonl(drive_root / "logs" / "events.jsonl", [])
    result = _resolve_task_id("last", drive_root)
    assert result is None


# ── collect_events / tools ─────────────────────────────────────────────────────

def test_collect_events_filters_by_task_id(drive_root):
    _write_jsonl(drive_root / "logs" / "events.jsonl", [
        {"ts": "2026-01-01T00:00:00+00:00", "type": "llm_round", "task_id": "aaa"},
        {"ts": "2026-01-01T00:01:00+00:00", "type": "llm_round", "task_id": "bbb"},
        {"ts": "2026-01-01T00:02:00+00:00", "type": "llm_round", "task_id": "aaa"},
    ])
    events = _collect_events("aaa", drive_root)
    assert len(events) == 2
    assert all(e["task_id"] == "aaa" for e in events)


def test_collect_tool_calls(drive_root):
    _write_jsonl(drive_root / "logs" / "tools.jsonl", [
        {"ts": "2026-01-01T00:00:00+00:00", "tool": "repo_read", "task_id": "aaa"},
        {"ts": "2026-01-01T00:01:00+00:00", "tool": "run_shell", "task_id": "bbb"},
    ])
    calls = _collect_tool_calls("aaa", drive_root)
    assert len(calls) == 1
    assert calls[0]["tool"] == "repo_read"


def test_collect_reflection(drive_root):
    _write_jsonl(drive_root / "logs" / "task_reflections.jsonl", [
        {"task_id": "aaa", "reflection": "it went well", "key_markers": ["TESTS_FAILED"]},
        {"task_id": "bbb", "reflection": "other task"},
    ])
    ref = _collect_reflection("aaa", drive_root)
    assert ref is not None
    assert ref["reflection"] == "it went well"


# ── build_digest ──────────────────────────────────────────────────────────────

def test_build_digest_basic():
    events = [
        {"ts": "2026-01-01T00:00:00+00:00", "type": "task_received",
         "task_id": "aaa",
         "task": {"id": "aaa", "type": "evolution", "text": "do things", "queued_at": "2026-01-01T00:00:00+00:00"}},
        {"ts": "2026-01-01T00:01:00+00:00", "type": "llm_round", "task_id": "aaa",
         "round": 1, "model": "copilot/claude-sonnet-4.6",
         "prompt_tokens": 1000, "completion_tokens": 200,
         "cost_usd": 0.0, "shadow_cost": 0.05},
    ]
    tool_calls = []
    reflection = None
    d = _build_digest("aaa", events, tool_calls, reflection)
    assert d["task_id"] == "aaa"
    assert d["task_type"] == "evolution"
    assert d["goal"] == "do things"
    assert d["rounds"] == 1
    assert d["prompt_tokens"] == 1000
    assert d["shadow_cost_usd"] == pytest.approx(0.05)


def test_build_digest_duration():
    events = [
        {"ts": "2026-01-01T00:00:00+00:00", "type": "task_received", "task_id": "x",
         "task": {"id": "x", "type": "chat", "text": "hi", "queued_at": "2026-01-01T00:00:00+00:00"}},
        {"ts": "2026-01-01T00:00:45+00:00", "type": "llm_round", "task_id": "x",
         "prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0, "shadow_cost": 0},
    ]
    d = _build_digest("x", events, [], None)
    assert d["duration_s"] == pytest.approx(45.0)


# ── format_text ───────────────────────────────────────────────────────────────

def test_format_text_contains_key_fields():
    digest = {
        "task_id": "abc123",
        "task_type": "evolution",
        "goal": "improve memory",
        "queued_at": "2026-01-01T00:00:00+00:00",
        "start_ts": "2026-01-01T00:00:00+00:00",
        "end_ts": "2026-01-01T00:01:00+00:00",
        "duration_s": 60.0,
        "model": "copilot/claude-sonnet-4.6",
        "rounds": 5,
        "prompt_tokens": 5000,
        "completion_tokens": 500,
        "cost_usd": 0.0,
        "shadow_cost_usd": 0.12,
        "tool_calls": [],
        "errors": [],
        "reflection": None,
    }
    text = _format_text(digest)
    assert "abc123" in text
    assert "evolution" in text
    assert "improve memory" in text
    assert "60.0s" in text
    assert "No errors" in text


# ── tool registration ──────────────────────────────────────────────────────────

def test_get_tools_returns_one_entry():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "task_digest"


def test_task_digest_schema_has_required():
    tools = get_tools()
    schema = tools[0].schema
    assert "task_id" in schema["parameters"]["required"]
