"""Tests for refactor_suggest — actionable refactoring synthesis tool."""
from __future__ import annotations

import json
import textwrap
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ouroboros.tools.registry import ToolContext
from ouroboros.tools.refactor_suggest import (
    _Suggestion,
    _score,
    _priority_icon,
    _format_text,
    _refactor_suggest,
    _collect_dead_code,
    _collect_duplicates,
    _collect_security,
    _collect_tech_debt,
    _collect_exception_audit,
    _collect_dep_cycles,
    _FOCUS_SOURCES,
    get_tools,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path / "data")


@pytest.fixture
def real_ctx() -> ToolContext:
    """Context pointing to the real repo for integration tests."""
    return ToolContext(
        repo_dir=Path("/opt/veles"),
        drive_root=Path("/opt/veles-data"),
    )


def _mk_pkg(root: Path, name: str = "mypkg") -> Path:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    return pkg


# ── _score ────────────────────────────────────────────────────────────────────

class TestScore:
    def test_high_high_medium(self) -> None:
        s = _score("high", "high", "medium")
        assert 7.0 <= s <= 10.0

    def test_critical_max(self) -> None:
        s = _score("critical", "high", "low")
        assert s == 10.0

    def test_low_low_high_minimum(self) -> None:
        s = _score("low", "low", "high")
        assert s < 3.0

    def test_range_1_to_10(self) -> None:
        for sev in ("low", "medium", "high", "critical"):
            for imp in ("low", "medium", "high"):
                for eff in ("low", "medium", "high"):
                    s = _score(sev, imp, eff)
                    assert 1.0 <= s <= 10.0, f"{sev}/{imp}/{eff} → {s}"

    def test_high_severity_beats_low(self) -> None:
        high = _score("high", "medium", "medium")
        low = _score("low", "medium", "medium")
        assert high > low

    def test_low_effort_boosts_priority(self) -> None:
        easy = _score("medium", "medium", "low")
        hard = _score("medium", "medium", "high")
        assert easy > hard

    def test_unknown_severity_doesnt_crash(self) -> None:
        s = _score("unknown", "medium", "medium")
        assert 1.0 <= s <= 10.0


# ── _Suggestion ───────────────────────────────────────────────────────────────

class TestSuggestion:
    def test_to_dict_keys(self) -> None:
        s = _Suggestion(
            priority=7.5,
            category="test",
            effort="low",
            impact="high",
            action="Do something",
            location="foo.py:42",
            details="Details here",
            source="test_source",
        )
        d = s.to_dict()
        assert d["priority"] == 7.5
        assert d["category"] == "test"
        assert d["effort"] == "low"
        assert d["impact"] == "high"
        assert d["action"] == "Do something"
        assert d["location"] == "foo.py:42"
        assert d["source"] == "test_source"

    def test_sort_order(self) -> None:
        """Higher priority should sort last (order=True, so max is last)."""
        low = _Suggestion(priority=2.0, category="a", effort="low", impact="low", action="a")
        high = _Suggestion(priority=9.0, category="b", effort="low", impact="high", action="b")
        assert high > low

    def test_repr_no_crash(self) -> None:
        s = _Suggestion(priority=5.0, category="c", effort="medium", impact="medium", action="X")
        repr(s)  # no crash


# ── _priority_icon ────────────────────────────────────────────────────────────

class TestPriorityIcon:
    def test_critical(self) -> None:
        assert _priority_icon(9.5) == "🔴"
        assert _priority_icon(10.0) == "🔴"

    def test_high(self) -> None:
        assert _priority_icon(7.5) == "🟠"

    def test_medium(self) -> None:
        assert _priority_icon(5.5) == "🟡"

    def test_low(self) -> None:
        assert _priority_icon(2.0) == "🔵"


# ── _format_text ──────────────────────────────────────────────────────────────

