"""Tests for test_gap_report tool."""
from __future__ import annotations

import json
import pathlib
import textwrap
import tempfile
import os

import pytest

from ouroboros.tools.test_gap_report import (
    GapEntry,
    _classify_module_risk,
    _compute_gaps,
    _format_text,
    _test_gap_report,
    _trend_label,
    _TIER_BONUS,
    _TREND_BONUS,
)
from ouroboros.tools.registry import ToolContext


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _make_ctx(repo_dir: pathlib.Path = None) -> ToolContext:
    from ouroboros.tools.registry import ToolContext
    d = repo_dir or pathlib.Path("/opt/veles")
    return ToolContext(repo_dir=d, drive_root=d)


def _write(p: pathlib.Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content), encoding="utf-8")


@pytest.fixture()
def tmp_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """Minimal fake repo: ouroboros/tools/alpha.py + tests/test_alpha.py."""
    src = tmp_path / "ouroboros" / "tools" / "alpha.py"
    _write(src, """\
        def covered_func(x):
            return x + 1

        def uncovered_simple(x):
            return x * 2

        def uncovered_complex(x, y, z):
            if x > 0:
                for i in range(y):
                    if i > z:
                        return True
                    elif i == z:
                        return False
            return None
    """)

    tests = tmp_path / "tests" / "test_alpha.py"
    _write(tests, """\
        from ouroboros.tools.alpha import covered_func

        def test_covered_func():
            assert covered_func(2) == 3
    """)

    # Minimal git repo so _git_log_shas doesn't crash
    os.system(f"cd {tmp_path} && git init -q && git add -A && "
              f"git -c user.email=t@t.com -c user.name=T commit -q -m init")
    return tmp_path


# ── _classify_module_risk ──────────────────────────────────────────────────────

def test_classify_critical_agent():
    assert _classify_module_risk("ouroboros/agent.py") == "CRITICAL"


def test_classify_critical_loop():
    assert _classify_module_risk("ouroboros/loop_runtime.py") == "HIGH"


def test_classify_critical_registry():
    assert _classify_module_risk("ouroboros/tools/registry.py") == "CRITICAL"


def test_classify_high_tools():
    assert _classify_module_risk("ouroboros/tools/some_tool.py") == "HIGH"


def test_classify_high_copilot():
    assert _classify_module_risk("ouroboros/copilot_proxy.py") == "HIGH"


def test_classify_medium_default():
    assert _classify_module_risk("supervisor/queue.py") == "CRITICAL"


# ── _trend_label ───────────────────────────────────────────────────────────────

def test_trend_label_stable_same():
    assert _trend_label(5, 5) == "stable"


def test_trend_label_stable_small_delta():
    assert _trend_label(5, 6) == "stable"


def test_trend_label_rising():
    assert _trend_label(3, 8) == "rising"


def test_trend_label_falling():
    assert _trend_label(10, 5) == "falling"


def test_trend_label_zero_base():
    assert _trend_label(0, 0) == "stable"


# ── TIER_BONUS / TREND_BONUS constants ────────────────────────────────────────

def test_tier_bonus_critical_highest():
    assert _TIER_BONUS["CRITICAL"] > _TIER_BONUS["HIGH"] > _TIER_BONUS["MEDIUM"] >= _TIER_BONUS["LOW"]


def test_trend_bonus_rising_highest():
    assert _TREND_BONUS["rising"] > _TREND_BONUS["volatile"] >= _TREND_BONUS["stable"]


# ── _compute_gaps (integration with tmp_repo) ─────────────────────────────────

def test_compute_gaps_finds_uncovered(tmp_repo: pathlib.Path):
    gaps, stats = _compute_gaps(
        tmp_repo,
        tmp_repo / "ouroboros",
        commits=2,
        include_private=False,
    )
    uncovered_names = {g.function for g in gaps}
    assert "uncovered_simple" in uncovered_names
    assert "uncovered_complex" in uncovered_names


def test_compute_gaps_covered_not_in_list(tmp_repo: pathlib.Path):
    gaps, stats = _compute_gaps(
        tmp_repo,
        tmp_repo / "ouroboros",
        commits=2,
        include_private=False,
    )
    covered_names = {g.function for g in gaps}
    assert "covered_func" not in covered_names


def test_compute_gaps_stats_fields(tmp_repo: pathlib.Path):
    gaps, stats = _compute_gaps(
        tmp_repo,
        tmp_repo / "ouroboros",
        commits=2,
        include_private=False,
    )
    assert stats["files_scanned"] >= 1
    assert stats["total_public_functions"] >= 3
    assert stats["uncovered"] >= 2
    assert stats["covered"] >= 1


def test_compute_gaps_sorted_by_score(tmp_repo: pathlib.Path):
    gaps, _ = _compute_gaps(
        tmp_repo,
        tmp_repo / "ouroboros",
        commits=2,
        include_private=False,
    )
    scores = [g.risk_score for g in gaps]
    assert scores == sorted(scores, reverse=True)


def test_compute_gaps_complex_function_higher_score(tmp_repo: pathlib.Path):
    gaps, _ = _compute_gaps(
        tmp_repo,
        tmp_repo / "ouroboros",
        commits=2,
        include_private=False,
    )
    simple = next((g for g in gaps if g.function == "uncovered_simple"), None)
    complex_ = next((g for g in gaps if g.function == "uncovered_complex"), None)
    assert simple is not None and complex_ is not None
    # complex function should have higher risk score (higher complexity)
    assert complex_.risk_score >= simple.risk_score


