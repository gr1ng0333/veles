"""Tests for ouroboros/tools/type_coverage.py"""

from __future__ import annotations

import ast
import json
import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("REPO_DIR", "/opt/veles")

from ouroboros.tools.type_coverage import (
    _ALL_CATEGORIES,
    _CATEGORY_MISSING_PARAM,
    _CATEGORY_MISSING_RETURN,
    _DefInfo,
    _FileSummary,
    _collect_py_files,
    _is_private,
    _qualified_name,
    _scan_file,
    _type_coverage,
    get_tools,
)
from ouroboros.tools.registry import ToolContext


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ctx(tmp_path: Path) -> ToolContext:
    ctx = MagicMock(spec=ToolContext)
    ctx.repo_dir = str(tmp_path)
    return ctx


def _write(tmp_path: Path, name: str, src: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(src), encoding="utf-8")
    return p


# ── Unit: _is_private ─────────────────────────────────────────────────────────

def test_is_private_dunder():
    assert _is_private("__init__")
    assert _is_private("__str__")


def test_is_private_single_underscore():
    assert _is_private("_helper")
    assert _is_private("_internal")


def test_is_private_public():
    assert not _is_private("scan")
    assert not _is_private("get_tools")


# ── Unit: _qualified_name ─────────────────────────────────────────────────────

def test_qualified_name_top_level():
    assert _qualified_name([], "myfunc") == "myfunc"


def test_qualified_name_method():
    assert _qualified_name(["MyClass"], "my_method") == "MyClass.my_method"


def test_qualified_name_nested():
    assert _qualified_name(["Outer", "Inner"], "run") == "Outer.Inner.run"


# ── Unit: _scan_file — fully annotated ───────────────────────────────────────

def test_fully_annotated_func(tmp_path):
    p = _write(tmp_path, "a.py", """\
        def add(x: int, y: int) -> int:
            return x + y
    """)
    results = _scan_file(p, "a.py", skip_private=False)
    assert results == []


def test_fully_annotated_method(tmp_path):
    p = _write(tmp_path, "a.py", """\
        class Foo:
            def bar(self, x: str) -> None:
                pass
    """)
    results = _scan_file(p, "a.py", skip_private=False)
    assert results == []


# ── Unit: _scan_file — missing param annotations ─────────────────────────────

def test_missing_param_annotation(tmp_path):
    p = _write(tmp_path, "a.py", """\
        def greet(name) -> str:
            return f"hello {name}"
    """)
    results = _scan_file(p, "a.py", skip_private=False)
    assert len(results) == 1
    assert "name" in results[0].missing_params
    assert not results[0].missing_return


def test_multiple_missing_params(tmp_path):
    p = _write(tmp_path, "a.py", """\
        def calc(a, b, c) -> int:
            return a + b + c
    """)
    results = _scan_file(p, "a.py", skip_private=False)
    assert len(results) == 1
    assert set(results[0].missing_params) == {"a", "b", "c"}


def test_self_excluded_from_missing(tmp_path):
    p = _write(tmp_path, "a.py", """\
        class Foo:
            def bar(self) -> None:
                pass
    """)
    results = _scan_file(p, "a.py", skip_private=False)
    assert results == []


def test_cls_excluded_from_missing(tmp_path):
    p = _write(tmp_path, "a.py", """\
        class Foo:
            @classmethod
            def create(cls) -> "Foo":
                return cls()
    """)
    results = _scan_file(p, "a.py", skip_private=False)
    assert results == []


# ── Unit: _scan_file — missing return annotation ─────────────────────────────

def test_missing_return_annotation(tmp_path):
    p = _write(tmp_path, "a.py", """\
        def do_work(x: int):
            pass
    """)
    results = _scan_file(p, "a.py", skip_private=False)
    assert len(results) == 1
    assert results[0].missing_return
    assert results[0].missing_params == []


def test_missing_both(tmp_path):
    p = _write(tmp_path, "a.py", """\
        def bad(x, y):
            pass
    """)
    results = _scan_file(p, "a.py", skip_private=False)
    assert len(results) == 1
    assert results[0].missing_return
    assert set(results[0].missing_params) == {"x", "y"}


# ── Unit: _scan_file — skip_private ──────────────────────────────────────────

def test_skip_private_flag(tmp_path):
    p = _write(tmp_path, "a.py", """\
        def _helper(x):
            pass
        def public_api(x: int) -> str:
            return str(x)
    """)
    results = _scan_file(p, "a.py", skip_private=True)
    assert results == []


def test_skip_private_false_includes_private(tmp_path):
    p = _write(tmp_path, "a.py", """\
        def _helper(x):
            pass
    """)
    results = _scan_file(p, "a.py", skip_private=False)
    assert len(results) == 1
    assert "_helper" in results[0].qualified_name


# ── Unit: _scan_file — vararg / kwarg ─────────────────────────────────────────

