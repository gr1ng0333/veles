"""Tests for security_scan tool."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ouroboros.tools.security_scan import (
    _CATEGORY_SEVERITY,
    _ALL_CATEGORIES,
    _collect_py_files,
    _scan_file,
    _scan_ast,
    _scan_regex,
    _security_scan,
    get_tools,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ctx(tmp_path: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.repo_dir = str(tmp_path)
    return ctx


def _write(tmp_path: Path, name: str, src: str) -> Path:
    f = tmp_path / name
    f.write_text(textwrap.dedent(src), encoding="utf-8")
    return f


# ── Category / severity metadata ──────────────────────────────────────────────

def test_all_categories_have_severity() -> None:
    for cat in _ALL_CATEGORIES:
        assert cat in _CATEGORY_SEVERITY, f"missing severity for {cat}"


def test_severities_are_valid() -> None:
    valid = {"low", "medium", "high"}
    for cat, sev in _CATEGORY_SEVERITY.items():
        assert sev in valid, f"invalid severity {sev!r} for {cat}"


def test_category_count() -> None:
    # We expect exactly 10 categories
    assert len(_ALL_CATEGORIES) == 10


# ── get_tools ─────────────────────────────────────────────────────────────────

def test_get_tools_returns_one_entry() -> None:
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "security_scan"


def test_get_tools_handler_callable() -> None:
    tools = get_tools()
    assert callable(tools[0].handler)


def test_get_tools_schema_has_required_fields() -> None:
    schema = get_tools()[0].schema
    assert schema["name"] == "security_scan"
    assert "description" in schema
    assert "parameters" in schema


# ── _collect_py_files ─────────────────────────────────────────────────────────

def test_collect_py_files_basic(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1")
    (tmp_path / "b.txt").write_text("nope")
    files = _collect_py_files(tmp_path, None, False)
    names = {f.name for f in files}
    assert "a.py" in names
    assert "b.txt" not in names


def test_collect_py_files_skip_tests(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text("x = 1")
    (tmp_path / "main.py").write_text("y = 2")

    without_skip = _collect_py_files(tmp_path, None, False)
    with_skip = _collect_py_files(tmp_path, None, True)

    assert any("main.py" in str(f) for f in with_skip)
    assert not any("test_x.py" in str(f) for f in with_skip)
    assert any("test_x.py" in str(f) for f in without_skip)


def test_collect_py_files_single_file(tmp_path: Path) -> None:
    f = tmp_path / "single.py"
    f.write_text("pass")
    files = _collect_py_files(tmp_path, "single.py", False)
    assert len(files) == 1
    assert files[0].name == "single.py"


def test_collect_py_files_subpath(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "inner.py").write_text("pass")
    (tmp_path / "outer.py").write_text("pass")

    files = _collect_py_files(tmp_path, "sub", False)
    names = {f.name for f in files}
    assert "inner.py" in names
    assert "outer.py" not in names


# ── eval_exec detections ──────────────────────────────────────────────────────

def test_detects_dynamic_eval(tmp_path: Path) -> None:
    f = _write(tmp_path, "ev.py", """
        user_input = input()
        eval(user_input)
    """)
    findings = _scan_file(f, "ev.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "eval_exec" in cats


def test_does_not_flag_literal_eval(tmp_path: Path) -> None:
    f = _write(tmp_path, "ev2.py", """
        result = eval("1 + 1")
    """)
    findings = _scan_file(f, "ev2.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "eval_exec" not in cats


def test_detects_exec_dynamic(tmp_path: Path) -> None:
    f = _write(tmp_path, "ex.py", """
        code = get_code()
        exec(code)
    """)
    findings = _scan_file(f, "ex.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "eval_exec" in cats


# ── hardcoded_secrets ─────────────────────────────────────────────────────────

def test_detects_hardcoded_password(tmp_path: Path) -> None:
    f = _write(tmp_path, "sec.py", """
        password = "mysecretpassword123"
    """)
    findings = _scan_file(f, "sec.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "hardcoded_secrets" in cats


def test_detects_hardcoded_token(tmp_path: Path) -> None:
    f = _write(tmp_path, "tok.py", """
        api_token = "sk-abc123xyz789"
    """)
    findings = _scan_file(f, "tok.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "hardcoded_secrets" in cats


def test_ignores_env_var_secret(tmp_path: Path) -> None:
    f = _write(tmp_path, "env.py", """
        password = os.environ.get("PASSWORD")
    """)
    findings = _scan_file(f, "env.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "hardcoded_secrets" not in cats


# ── shell_injection ────────────────────────────────────────────────────────────

def test_detects_subprocess_shell_true(tmp_path: Path) -> None:
    f = _write(tmp_path, "sh.py", """
        import subprocess
        subprocess.run(cmd, shell=True)
    """)
    findings = _scan_file(f, "sh.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "shell_injection" in cats


def test_does_not_flag_subprocess_shell_false(tmp_path: Path) -> None:
    f = _write(tmp_path, "sh2.py", """
        import subprocess
        subprocess.run(["ls", "-la"], shell=False)
    """)
    findings = _scan_file(f, "sh2.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "shell_injection" not in cats


def test_detects_os_system(tmp_path: Path) -> None:
    f = _write(tmp_path, "ossys.py", """
        import os
        os.system("rm -rf /tmp/test")
    """)
    findings = _scan_file(f, "ossys.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "shell_injection" in cats


# ── deserialization ────────────────────────────────────────────────────────────

def test_detects_pickle_loads(tmp_path: Path) -> None:
    f = _write(tmp_path, "pkl.py", """
        import pickle
        obj = pickle.loads(data)
    """)
    findings = _scan_file(f, "pkl.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "deserialization" in cats


def test_detects_marshal_loads(tmp_path: Path) -> None:
    f = _write(tmp_path, "mar.py", """
        import marshal
        obj = marshal.loads(raw)
    """)
    findings = _scan_file(f, "mar.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "deserialization" in cats


# ── sql_injection ──────────────────────────────────────────────────────────────

def test_detects_sql_fstring(tmp_path: Path) -> None:
    f = _write(tmp_path, "sql.py", """
        cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")
    """)
    findings = _scan_file(f, "sql.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "sql_injection" in cats


def test_detects_sql_percent_format(tmp_path: Path) -> None:
    f = _write(tmp_path, "sql2.py", """
        cursor.execute("SELECT * FROM t WHERE x = %s" % value)
    """)
    findings = _scan_file(f, "sql2.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "sql_injection" in cats


def test_safe_parameterized_query(tmp_path: Path) -> None:
    f = _write(tmp_path, "sql3.py", """
        cursor.execute("SELECT * FROM t WHERE x = ?", (value,))
    """)
    findings = _scan_file(f, "sql3.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "sql_injection" not in cats


# ── weak_crypto ────────────────────────────────────────────────────────────────

def test_detects_md5(tmp_path: Path) -> None:
    f = _write(tmp_path, "cry.py", """
        import hashlib
        h = hashlib.md5(data)
    """)
    findings = _scan_file(f, "cry.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "weak_crypto" in cats


def test_detects_sha1(tmp_path: Path) -> None:
    f = _write(tmp_path, "cry2.py", """
        import hashlib
        h = hashlib.sha1(data)
    """)
    findings = _scan_file(f, "cry2.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "weak_crypto" in cats


# ── yaml_unsafe ────────────────────────────────────────────────────────────────

def test_detects_yaml_load_no_loader(tmp_path: Path) -> None:
    f = _write(tmp_path, "yml.py", """
        import yaml
        data = yaml.load(stream)
    """)
    findings = _scan_file(f, "yml.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "yaml_unsafe" in cats


def test_yaml_safe_load_ok(tmp_path: Path) -> None:
    f = _write(tmp_path, "yml2.py", """
        import yaml
        data = yaml.safe_load(stream)
    """)
    findings = _scan_file(f, "yml2.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "yaml_unsafe" not in cats


# ── xml_unsafe ─────────────────────────────────────────────────────────────────

def test_detects_xml_etree(tmp_path: Path) -> None:
    f = _write(tmp_path, "xml1.py", """
        from xml.etree import ElementTree as ET
        tree = ET.parse(source)
    """)
    findings = _scan_file(f, "xml1.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "xml_unsafe" in cats


# ── debug_code ─────────────────────────────────────────────────────────────────

def test_detects_breakpoint(tmp_path: Path) -> None:
    f = _write(tmp_path, "dbg.py", """
        def foo():
            breakpoint()
            return 42
    """)
    findings = _scan_file(f, "dbg.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "debug_code" in cats


def test_detects_pdb_set_trace(tmp_path: Path) -> None:
    f = _write(tmp_path, "dbg2.py", """
        import pdb
        pdb.set_trace()
    """)
    findings = _scan_file(f, "dbg2.py", _ALL_CATEGORIES, 0)
    cats = [x.category for x in findings]
    assert "debug_code" in cats


# ── min_severity filter ────────────────────────────────────────────────────────

def test_min_severity_high_filters_medium(tmp_path: Path) -> None:
    f = _write(tmp_path, "filt.py", """
        import hashlib
        h = hashlib.md5(data)   # weak_crypto = medium
        os.system("ls")          # shell_injection = high
    """)
    findings = _scan_file(f, "filt.py", _ALL_CATEGORIES, min_level=2)  # high only
    cats = {x.category for x in findings}
    assert "shell_injection" in cats
    assert "weak_crypto" not in cats


def test_min_severity_low_shows_all(tmp_path: Path) -> None:
    f = _write(tmp_path, "filtlow.py", """
        import hashlib
        h = hashlib.md5(data)
        breakpoint()
    """)
    findings = _scan_file(f, "filtlow.py", _ALL_CATEGORIES, min_level=0)
    cats = {x.category for x in findings}
    assert "weak_crypto" in cats
    assert "debug_code" in cats


# ── category filter ────────────────────────────────────────────────────────────

def test_category_filter_only_eval_exec(tmp_path: Path) -> None:
    f = _write(tmp_path, "catfilt.py", """
        import hashlib
        h = hashlib.md5(data)
        eval(user_input)
    """)
    findings = _scan_file(f, "catfilt.py", {"eval_exec"}, min_level=0)
    cats = {x.category for x in findings}
    assert "eval_exec" in cats
    assert "weak_crypto" not in cats


# ── _security_scan integration ────────────────────────────────────────────────

def test_security_scan_text_output(tmp_path: Path) -> None:
    _write(tmp_path, "main.py", """
        password = "hunter2"
    """)
    ctx = _ctx(tmp_path)
    result = _security_scan(ctx)
    assert "Security Scan" in result
    assert "hardcoded_secrets" in result


def test_security_scan_json_output(tmp_path: Path) -> None:
    _write(tmp_path, "main.py", """
        password = "secretvalue"
    """)
    ctx = _ctx(tmp_path)
    result = _security_scan(ctx, format="json")
    data = json.loads(result)
    assert "findings" in data
    assert "by_category" in data
    assert "total_files" in data


def test_security_scan_no_findings(tmp_path: Path) -> None:
    _write(tmp_path, "clean.py", """
        x = 1 + 2
        print(x)
    """)
    ctx = _ctx(tmp_path)
    result = _security_scan(ctx)
    assert "No security issues" in result


def test_security_scan_invalid_min_severity(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = _security_scan(ctx, min_severity="critical")
    assert "Unknown min_severity" in result


def test_security_scan_invalid_category(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = _security_scan(ctx, categories="nonexistent_category")
    assert "Unknown categories" in result


def test_security_scan_skip_tests_flag(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    _write(tests_dir, "test_bad.py", """
        password = "shouldbeignored"
    """)
    _write(tmp_path, "prod.py", """
        password = "shouldbedetected"
    """)
    ctx = _ctx(tmp_path)

    with_skip = _security_scan(ctx, skip_tests=True)
    without_skip = _security_scan(ctx, skip_tests=False)

    # With skip: only prod.py scanned → 1 finding
    data_with = json.loads(_security_scan(ctx, skip_tests=True, format="json"))
    data_without = json.loads(_security_scan(ctx, skip_tests=False, format="json"))
    assert data_without["total_findings"] > data_with["total_findings"]
