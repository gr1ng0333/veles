"""Tests for ouroboros/tools/duplicate_code.py"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("REPO_DIR", "/opt/veles")

from ouroboros.tools.duplicate_code import (
    _CLONE_TYPE_EXACT,
    _CLONE_TYPE_NORMALIZED,
    _collect_py_files,
    _exact_hash,
    _normalized_hash,
    _body_lines,
    _extract_functions,
    _find_clones,
    _duplicate_code,
    get_tools,
)
from ouroboros.tools.registry import ToolContext

import ast


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


def _parse_func(src: str) -> ast.FunctionDef:
    tree = ast.parse(textwrap.dedent(src))
    return tree.body[0]


# ── _collect_py_files ─────────────────────────────────────────────────────────

def test_collect_single_file(tmp_path):
    f = _write(tmp_path, "a.py", "x = 1")
    files = _collect_py_files(tmp_path)
    assert f in files


def test_collect_skips_pycache(tmp_path):
    _write(tmp_path, "__pycache__/a.py", "x = 1")
    _write(tmp_path, "b.py", "x = 1")
    files = _collect_py_files(tmp_path)
    assert all("__pycache__" not in str(f) for f in files)


def test_collect_subpath_filter(tmp_path):
    _write(tmp_path, "pkg/a.py", "x = 1")
    _write(tmp_path, "other/b.py", "x = 1")
    files = _collect_py_files(tmp_path, subpath="pkg")
    assert all("pkg" in str(f) for f in files)
    assert not any("other" in str(f) for f in files)


def test_collect_direct_file(tmp_path):
    f = _write(tmp_path, "solo.py", "x = 1")
    files = _collect_py_files(f)
    assert files == [f]


# ── _exact_hash ───────────────────────────────────────────────────────────────

def test_exact_hash_same_body():
    src1 = "def f():\n    return 42\n"
    src2 = "def g():\n    return 42\n"
    f1 = _parse_func(src1)
    f2 = _parse_func(src2)
    assert _exact_hash(f1) == _exact_hash(f2)


def test_exact_hash_different_body():
    src1 = "def f():\n    return 42\n"
    src2 = "def f():\n    return 99\n"
    f1 = _parse_func(src1)
    f2 = _parse_func(src2)
    assert _exact_hash(f1) != _exact_hash(f2)


def test_exact_hash_is_hex_string():
    func = _parse_func("def f():\n    pass\n")
    h = _exact_hash(func)
    assert isinstance(h, str)
    assert len(h) == 64  # SHA-256


# ── _normalized_hash ──────────────────────────────────────────────────────────

def test_normalized_hash_renames_vars():
    src1 = "def f(a, b):\n    return a + b\n"
    src2 = "def g(x, y):\n    return x + y\n"
    f1 = _parse_func(src1)
    f2 = _parse_func(src2)
    assert _normalized_hash(f1) == _normalized_hash(f2)


def test_normalized_hash_replaces_strings():
    src1 = "def f():\n    return 'hello'\n"
    src2 = "def g():\n    return 'world'\n"
    f1 = _parse_func(src1)
    f2 = _parse_func(src2)
    assert _normalized_hash(f1) == _normalized_hash(f2)


def test_normalized_hash_replaces_numbers():
    src1 = "def f():\n    return 1\n"
    src2 = "def g():\n    return 99\n"
    f1 = _parse_func(src1)
    f2 = _parse_func(src2)
    assert _normalized_hash(f1) == _normalized_hash(f2)


def test_normalized_hash_different_structure():
    src1 = "def f(a):\n    return a + 1\n"
    src2 = "def g(x):\n    return x * x\n"
    f1 = _parse_func(src1)
    f2 = _parse_func(src2)
    assert _normalized_hash(f1) != _normalized_hash(f2)


def test_exact_differs_from_normalized_when_vars_renamed():
    src1 = "def f(a):\n    return a\n"
    src2 = "def g(z):\n    return z\n"
    f1 = _parse_func(src1)
    f2 = _parse_func(src2)
    # exact hashes differ (different arg name), normalized match
    assert _exact_hash(f1) != _exact_hash(f2)
    assert _normalized_hash(f1) == _normalized_hash(f2)


# ── _body_lines ───────────────────────────────────────────────────────────────

def test_body_lines_simple():
    src = "def f():\n    x = 1\n    return x\n"
    func = _parse_func(src)
    assert _body_lines(func) >= 1


def test_body_lines_multiline():
    src = "\n".join(["def f():"] + [f"    x{i} = {i}" for i in range(10)] + ["    return x0"])
    func = _parse_func(src)
    assert _body_lines(func) >= 5


# ── _extract_functions ────────────────────────────────────────────────────────

def test_extract_functions_basic(tmp_path):
    src = """
def alpha():
    return 1

def beta():
    return 2