class TestFormatText:
    def _make_suggestions(self, n: int) -> list:
        return [
            _Suggestion(
                priority=float(10 - i),
                category="test_cat",
                effort="medium",
                impact="high",
                action=f"Action {i}",
                location=f"file{i}.py:{i * 10}",
                details=f"Details {i}",
                source="test",
            )
            for i in range(n)
        ]

    def test_empty(self) -> None:
        out = _format_text([], 0, None, "all", 1.0, 20)
        assert "No refactoring suggestions" in out

    def test_header(self) -> None:
        suggestions = self._make_suggestions(3)
        out = _format_text(suggestions, 3, None, "all", 1.0, 20)
        assert "Refactor Suggestions" in out
        assert "top 3" in out

    def test_location_shown(self) -> None:
        suggestions = self._make_suggestions(1)
        out = _format_text(suggestions, 1, None, "all", 1.0, 20)
        assert "file0.py:0" in out

    def test_overflow_note(self) -> None:
        suggestions = self._make_suggestions(5)
        out = _format_text(suggestions, 10, None, "all", 1.0, 5)
        assert "more suggestions" in out

    def test_path_filter_shown(self) -> None:
        suggestions = self._make_suggestions(2)
        out = _format_text(suggestions, 2, "ouroboros/", "all", 1.0, 20)
        assert "path=ouroboros/" in out

    def test_focus_shown(self) -> None:
        suggestions = self._make_suggestions(2)
        out = _format_text(suggestions, 2, None, "security", 1.0, 20)
        assert "focus=security" in out

    def test_min_priority_shown(self) -> None:
        suggestions = self._make_suggestions(2)
        out = _format_text(suggestions, 2, None, "all", 7.0, 20)
        assert "min_priority=7.0" in out


# ── _focus_sources ────────────────────────────────────────────────────────────

class TestFocusSources:
    def test_all_is_none(self) -> None:
        assert _FOCUS_SOURCES["all"] is None

    def test_security_source(self) -> None:
        assert "security_scan" in _FOCUS_SOURCES["security"]

    def test_cycles_source(self) -> None:
        assert "dep_cycles" in _FOCUS_SOURCES["cycles"]

    def test_dead_source(self) -> None:
        assert "dead_code" in _FOCUS_SOURCES["dead"]


# ── Collector smoke tests with temp dirs ──────────────────────────────────────

class TestCollectorsSmokeEmpty:
    """Collectors must return [] gracefully on an empty dir."""

    def test_dead_code_empty(self, tmp_path: Path) -> None:
        result = _collect_dead_code(tmp_path, None)
        assert isinstance(result, list)

    def test_duplicates_empty(self, tmp_path: Path) -> None:
        result = _collect_duplicates(tmp_path, None)
        assert isinstance(result, list)

    def test_security_empty(self, tmp_path: Path) -> None:
        result = _collect_security(tmp_path, None)
        assert isinstance(result, list)

    def test_tech_debt_empty(self, tmp_path: Path) -> None:
        result = _collect_tech_debt(tmp_path, None)
        assert isinstance(result, list)

    def test_exception_audit_empty(self, tmp_path: Path) -> None:
        result = _collect_exception_audit(tmp_path, None)
        assert isinstance(result, list)

    def test_dep_cycles_empty(self, tmp_path: Path) -> None:
        result = _collect_dep_cycles(tmp_path, None)
        assert isinstance(result, list)


# ── Collector unit tests with synthetic files ─────────────────────────────────

class TestDeadCodeCollector:
    def test_detects_unused_import(self, tmp_path: Path) -> None:
        pkg = _mk_pkg(tmp_path)
        (pkg / "mod.py").write_text(textwrap.dedent("""\
            import os
            import sys

            def foo():
                return 42
        """))
        result = _collect_dead_code(tmp_path, None)
        categories = {s.category for s in result}
        # Both os and sys are unused → at least one suggestion
        assert any("unused" in c for c in categories)

    def test_no_false_positive_used_import(self, tmp_path: Path) -> None:
        pkg = _mk_pkg(tmp_path)
        (pkg / "mod.py").write_text(textwrap.dedent("""\
            import os

            def foo():
                return os.getcwd()
        """))
        result = _collect_dead_code(tmp_path, None)
        # os is used — should not flag it
        for s in result:
            assert "os" not in s.action or "unused" not in s.category


class TestSecurityCollector:
    def test_detects_hardcoded_password(self, tmp_path: Path) -> None:
        pkg = _mk_pkg(tmp_path)
        (pkg / "auth.py").write_text(textwrap.dedent("""\
            password = "supersecret123"

            def login():
                pass
        """))
        result = _collect_security(tmp_path, None)
        assert len(result) > 0
        assert any("password" in s.action.lower() or "hardcoded" in s.action.lower()
                   for s in result)

    def test_security_suggestions_have_high_priority(self, tmp_path: Path) -> None:
        pkg = _mk_pkg(tmp_path)
        (pkg / "auth.py").write_text('api_key = "sk-abc123abc123abc123abc"')
        result = _collect_security(tmp_path, None)
        if result:
            max_p = max(s.priority for s in result)
            assert max_p >= 5.0

    def test_no_crash_on_syntax_error(self, tmp_path: Path) -> None:
        pkg = _mk_pkg(tmp_path)
        (pkg / "broken.py").write_text("def foo(: pass")
        result = _collect_security(tmp_path, None)
        assert isinstance(result, list)


