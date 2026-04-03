"""Tests for evolution_report tool."""

from __future__ import annotations

import json
import pathlib
import tempfile
from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch, MagicMock

import pytest

from ouroboros.tools.evolution_report import (
    _parse_ts,
    _fmt_dur,
    _build_evolution_record,
    _collect_evolution_tasks,
    _format_text,
    _evolution_report,
    get_tools,
)
from ouroboros.tools.registry import ToolContext


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_ts(h: int, m: int = 0) -> str:
    return f"2026-04-03T{h:02d}:{m:02d}:00+00:00"


def _make_events(task_id: str = "abc12345", cycle: int = 100) -> list:
    return [
        {"type": "evolution_enqueued", "ts": _make_ts(10), "task_id": task_id, "cycle": cycle},
        {"type": "task_received", "task_id": task_id, "ts": _make_ts(10), "task_type": "evolution"},
        {"type": "llm_usage", "task_id": task_id, "ts": _make_ts(10, 5),
         "model": "copilot/claude-sonnet-4.6", "shadow_cost": 0.05, "cost": 0.0},
        {"type": "llm_usage", "task_id": task_id, "ts": _make_ts(10, 10),
         "model": "copilot/claude-sonnet-4.6", "shadow_cost": 0.04, "cost": 0.0},
        {"type": "task_done", "task_id": task_id, "ts": _make_ts(10, 20),
         "task_type": "evolution", "rounds": 18},
    ]


def _make_ctx(drive_root: str) -> ToolContext:
    ctx = MagicMock(spec=ToolContext)
    ctx.drive_root = pathlib.Path(drive_root)
    return ctx


# ── Unit tests ────────────────────────────────────────────────────────────────

def test_parse_ts_valid():
    dt = _parse_ts("2026-04-03T10:00:00+00:00")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_ts_invalid():
    assert _parse_ts("") is None
    assert _parse_ts("not-a-date") is None


def test_fmt_dur_seconds():
    assert _fmt_dur(45) == "45s"


def test_fmt_dur_minutes():
    result = _fmt_dur(125)
    assert "2m" in result


def test_fmt_dur_hours():
    result = _fmt_dur(3700)
    assert "h" in result


def test_collect_evolution_tasks_basic():
    events = _make_events("task001", cycle=42)
    tasks = _collect_evolution_tasks(events, limit=10, target_cycle=None)
    assert len(tasks) == 1
    tid, cycle, start_dt, end_dt = tasks[0]
    assert "task001" in tid
    assert cycle == 42
    assert start_dt is not None
    assert end_dt is not None


def test_collect_evolution_tasks_limit():
    all_events = []
    for i in range(7):
        all_events.extend(_make_events(f"task{i:04d}", cycle=i + 1))
    tasks = _collect_evolution_tasks(all_events, limit=3, target_cycle=None)
    assert len(tasks) == 3


def test_collect_evolution_tasks_cycle_filter():
    all_events = _make_events("task0001", cycle=10) + _make_events("task0002", cycle=20)
    tasks = _collect_evolution_tasks(all_events, limit=10, target_cycle=20)
    assert len(tasks) == 1
    assert tasks[0][1] == 20


def test_collect_evolution_tasks_no_end():
    """Running tasks should have end_dt=None."""
    events = [
        {"type": "evolution_enqueued", "ts": _make_ts(10), "task_id": "running01", "cycle": 99},
        {"type": "task_received", "task_id": "running01", "ts": _make_ts(10), "task_type": "evolution"},
    ]
    tasks = _collect_evolution_tasks(events, limit=10, target_cycle=None)
    assert len(tasks) == 1
    assert tasks[0][3] is None  # end_dt is None