def test_vararg_unannotated(tmp_path):
    p = _write(tmp_path, "a.py", """\
        def fn(*args) -> None:
            pass
    """)
    results = _scan_file(p, "a.py", skip_private=False)
    assert len(results) == 1
    assert "args" in results[0].missing_params


def test_kwarg_annotated(tmp_path):
    p = _write(tmp_path, "a.py", """\
        def fn(**kwargs: str) -> None:
            pass
    """)
    results = _scan_file(p, "a.py", skip_private=False)
    assert results == []


# ── Unit: _scan_file — async function ────────────────────────────────────────

def test_async_function_missing(tmp_path):
    p = _write(tmp_path, "a.py", """\
        async def fetch(url):
            pass
    """)
    results = _scan_file(p, "a.py", skip_private=False)
    assert len(results) == 1
    assert results[0].missing_return


# ── Unit: _scan_file — nested function ───────────────────────────────────────

def test_nested_function_reported(tmp_path):
    p = _write(tmp_path, "a.py", """\
        def outer(x: int) -> None:
            def inner(y):
                pass
    """)
    results = _scan_file(p, "a.py", skip_private=False)
    assert any("inner" in r.qualified_name for r in results)


def test_method_qualified_name(tmp_path):
    p = _write(tmp_path, "a.py", """\
        class Scanner:
            def run(self, path) -> None:
                pass
    """)
    results = _scan_file(p, "a.py", skip_private=False)
    assert results[0].qualified_name == "Scanner.run"


# ── Integration: _type_coverage ──────────────────────────────────────────────

def test_type_coverage_text_output(tmp_path):
    _write(tmp_path, "a.py", "def func(x) -> int:\n    return x\n")
    out = _type_coverage(_ctx(tmp_path), format="text")
    assert "Type Coverage Report" in out
    assert "missing_param_annotations" in out


def test_type_coverage_json_output(tmp_path):
    _write(tmp_path, "a.py", "def func(x) -> int:\n    return x\n")
    out = _type_coverage(_ctx(tmp_path), format="json")
    data = json.loads(out)
    assert "total_files" in data
    assert "coverage_pct" in data
    assert "files" in data


def test_type_coverage_fully_clean(tmp_path):
    _write(tmp_path, "a.py", "def func(x: int) -> int:\n    return x\n")
    out = _type_coverage(_ctx(tmp_path), format="json")
    data = json.loads(out)
    assert data["coverage_pct"] == 100.0


def test_type_coverage_category_param_only(tmp_path):
    _write(tmp_path, "a.py", "def func(x) -> int:\n    return x\n")
    out = _type_coverage(_ctx(tmp_path), category="missing_param_annotations", format="json")
    data = json.loads(out)
    # Every file entry for missing_return should be present as empty (filtered)
    for f in data["files"]:
        assert "missing_return_items" in f  # key still present in full file data
    # The text output only shows param category
    text_out = _type_coverage(_ctx(tmp_path), category="missing_param_annotations", format="text")
    assert "missing_param_annotations" in text_out


def test_type_coverage_category_return_only(tmp_path):
    _write(tmp_path, "a.py", "def func(x: int):\n    return x\n")
    out = _type_coverage(_ctx(tmp_path), category="missing_return_annotations", format="text")
    assert "missing_return_annotations" in out
    assert "missing_param_annotations" not in out


def test_type_coverage_invalid_category(tmp_path):
    out = _type_coverage(_ctx(tmp_path), category="garbage")
    assert "Unknown category" in out


def test_type_coverage_path_filter(tmp_path):
    subdir = tmp_path / "sub"
    subdir.mkdir()
    (subdir / "m.py").write_text("def f(x): pass\n", encoding="utf-8")
    (tmp_path / "root.py").write_text("def g(x: int) -> int: return x\n", encoding="utf-8")
    out = _type_coverage(_ctx(tmp_path), path="sub", format="json")
    data = json.loads(out)
    files = [f["file"] for f in data["files"]]
    assert any("m.py" in f for f in files)
    assert not any("root.py" in f for f in files)


def test_type_coverage_skip_private(tmp_path):
    _write(tmp_path, "a.py", """\
        def _internal(x):
            pass
        def public(x: int) -> int:
            return x
    """)
    out = _type_coverage(_ctx(tmp_path), skip_private=True, format="json")
    data = json.loads(out)
    for f in data["files"]:
        for item in f["missing_param_items"]:
            assert not item["name"].startswith("_")


# ── Tool registration ─────────────────────────────────────────────────────────

def test_get_tools_returns_one():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "type_coverage"


def test_tool_schema_properties():
    schema = get_tools()[0].schema
    props = schema["parameters"]["properties"]
    assert "path" in props
    assert "category" in props
    assert "format" in props
    assert "min_missing" in props
    assert "skip_private" in props


def test_all_categories_defined():
    assert _CATEGORY_MISSING_PARAM in _ALL_CATEGORIES
    assert _CATEGORY_MISSING_RETURN in _ALL_CATEGORIES
    assert len(_ALL_CATEGORIES) == 2
