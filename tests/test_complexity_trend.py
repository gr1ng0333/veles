"""Tests for ouroboros/tools/complexity_trend.py"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from ouroboros.tools.complexity_trend import (
    FunctionTrend,
    _analyze_trends,
    _complexity_trend,
    _cyclomatic,
    _extract_function_complexities,
    _filter_trends,
    _format_text,
    _git_log_shas,
    get_tools,
)
from ouroboros.tools.registry import ToolContext

import ast


# ── fixtures ───────────────────────────────────────────────────────────────────

_REPO = Path("/opt/veles")


def _ctx() -> ToolContext:
    return ToolContext(repo_dir=str(_REPO), drive_root="/opt/veles-data")


# ── _cyclomatic ────────────────────────────────────────────────────────────────

def test_cyclomatic_simple_function():
    src = "def f():\n    pass\n"
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert _cyclomatic(fn) == 0


def test_cyclomatic_with_if():
    src = "def f(x):\n    if x:\n        pass\n"
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert _cyclomatic(fn) == 1


def test_cyclomatic_nested_control_flow():
    src = (
        "def f(x, y):\n"
        "    if x:\n"
        "        for i in range(y):\n"
        "            if i > 0:\n"
        "                pass\n"
    )
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert _cyclomatic(fn) == 3


def test_cyclomatic_bool_op():
    src = "def f(a, b, c):\n    return a and b and c\n"
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    # BoolOp with 3 values → +2
    assert _cyclomatic(fn) == 2


# ── _extract_function_complexities ────────────────────────────────────────────

def test_extract_empty():
    assert _extract_function_complexities("") == {}


def test_extract_syntax_error():
    assert _extract_function_complexities("def broken(:\n    pass") == {}


def test_extract_top_level():
    src = "def simple():\n    pass\n"
    result = _extract_function_complexities(src)
    assert "simple" in result
    assert result["simple"] == 0


def test_extract_method():
    src = "class Foo:\n    def bar(self):\n        if True:\n            pass\n"
    result = _extract_function_complexities(src)
    assert "Foo.bar" in result
    assert result["Foo.bar"] == 1


def test_extract_multiple_functions():
    src = (
        "def a():\n    pass\n\n"
        "def b():\n    if True:\n        return 1\n"
    )
    result = _extract_function_complexities(src)
    assert "a" in result
    assert "b" in result
    assert result["a"] == 0
    assert result["b"] == 1


# ── FunctionTrend data model ───────────────────────────────────────────────────

def test_function_trend_delta_rising():
    t = FunctionTrend(file="f.py", function="foo", history=[2, 5, 8])
    assert t.delta == 6      # 8 - 2
    assert t.oldest == 2
    assert t.newest == 8
    assert t.peak == 8
    assert t.trend_label == "rising"


def test_function_trend_delta_falling():
    t = FunctionTrend(file="f.py", function="foo", history=[10, 7, 4])
    assert t.delta == -6
    assert t.trend_label == "falling"


def test_function_trend_volatile():
    # Swings: 2 → 15 → 3 — swing=13, delta=1
    t = FunctionTrend(file="f.py", function="foo", history=[2, 15, 3])
    assert t.trend_label == "volatile"


def test_function_trend_stable_small_delta():
    t = FunctionTrend(file="f.py", function="foo", history=[5, 5, 6])
    # delta=1, <2 → stable
    assert t.trend_label == "stable"


def test_function_trend_empty_history():
    t = FunctionTrend(file="f.py", function="foo", history=[])
    assert t.delta == 0
    assert t.oldest == 0
    assert t.newest == 0
    assert t.peak == 0


def test_function_trend_single_entry():
    t = FunctionTrend(file="f.py", function="foo", history=[7])
    assert t.delta == 0


# ── _filter_trends ─────────────────────────────────────────────────────────────

def _make_trends() -> List[FunctionTrend]:
    return [
        FunctionTrend(file="f.py", function="rising_fn", history=[2, 4, 9]),
        FunctionTrend(file="f.py", function="falling_fn", history=[10, 6, 3]),
        FunctionTrend(file="f.py", function="stable_fn", history=[5, 5, 5]),
        FunctionTrend(file="g.py", function="volatile_fn", history=[2, 20, 3]),
    ]


def test_filter_trends_min_delta():
    trends = _make_trends()
    filtered = _filter_trends(trends, min_delta=5, trend_filter=None, min_complexity=0)
    names = {t.function for t in filtered}
    assert "rising_fn" in names    # delta=7
    assert "falling_fn" in names   # delta=-7
    assert "stable_fn" not in names  # delta=0


def test_filter_trends_by_label():
    trends = _make_trends()
    filtered = _filter_trends(trends, min_delta=1, trend_filter="falling", min_complexity=0)
    assert all(t.trend_label == "falling" for t in filtered)


def test_filter_trends_min_complexity():
    trends = _make_trends()
    filtered = _filter_trends(trends, min_delta=0, trend_filter=None, min_complexity=10)
    # only volatile (peak=20) and falling (peak=10)
    peaks = {t.function for t in filtered}
    assert "volatile_fn" in peaks
    assert "falling_fn" in peaks
    assert "rising_fn" not in peaks  # peak=9 < 10


def test_filter_trends_sort_order():
    """Rising functions should appear before stable ones."""
    trends = _make_trends()
    filtered = _filter_trends(trends, min_delta=1, trend_filter=None, min_complexity=0)
    labels = [t.trend_label for t in filtered]
    if "rising" in labels and "stable" in labels:
        assert labels.index("rising") < labels.index("stable")


# ── _format_text ───────────────────────────────────────────────────────────────

def test_format_text_header():
    trends = _make_trends()
    filtered = _filter_trends(trends, min_delta=1, trend_filter=None, min_complexity=0)
    text = _format_text(filtered, ["sha1", "sha2"], "abc1234", "def5678", len(trends))
    assert "Complexity Trend" in text
    assert "abc1234" in text
    assert "def5678" in text


def test_format_text_shows_functions():
    trends = [FunctionTrend(file="ouroboros/loop.py", function="run", history=[3, 9])]
    filtered = _filter_trends(trends, min_delta=3, trend_filter=None, min_complexity=0)
    text = _format_text(filtered, ["a", "b"], "aaaaaaa", "bbbbbbb", 1)
    assert "run" in text
    assert "rising" in text


def test_format_text_empty():
    text = _format_text([], ["a", "b"], "aaaaaaa", "bbbbbbb", 0)
    assert "No significant" in text


# ── _git_log_shas ──────────────────────────────────────────────────────────────

def test_git_log_shas_returns_list():
    shas = _git_log_shas(_REPO, 5)
    assert isinstance(shas, list)
    assert len(shas) >= 1


def test_git_log_shas_count_bounded():
    shas = _git_log_shas(_REPO, 3)
    assert len(shas) <= 3


# ── _complexity_trend integration ─────────────────────────────────────────────

def test_complexity_trend_returns_result():
    result = _complexity_trend(
        _ctx(), commits=5, min_delta=1, _repo_dir=_REPO
    )
    assert "result" in result


def test_complexity_trend_text_format():
    result = _complexity_trend(
        _ctx(), commits=5, format="text", min_delta=1, _repo_dir=_REPO
    )
    assert "Complexity Trend" in result["result"]


def test_complexity_trend_json_format():
    result = _complexity_trend(
        _ctx(), commits=5, format="json", min_delta=1, _repo_dir=_REPO
    )
    data = json.loads(result["result"])
    assert "functions" in data
    assert "total_tracked" in data
    assert "commits" in data


def test_complexity_trend_trend_filter():
    result = _complexity_trend(
        _ctx(), commits=10, trend="rising", min_delta=1, _repo_dir=_REPO, format="json"
    )
    data = json.loads(result["result"])
    for fn in data["functions"]:
        assert fn["trend"] == "rising"


def test_complexity_trend_path_filter():
    result = _complexity_trend(
        _ctx(), commits=10, path="ouroboros/tools/", min_delta=1,
        _repo_dir=_REPO, format="json"
    )
    data = json.loads(result["result"])
    for fn in data["functions"]:
        assert "ouroboros/tools/" in fn["file"]


def test_complexity_trend_commits_clamp():
    """commits > 50 should be silently clamped to 50."""
    result = _complexity_trend(
        _ctx(), commits=999, format="json", min_delta=1, _repo_dir=_REPO
    )
    data = json.loads(result["result"])
    assert data["commits"] <= 50


# ── get_tools ──────────────────────────────────────────────────────────────────

def test_get_tools_returns_one():
    tools = get_tools()
    assert len(tools) == 1


def test_get_tools_name():
    tools = get_tools()
    assert tools[0].name == "complexity_trend"


def test_get_tools_schema_properties():
    tools = get_tools()
    props = tools[0].schema["parameters"]["properties"]
    assert "commits" in props
    assert "path" in props
    assert "min_delta" in props
    assert "trend" in props
    assert "format" in props