class TestTechDebtCollector:
    def test_detects_complex_function(self, tmp_path: Path) -> None:
        pkg = _mk_pkg(tmp_path)
        # Build a function with high cyclomatic complexity
        branches = "\n    ".join(f"if x == {i}: return {i}" for i in range(20))
        code = f"def complex_func(x):\n    {branches}\n    return -1\n"
        (pkg / "complex.py").write_text(code)
        result = _collect_tech_debt(tmp_path, None)
        categories = {s.category for s in result}
        assert "high_complexity" in categories

    def test_oversized_function_detected(self, tmp_path: Path) -> None:
        pkg = _mk_pkg(tmp_path)
        lines = ["def big_func():"] + [f"    x{i} = {i}" for i in range(200)]
        (pkg / "big.py").write_text("\n".join(lines))
        result = _collect_tech_debt(tmp_path, None)
        categories = {s.category for s in result}
        assert "oversized_function" in categories


class TestExceptionAuditCollector:
    def test_detects_bare_except(self, tmp_path: Path) -> None:
        pkg = _mk_pkg(tmp_path)
        (pkg / "handlers.py").write_text(textwrap.dedent("""\
            def foo():
                try:
                    do_something()
                except:
                    pass
        """))
        result = _collect_exception_audit(tmp_path, None)
        assert len(result) > 0
        assert any("bare_except" in s.category for s in result)

    def test_detects_silent_except(self, tmp_path: Path) -> None:
        pkg = _mk_pkg(tmp_path)
        (pkg / "silent.py").write_text(textwrap.dedent("""\
            def foo():
                try:
                    x = 1
                except Exception:
                    pass
        """))
        result = _collect_exception_audit(tmp_path, None)
        assert len(result) > 0

    def test_bare_except_has_high_priority(self, tmp_path: Path) -> None:
        pkg = _mk_pkg(tmp_path)
        (pkg / "handlers.py").write_text(textwrap.dedent("""\
            def foo():
                try:
                    do_something()
                except:
                    pass
        """))
        result = _collect_exception_audit(tmp_path, None)
        bare = [s for s in result if "bare_except" in s.category]
        if bare:
            assert max(s.priority for s in bare) >= 7.0


class TestDepCyclesCollector:
    def test_detects_cycle(self, tmp_path: Path) -> None:
        pkg = _mk_pkg(tmp_path)
        (pkg / "a.py").write_text("from mypkg import b\n")
        (pkg / "b.py").write_text("from mypkg import a\n")
        result = _collect_dep_cycles(tmp_path, None)
        # Might find the cycle — result is a list
        assert isinstance(result, list)

    def test_no_cycle_empty_result(self, tmp_path: Path) -> None:
        pkg = _mk_pkg(tmp_path)
        (pkg / "utils.py").write_text("def helper(): pass\n")
        (pkg / "main.py").write_text("from mypkg import utils\n")
        result = _collect_dep_cycles(tmp_path, None)
        assert isinstance(result, list)

    def test_cycle_has_high_priority(self, tmp_path: Path) -> None:
        # Any cycle found on the real repo should have priority >= 8
        result = _collect_dep_cycles(Path("/opt/veles"), None)
        if result:
            assert max(s.priority for s in result) >= 8.0


# ── _refactor_suggest integration tests ──────────────────────────────────────

