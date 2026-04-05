"""Tests for ouroboros/tools/semantic_diff.py"""
from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.tools.semantic_diff import (
    ClassInfo,
    FileDiff,
    FuncInfo,
    ModuleSnapshot,
    _analyse,
    _changed_py_files,
    _compare_snapshots,
    _format_json,
    _format_text,
    _git_show,
    _parse_source,
    _resolve_ref,
    _semantic_diff,
    get_tools,
)
from ouroboros.tools.registry import ToolContext


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _ctx():
    return ToolContext(repo_dir="/opt/veles", drive_root="/opt/veles-data")


# ── _parse_source ──────────────────────────────────────────────────────────────

def test_parse_source_empty():
    snap = _parse_source("")
    assert snap.functions == {}
    assert snap.classes == {}


def test_parse_source_simple_function():
    src = "def foo():\n    pass\n"
    snap = _parse_source(src)
    assert "foo" in snap.functions
    fi = snap.functions["foo"]
    assert fi.name == "foo"
    assert not fi.is_method
    assert fi.parent_class is None


def test_parse_source_class_with_methods():
    src = "class MyClass:\n    def method_a(self):\n        pass\n    def method_b(self):\n        pass\n"
    snap = _parse_source(src)
    assert "MyClass" in snap.classes
    assert "MyClass.method_a" in snap.functions
    assert "MyClass.method_b" in snap.functions
    assert snap.functions["MyClass.method_a"].is_method
    assert snap.functions["MyClass.method_a"].parent_class == "MyClass"


def test_parse_source_async_function():
    src = "async def async_fn():\n    pass\n"
    snap = _parse_source(src)
    assert "async_fn" in snap.functions


def test_parse_source_invalid_syntax():
    snap = _parse_source("def broken(:\n    pass\n")
    assert snap.functions == {}
    assert snap.classes == {}


def test_parse_source_line_count():
    src = "def big():\n    a = 1\n    b = 2\n    c = 3\n    return a + b + c\n"
    snap = _parse_source(src)
    assert snap.functions["big"].line_count >= 2


def test_parse_source_full_name_method():
    src = "class Foo:\n    def bar(self):\n        pass\n"
    snap = _parse_source(src)
    fi = snap.functions["Foo.bar"]
    assert fi.full_name == "Foo.bar"


def test_parse_source_top_level_full_name():
    src = "def standalone():\n    pass\n"
    snap = _parse_source(src)
    assert snap.functions["standalone"].full_name == "standalone"


# ── FuncInfo / FileDiff helpers ────────────────────────────────────────────────

def test_file_diff_total_changes_empty():
    fd = FileDiff(path="x.py")
    assert fd.total_changes == 0
    assert fd.is_empty()


def test_file_diff_total_changes_counts():
    fd = FileDiff(
        path="x.py",
        added_funcs=["a", "b"],
        removed_funcs=["c"],
        modified_funcs=["d"],
        added_classes=["E"],
    )
    assert fd.total_changes == 5
    assert not fd.is_empty()


def test_file_diff_file_added_flag():
    fd = FileDiff(path="x.py", file_added=True)
    assert fd.total_changes == 1


# ── _compare_snapshots ─────────────────────────────────────────────────────────

def test_compare_snapshots_new_file():
    src = "def new_fn():\n    pass\nclass NewCls:\n    pass\n"
    snap_b = _parse_source(src)
    fd = _compare_snapshots("new.py", None, snap_b)
    assert fd.file_added
    assert "new_fn" in fd.added_funcs
    assert "NewCls" in fd.added_classes


def test_compare_snapshots_deleted_file():
    src = "def old_fn():\n    pass\n"
    snap_a = _parse_source(src)
    fd = _compare_snapshots("old.py", snap_a, None)
    assert fd.file_removed
    assert "old_fn" in fd.removed_funcs


def test_compare_snapshots_added_func():
    src_a = "def foo():\n    pass\n"
    src_b = "def foo():\n    pass\ndef bar():\n    pass\n"
    snap_a = _parse_source(src_a)
    snap_b = _parse_source(src_b)
    fd = _compare_snapshots("f.py", snap_a, snap_b)
    assert "bar" in fd.added_funcs
    assert "foo" not in fd.added_funcs