"""
    f = _write(tmp_path, "mod.py", src)
    recs = _extract_functions(f, "mod.py")
    names = {r.name for r in recs}
    assert "alpha" in names
    assert "beta" in names


def test_extract_functions_syntax_error(tmp_path):
    f = _write(tmp_path, "bad.py", "def broken(:\n    pass")
    recs = _extract_functions(f, "bad.py")
    assert recs == []


def test_extract_functions_stores_file_and_line(tmp_path):
    src = "def foo():\n    return 42\n"
    f = _write(tmp_path, "x.py", src)
    recs = _extract_functions(f, "x.py")
    assert recs[0].file == "x.py"
    assert recs[0].line == 1


# ── _find_clones ──────────────────────────────────────────────────────────────

def _make_records(tmp_path: Path, files: dict) -> list:
    """Write files and extract all records."""
    from ouroboros.tools.duplicate_code import _FuncRecord
    recs = []
    for name, src in files.items():
        p = _write(tmp_path, name, src)
        recs.extend(_extract_functions(p, name))
    return recs


def test_find_clones_exact(tmp_path):
    src_a = "def f():\n    x = 1\n    y = 2\n    return x + y\n"
    src_b = "def g():\n    x = 1\n    y = 2\n    return x + y\n"
    recs = _make_records(tmp_path, {"a.py": src_a, "b.py": src_b})
    groups = _find_clones(recs, min_lines=1, min_group_size=2, clone_type="exact")
    assert any(g["clone_type"] == "exact" for g in groups)


def test_find_clones_normalized(tmp_path):
    src_a = "def f(a, b):\n    result = a + b\n    return result\n"
    src_b = "def g(x, y):\n    total = x + y\n    return total\n"
    recs = _make_records(tmp_path, {"a.py": src_a, "b.py": src_b})
    groups = _find_clones(recs, min_lines=1, min_group_size=2, clone_type="normalized")
    assert any(g["clone_type"] == "normalized" for g in groups)


def test_find_clones_min_lines_filter(tmp_path):
    src_a = "def f():\n    return 1\n"  # 1 line body
    src_b = "def g():\n    return 1\n"
    recs = _make_records(tmp_path, {"a.py": src_a, "b.py": src_b})
    groups = _find_clones(recs, min_lines=10, min_group_size=2, clone_type="exact")
    assert groups == []


def test_find_clones_min_group_size(tmp_path):
    src_a = "def f():\n    x = 1\n    return x\n"
    src_b = "def g():\n    x = 1\n    return x\n"
    recs = _make_records(tmp_path, {"a.py": src_a, "b.py": src_b})
    groups = _find_clones(recs, min_lines=1, min_group_size=3, clone_type="exact")
    assert groups == []


def test_find_clones_no_duplicates(tmp_path):
    src_a = "def f():\n    return 1 + 2\n"
    src_b = "def g():\n    return 'hello'\n"
    recs = _make_records(tmp_path, {"a.py": src_a, "b.py": src_b})
    groups = _find_clones(recs, min_lines=1, min_group_size=2, clone_type="all")
    assert groups == []


# ── _duplicate_code integration ───────────────────────────────────────────────

def test_duplicate_code_text_output(tmp_path):
    src = "def f():\n    x = 1\n    y = 2\n    return x + y\n"
    _write(tmp_path, "a.py", src)
    _write(tmp_path, "b.py", src.replace("def f(", "def g("))
    result = _duplicate_code(_ctx(tmp_path))
    assert "Duplicate Code Report" in result


def test_duplicate_code_json_output(tmp_path):
    src = "def f():\n    x = 1\n    return x\n"
    _write(tmp_path, "a.py", src)
    _write(tmp_path, "b.py", src.replace("def f(", "def h("))
    result = _duplicate_code(_ctx(tmp_path), format="json")
    data = json.loads(result)
    assert "total_files" in data
    assert "clone_groups" in data


def test_duplicate_code_no_clones(tmp_path):
    _write(tmp_path, "a.py", "def f():\n    return 1\n")
    _write(tmp_path, "b.py", "def g():\n    return 'hello'\n")
    result = _duplicate_code(_ctx(tmp_path), min_lines=1)
    assert "No duplicate" in result


def test_duplicate_code_invalid_clone_type(tmp_path):
    _write(tmp_path, "a.py", "x = 1")
    result = _duplicate_code(_ctx(tmp_path), clone_type="bogus")
    assert "Unknown" in result


def test_duplicate_code_exact_only(tmp_path):
    src_a = "def f():\n    x = 1\n    y = 2\n    return x + y\n"
    src_b = "def g():\n    x = 1\n    y = 2\n    return x + y\n"
    _write(tmp_path, "a.py", src_a)
    _write(tmp_path, "b.py", src_b)
    result = _duplicate_code(_ctx(tmp_path), clone_type="exact", min_lines=1)
    assert "EXACT" in result


def test_duplicate_code_path_filter(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "other").mkdir()
    src = "def f():\n    x = 1\n    return x\n"
    _write(tmp_path, "pkg/a.py", src)
    _write(tmp_path, "other/b.py", src.replace("def f", "def g"))
    result = _duplicate_code(_ctx(tmp_path), path="pkg")
    assert "Duplicate Code Report" in result


def test_duplicate_code_json_has_instances(tmp_path):
    src = "def common():\n    a = 1\n    b = 2\n    return a + b\n"
    _write(tmp_path, "mod1.py", src)
    _write(tmp_path, "mod2.py", src.replace("def common", "def also_common"))
    result = _duplicate_code(_ctx(tmp_path), format="json", min_lines=1)
    data = json.loads(result)
    for grp in data.get("clone_groups", []):
        assert "instances" in grp
        for inst in grp["instances"]:
            assert "file" in inst
            assert "line" in inst
            assert "name" in inst


# ── get_tools ─────────────────────────────────────────────────────────────────

def test_get_tools_returns_list():
    tools = get_tools()
    assert isinstance(tools, list)
    assert len(tools) == 1


def test_get_tools_name():
    assert get_tools()[0].name == "duplicate_code"


def test_get_tools_schema_has_properties():
    schema = get_tools()[0].schema
    assert "parameters" in schema
    props = schema["parameters"]["properties"]
    assert "path" in props
    assert "clone_type" in props
    assert "min_lines" in props


def test_get_tools_handler_callable():
    entry = get_tools()[0]
    assert callable(entry.handler)
