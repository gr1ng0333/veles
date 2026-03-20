"""Tests for ouroboros.reflection — Execution Reflections (process memory)."""

from __future__ import annotations

import json
import pathlib

import pytest

from ouroboros.reflection import (
    maybe_create_reflection,
    should_generate_reflection,
    format_recent_reflections,
    generate_reflection_template,
    _detect_markers,
    _collect_error_details,
    _build_trace_summary,
)


# ------------------------------------------------------------------
# should_generate_reflection
# ------------------------------------------------------------------


def test_no_reflection_for_clean_task():
    """Successful task with 0 errors should not generate reflection."""
    assert not should_generate_reflection(
        task_eval={"ok": True, "tool_errors": 0, "tool_calls": 5},
        response_text="Done successfully",
        llm_trace={"tool_calls": [{"tool": "run_shell", "result": "ok"}]},
        rounds=3,
        max_rounds=30,
    )


def test_reflection_triggered_by_tool_errors():
    assert should_generate_reflection(
        task_eval={"ok": True, "tool_errors": 3, "tool_calls": 10},
        response_text="Fixed with issues",
        llm_trace={"tool_calls": []},
        rounds=8,
        max_rounds=30,
    )


def test_reflection_triggered_by_failed_task():
    assert should_generate_reflection(
        task_eval={"ok": False, "tool_errors": 0, "tool_calls": 2},
        response_text="Failed to complete",
        llm_trace={"tool_calls": []},
        rounds=5,
        max_rounds=30,
    )


def test_reflection_triggered_by_near_max_rounds():
    assert should_generate_reflection(
        task_eval={"ok": True, "tool_errors": 0, "tool_calls": 25},
        response_text="Done",
        llm_trace={"tool_calls": []},
        rounds=26,
        max_rounds=30,
    )


def test_reflection_triggered_at_exactly_80_percent():
    # 24/30 = 80% — should trigger
    assert should_generate_reflection(
        task_eval={"ok": True, "tool_errors": 0},
        response_text="ok",
        llm_trace={"tool_calls": []},
        rounds=24,
        max_rounds=30,
    )


def test_no_reflection_at_79_percent():
    # 23/30 = 76.7% — should NOT trigger
    assert not should_generate_reflection(
        task_eval={"ok": True, "tool_errors": 0},
        response_text="ok",
        llm_trace={"tool_calls": []},
        rounds=23,
        max_rounds=30,
    )


def test_reflection_triggered_by_error_marker_in_response():
    assert should_generate_reflection(
        task_eval={"ok": True, "tool_errors": 0},
        response_text="Build failed: TESTS_FAILED with 3 errors",
        llm_trace={"tool_calls": []},
        rounds=5,
        max_rounds=30,
    )


def test_reflection_triggered_by_is_error_in_trace():
    assert should_generate_reflection(
        task_eval={"ok": True, "tool_errors": 0},
        response_text="Done",
        llm_trace={"tool_calls": [
            {"tool": "run_shell", "result": "command not found", "is_error": True},
        ]},
        rounds=2,
        max_rounds=30,
    )


def test_reflection_triggered_by_marker_in_trace_result():
    assert should_generate_reflection(
        task_eval={"ok": True, "tool_errors": 0},
        response_text="ok",
        llm_trace={"tool_calls": [
            {"tool": "repo_write_commit", "result": "COMMIT_BLOCKED: lint errors"},
        ]},
        rounds=3,
        max_rounds=30,
    )


# ------------------------------------------------------------------
# maybe_create_reflection — full pipeline
# ------------------------------------------------------------------


def test_maybe_create_reflection_returns_none_for_clean_task(tmp_path):
    result = maybe_create_reflection(
        task_id="test1",
        task_text="do something",
        task_eval={"ok": True, "tool_errors": 0, "tool_calls": 5},
        response_text="Done successfully",
        rounds=3,
        max_rounds=30,
        drive_root=tmp_path,
        llm_trace={"tool_calls": []},
    )
    assert result is None


def test_maybe_create_reflection_for_tool_errors(tmp_path):
    result = maybe_create_reflection(
        task_id="test2",
        task_text="fix the bug",
        task_eval={"ok": True, "tool_errors": 3, "tool_calls": 10},
        response_text="Fixed with issues",
        rounds=8,
        max_rounds=30,
        drive_root=tmp_path,
        llm_trace={"tool_calls": [
            {"tool": "run_shell", "result": "error", "is_error": True},
            {"tool": "run_shell", "result": "error", "is_error": True},
            {"tool": "repo_write_commit", "result": "error", "is_error": True},
        ]},
    )
    assert result is not None
    assert result["task_id"] == "test2"
    assert result["error_count"] == 3


