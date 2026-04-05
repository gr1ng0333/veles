"""Tests for ouroboros/tools/tech_debt.py"""

from __future__ import annotations

import ast
import json
import os
import textwrap
import types
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

# Patch REPO_DIR before import to avoid live filesystem side effects
os.environ.setdefault("REPO_DIR", "/opt/veles")

from ouroboros.tools.tech_debt import (
    _ALL_CATEGORIES,
    _GOD_OBJECT_METHODS,
    _HIGH_COMPLEXITY_THRESHOLD,
    _MAX_FUNCTION_LINES,
    _MAX_MODULE_LINES,
    _MAX_NESTING,
    _MAX_PARAMS,
    _SEVERITY_MAP,
    _cyclomatic,
    _max_nesting_depth,
    _param_count,
    _scan_file,
    _tech_debt,
    get_tools,
)
from ouroboros.tools.registry import ToolContext


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_ctx(tmp_path: Path) -> ToolContext:
    ctx = MagicMock(spec=ToolContext)
    ctx.repo_dir = str(tmp_path)
    return ctx


def _write_py(tmp_path: Path, name: str, src: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(src), encoding="utf-8")
    return p


# ── Unit: _cyclomatic ──────────────────────────────────────────────────────────

def test_cyclomatic_simple():
    src = "def f(x):\n    if x:\n        return 1\n    return 0\n"
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert _cyclomatic(fn) >= 1


def test_cyclomatic_no_branches():
    src = "def f():\n    return 42\n"
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert _cyclomatic(fn) == 0


def test_cyclomatic_bool_ops():
    src = "def f(a, b, c):\n    return a and b and c\n"
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    # BoolOp with 3 values → 2 branches
    assert _cyclomatic(fn) == 2


# ── Unit: _max_nesting_depth ───────────────────────────────────────────────────

def test_max_nesting_shallow():
    src = "def f(x):\n    if x:\n        return 1\n"
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert _max_nesting_depth(fn) == 1


def test_max_nesting_deep():
    # 6 levels deep
    src = "def f(x):\n    if x:\n        for i in range(1):\n            while True:\n                try:\n                    if i:\n                        pass\n                except Exception:\n                    pass\n"
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert _max_nesting_depth(fn) >= 5


# ── Unit: _param_count ────────────────────────────────────────────────────────

def test_param_count_basic():
    src = "def f(a, b, c): pass\n"
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert _param_count(fn.args) == 3


def test_param_count_excludes_self():
    src = "class A:\n    def m(self, a, b): pass\n"
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert _param_count(fn.args) == 2


def test_param_count_varargs():
    src = "def f(a, *args, **kwargs): pass\n"
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert _param_count(fn.args) == 3  # a + *args + **kwargs


# ── Unit: _scan_file ──────────────────────────────────────────────────────────

def test_scan_file_clean(tmp_path):
    p = _write_py(tmp_path, "clean.py", "def greet(name: str) -> str:\n    return f'hello {name}'\n")
    result = _scan_file(p, tmp_path)
    assert result is not None
    for cat in _ALL_CATEGORIES:
        assert cat in result
    # All categories empty for a clean file
    for cat in ["oversized_functions", "too_many_params", "high_complexity",
                "oversized_modules", "deep_nesting", "god_objects"]:
        assert result[cat] == [], f"Expected no debt in {cat}"


def test_scan_file_oversized_function(tmp_path):
    # Function that is >150 lines
    body = "def big_func(x):\n" + "    pass\n" * 155
    p = _write_py(tmp_path, "bigfunc.py", body)
    result = _scan_file(p, tmp_path)
    assert result is not None
    assert len(result["oversized_functions"]) == 1
    item = result["oversized_functions"][0]
    assert item["function"] == "big_func"
    assert item["lines"] > 150


def test_scan_file_too_many_params(tmp_path):
    params = ", ".join(f"p{i}" for i in range(10))
    src = f"def heavy({params}): pass\n"
    p = _write_py(tmp_path, "params.py", src)
    result = _scan_file(p, tmp_path)
    assert result is not None
    assert len(result["too_many_params"]) == 1
    assert result["too_many_params"][0]["param_count"] == 10


def test_scan_file_todo_comment(tmp_path):
    src = "x = 1\n# TODO: refactor this\ny = 2\n# FIXME: handle edge case\n"
    p = _write_py(tmp_path, "todos.py", src)
    result = _scan_file(p, tmp_path)
    assert result is not None
    tags = {item["tag"] for item in result["fixme_todo"]}
    assert "TODO" in tags
    assert "FIXME" in tags