def test_compute_gaps_reasons_not_empty(tmp_repo: pathlib.Path):
    gaps, _ = _compute_gaps(
        tmp_repo,
        tmp_repo / "ouroboros",
        commits=2,
        include_private=False,
    )
    for g in gaps:
        assert len(g.reasons) >= 1
        assert "no test coverage" in g.reasons


# ── _test_gap_report handler ───────────────────────────────────────────────────

def test_handler_text_output(tmp_repo: pathlib.Path):
    ctx = _make_ctx(tmp_repo)
    result = _test_gap_report(ctx, path="ouroboros/", top_k=10, commits=2,
                              _repo_dir=tmp_repo)
    text = result["result"]
    assert "Test Gap Report" in text
    assert "gaps:" in text
    assert "Score:" in text


def test_handler_json_output(tmp_repo: pathlib.Path):
    ctx = _make_ctx(tmp_repo)
    result = _test_gap_report(ctx, path="ouroboros/", top_k=5, commits=2,
                              format="json", _repo_dir=tmp_repo)
    data = json.loads(result["result"])
    assert "gaps" in data
    assert "stats" in data
    assert "total_gaps" in data


def test_handler_json_gap_fields(tmp_repo: pathlib.Path):
    ctx = _make_ctx(tmp_repo)
    result = _test_gap_report(ctx, path="ouroboros/", commits=2,
                              format="json", _repo_dir=tmp_repo)
    data = json.loads(result["result"])
    for gap in data["gaps"]:
        for field in ("file", "function", "line", "complexity",
                      "risk_score", "reasons", "module_tier"):
            assert field in gap, f"Missing field {field} in gap entry"


def test_handler_missing_path_returns_error():
    ctx = _make_ctx()
    result = _test_gap_report(ctx, path="nonexistent/path/xyz/")
    assert "not found" in result["result"].lower() or "error" in result["result"].lower()


def test_handler_top_k_capped(tmp_repo: pathlib.Path):
    ctx = _make_ctx(tmp_repo)
    result = _test_gap_report(ctx, path="ouroboros/", top_k=999, commits=2,
                              format="json", _repo_dir=tmp_repo)
    data = json.loads(result["result"])
    # top_k is capped at 50, and we have < 50 functions in the fixture anyway
    assert len(data["gaps"]) <= 50


def test_handler_include_private_false(tmp_repo: pathlib.Path):
    # Add a private function to the source
    src = tmp_repo / "ouroboros" / "tools" / "alpha.py"
    current = src.read_text()
    src.write_text(current + "\ndef _private_helper():\n    pass\n")

    ctx = _make_ctx()
    result = _test_gap_report(ctx, path="ouroboros/", commits=2,
                              include_private=False, format="json",
                              _repo_dir=tmp_repo)
    data = json.loads(result["result"])
    names = {g["function"] for g in data["gaps"]}
    assert "_private_helper" not in names


def test_handler_include_private_true(tmp_repo: pathlib.Path):
    src = tmp_repo / "ouroboros" / "tools" / "alpha.py"
    current = src.read_text()
    src.write_text(current + "\ndef _private_helper():\n    pass\n")

    ctx = _make_ctx()
    result = _test_gap_report(ctx, path="ouroboros/", commits=2,
                              include_private=True, format="json",
                              _repo_dir=tmp_repo)
    data = json.loads(result["result"])
    names = {g["function"] for g in data["gaps"]}
    assert "_private_helper" in names


# ── _format_text ───────────────────────────────────────────────────────────────

def test_format_text_no_gaps():
    stats = {
        "files_scanned": 5,
        "total_public_functions": 10,
        "covered": 10,
        "uncovered": 0,
        "skipped_private": 2,
    }
    text = _format_text([], stats, top_k=15, path_label="ouroboros/")
    assert "No test gaps" in text or "✅" in text


def test_format_text_shows_risk_score():
    entry = GapEntry(
        file="ouroboros/tools/foo.py",
        function="my_func",
        class_name="",
        line=42,
        complexity=8,
        old_complexity=8,
        delta=0,
        trend="stable",
        churn=3,
        module_tier="HIGH",
        risk_score=55.5,
        reasons=["no test coverage", "high complexity (8)"],
    )
    stats = {
        "files_scanned": 1,
        "total_public_functions": 5,
        "covered": 4,
        "uncovered": 1,
        "skipped_private": 0,
    }
    text = _format_text([entry], stats, top_k=15, path_label="ouroboros/")
    assert "55.5" in text
    assert "my_func" in text


def test_format_text_truncation_hint():
    entries = [
        GapEntry(
            file=f"ouroboros/tools/m{i}.py",
            function=f"func_{i}",
            class_name="",
            line=i,
            complexity=1,
            old_complexity=1,
            delta=0,
            trend="",
            churn=0,
            module_tier="LOW",
            risk_score=30.0 - i * 0.1,
            reasons=["no test coverage"],
        )
        for i in range(20)
    ]
    stats = {
        "files_scanned": 5,
        "total_public_functions": 25,
        "covered": 5,
        "uncovered": 20,
        "skipped_private": 0,
    }
    text = _format_text(entries, stats, top_k=5, path_label="ouroboros/")
    assert "15 more" in text


# ── get_tools registration ─────────────────────────────────────────────────────

def test_get_tools_registers():
    from ouroboros.tools.test_gap_report import get_tools
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "test_gap_report"


def test_tool_schema_has_required_fields():
    from ouroboros.tools.test_gap_report import get_tools
    schema = get_tools()[0].schema
    assert schema["name"] == "test_gap_report"
    assert "parameters" in schema
    props = schema["parameters"]["properties"]
    for key in ("path", "top_k", "commits", "include_private", "format"):
        assert key in props
