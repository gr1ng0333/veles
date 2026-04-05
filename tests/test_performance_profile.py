"""Tests for performance_profile — runtime observability tool."""

import json
import pathlib
import tempfile
from datetime import datetime, timezone, timedelta

import pytest

from ouroboros.tools.performance_profile import (
    _parse_ts,
    _load_jsonl,
    _compute_tool_stats,
    _compute_model_stats,
    _compute_task_stats,
    _compute_error_stats,
    _performance_profile,
    get_tools,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _iso_ago(days: float = 0, hours: float = 0) -> str:
    dt = datetime.now(tz=timezone.utc) - timedelta(days=days, hours=hours)
    return dt.isoformat()


def _make_drive(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a minimal drive structure with test log files."""
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    return tmp_path


def _write_tools_jsonl(drive: pathlib.Path, records: list) -> None:
    p = drive / "logs" / "tools.jsonl"
    with open(p, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _write_events_jsonl(drive: pathlib.Path, records: list) -> None:
    p = drive / "logs" / "events.jsonl"
    with open(p, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ── _parse_ts ──────────────────────────────────────────────────────────────────

def test_parse_ts_iso():
    dt = _parse_ts("2026-04-05T03:00:00+00:00")
    assert dt is not None
    assert dt.year == 2026


def test_parse_ts_z():
    dt = _parse_ts("2026-04-05T03:00:00Z")
    assert dt is not None


def test_parse_ts_invalid():
    assert _parse_ts("not-a-date") is None
    assert _parse_ts("") is None


# ── _load_jsonl ────────────────────────────────────────────────────────────────

def test_load_jsonl_filters_old(tmp_path):
    p = tmp_path / "test.jsonl"
    old = {"ts": _iso_ago(days=10), "tool": "old"}
    new = {"ts": _iso_ago(days=1), "tool": "new"}
    with open(p, "w") as f:
        f.write(json.dumps(old) + "\n")
        f.write(json.dumps(new) + "\n")
    since = datetime.now(tz=timezone.utc) - timedelta(days=3)
    result = _load_jsonl(p, since)
    assert len(result) == 1
    assert result[0]["tool"] == "new"


def test_load_jsonl_missing_file(tmp_path):
    result = _load_jsonl(tmp_path / "nonexistent.jsonl", datetime.now(tz=timezone.utc))
    assert result == []


def test_load_jsonl_skips_invalid_json(tmp_path):
    p = tmp_path / "test.jsonl"
    with open(p, "w") as f:
        f.write("not-json\n")
        f.write(json.dumps({"ts": _iso_ago(hours=1), "tool": "ok"}) + "\n")
    since = datetime.now(tz=timezone.utc) - timedelta(hours=2)
    result = _load_jsonl(p, since)
    assert len(result) == 1
    assert result[0]["tool"] == "ok"


# ── _compute_tool_stats ────────────────────────────────────────────────────────

def test_tool_stats_basic():
    tool_records = [
        {"tool": "repo_read", "task_id": "t1"},
        {"tool": "repo_read", "task_id": "t1"},
        {"tool": "run_shell", "task_id": "t2"},
    ]
    timeout_records = [{"tool": "run_shell"}]
    stats = _compute_tool_stats(tool_records, timeout_records)
    assert stats[0]["tool"] == "repo_read"
    assert stats[0]["calls"] == 2
    assert stats[0]["unique_tasks"] == 1
    assert stats[0]["timeouts"] == 0
    shell = next(s for s in stats if s["tool"] == "run_shell")
    assert shell["timeouts"] == 1
    assert shell["timeout_rate"] == 1.0


def test_tool_stats_no_timeouts():
    tool_records = [{"tool": "chat_history", "task_id": "t1"}]
    stats = _compute_tool_stats(tool_records, [])
    assert stats[0]["timeout_rate"] == 0.0


def test_tool_stats_empty():
    assert _compute_tool_stats([], []) == []


def test_tool_stats_sorted_by_calls():
    records = [
        {"tool": "a", "task_id": "t1"},
        {"tool": "b", "task_id": "t2"},
        {"tool": "b", "task_id": "t3"},
    ]
    stats = _compute_tool_stats(records, [])
    assert stats[0]["tool"] == "b"


# ── _compute_model_stats ───────────────────────────────────────────────────────

def test_model_stats_basic():
    rounds = [
        {"model": "claude-sonnet", "cost_usd": 0.01, "prompt_tokens": 1000,
         "completion_tokens": 100, "cached_tokens": 800},
        {"model": "claude-sonnet", "cost_usd": 0.02, "prompt_tokens": 2000,
         "completion_tokens": 200, "cached_tokens": 1000},
        {"model": "gpt-4o", "cost_usd": 0.05, "prompt_tokens": 500,
         "completion_tokens": 50, "cached_tokens": 0},
    ]
    stats = _compute_model_stats(rounds)
    # sorted by cost desc → gpt-4o first
    assert stats[0]["model"] == "gpt-4o"
    sonnet = next(s for s in stats if s["model"] == "claude-sonnet")
    assert sonnet["rounds"] == 2
    assert abs(sonnet["cost_usd"] - 0.03) < 1e-9
    assert sonnet["avg_cost_per_round"] == pytest.approx(0.015, abs=1e-9)
    assert sonnet["cache_hit_rate"] == pytest.approx(1800 / 3000, rel=1e-3)


def test_model_stats_empty():
    assert _compute_model_stats([]) == []


def test_model_stats_zero_prompt_tokens():
    rounds = [{"model": "m", "cost_usd": 0.0, "prompt_tokens": 0,
               "completion_tokens": 0, "cached_tokens": 0}]
    stats = _compute_model_stats(rounds)
    assert stats[0]["cache_hit_rate"] == 0.0


# ── _compute_task_stats ────────────────────────────────────────────────────────

def test_task_stats_basic():
    evals = [
        {"task_type": "evolution", "ok": True, "duration_sec": 30.0, "tool_calls": 5, "tool_errors": 0},
        {"task_type": "evolution", "ok": False, "duration_sec": 10.0, "tool_calls": 2, "tool_errors": 1},
        {"task_type": "task", "ok": True, "duration_sec": 5.0, "tool_calls": 3, "tool_errors": 0},
    ]
    stats = _compute_task_stats(evals)
    evo = next(s for s in stats if s["task_type"] == "evolution")
    assert evo["count"] == 2
    assert evo["failed"] == 1
    assert evo["error_rate"] == 0.5
    assert evo["avg_duration_sec"] == 20.0


def test_task_stats_empty():
    assert _compute_task_stats([]) == []


def test_task_stats_p95():
    # 20 items: p95 = item at index 19 of sorted list
    evals = [
        {"task_type": "t", "ok": True, "duration_sec": float(i), "tool_calls": 1, "tool_errors": 0}
        for i in range(20)
    ]
    stats = _compute_task_stats(evals)
    assert stats[0]["p95_duration_sec"] == 19.0


def test_task_stats_missing_fields():
    # Records without optional fields should not crash
    evals = [{"task_type": "task", "ok": True}]
    stats = _compute_task_stats(evals)
    assert stats[0]["avg_duration_sec"] is None
    assert stats[0]["avg_tool_calls"] is None


# ── _compute_error_stats ───────────────────────────────────────────────────────

def test_error_stats_basic():
    api_errors = [
        {"type": "llm_api_error", "error": "500 Internal"},
        {"type": "llm_api_error", "error": "500 Internal"},
        {"type": "llm_api_error", "error": "429 Rate Limit"},
    ]
    timeouts = [
        {"type": "tool_timeout", "tool": "run_shell"},
        {"type": "tool_timeout", "tool": "run_shell"},
        {"type": "tool_timeout", "tool": "repo_write_commit"},
    ]
    stats = _compute_error_stats(api_errors, timeouts)
    assert stats["api_errors_total"] == 3
    assert stats["tool_timeouts_total"] == 3
    assert stats["top_timeout_tools"][0] == ("run_shell", 2)
    assert stats["top_api_errors"][0][1] == 2  # "500 Internal" × 2


def test_error_stats_empty():
    stats = _compute_error_stats([], [])
    assert stats["api_errors_total"] == 0
    assert stats["tool_timeouts_total"] == 0
    assert stats["top_api_errors"] == []
    assert stats["top_timeout_tools"] == []


# ── _performance_profile (integration) ────────────────────────────────────────

def _make_test_drive(tmp_path):
    drive = _make_drive(tmp_path)
    _write_tools_jsonl(drive, [
        {"ts": _iso_ago(hours=1), "tool": "repo_read", "task_id": "t1"},
        {"ts": _iso_ago(hours=2), "tool": "run_shell", "task_id": "t2"},
        {"ts": _iso_ago(hours=2), "tool": "run_shell", "task_id": "t2"},
    ])
    _write_events_jsonl(drive, [
        {"ts": _iso_ago(hours=1), "type": "llm_round", "model": "sonnet", "task_id": "t1",
         "round": 1, "cost_usd": 0.01, "prompt_tokens": 1000, "completion_tokens": 100,
         "cached_tokens": 500, "cache_write_tokens": 0},
        {"ts": _iso_ago(hours=1), "type": "task_eval", "task_id": "t1", "task_type": "evolution",
         "ok": True, "duration_sec": 30.0, "tool_calls": 5, "tool_errors": 0},
        {"ts": _iso_ago(hours=2), "type": "llm_api_error", "task_id": "t2",
         "error": "500 Internal Server Error"},
        {"ts": _iso_ago(hours=2), "type": "tool_timeout", "task_id": "t2", "tool": "run_shell"},
    ])
    return drive


def test_performance_profile_text(tmp_path):
    drive = _make_test_drive(tmp_path)
    result = _performance_profile(None, days=7, view="all", format="text",
                                   _drive_root=str(drive))
    assert "Performance Profile" in result
    assert "Tool Call Profile" in result
    assert "Model Cost Profile" in result
    assert "Task Type Profile" in result
    assert "Error" in result


def test_performance_profile_json(tmp_path):
    drive = _make_test_drive(tmp_path)
    result = _performance_profile(None, days=7, view="all", format="json",
                                   _drive_root=str(drive))
    data = json.loads(result)
    assert "summary" in data
    assert "tools" in data
    assert "models" in data
    assert "tasks" in data
    assert "errors" in data
    assert data["summary"]["tool_calls"] == 3
    assert data["summary"]["llm_rounds"] == 1


def test_performance_profile_view_tools(tmp_path):
    drive = _make_test_drive(tmp_path)
    result = _performance_profile(None, days=7, view="tools", format="text",
                                   _drive_root=str(drive))
    assert "Tool Call Profile" in result
    assert "Model Cost Profile" not in result


def test_performance_profile_view_models(tmp_path):
    drive = _make_test_drive(tmp_path)
    result = _performance_profile(None, days=7, view="models", format="text",
                                   _drive_root=str(drive))
    assert "Model Cost Profile" in result
    assert "Task Type Profile" not in result


def test_performance_profile_view_tasks(tmp_path):
    drive = _make_test_drive(tmp_path)
    result = _performance_profile(None, days=7, view="tasks", format="text",
                                   _drive_root=str(drive))
    assert "Task Type Profile" in result
    assert "Tool Call Profile" not in result


def test_performance_profile_view_errors(tmp_path):
    drive = _make_test_drive(tmp_path)
    result = _performance_profile(None, days=7, view="errors", format="text",
                                   _drive_root=str(drive))
    assert "Error" in result
    assert "Tool Call Profile" not in result


def test_performance_profile_empty_logs(tmp_path):
    drive = _make_drive(tmp_path)
    _write_tools_jsonl(drive, [])
    _write_events_jsonl(drive, [])
    result = _performance_profile(None, days=7, view="all", format="text",
                                   _drive_root=str(drive))
    assert "Performance Profile" in result
    assert "No tool calls found" in result


def test_performance_profile_no_log_files(tmp_path):
    """Should not crash if log files don't exist."""
    drive = _make_drive(tmp_path)
    result = _performance_profile(None, days=7, view="all", format="text",
                                   _drive_root=str(drive))
    assert "Performance Profile" in result


def test_performance_profile_old_records_excluded(tmp_path):
    """Records older than the window should not be counted."""
    drive = _make_drive(tmp_path)
    _write_tools_jsonl(drive, [
        {"ts": _iso_ago(days=30), "tool": "old_tool", "task_id": "t_old"},
        {"ts": _iso_ago(hours=1), "tool": "new_tool", "task_id": "t_new"},
    ])
    _write_events_jsonl(drive, [])
    result = _performance_profile(None, days=7, view="tools", format="json",
                                   _drive_root=str(drive))
    data = json.loads(result)
    tool_names = [t["tool"] for t in data["tools"]]
    assert "new_tool" in tool_names
    assert "old_tool" not in tool_names


def test_performance_profile_timeout_flag_in_text(tmp_path):
    """Tools with >5% timeout rate should show ⚠ in text output."""
    drive = _make_drive(tmp_path)
    # 10 calls to run_shell, 1 timeout = 10% rate
    _write_tools_jsonl(drive, [
        {"ts": _iso_ago(hours=1), "tool": "run_shell", "task_id": f"t{i}"}
        for i in range(10)
    ])
    _write_events_jsonl(drive, [
        {"ts": _iso_ago(hours=1), "type": "tool_timeout", "tool": "run_shell"},
    ])
    result = _performance_profile(None, days=7, view="tools", format="text",
                                   _drive_root=str(drive))
    assert "⚠" in result


def test_get_tools_registration():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "performance_profile"
    schema = tools[0].schema
    assert schema["name"] == "performance_profile"
    assert "parameters" in schema
    props = schema["parameters"]["properties"]
    assert "days" in props
    assert "view" in props
    assert "format" in props


def test_get_tools_view_enum():
    tools = get_tools()
    view_prop = tools[0].schema["parameters"]["properties"]["view"]
    assert "all" in view_prop["enum"]
    assert "tools" in view_prop["enum"]
    assert "models" in view_prop["enum"]
    assert "tasks" in view_prop["enum"]
    assert "errors" in view_prop["enum"]