def test_maybe_create_reflection_for_failed_task(tmp_path):
    result = maybe_create_reflection(
        task_id="test3",
        task_text="deploy changes",
        task_eval={"ok": False, "tool_errors": 0, "tool_calls": 2},
        response_text="Failed to complete",
        rounds=5,
        max_rounds=30,
        drive_root=tmp_path,
        llm_trace={"tool_calls": []},
    )
    assert result is not None
    assert "Failed" in result["reflection"] or "failed" in result["reflection"]


def test_maybe_create_reflection_for_near_max_rounds(tmp_path):
    result = maybe_create_reflection(
        task_id="test4",
        task_text="complex refactoring",
        task_eval={"ok": True, "tool_errors": 0, "tool_calls": 25},
        response_text="Done",
        rounds=26,
        max_rounds=30,
        drive_root=tmp_path,
        llm_trace={"tool_calls": []},
    )
    assert result is not None
    assert "26/30" in result["reflection"] or result["rounds"] == 26


def test_reflection_persisted_to_jsonl(tmp_path):
    maybe_create_reflection(
        task_id="test5",
        task_text="test persistence",
        task_eval={"ok": False, "tool_errors": 1, "tool_calls": 3},
        response_text="Error occurred",
        rounds=5,
        max_rounds=30,
        drive_root=tmp_path,
        llm_trace={"tool_calls": [
            {"tool": "run_shell", "result": "fail", "is_error": True},
        ]},
    )
    path = tmp_path / "logs" / "task_reflections.jsonl"
    assert path.exists()
    lines = path.read_text().strip().split("\n")
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["task_id"] == "test5"
    assert "key_markers" in data
    assert "reflection" in data


def test_multiple_reflections_appended(tmp_path):
    for i in range(3):
        maybe_create_reflection(
            task_id=f"multi_{i}",
            task_text=f"task {i}",
            task_eval={"ok": False, "tool_errors": 0, "tool_calls": 1},
            response_text="failed",
            rounds=1,
            max_rounds=30,
            drive_root=tmp_path,
            llm_trace={"tool_calls": []},
        )
    path = tmp_path / "logs" / "task_reflections.jsonl"
    lines = path.read_text().strip().split("\n")
    assert len(lines) == 3
    for i, line in enumerate(lines):
        data = json.loads(line)
        assert data["task_id"] == f"multi_{i}"


# ------------------------------------------------------------------
# generate_reflection_template
# ------------------------------------------------------------------


def test_template_reflection_for_failed_task():
    entry = generate_reflection_template(
        task_id="t1",
        task_text="deploy",
        task_eval={"ok": False, "tool_errors": 0},
        response_text="failed",
        llm_trace={"tool_calls": []},
        rounds=5,
        max_rounds=30,
    )
    assert entry["task_id"] == "t1"
    assert "Task failed" in entry["reflection"]


def test_template_reflection_with_error_tools():
    entry = generate_reflection_template(
        task_id="t2",
        task_text="fix bug",
        task_eval={"ok": True, "tool_errors": 2},
        response_text="done",
        llm_trace={"tool_calls": [
            {"tool": "run_shell", "is_error": True, "result": "err"},
            {"tool": "repo_write_commit", "is_error": True, "result": "err"},
        ]},
        rounds=10,
        max_rounds=30,
    )
    assert "run_shell" in entry["reflection"]
    assert entry["error_count"] == 2
    assert "run_shell" in entry["error_tools"]


def test_template_near_max_rounds():
    entry = generate_reflection_template(
        task_id="t3",
        task_text="refactor",
        task_eval={"ok": True, "tool_errors": 0},
        response_text="done",
        llm_trace={"tool_calls": []},
        rounds=25,
        max_rounds=30,
    )
    assert "25/30" in entry["reflection"]


# ------------------------------------------------------------------
# _detect_markers
# ------------------------------------------------------------------


def test_detect_markers_from_trace():
    markers = _detect_markers(
        {"tool_calls": [
            {"tool": "x", "result": "TESTS_FAILED: 3 errors"},
            {"tool": "y", "result": "COMMIT_BLOCKED by lint"},
        ]},
        "",
    )
    assert "COMMIT_BLOCKED" in markers
    assert "TESTS_FAILED" in markers