def test_scan_file_god_object(tmp_path):
    # Class with 22 methods
    methods = "\n".join(f"    def method_{i}(self): pass" for i in range(22))
    src = f"class GodClass:\n{methods}\n"
    p = _write_py(tmp_path, "god.py", src)
    result = _scan_file(p, tmp_path)
    assert result is not None
    assert len(result["god_objects"]) == 1
    assert result["god_objects"][0]["method_count"] == 22


def test_scan_file_oversized_module(tmp_path):
    # Module with > 1000 lines
    src = "x = 1\n" * 1005
    p = _write_py(tmp_path, "huge.py", src)
    result = _scan_file(p, tmp_path)
    assert result is not None
    assert len(result["oversized_modules"]) == 1
    assert result["oversized_modules"][0]["lines"] > 1000


def test_scan_file_syntax_error_returns_partial(tmp_path):
    src = "def broken(\n"
    p = _write_py(tmp_path, "broken.py", src)
    result = _scan_file(p, tmp_path)
    # Should not raise; returns a partial result
    assert result is not None


# ── Integration: _tech_debt ───────────────────────────────────────────────────

def test_tech_debt_returns_text(tmp_path):
    _write_py(tmp_path, "sample.py", "x = 1\n# TODO: do something\n")
    ctx = _make_ctx(tmp_path)
    output = _tech_debt(ctx, path=None, format="text")
    assert isinstance(output, str)
    assert "Tech Debt Report" in output


def test_tech_debt_returns_json(tmp_path):
    _write_py(tmp_path, "sample.py", "x = 1\n")
    ctx = _make_ctx(tmp_path)
    output = _tech_debt(ctx, format="json")
    data = json.loads(output)
    assert "debt" in data
    assert "total_files" in data
    assert "summary" in data


def test_tech_debt_category_filter(tmp_path):
    _write_py(tmp_path, "sample.py", "x = 1\n# TODO: stub\n")
    ctx = _make_ctx(tmp_path)
    output = _tech_debt(ctx, category="fixme_todo", format="json")
    data = json.loads(output)
    # Other categories should be empty lists
    for cat in _ALL_CATEGORIES:
        if cat != "fixme_todo":
            assert data["debt"][cat] == [], f"Category {cat} should be empty with filter"


def test_tech_debt_invalid_category(tmp_path):
    ctx = _make_ctx(tmp_path)
    output = _tech_debt(ctx, category="nonexistent")
    assert "Unknown category" in output


def test_tech_debt_invalid_severity(tmp_path):
    ctx = _make_ctx(tmp_path)
    output = _tech_debt(ctx, min_severity="extreme")
    assert "min_severity" in output


def test_tech_debt_min_severity_high(tmp_path):
    # fixme_todo is "low" severity → should be excluded with min_severity="high"
    _write_py(tmp_path, "todos.py", "# TODO: fix me\n")
    ctx = _make_ctx(tmp_path)
    output = _tech_debt(ctx, min_severity="high", format="json")
    data = json.loads(output)
    assert data["debt"]["fixme_todo"] == []


def test_tech_debt_path_subdirectory(tmp_path):
    subdir = tmp_path / "sub"
    subdir.mkdir()
    (subdir / "mod.py").write_text("# TODO: check\n", encoding="utf-8")
    # A file outside the subdir — should NOT appear
    (tmp_path / "outer.py").write_text("# FIXME: other\n", encoding="utf-8")
    ctx = _make_ctx(tmp_path)
    output = _tech_debt(ctx, path="sub", format="json")
    data = json.loads(output)
    files_hit = [item["file"] for item in data["debt"]["fixme_todo"]]
    # Only "sub/mod.py" should appear
    assert any("mod.py" in f for f in files_hit)
    assert not any("outer.py" in f for f in files_hit)


# ── Tool registration ──────────────────────────────────────────────────────────

def test_get_tools_returns_one_entry():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "tech_debt"


def test_tool_schema_has_required_fields():
    tool = get_tools()[0]
    schema = tool.schema
    assert schema["name"] == "tech_debt"
    assert "description" in schema
    props = schema["parameters"]["properties"]
    assert "path" in props
    assert "category" in props
    assert "min_severity" in props
    assert "format" in props


def test_all_categories_covered():
    """All _ALL_CATEGORIES must have an entry in _SEVERITY_MAP."""
    for cat in _ALL_CATEGORIES:
        assert cat in _SEVERITY_MAP, f"Missing severity for {cat}"


def test_constants_match_bible_p5():
    """Thresholds must match BIBLE P5 values."""
    assert _MAX_FUNCTION_LINES == 150
    assert _MAX_PARAMS == 8
    assert _MAX_MODULE_LINES == 1000
