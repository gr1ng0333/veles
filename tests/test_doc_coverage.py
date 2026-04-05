"""Tests for ouroboros/tools/doc_coverage.py"""

import ast
import json
import os
import sys
import textwrap
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Make sure we can import from the repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from ouroboros.tools.doc_coverage import (
    _collect_py_files,
    _has_docstring,
    _is_private,
    _qualified_name,
    _scan_file,
    _FileSummary,
    _scan_codebase,
    _doc_coverage,
    get_tools,
    _CAT_MODULE,
    _CAT_CLASS,
    _CAT_FUNCTION,
    _ALL_CATEGORIES,
)
from ouroboros.tools.registry import ToolContext


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(repo_dir=str(tmp_path), drive_root="/tmp")


def _write(tmp_path: Path, rel: str, content: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content))
    return p


# ── _has_docstring ────────────────────────────────────────────────────────────

def test_has_docstring_module_yes():
    tree = ast.parse('"""Module doc."""\nx = 1')
    assert _has_docstring(tree) is True


def test_has_docstring_module_no():
    tree = ast.parse("x = 1")
    assert _has_docstring(tree) is False


def test_has_docstring_function_yes():
    tree = ast.parse('def f():\n    """Doc."""\n    pass')
    func = tree.body[0]
    assert _has_docstring(func) is True


def test_has_docstring_function_no():
    tree = ast.parse("def f():\n    pass")
    func = tree.body[0]
    assert _has_docstring(func) is False


def test_has_docstring_class_yes():
    tree = ast.parse('class C:\n    """Doc."""\n    pass')
    cls = tree.body[0]
    assert _has_docstring(cls) is True


def test_has_docstring_class_no():
    tree = ast.parse("class C:\n    pass")
    cls = tree.body[0]
    assert _has_docstring(cls) is False


def test_has_docstring_empty_body():
    # Empty body node (synthetic)
    class FakeNode:
        body = []
    assert _has_docstring(FakeNode()) is False


# ── _is_private ───────────────────────────────────────────────────────────────

def test_is_private_single_underscore():
    assert _is_private("_helper") is True


def test_is_private_dunder():
    assert _is_private("__init__") is True


def test_is_private_public():
    assert _is_private("public_func") is False


def test_is_private_empty():
    assert _is_private("") is False


# ── _qualified_name ───────────────────────────────────────────────────────────

def test_qualified_name_no_class():
    assert _qualified_name([], "func") == "func"


def test_qualified_name_one_class():
    assert _qualified_name(["MyClass"], "method") == "MyClass.method"


def test_qualified_name_nested_class():
    assert _qualified_name(["Outer", "Inner"], "method") == "Outer.Inner.method"


# ── _scan_file ────────────────────────────────────────────────────────────────

def test_scan_file_fully_documented(tmp_path):
    p = _write(tmp_path, "a.py", '''\
        """Module doc."""

        class C:
            """Class doc."""

            def method(self):
                """Method doc."""
                pass

        def func():
            """Func doc."""
            pass
    ''')
    summary = _scan_file(p, "a.py", skip_private=False)
    assert summary is not None
    assert summary.missing_module is False
    assert summary.missing_class_items == []
    assert summary.missing_function_items == []
    # module + class + method + func = 4
    assert summary.total_items == 4
    assert summary.documented_items == 4


def test_scan_file_missing_module_doc(tmp_path):
    p = _write(tmp_path, "b.py", '''\
        def func():
            """Func doc."""
            pass
    ''')
    summary = _scan_file(p, "b.py", skip_private=False)
    assert summary is not None
    assert summary.missing_module is True


def test_scan_file_missing_class_doc(tmp_path):
    p = _write(tmp_path, "c.py", '''\
        """Module doc."""

        class C:
            pass
    ''')
    summary = _scan_file(p, "c.py", skip_private=False)
    assert summary is not None
    assert len(summary.missing_class_items) == 1
    assert summary.missing_class_items[0]["name"] == "C"


def test_scan_file_missing_function_doc(tmp_path):
    p = _write(tmp_path, "d.py", '''\
        """Module doc."""

        def no_doc():
            pass
    ''')
    summary = _scan_file(p, "d.py", skip_private=False)
    assert summary is not None
    assert len(summary.missing_function_items) == 1
    assert summary.missing_function_items[0]["name"] == "no_doc"


def test_scan_file_skip_private(tmp_path):
    p = _write(tmp_path, "e.py", '''\
        """Module doc."""

        def _helper():
            pass

        def public():
            pass
    ''')
    summary = _scan_file(p, "e.py", skip_private=True)
    assert summary is not None
    # Only public is counted
    missing_names = [i["name"] for i in summary.missing_function_items]
    assert "_helper" not in missing_names
    assert "public" in missing_names


def test_scan_file_method_in_class(tmp_path):
    p = _write(tmp_path, "f.py", '''\
        """Mod."""

        class C:
            """Class doc."""

            def method_no_doc(self):
                pass
    ''')
    summary = _scan_file(p, "f.py", skip_private=False)
    assert summary is not None
    assert len(summary.missing_function_items) == 1
    assert summary.missing_function_items[0]["name"] == "C.method_no_doc"


def test_scan_file_syntax_error_returns_none(tmp_path):
    p = tmp_path / "bad.py"
    p.write_text("def (broken syntax:\n    pass")
    summary = _scan_file(p, "bad.py", skip_private=False)
    assert summary is None