def test_detect_markers_from_response():
    markers = _detect_markers(
        {"tool_calls": []},
        "REVIEW_BLOCKED due to missing tests",
    )
    assert "REVIEW_BLOCKED" in markers


def test_detect_markers_empty():
    assert _detect_markers({"tool_calls": []}, "") == []


# ------------------------------------------------------------------
# _collect_error_details
# ------------------------------------------------------------------


def test_collect_error_details_filters_errors():
    details = _collect_error_details({
        "tool_calls": [
            {"tool": "run_shell", "result": "success"},
            {"tool": "run_shell", "result": "TOOL_ERROR: bad cmd", "is_error": True},
            {"tool": "repo_read", "result": "file contents..."},
        ],
    })
    assert "TOOL_ERROR" in details
    assert "success" not in details
    assert "file contents" not in details


def test_collect_error_details_respects_cap():
    details = _collect_error_details(
        {"tool_calls": [
            {"tool": "x", "result": "A" * 5000, "is_error": True},
        ]},
        cap=100,
    )
    assert len(details) <= 200  # allows some overhead for tool name prefix


def test_collect_error_details_empty():
    assert _collect_error_details({"tool_calls": []}) == "(no error details captured)"


# ------------------------------------------------------------------
# _build_trace_summary
# ------------------------------------------------------------------


def test_build_trace_summary_basic():
    summary = _build_trace_summary({
        "tool_calls": [
            {"tool": "run_shell", "result": "ok"},
            {"tool": "repo_write_commit", "result": "err", "is_error": True},
        ],
    })
    assert "run_shell [ok]" in summary
    assert "repo_write_commit [ERROR]" in summary


def test_build_trace_summary_empty():
    assert _build_trace_summary({"tool_calls": []}) == "(no tool calls)"


# ------------------------------------------------------------------
# format_recent_reflections
# ------------------------------------------------------------------


def test_format_reflections_empty():
    assert format_recent_reflections([]) == ""


def test_format_reflections_single():
    entries = [{
        "ts": "2026-03-20T12:00:00Z",
        "task_id": "abc12345",
        "goal": "fix tests",
        "key_markers": ["TESTS_FAILED"],
        "rounds": 15,
        "max_rounds": 30,
        "error_count": 3,
        "reflection": "Tests failed due to missing fixture. Add conftest.py.",
    }]
    text = format_recent_reflections(entries)
    assert "### 2026-03-20T12:00" in text
    assert "abc12345" in text
    assert "fix tests" in text
    assert "TESTS_FAILED" in text
    assert "15/30" in text
    assert "missing fixture" in text


def test_format_reflections_respects_limit():
    entries = [
        {
            "ts": f"2026-03-{20 + i}T12:00:00Z",
            "task_id": f"id_{i}",
            "goal": f"task {i}",
            "reflection": f"Reflection {i}",
        }
        for i in range(15)
    ]
    text = format_recent_reflections(entries, limit=5)
    # Only last 5 entries should appear
    assert "id_10" in text
    assert "id_14" in text
    assert "id_0" not in text
    assert "id_9" not in text


def test_format_reflections_respects_max_chars():
    entries = [
        {
            "ts": "2026-03-20T12:00:00Z",
            "task_id": f"id_{i}",
            "goal": "x" * 200,
            "reflection": "y" * 500,
        }
        for i in range(20)
    ]
    text = format_recent_reflections(entries, limit=20, max_chars=2000)
    assert len(text) <= 3000  # approximate, blocks are added atomically


def test_format_reflections_no_max_rounds_shows_just_rounds():
    entries = [{
        "ts": "2026-03-20T12:00:00Z",
        "task_id": "abc",
        "rounds": 5,
        "reflection": "ok",
    }]
    text = format_recent_reflections(entries)
    assert "Rounds: 5" in text
    assert "/" not in text.split("Rounds:")[1].split("\n")[0]


# ------------------------------------------------------------------
# maybe_create_reflection — exception safety
# ------------------------------------------------------------------


def test_maybe_create_reflection_never_raises(tmp_path):
    """Even with broken inputs, should return None instead of raising."""
    result = maybe_create_reflection(
        task_id=None,  # type: ignore
        task_text=None,  # type: ignore
        task_eval={},  # missing keys
        response_text=None,  # type: ignore
        rounds="abc",  # type: ignore — intentionally wrong type
        max_rounds=None,  # type: ignore
        drive_root=tmp_path,
        llm_trace={},
    )
    # Should not raise, returns either None or a reflection
    assert result is None or isinstance(result, dict)
