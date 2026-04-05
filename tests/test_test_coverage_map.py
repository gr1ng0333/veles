"""Tests for test_coverage_map — per-function coverage heuristics."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import List

import pytest

from ouroboros.tools.test_coverage_map import (
    FunctionCoverage,
    FunctionInfo,
    TestFileInfo,
    _analyse_file,
    _check_coverage_signals,
    _collect_py_files,
    _compute_stats,
    _extract_functions,
    _find_test_files,
    _format_dir_text,
    _format_file_text,
    _resolve_target,
    _test_coverage_map,
    get_tools,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


# ── _collect_py_files ─────────────────────────────────────────────────────────

class TestCollectPyFiles:
    def test_single_file(self, tmp_path: Path) -> None:
        f = _write(tmp_path / "a.py", "x = 1")
        result = _collect_py_files(f)
        assert result == [f]

    def test_directory(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.py", "")
        _write(tmp_path / "b.py", "")
        _write(tmp_path / "__pycache__" / "x.pyc", "")
        result = _collect_py_files(tmp_path)
        names = {p.name for p in result}
        assert "a.py" in names
        assert "b.py" in names

    def test_skips_pycache(self, tmp_path: Path) -> None:
        _write(tmp_path / "__pycache__" / "cached.py", "")
        result = _collect_py_files(tmp_path)
        assert all("__pycache__" not in str(p) for p in result)


# ── _extract_functions ────────────────────────────────────────────────────────

class TestExtractFunctions:
    def test_top_level_functions(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "mod.py", "def foo(): pass\ndef bar(): pass\n")
        funcs = _extract_functions(src)
        names = {f.name for f in funcs}
        assert names == {"foo", "bar"}

    def test_class_methods(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "mod.py", (
            "class MyClass:\n"
            "    def method_a(self): pass\n"
            "    def method_b(self): pass\n"
        ))
        funcs = _extract_functions(src)
        assert all(f.is_method for f in funcs)
        assert all(f.class_name == "MyClass" for f in funcs)
        assert {f.name for f in funcs} == {"method_a", "method_b"}

    def test_private_detection(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "mod.py", "def _private(): pass\ndef public(): pass\n")
        funcs = _extract_functions(src)
        priv = [f for f in funcs if f.name == "_private"]
        pub = [f for f in funcs if f.name == "public"]
        assert priv[0].is_private is True
        assert pub[0].is_private is False

    def test_dunder_skipped(self, tmp_path: Path) -> None:
        # __init__ etc. are NOT in extract_functions but will be skipped in analyse_file
        src = _write(tmp_path / "mod.py", (
            "class C:\n"
            "    def __init__(self): pass\n"
            "    def public(self): pass\n"
        ))
        funcs = _extract_functions(src)
        # __init__ is returned here but filtered in _analyse_file
        names = {f.name for f in funcs}
        assert "public" in names

    def test_syntax_error_returns_empty(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "bad.py", "def foo(: pass")
        assert _extract_functions(src) == []


# ── TestFileInfo ──────────────────────────────────────────────────────────────

class TestTestFileInfo:
    def test_collects_test_names(self, tmp_path: Path) -> None:
        f = _write(tmp_path / "test_foo.py", (
            "def test_my_func(): pass\n"
            "def test_other(): pass\n"
        ))
        tfi = TestFileInfo(f)
        assert "test_my_func" in tfi.test_func_names
        assert "test_other" in tfi.test_func_names

    def test_collects_call_names(self, tmp_path: Path) -> None:
        f = _write(tmp_path / "test_foo.py", (
            "def test_something():\n"
            "    result = my_func(1, 2)\n"
            "    assert result\n"
        ))
        tfi = TestFileInfo(f)
        assert "my_func" in tfi.call_names

    def test_collects_import_names(self, tmp_path: Path) -> None:
        f = _write(tmp_path / "test_foo.py", (
            "from mymodule import helper_fn\n"
            "def test_x(): pass\n"
        ))
        tfi = TestFileInfo(f)
        assert "helper_fn" in tfi.import_names

    def test_class_test_methods(self, tmp_path: Path) -> None:
        f = _write(tmp_path / "test_bar.py", (
            "class TestSomething:\n"
            "    def test_method(self):\n"
            "        target_fn()\n"
        ))
        tfi = TestFileInfo(f)
        assert "test_method" in tfi.test_func_names
        assert "target_fn" in tfi.call_names


# ── _check_coverage_signals ───────────────────────────────────────────────────

class TestCheckCoverageSignals:
    def _make_func(self, name: str, class_name: str = "") -> FunctionInfo:
        return FunctionInfo(name=name, line=1, is_method=bool(class_name), class_name=class_name)

    def _make_tfi(self, tmp_path: Path, name: str, content: str) -> TestFileInfo:
        f = _write(tmp_path / name, content)
        return TestFileInfo(f)

    def test_name_match_exact(self, tmp_path: Path) -> None:
        func = self._make_func("foo")
        tfi = self._make_tfi(tmp_path, "test_mod.py", "def test_foo(): pass\n")
        covered, signal = _check_coverage_signals(func, [tfi])
        assert covered is True
        assert signal == "name_match"

    def test_name_match_prefix(self, tmp_path: Path) -> None:
        func = self._make_func("foo")
        tfi = self._make_tfi(tmp_path, "test_mod.py", "def test_foo_edge_case(): pass\n")
        covered, signal = _check_coverage_signals(func, [tfi])
        assert covered is True
        assert signal == "name_match"

    def test_call_ref(self, tmp_path: Path) -> None:
        func = self._make_func("compute")
        tfi = self._make_tfi(tmp_path, "test_mod.py", (
            "def test_something():\n"
            "    result = compute(5)\n"
        ))
        covered, signal = _check_coverage_signals(func, [tfi])
        assert covered is True
        assert signal == "call_ref"

    def test_import_ref(self, tmp_path: Path) -> None:
        func = self._make_func("parse_data")
        tfi = self._make_tfi(tmp_path, "test_mod.py", (
            "from mymod import parse_data\n"
            "def test_x(): pass\n"
        ))
        covered, signal = _check_coverage_signals(func, [tfi])
        assert covered is True
        assert signal == "import_ref"

    def test_uncovered(self, tmp_path: Path) -> None:
        func = self._make_func("forgotten_func")
        tfi = self._make_tfi(tmp_path, "test_mod.py", "def test_other(): pass\n")
        covered, signal = _check_coverage_signals(func, [tfi])
        assert covered is False
        assert signal == ""

    def test_no_test_files(self) -> None:
        func = self._make_func("foo")
        covered, signal = _check_coverage_signals(func, [])
        assert covered is False


# ── _compute_stats ────────────────────────────────────────────────────────────

class TestComputeStats:
    def _cov(self, status: str) -> FunctionCoverage:
        fi = FunctionInfo(name="f", line=1)
        return FunctionCoverage(fi, status=status, signal="")

    def test_all_covered(self) -> None:
        items = [self._cov("covered")] * 4
        stats = _compute_stats(items)
        assert stats["coverage_pct"] == 100.0
        assert stats["covered"] == 4
        assert stats["uncovered"] == 0

    def test_none_covered(self) -> None:
        items = [self._cov("uncovered")] * 3
        stats = _compute_stats(items)
        assert stats["coverage_pct"] == 0.0
        assert stats["uncovered"] == 3

    def test_skipped_excluded(self) -> None:
        items = [self._cov("covered"), self._cov("skipped"), self._cov("uncovered")]
        stats = _compute_stats(items)
        assert stats["total_public"] == 2  # covered + uncovered only
        assert stats["covered"] == 1

    def test_empty(self) -> None:
        stats = _compute_stats([])
        assert stats["coverage_pct"] == 0.0
        assert stats["total_public"] == 0


# ── _analyse_file integration ─────────────────────────────────────────────────

class TestAnalyseFile:
    def test_covered_by_name_match(self, tmp_path: Path) -> None:
        repo = tmp_path
        tests_dir = repo / "tests"
        tests_dir.mkdir()

        src = _write(repo / "mymod.py", "def my_func(): pass\ndef other(): pass\n")
        _write(tests_dir / "test_mymod.py", "def test_my_func(): pass\n")

        coverage, test_files = _analyse_file(src, repo, include_private=False, status_filter=None)
        assert len(test_files) == 1
        statuses = {c.name: c.status for c in coverage}
        assert statuses["my_func"] == "covered"
        assert statuses["other"] == "uncovered"

    def test_private_excluded_by_default(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "mod.py", "def _private(): pass\ndef public(): pass\n")
        coverage, _ = _analyse_file(src, tmp_path, include_private=False, status_filter=None)
        skipped = [c for c in coverage if c.status == "skipped"]
        assert any(c.name == "_private" for c in skipped)

    def test_private_included_when_requested(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "mod.py", "def _private(): pass\n")
        coverage, _ = _analyse_file(src, tmp_path, include_private=True, status_filter=None)
        assert any(c.name == "_private" and c.status == "uncovered" for c in coverage)

    def test_status_filter_uncovered(self, tmp_path: Path) -> None:
        repo = tmp_path
        tests_dir = repo / "tests"
        tests_dir.mkdir()
        src = _write(repo / "mod.py", "def covered_fn(): pass\ndef uncovered_fn(): pass\n")
        _write(tests_dir / "test_mod.py", "def test_covered_fn(): pass\n")

        coverage, _ = _analyse_file(src, repo, include_private=False, status_filter="uncovered")
        assert all(c.status == "uncovered" for c in coverage)
        assert any(c.name == "uncovered_fn" for c in coverage)


# ── Handler end-to-end ────────────────────────────────────────────────────────

class TestHandler:
    def test_text_output_single_file(self, tmp_path: Path) -> None:
        repo = tmp_path
        tests_dir = repo / "tests"
        tests_dir.mkdir()
        _write(repo / "mymod.py", "def alpha(): pass\ndef beta(): pass\n")
        _write(tests_dir / "test_mymod.py", "def test_alpha(): pass\n")

        result = _test_coverage_map(None, target=str(repo / "mymod.py"))
        assert "Test Coverage Map" in result
        assert "alpha" in result
        assert "beta" in result

    def test_json_output_single_file(self, tmp_path: Path) -> None:
        repo = tmp_path
        tests_dir = repo / "tests"
        tests_dir.mkdir()
        _write(repo / "mymod.py", "def foo(): pass\n")
        _write(tests_dir / "test_mymod.py", "def test_foo(): pass\n")

        result = _test_coverage_map(None, target=str(repo / "mymod.py"), format="json")
        data = json.loads(result)
        assert "functions" in data
        assert "stats" in data
        assert data["stats"]["covered"] >= 1

    def test_invalid_target(self, tmp_path: Path) -> None:
        result = _test_coverage_map(None, target="/nonexistent/path/foo.py")
        assert "Cannot resolve" in result or "❌" in result

    def test_directory_mode(self, tmp_path: Path) -> None:
        repo = tmp_path
        tests_dir = repo / "tests"
        tests_dir.mkdir()
        _write(repo / "pkg" / "a.py", "def func_a(): pass\n")
        _write(repo / "pkg" / "b.py", "def func_b(): pass\n")
        _write(tests_dir / "test_a.py", "def test_func_a(): pass\n")

        result = _test_coverage_map(None, target=str(repo / "pkg"))
        assert "Test Coverage Map" in result

    def test_get_tools_returns_entry(self) -> None:
        tools = get_tools()
        assert len(tools) == 1
        assert tools[0].name == "test_coverage_map"