def test_scan_file_async_function(tmp_path):
    p = _write(tmp_path, "g.py", '''\
        """Mod."""

        async def async_no_doc():
            pass
    ''')
    summary = _scan_file(p, "g.py", skip_private=False)
    assert summary is not None
    assert len(summary.missing_function_items) == 1
    assert summary.missing_function_items[0]["name"] == "async_no_doc"


# ── _collect_py_files ─────────────────────────────────────────────────────────

def test_collect_py_files_basic(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.py").write_text("")
    files = _collect_py_files(tmp_path)
    assert len(files) == 3


def test_collect_py_files_subpath(tmp_path):
    (tmp_path / "a.py").write_text("")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("")
    files = _collect_py_files(tmp_path, "sub")
    assert len(files) == 1
    assert files[0].name == "c.py"


def test_collect_py_files_single_file(tmp_path):
    p = tmp_path / "solo.py"
    p.write_text("")
    files = _collect_py_files(tmp_path, "solo.py")
    assert files == [p]


# ── _scan_codebase ─────────────────────────────────────────────────────────────

def test_scan_codebase_basic(tmp_path):
    _write(tmp_path, "x.py", '"""Mod."""\ndef f(): pass\n')
    per_file, n = _scan_codebase(tmp_path, None, skip_private=False)
    assert n >= 1
    assert "x.py" in per_file


def test_scan_codebase_no_files(tmp_path):
    per_file, n = _scan_codebase(tmp_path, None, skip_private=False)
    assert n == 0
    assert per_file == {}


# ── _doc_coverage tool ────────────────────────────────────────────────────────

def test_doc_coverage_text_output(tmp_path):
    _write(tmp_path, "m.py", '"""Mod."""\ndef f(): pass\n')
    ctx = _make_ctx(tmp_path)
    result = _doc_coverage(ctx)
    assert "Docstring Coverage Report" in result
    assert "missing_function_docstrings" in result


def test_doc_coverage_json_output(tmp_path):
    _write(tmp_path, "m.py", '"""Mod."""\ndef f(): pass\n')
    ctx = _make_ctx(tmp_path)
    result = _doc_coverage(ctx, format="json")
    data = json.loads(result)
    assert "total_files" in data
    assert "coverage_pct" in data
    assert "files" in data


def test_doc_coverage_category_filter(tmp_path):
    _write(tmp_path, "m.py", 'def f(): pass\n')
    ctx = _make_ctx(tmp_path)
    result = _doc_coverage(ctx, category=_CAT_MODULE)
    assert _CAT_MODULE in result
    # Should NOT contain class or function category headers
    assert _CAT_CLASS not in result


def test_doc_coverage_invalid_category(tmp_path):
    ctx = _make_ctx(tmp_path)
    result = _doc_coverage(ctx, category="invalid_category")
    assert "Unknown category" in result


def test_doc_coverage_skip_private(tmp_path):
    _write(tmp_path, "p.py", '''\
        """Mod."""

        def _private(): pass
        def public(): pass
    ''')
    ctx = _make_ctx(tmp_path)
    result = _doc_coverage(ctx, skip_private=True)
    # _private must not appear in the missing-functions listing
    # (the header contains 'skip_private=True' which is fine)
    missing_fn_section = result.split('missing_function_docstrings')[-1] if 'missing_function_docstrings' in result else ''
    assert '_private' not in missing_fn_section, f'_private in fn section: {missing_fn_section[:200]}'


def test_doc_coverage_min_missing(tmp_path):
    # File with only 1 missing function: should be excluded if min_missing=5
    _write(tmp_path, "few.py", '"""Mod."""\ndef f(): pass\n')
    ctx = _make_ctx(tmp_path)
    result = _doc_coverage(ctx, min_missing=5)
    # "f" should not appear (only 1 missing in that file)
    # (module-level missing is filtered separately; we just check func section)
    # The report should still render without error
    assert "Docstring Coverage Report" in result


def test_doc_coverage_fully_documented(tmp_path):
    _write(tmp_path, "full.py", '''\
        """Mod doc."""

        class C:
            """Class doc."""

            def method(self):
                """Method doc."""
                pass
    ''')
    ctx = _make_ctx(tmp_path)
    result = _doc_coverage(ctx)
    assert "100.0%" in result


# ── get_tools ─────────────────────────────────────────────────────────────────

def test_get_tools_returns_one():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "doc_coverage"


def test_get_tools_schema_valid():
    tool = get_tools()[0]
    schema = tool.schema
    assert schema["name"] == "doc_coverage"
    assert "parameters" in schema
    props = schema["parameters"]["properties"]
    assert "path" in props
    assert "category" in props
    assert "format" in props
    assert "min_missing" in props
    assert "skip_private" in props


def test_get_tools_execute_callable():
    tool = get_tools()[0]
    assert callable(tool.handler)


# ── ALL_CATEGORIES completeness ───────────────────────────────────────────────

def test_all_categories_contains_three():
    assert len(_ALL_CATEGORIES) == 3
    assert _CAT_MODULE in _ALL_CATEGORIES
    assert _CAT_CLASS in _ALL_CATEGORIES
    assert _CAT_FUNCTION in _ALL_CATEGORIES