def test_build_evolution_record_no_commits():
    """build_evolution_record with no git commits should still return valid record."""
    events = _make_events("abcd1234", cycle=5)
    start_dt = _parse_ts(_make_ts(10))
    end_dt = _parse_ts(_make_ts(10, 20))
    with patch("ouroboros.tools.evolution_report._git_commits_in_window", return_value=[]):
        rec = _build_evolution_record("abcd1234", 5, start_dt, end_dt, events, [])
    assert rec["task_id"] == "abcd1234"[:8]
    assert rec["cycle"] == 5
    assert rec["status"] == "done"
    assert rec["commits"] == []
    assert rec["shadow_cost_usd"] == pytest.approx(0.09, abs=0.01)


def test_build_evolution_record_with_commits():
    events = _make_events("abcd5678", cycle=6)
    start_dt = _parse_ts(_make_ts(10))
    end_dt = _parse_ts(_make_ts(10, 20))
    fake_commits = [{"hash": "deadbeef1234", "ts": _make_ts(10, 10), "msg": "feat: add something"}]
    with patch("ouroboros.tools.evolution_report._git_commits_in_window", return_value=fake_commits):
        with patch("ouroboros.tools.evolution_report._git_diff_stat", return_value="2 files changed, 30 insertions"):
            rec = _build_evolution_record("abcd5678", 6, start_dt, end_dt, events, [])
    assert len(rec["commits"]) == 1
    assert rec["commits"][0]["hash"] == "deadbeef"


def test_build_evolution_record_tool_timeouts():
    events = _make_events("task9999", cycle=7) + [
        {"type": "tool_timeout", "task_id": "task9999", "ts": _make_ts(10, 5), "tool": "repo_write_commit"},
    ]
    start_dt = _parse_ts(_make_ts(10))
    end_dt = _parse_ts(_make_ts(10, 20))
    with patch("ouroboros.tools.evolution_report._git_commits_in_window", return_value=[]):
        rec = _build_evolution_record("task9999", 7, start_dt, end_dt, events, [])
    assert "repo_write_commit" in rec["tool_timeouts"]


def test_format_text_no_records():
    result = _format_text([])
    assert "No evolution tasks found" in result


def test_format_text_with_records():
    rec = {
        "task_id": "abcd1234",
        "cycle": 42,
        "status": "done",
        "start_ts": _make_ts(10),
        "end_ts": _make_ts(10, 20),
        "duration": "20m",
        "rounds": 18,
        "models": {"copilot/claude-sonnet-4.6": 18},
        "shadow_cost_usd": 0.09,
        "real_cost_usd": 0.0,
        "tool_errors": [],
        "tool_timeouts": [],
        "commits": [
            {"hash": "deadbeef", "ts": _make_ts(10, 10), "msg": "feat: add something", "stat": "2 files changed"},
        ],
    }
    result = _format_text([rec])
    assert "cycle #42" in result
    assert "deadbeef" in result
    assert "feat: add something" in result
    assert "20m" in result


def test_evolution_report_tool_no_events():
    with tempfile.TemporaryDirectory() as tmpdir:
        (pathlib.Path(tmpdir) / "logs").mkdir()
        ctx = _make_ctx(tmpdir)
        result = _evolution_report(ctx, limit=5)
    assert "No evolution tasks found" in result or "no evolution tasks" in result.lower()


def test_evolution_report_tool_json_format():
    """JSON format should return a valid JSON list."""
    events = _make_events("jsontest1", cycle=1)
    with tempfile.TemporaryDirectory() as tmpdir:
        logs_dir = pathlib.Path(tmpdir) / "logs"
        logs_dir.mkdir()
        events_file = logs_dir / "events.jsonl"
        with events_file.open("w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        # No tools.jsonl — should handle gracefully
        ctx = _make_ctx(tmpdir)
        with patch("ouroboros.tools.evolution_report._git_commits_in_window", return_value=[]):
            result = _evolution_report(ctx, limit=5, format="json")
    parsed = json.loads(result)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["cycle"] == 1


def test_get_tools_registration():
    """Tool is registered with correct name and schema."""
    tools = get_tools()
    assert len(tools) == 1
    t = tools[0]
    assert t.name == "evolution_report"
    assert "evolution_report" in t.schema["name"]
    assert "parameters" in t.schema
