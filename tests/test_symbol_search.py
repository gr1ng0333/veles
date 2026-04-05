"""Tests for symbol_search — AST-based cross-reference finder."""
from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ouroboros.tools.symbol_search import (
    SymbolMatch,
    SymbolReport,
    _collect_py_files,
    _dedup,
    _format_text,
    _get_context,
    _scan_file,
    _symbol_search,
    get_tools,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Create a minimal repo with several Python files for testing."""
    # module_a.py — defines ToolEntry class + variable
    (tmp_path / "module_a.py").write_text(textwrap.dedent("""\
        MY_CONST = 42
        OTHER_CONST = "hello"

        class ToolEntry:
            def execute(self, ctx):
                return MY_CONST

        def helper(x):
            return x + MY_CONST
    """))

    # module_b.py — imports and uses ToolEntry + MY_CONST
    (tmp_path / "module_b.py").write_text(textwrap.dedent("""\
        from module_a import ToolEntry, MY_CONST

        def create_tool():
            entry = ToolEntry()
            return entry

        class Registry:
            def register(self, tool: ToolEntry):
                self.tools = []
                self.tools.append(tool)
    """))

    # sub/module_c.py — another usage of ToolEntry
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "module_c.py").write_text(textwrap.dedent("""\
        from module_a import ToolEntry

        _private = ToolEntry()

        def get_tools():
            return [_private]
    """))

    # empty_module.py — no occurrences
    (tmp_path / "empty_module.py").write_text("x = 1\ny = 2\n")

    # syntax_error.py — should be skipped gracefully
    (tmp_path / "syntax_error.py").write_text("def broken(\n")

    return tmp_path


@pytest.fixture
def ctx(tmp_repo: Path) -> MagicMock:
    mock = MagicMock()
    mock.repo_dir = str(tmp_repo)
    return mock


# ── _collect_py_files ─────────────────────────────────────────────────────────

def test_collect_py_files_dir(tmp_repo: Path) -> None:
    files = _collect_py_files(tmp_repo)
    names = [f.name for f in files]
    assert "module_a.py" in names
    assert "module_b.py" in names
    assert "module_c.py" in names


def test_collect_py_files_single(tmp_repo: Path) -> None:
    single = tmp_repo / "module_a.py"
    files = _collect_py_files(single)
    assert files == [single]


def test_collect_py_files_subpath(tmp_repo: Path) -> None:
    files = _collect_py_files(tmp_repo, "sub")
    assert len(files) == 1
    assert files[0].name == "module_c.py"


def test_collect_py_files_skips_pycache(tmp_repo: Path) -> None:
    pycache = tmp_repo / "__pycache__"
    pycache.mkdir()
    (pycache / "cached.py").write_text("x = 1")
    files = _collect_py_files(tmp_repo)
    assert not any(f.parent.name == "__pycache__" for f in files)


# ── _get_context ──────────────────────────────────────────────────────────────

def test_get_context_middle() -> None:
    lines = ["line1", "line2", "line3", "line4", "line5"]
    ctx = _get_context(lines, lineno=3, context_lines=1)
    assert "line2" in ctx
    assert "line3" in ctx
    assert "line4" in ctx
    assert "→" in ctx  # marks the target line


def test_get_context_boundary_start() -> None:
    lines = ["line1", "line2", "line3"]
    ctx = _get_context(lines, lineno=1, context_lines=2)
    assert "line1" in ctx
    # Should not crash on out-of-bounds


def test_get_context_boundary_end() -> None:
    lines = ["line1", "line2", "line3"]
    ctx = _get_context(lines, lineno=3, context_lines=2)
    assert "line3" in ctx


def test_get_context_zero_lines() -> None:
    lines = ["line1", "line2", "line3"]
    ctx = _get_context(lines, lineno=2, context_lines=0)
    # Only the target line itself
    assert "line2" in ctx
    assert "line1" not in ctx
    assert "line3" not in ctx


# ── _dedup ────────────────────────────────────────────────────────────────────

def test_dedup_removes_exact_duplicates() -> None:
    m1 = SymbolMatch(file="a.py", line=10, col=0, kind="usage", context="", symbol="X")
    m2 = SymbolMatch(file="a.py", line=10, col=5, kind="usage", context="other", symbol="X")  # same (file,line,kind)
    result = _dedup([m1, m2])
    assert len(result) == 1
    assert result[0] is m1


def test_dedup_keeps_different_lines() -> None:
    m1 = SymbolMatch(file="a.py", line=10, col=0, kind="usage", context="", symbol="X")
    m2 = SymbolMatch(file="a.py", line=11, col=0, kind="usage", context="", symbol="X")
    result = _dedup([m1, m2])
    assert len(result) == 2


def test_dedup_keeps_different_kinds() -> None:
    m1 = SymbolMatch(file="a.py", line=10, col=0, kind="usage", context="", symbol="X")
    m2 = SymbolMatch(file="a.py", line=10, col=0, kind="function_def", context="", symbol="X")
    result = _dedup([m1, m2])
    assert len(result) == 2


# ── _scan_file ────────────────────────────────────────────────────────────────

def test_scan_finds_class_def(tmp_repo: Path) -> None:
    report = _scan_file(
        tmp_repo / "module_a.py", tmp_repo,
        symbol="ToolEntry", want_defs=True, want_uses=False, context_lines=0,
    )
    defs = report.definitions
    assert len(defs) >= 1
    assert any(d.kind == "class_def" for d in defs)
    assert any(d.symbol == "ToolEntry" for d in defs)


def test_scan_finds_function_def(tmp_repo: Path) -> None:
    report = _scan_file(
        tmp_repo / "module_a.py", tmp_repo,
        symbol="helper", want_defs=True, want_uses=False, context_lines=0,
    )
    assert any(d.kind == "function_def" for d in report.definitions)


def test_scan_finds_variable_def(tmp_repo: Path) -> None:
    report = _scan_file(
        tmp_repo / "module_a.py", tmp_repo,
        symbol="MY_CONST", want_defs=True, want_uses=False, context_lines=0,
    )
    assert any(d.kind == "variable_def" for d in report.definitions)


def test_scan_finds_import_def(tmp_repo: Path) -> None:
    report = _scan_file(
        tmp_repo / "module_b.py", tmp_repo,
        symbol="ToolEntry", want_defs=True, want_uses=False, context_lines=0,
    )
    assert any(d.kind == "import_def" for d in report.definitions)


def test_scan_finds_usages(tmp_repo: Path) -> None:
    report = _scan_file(
        tmp_repo / "module_b.py", tmp_repo,
        symbol="ToolEntry", want_defs=False, want_uses=True, context_lines=0,
    )
    assert len(report.usages) >= 1


def test_scan_finds_attribute_usage(tmp_repo: Path) -> None:
    # self.tools — "tools" is an attribute usage
    report = _scan_file(
        tmp_repo / "module_b.py", tmp_repo,
        symbol="tools", want_defs=False, want_uses=True, context_lines=0,
    )
    assert any(u.kind == "attribute_usage" for u in report.usages)


def test_scan_syntax_error_graceful(tmp_repo: Path) -> None:
    # syntax_error.py should not raise
    report = _scan_file(
        tmp_repo / "syntax_error.py", tmp_repo,
        symbol="broken", want_defs=True, want_uses=True, context_lines=0,
    )
    assert isinstance(report, SymbolReport)


def test_scan_import_usage_tracked(tmp_repo: Path) -> None:
    # 'from module_a import ToolEntry' — ToolEntry appears as import_usage in module_b
    report = _scan_file(
        tmp_repo / "module_b.py", tmp_repo,
        symbol="ToolEntry", want_defs=False, want_uses=True, context_lines=0,
    )
    assert any(u.kind == "import_usage" for u in report.usages)


# ── _symbol_search (integration) ─────────────────────────────────────────────

def test_symbol_search_finds_class_across_files(ctx: MagicMock, tmp_repo: Path) -> None:
    result = _symbol_search(ctx, symbol="ToolEntry")
    assert "ToolEntry" in result
    assert "class_def" in result or "class def" in result
    assert "Definitions" in result
    assert "Usages" in result


def test_symbol_search_kind_def_only(ctx: MagicMock) -> None:
    result = _symbol_search(ctx, symbol="ToolEntry", kind="def")
    assert "Definitions" in result
    assert "Usages" not in result


def test_symbol_search_kind_use_only(ctx: MagicMock) -> None:
    result = _symbol_search(ctx, symbol="ToolEntry", kind="use")
    assert "Usages" in result
    assert "Definitions" not in result


def test_symbol_search_not_found(ctx: MagicMock) -> None:
    result = _symbol_search(ctx, symbol="NonExistentXYZ123")
    assert "not found" in result or "0" in result


def test_symbol_search_json_format(ctx: MagicMock) -> None:
    result = _symbol_search(ctx, symbol="ToolEntry", format="json")
    data = json.loads(result)
    assert "symbol" in data
    assert data["symbol"] == "ToolEntry"
    assert "definitions" in data
    assert "usages" in data
    assert isinstance(data["definitions"], list)
    assert isinstance(data["usages"], list)


def test_symbol_search_json_includes_filters(ctx: MagicMock) -> None:
    result = _symbol_search(ctx, symbol="MY_CONST", kind="def", format="json")
    data = json.loads(result)
    assert data["filters"]["kind"] == "def"


def test_symbol_search_path_filter(ctx: MagicMock, tmp_repo: Path) -> None:
    # Limit to sub/ — ToolEntry defined in module_a.py which is NOT in sub/
    result = _symbol_search(ctx, symbol="ToolEntry", path="sub", kind="def")
    # Should not find the class_def (it's in module_a.py, not sub/)
    # but should find import_def in sub/module_c.py
    data_json = _symbol_search(ctx, symbol="ToolEntry", path="sub", format="json")
    data = json.loads(data_json)
    files = [d["file"] for d in data["definitions"]]
    assert all("sub" in f for f in files)


def test_symbol_search_empty_symbol_error(ctx: MagicMock) -> None:
    result = _symbol_search(ctx, symbol="")
    assert "Error" in result or "required" in result


def test_symbol_search_invalid_kind_error(ctx: MagicMock) -> None:
    result = _symbol_search(ctx, symbol="ToolEntry", kind="bogus")
    assert "Unknown" in result or "bogus" in result


def test_symbol_search_context_lines_zero(ctx: MagicMock) -> None:
    # With context_lines=0, no source snippet should appear in output
    result = _symbol_search(ctx, symbol="MY_CONST", context_lines=0)
    # Should still find definitions but no arrow markers
    assert "MY_CONST" in result


def test_symbol_search_max_results_limits(ctx: MagicMock) -> None:
    result = _symbol_search(ctx, symbol="ToolEntry", max_results=1)
    # Should show "more" notice if there are many results
    # At minimum, doesn't crash and returns output
    assert "ToolEntry" in result


def test_symbol_search_variable_cross_file(ctx: MagicMock) -> None:
    result_json = _symbol_search(ctx, symbol="MY_CONST", format="json")
    data = json.loads(result_json)
    # MY_CONST defined in module_a.py and used in module_a.py + imported in module_b.py
    def_files = [d["file"] for d in data["definitions"]]
    assert any("module_a" in f for f in def_files)


# ── get_tools ─────────────────────────────────────────────────────────────────

def test_get_tools_returns_list() -> None:
    tools = get_tools()
    assert isinstance(tools, list)
    assert len(tools) == 1


def test_get_tools_name() -> None:
    tools = get_tools()
    assert tools[0].name == "symbol_search"


def test_get_tools_schema_has_required() -> None:
    schema = get_tools()[0].schema
    assert "symbol" in schema["parameters"]["required"]


def test_get_tools_handler_callable() -> None:
    tools = get_tools()
    assert callable(tools[0].handler)