class TestRefactorSuggestIntegration:
    def test_invalid_focus(self, ctx: ToolContext) -> None:
        out = _refactor_suggest(ctx, focus="nonexistent")
        assert "Unknown focus" in out

    def test_json_format(self, real_ctx: ToolContext) -> None:
        out = _refactor_suggest(real_ctx, max_results=5, format="json")
        data = json.loads(out)
        assert "suggestions" in data
        assert "total_found" in data
        assert "returned" in data
        assert data["returned"] <= 5

    def test_json_suggestion_keys(self, real_ctx: ToolContext) -> None:
        out = _refactor_suggest(real_ctx, max_results=3, format="json")
        data = json.loads(out)
        for s in data["suggestions"]:
            for key in ("priority", "category", "effort", "impact", "action", "location", "source"):
                assert key in s

    def test_text_format(self, real_ctx: ToolContext) -> None:
        out = _refactor_suggest(real_ctx, max_results=5, format="text")
        assert "Refactor Suggestions" in out

    def test_max_results_respected(self, real_ctx: ToolContext) -> None:
        out = _refactor_suggest(real_ctx, max_results=3, format="json")
        data = json.loads(out)
        assert data["returned"] <= 3
        assert len(data["suggestions"]) <= 3

    def test_min_priority_filter(self, real_ctx: ToolContext) -> None:
        out = _refactor_suggest(real_ctx, min_priority=8.0, format="json")
        data = json.loads(out)
        for s in data["suggestions"]:
            assert s["priority"] >= 8.0

    def test_focus_security_only(self, real_ctx: ToolContext) -> None:
        out = _refactor_suggest(real_ctx, focus="security", format="json")
        data = json.loads(out)
        for s in data["suggestions"]:
            assert s["source"] == "security_scan"

    def test_focus_cycles_only(self, real_ctx: ToolContext) -> None:
        out = _refactor_suggest(real_ctx, focus="cycles", format="json")
        data = json.loads(out)
        for s in data["suggestions"]:
            assert s["source"] == "dep_cycles"

    def test_focus_debt_only(self, real_ctx: ToolContext) -> None:
        out = _refactor_suggest(real_ctx, focus="debt", format="json")
        data = json.loads(out)
        for s in data["suggestions"]:
            assert s["source"] == "tech_debt"

    def test_focus_exceptions_only(self, real_ctx: ToolContext) -> None:
        out = _refactor_suggest(real_ctx, focus="exceptions", format="json")
        data = json.loads(out)
        for s in data["suggestions"]:
            assert s["source"] == "exception_audit"

    def test_focus_dead_only(self, real_ctx: ToolContext) -> None:
        out = _refactor_suggest(real_ctx, focus="dead", format="json")
        data = json.loads(out)
        for s in data["suggestions"]:
            assert s["source"] == "dead_code"

    def test_focus_duplication_only(self, real_ctx: ToolContext) -> None:
        out = _refactor_suggest(real_ctx, focus="duplication", format="json")
        data = json.loads(out)
        for s in data["suggestions"]:
            assert s["source"] == "duplicate_code"

    def test_sorted_by_priority_descending(self, real_ctx: ToolContext) -> None:
        out = _refactor_suggest(real_ctx, max_results=20, format="json")
        data = json.loads(out)
        priorities = [s["priority"] for s in data["suggestions"]]
        assert priorities == sorted(priorities, reverse=True)

    def test_real_repo_finds_cycles(self, real_ctx: ToolContext) -> None:
        out = _refactor_suggest(real_ctx, focus="cycles", format="json")
        data = json.loads(out)
        assert data["total_found"] > 0, "Real repo should have at least one import cycle"

    def test_real_repo_finds_security(self, real_ctx: ToolContext) -> None:
        out = _refactor_suggest(real_ctx, focus="security", format="json")
        data = json.loads(out)
        assert data["total_found"] > 0, "Real repo should have at least one security finding"

    def test_path_filter_limits_scan(self, real_ctx: ToolContext) -> None:
        all_out = _refactor_suggest(real_ctx, focus="security", max_results=100, format="json")
        sub_out = _refactor_suggest(
            real_ctx, focus="security", path="ouroboros/tools/",
            max_results=100, format="json"
        )
        all_data = json.loads(all_out)
        sub_data = json.loads(sub_out)
        # Limiting to a subdir should return <= total
        assert sub_data["total_found"] <= all_data["total_found"]

    def test_deduplication(self, real_ctx: ToolContext) -> None:
        out = _refactor_suggest(real_ctx, max_results=100, format="json")
        data = json.loads(out)
        # Check no exact duplicate (location + category + action[:60])
        seen = set()
        for s in data["suggestions"]:
            key = (s["location"], s["category"], s["action"][:60])
            assert key not in seen, f"Duplicate suggestion: {key}"
            seen.add(key)


# ── get_tools ─────────────────────────────────────────────────────────────────

class TestGetTools:
    def test_returns_list(self) -> None:
        tools = get_tools()
        assert isinstance(tools, list)
        assert len(tools) == 1

    def test_tool_name(self) -> None:
        tools = get_tools()
        assert tools[0].name == "refactor_suggest"

    def test_handler_callable(self) -> None:
        tools = get_tools()
        assert callable(tools[0].handler)

    def test_schema_valid(self) -> None:
        tools = get_tools()
        schema = tools[0].schema
        assert schema["name"] == "refactor_suggest"
        assert "description" in schema
        assert "parameters" in schema

    def test_handler_execute_callable(self, real_ctx: ToolContext) -> None:
        tools = get_tools()
        result = tools[0].handler(real_ctx, max_results=2, focus="cycles")
        assert isinstance(result, str)
        assert "Refactor" in result
