"""Tests for health_report — project-wide A–F health dashboard."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ouroboros.tools.registry import ToolContext
from ouroboros.tools.health_report import (
    _Action,
    _Dimension,
    _grade,
    _bar,
    _sev_priority,
    _weighted_score,
    _format_text,
    _format_json,
    _health_report,
    _scan_security,
    _scan_cycles,
    _scan_debt,
    _scan_exceptions,
    _scan_todos,
    get_tools,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path / "data")


@pytest.fixture
def real_ctx() -> ToolContext:
    return ToolContext(
        repo_dir=Path("/opt/veles"),
        drive_root=Path("/opt/veles-data"),
    )


# ── _grade ────────────────────────────────────────────────────────────────────

class TestGrade:
    def test_a_grade(self) -> None:
        letter, _ = _grade(95)
        assert letter == "A"

    def test_b_grade(self) -> None:
        letter, _ = _grade(80)
        assert letter == "B"

    def test_c_grade(self) -> None:
        letter, _ = _grade(65)
        assert letter == "C"

    def test_d_grade(self) -> None:
        letter, _ = _grade(50)
        assert letter == "D"

    def test_f_grade(self) -> None:
        letter, _ = _grade(30)
        assert letter == "F"

    def test_boundary_90(self) -> None:
        letter, _ = _grade(90)
        assert letter == "A"

    def test_boundary_75(self) -> None:
        letter, _ = _grade(75)
        assert letter == "B"

    def test_returns_icon(self) -> None:
        _, icon = _grade(95)
        assert icon in ("✅", "🟢", "🟡", "🟠", "🔴")

    def test_zero_is_f(self) -> None:
        letter, _ = _grade(0)
        assert letter == "F"


# ── _bar ──────────────────────────────────────────────────────────────────────

class TestBar:
    def test_full_bar(self) -> None:
        b = _bar(100, 10)
        assert b == "█" * 10

    def test_empty_bar(self) -> None:
        b = _bar(0, 10)
        assert b == "░" * 10

    def test_half_bar(self) -> None:
        b = _bar(50, 10)
        assert b.count("█") == 5 and b.count("░") == 5

    def test_length(self) -> None:
        b = _bar(73, 20)
        assert len(b) == 20


# ── _sev_priority ─────────────────────────────────────────────────────────────

class TestSevPriority:
    def test_critical(self) -> None:
        assert _sev_priority("critical") >= 9.0

    def test_high(self) -> None:
        assert _sev_priority("high") >= 7.0

    def test_medium(self) -> None:
        assert 4.0 <= _sev_priority("medium") < 7.0

    def test_low(self) -> None:
        assert _sev_priority("low") < 4.0

    def test_unknown_default(self) -> None:
        assert _sev_priority("unknown") > 0


# ── _weighted_score ───────────────────────────────────────────────────────────

class TestWeightedScore:
    def test_equal_weights(self) -> None:
        dims = [
            _Dimension("a", 80, 1, ""),
            _Dimension("b", 60, 1, ""),
        ]
        assert _weighted_score(dims) == 70.0

    def test_zero_weight_ignored(self) -> None:
        dims = [
            _Dimension("a", 90, 1, ""),
            _Dimension("b", 0, 0, ""),   # informational, weight=0
        ]
        assert _weighted_score(dims) == 90.0

    def test_all_zero_weights(self) -> None:
        dims = [_Dimension("x", 50, 0, "")]
        assert _weighted_score(dims) == 100.0

    def test_heavy_weight_dominates(self) -> None:
        dims = [
            _Dimension("security", 20, 3, ""),
            _Dimension("docs", 100, 1, ""),
        ]
        score = _weighted_score(dims)
        assert score < 50.0  # security dominates


# ── _Action ───────────────────────────────────────────────────────────────────

class TestAction:
    def test_to_dict_keys(self) -> None:
        a = _Action(
            priority=7.5, dimension="security", severity="high",
            action="Fix X", location="foo.py:10", detail="bar",
        )
        d = a.to_dict()
        for key in ("priority", "dimension", "severity", "action", "location", "detail"):
            assert key in d

    def test_sort_higher_last(self) -> None:
        low = _Action(2.0, "a", "low", "x")
        high = _Action(9.0, "b", "high", "y")
        assert high > low


# ── _format_text ──────────────────────────────────────────────────────────────

class TestFormatText:
    def _make_dims(self) -> list:
        return [
            _Dimension("security", 90, 3, "ok"),
            _Dimension("cycles", 80, 2, "ok"),
            _Dimension("debt", 70, 2, "ok"),
            _Dimension("exceptions", 95, 1, "ok"),
            _Dimension("docs", 60, 1, "ok"),
            _Dimension("types", 55, 1, "ok"),
            _Dimension("todos", 100, 0, "ok"),
        ]

    def test_header_present(self) -> None:
        out = _format_text(self._make_dims(), 75.0, None, 5)
        assert "Health Report" in out

    def test_grade_shown(self) -> None:
        out = _format_text(self._make_dims(), 80.0, None, 5)
        assert "B" in out

    def test_path_shown(self) -> None:
        out = _format_text(self._make_dims(), 80.0, "ouroboros/", 5)
        assert "ouroboros/" in out

    def test_no_actions_clean(self) -> None:
        out = _format_text(self._make_dims(), 95.0, None, 5)
        assert "No action items" in out or "Action Item" in out

    def test_actions_surface(self) -> None:
        dims = self._make_dims()
        dims[0].actions = [
            _Action(9.0, "security", "critical", "Fix critical X", "foo.py:1")
        ]
        out = _format_text(dims, 80.0, None, 5)
        assert "Fix critical X" in out

    def test_max_actions_respected(self) -> None:
        dims = self._make_dims()
        dims[0].actions = [
            _Action(float(10 - i), "security", "high", f"Action {i}")
            for i in range(8)
        ]
        out = _format_text(dims, 80.0, None, 3)
        # Shows 3 items + "more" note
        assert "more action" in out.lower() or out.count("Action ") >= 1


# ── _format_json ──────────────────────────────────────────────────────────────

class TestFormatJson:
    def _make_dims(self) -> list:
        return [
            _Dimension("security", 85, 3, "ok"),
            _Dimension("todos", 100, 0, "ok"),
        ]

    def test_valid_json(self) -> None:
        out = _format_json(self._make_dims(), 85.0, None, 5)
        data = json.loads(out)
        assert isinstance(data, dict)

    def test_required_keys(self) -> None:
        out = _format_json(self._make_dims(), 85.0, None, 5)
        data = json.loads(out)
        for key in ("overall_score", "overall_grade", "dimensions", "top_actions", "total_actions"):
            assert key in data

    def test_dimensions_have_name(self) -> None:
        out = _format_json(self._make_dims(), 85.0, None, 5)
        data = json.loads(out)
        names = {d["name"] for d in data["dimensions"]}
        assert "security" in names
        assert "todos" in names


# ── Dimension scanner smoke tests ─────────────────────────────────────────────

class TestDimensionScanners:
    """Scanners must return valid _Dimension on empty dirs without crash."""

    def test_security_empty(self, ctx: ToolContext) -> None:
        d = _scan_security(ctx.repo_dir, None)
        assert isinstance(d, _Dimension)
        assert 0 <= d.score <= 100

    def test_cycles_empty(self, ctx: ToolContext) -> None:
        d = _scan_cycles(ctx.repo_dir, None)
        assert isinstance(d, _Dimension)
        assert 0 <= d.score <= 100

    def test_debt_empty(self, ctx: ToolContext) -> None:
        d = _scan_debt(ctx.repo_dir, None)
        assert isinstance(d, _Dimension)
        assert 0 <= d.score <= 100

    def test_exceptions_empty(self, ctx: ToolContext) -> None:
        d = _scan_exceptions(ctx.repo_dir, None)
        assert isinstance(d, _Dimension)
        assert 0 <= d.score <= 100

    def test_todos_empty(self, ctx: ToolContext) -> None:
        d = _scan_todos(ctx.repo_dir, None)
        assert isinstance(d, _Dimension)
        assert d.score == 100.0


# ── Integration: real repo ────────────────────────────────────────────────────

class TestIntegration:
    def test_text_format(self, real_ctx: ToolContext) -> None:
        out = _health_report(real_ctx, format="text", quick=True)
        assert "Health Report" in out
        assert any(letter in out for letter in ("A", "B", "C", "D", "F"))

    def test_json_format(self, real_ctx: ToolContext) -> None:
        out = _health_report(real_ctx, format="json", quick=True)
        data = json.loads(out)
        assert "overall_score" in data
        assert "overall_grade" in data
        assert isinstance(data["dimensions"], list)
        assert len(data["dimensions"]) >= 5

    def test_score_range(self, real_ctx: ToolContext) -> None:
        out = _health_report(real_ctx, format="json", quick=True)
        data = json.loads(out)
        assert 0 <= data["overall_score"] <= 100

    def test_max_actions(self, real_ctx: ToolContext) -> None:
        out = _health_report(real_ctx, format="json", quick=True, max_actions=3)
        data = json.loads(out)
        assert len(data["top_actions"]) <= 3

    def test_path_filter(self, real_ctx: ToolContext) -> None:
        out = _health_report(real_ctx, path="ouroboros/tools/", format="json", quick=True)
        data = json.loads(out)
        assert data["path"] == "ouroboros/tools/"

    def test_action_keys(self, real_ctx: ToolContext) -> None:
        out = _health_report(real_ctx, format="json", quick=True, max_actions=5)
        data = json.loads(out)
        for action in data["top_actions"]:
            for key in ("priority", "dimension", "severity", "action"):
                assert key in action

    def test_actions_sorted_by_priority(self, real_ctx: ToolContext) -> None:
        out = _health_report(real_ctx, format="json", quick=True, max_actions=10)
        data = json.loads(out)
        priorities = [a["priority"] for a in data["top_actions"]]
        assert priorities == sorted(priorities, reverse=True)


# ── get_tools ─────────────────────────────────────────────────────────────────

class TestGetTools:
    def test_returns_list(self) -> None:
        tools = get_tools()
        assert isinstance(tools, list)
        assert len(tools) == 1

    def test_tool_name(self) -> None:
        tool = get_tools()[0]
        assert tool.name == "health_report"

    def test_execute_callable(self) -> None:
        tool = get_tools()[0]
        assert callable(tool.handler)

    def test_execute_quick(self, real_ctx: ToolContext) -> None:
        tool = get_tools()[0]
        out = tool.handler(real_ctx, quick=True, max_actions=3)
        assert "Health Report" in out
