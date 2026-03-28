"""Tests for ast_analyze tool."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ouroboros.tools.ast_analyze import (
    _analyze_file,
    _directory_summary,
    _complexity,
    _calls_in,
    get_tools,
)
import ast


REPO = Path("/opt/veles")


# ── unit tests ────────────────────────────────────────────────────────────────

def test_complexity_simple():
    code = """
def f(x):
    if x > 0:
        for i in range(x):
            pass
"""
    tree = ast.parse(code)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert _complexity(fn) == 2  # if + for


def test_calls_in():
    code = "def f(): foo(); bar.baz()"
    tree = ast.parse(code)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    calls = _calls_in(fn)
    assert "foo" in calls
    assert "bar.baz" in calls


def test_analyze_file_self():
    """Analyze ast_analyze.py itself — sanity check."""
    p = REPO / "ouroboros" / "tools" / "ast_analyze.py"
    result = _analyze_file(p, include_calls=True, include_private=True, min_complexity=0, sort_by="complexity")
    assert result.get("error") is None
    assert result["lines_total"] > 100
    assert result["summary"]["functions"] > 5
    assert result["summary"]["total_complexity"] > 0
    # Should find our complex helpers
    fn_names = [f["name"] for f in result["functions"]]
    assert "_analyze_file" in fn_names
    assert "_directory_summary" in fn_names


def test_analyze_file_private_filter():
    p = REPO / "ouroboros" / "tools" / "ast_analyze.py"
    result = _analyze_file(p, include_calls=False, include_private=False, min_complexity=0, sort_by="line")
    fn_names = [f["name"] for f in result["functions"]]
    # Private functions like _analyze_file should be excluded
    assert all(not n.startswith("_") or n.startswith("__") for n in fn_names)


def test_analyze_file_min_complexity():
    p = REPO / "ouroboros" / "tools" / "ast_analyze.py"
    result = _analyze_file(p, include_calls=False, include_private=True, min_complexity=10, sort_by="complexity")
    # With min_complexity=10, should only show complex functions
    for fn in result["functions"]:
        assert fn["complexity"] >= 10
    # And high complexity functions should appear
    assert len(result["functions"]) >= 1


def test_directory_summary():
    d = _directory_summary(REPO / "ouroboros" / "tools", True, 0, "complexity")
    assert d["files_analyzed"] > 10
    files = d["files"]
    # Files sorted by total_complexity descending
    for i in range(len(files) - 1):
        a, b = files[i], files[i + 1]
        if "error" not in a and "error" not in b:
            assert a.get("total_complexity", 0) >= b.get("total_complexity", 0)
    # Known complex file should appear near top
    top_names = [r["file"] for r in files[:5]]
    assert any("browser" in n or "search" in n for n in top_names)


def test_directory_summary_sort_size():
    d = _directory_summary(REPO / "ouroboros" / "tools", True, 0, "size")
    files = [r for r in d["files"] if "error" not in r]
    for i in range(len(files) - 1):
        assert files[i].get("lines", 0) >= files[i + 1].get("lines", 0)


def test_handle_ast_analyze_via_tool():
    """Integration: tool entry handler returns valid JSON."""
    from ouroboros.tools.registry import ToolContext
    tools = get_tools()
    assert len(tools) == 1
    t = tools[0]
    assert t.name == "ast_analyze"
    ctx = ToolContext(repo_dir=str(REPO), drive_root="/opt/veles-data")
    output = t.handler(ctx, path="ouroboros/tools/ast_analyze.py", sort_by="complexity")
    data = json.loads(output)
    assert "summary" in data
    assert data["summary"]["functions"] > 0


def test_handle_ast_analyze_missing_path():
    from ouroboros.tools.registry import ToolContext
    tools = get_tools()
    ctx = ToolContext(repo_dir=str(REPO), drive_root="/opt/veles-data")
    output = tools[0].handler(ctx, path="nonexistent_file_xyz.py")
    data = json.loads(output)
    assert "error" in data


def test_tool_schema_valid():
    tools = get_tools()
    t = tools[0]
    schema = t.schema
    assert schema["name"] == "ast_analyze"
    assert "path" in schema["parameters"]["properties"]
    assert schema["parameters"]["required"] == ["path"]