def test_compare_snapshots_removed_func():
    src_a = "def foo():\n    pass\ndef old():\n    pass\n"
    src_b = "def foo():\n    pass\n"
    snap_a = _parse_source(src_a)
    snap_b = _parse_source(src_b)
    fd = _compare_snapshots("f.py", snap_a, snap_b)
    assert "old" in fd.removed_funcs


def test_compare_snapshots_modified_func():
    src_a = "def foo():\n    pass\n"
    src_b = "def foo():\n    a = 1\n    b = 2\n    return a + b\n"
    snap_a = _parse_source(src_a)
    snap_b = _parse_source(src_b)
    fd = _compare_snapshots("f.py", snap_a, snap_b)
    assert "foo" in fd.modified_funcs


def test_compare_snapshots_both_none():
    fd = _compare_snapshots("x.py", None, None)
    assert fd.is_empty()


# ── _format_text / _format_json ────────────────────────────────────────────────

def _make_diffs():
    fd = FileDiff(
        path="ouroboros/tools/foo.py",
        added_funcs=["new_fn"],
        removed_funcs=["old_fn"],
        modified_funcs=["changed_fn"],
        added_classes=["NewClass"],
    )
    totals = {
        "added_funcs": 1, "removed_funcs": 1, "modified_funcs": 1,
        "added_classes": 1, "removed_classes": 0,
        "files_added": 0, "files_removed": 0,
    }
    return [fd], totals


def test_format_text_contains_summary():
    diffs, totals = _make_diffs()
    text = _format_text(diffs, totals, "HEAD~1", "HEAD", "abc1234", "def5678")
    assert "Semantic Diff" in text
    assert "abc1234" in text
    assert "def5678" in text
    assert "+1 funcs" in text


def test_format_text_contains_file():
    diffs, totals = _make_diffs()
    text = _format_text(diffs, totals, "HEAD~1", "HEAD", "abc", "def")
    assert "ouroboros/tools/foo.py" in text
    assert "new_fn" in text
    assert "old_fn" in text
    assert "changed_fn" in text
    assert "NewClass" in text


def test_format_text_empty_diffs():
    text = _format_text([], {k: 0 for k in ["added_funcs","removed_funcs","modified_funcs","added_classes","removed_classes","files_added","files_removed"]}, "a", "b", "a", "b")
    assert "No semantic changes" in text


def test_format_json_structure():
    diffs, totals = _make_diffs()
    result = _format_json(diffs, totals, "HEAD~1", "HEAD", "abc", "def")
    assert result["ref_a"] == "HEAD~1"
    assert result["ref_b"] == "HEAD"
    assert "totals" in result
    assert len(result["files"]) == 1
    fdata = result["files"][0]
    assert "added_funcs" in fdata
    assert fdata["added_funcs"] == ["new_fn"]


# ── get_tools ──────────────────────────────────────────────────────────────────

def test_get_tools_returns_one():
    tools = get_tools()
    assert len(tools) == 1


def test_get_tools_name():
    tools = get_tools()
    assert tools[0].name == "semantic_diff"


def test_get_tools_schema_has_ref_params():
    tools = get_tools()
    props = tools[0].schema["parameters"]["properties"]
    assert "ref_a" in props
    assert "ref_b" in props
    assert "path_filter" in props
    assert "format" in props


# ── _semantic_diff integration (uses real git) ─────────────────────────────────

def test_semantic_diff_head_to_head():
    """HEAD~1 → HEAD on real repo should return a non-error result."""
    ctx = _ctx()
    result = _semantic_diff(ctx, ref_a="HEAD~1", ref_b="HEAD", format="text")
    assert "result" in result
    assert "Semantic Diff" in result["result"]


def test_semantic_diff_json_format():
    ctx = _ctx()
    result = _semantic_diff(ctx, ref_a="HEAD~1", ref_b="HEAD", format="json")
    assert "result" in result
    data = json.loads(result["result"])
    assert "totals" in data
    assert "files" in data


def test_semantic_diff_path_filter():
    ctx = _ctx()
    result = _semantic_diff(
        ctx, ref_a="HEAD~5", ref_b="HEAD",
        path_filter="ouroboros/tools", format="text"
    )
    assert "result" in result
    text = result["result"]
    # All listed files must contain the filter string
    for line in text.splitlines():
        if line.startswith("### "):
            filepath = line.split("### ")[1].split("  ")[0]
            assert "ouroboros/tools" in filepath, f"Unexpected file: {filepath}"
