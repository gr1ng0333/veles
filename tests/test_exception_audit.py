"""Tests for ouroboros/tools/exception_audit.py"""

from __future__ import annotations

import ast
import json
import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("REPO_DIR", "/opt/veles")

from ouroboros.tools.exception_audit import (
    _ALL_PATTERNS,
    _BROAD_BASES,
    _SEVERITY,
    _SEVERITY_ORDER,
    _collect_py_files,
    _body_is_silent,
    _body_has_reraise,
    _body_has_reraise_with_cause,
    _has_new_raise,
    _is_bare,
    _is_broad,
    _scan_file,
    _exception_audit,
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


def _parse_handler(src: str) -> ast.ExceptHandler:
    """Parse 'try: pass\nexcept ...: ...' and return the handler."""
    tree = ast.parse(textwrap.dedent(src))
    try_node = tree.body[0]
    assert isinstance(try_node, ast.Try)
    return try_node.handlers[0]


# ── Constant / config tests ────────────────────────────────────────────────────

def test_all_patterns_defined():
    assert "bare_except" in _ALL_PATTERNS
    assert "broad_except" in _ALL_PATTERNS
    assert "silent_except" in _ALL_PATTERNS
    assert "reraise_as_new" in _ALL_PATTERNS
    assert "string_exception" in _ALL_PATTERNS
    assert "overly_nested" in _ALL_PATTERNS


def test_severity_defined_for_all_patterns():
    for pat in _ALL_PATTERNS:
        assert pat in _SEVERITY, f"no severity for {pat}"
        assert _SEVERITY[pat] in ("low", "medium", "high")


def test_severity_order():
    assert _SEVERITY_ORDER["low"] < _SEVERITY_ORDER["medium"]
    assert _SEVERITY_ORDER["medium"] < _SEVERITY_ORDER["high"]


# ── _is_bare ──────────────────────────────────────────────────────────────────

def test_is_bare_true():
    handler = _parse_handler("try:\n    pass\nexcept:\n    pass")
    assert _is_bare(handler)


def test_is_bare_false_for_typed():
    handler = _parse_handler("try:\n    pass\nexcept ValueError:\n    pass")
    assert not _is_bare(handler)


# ── _is_broad ─────────────────────────────────────────────────────────────────

def test_is_broad_exception():
    handler = _parse_handler("try:\n    pass\nexcept Exception:\n    pass")
    assert _is_broad(handler)


def test_is_broad_base_exception():
    handler = _parse_handler("try:\n    pass\nexcept BaseException:\n    pass")
    assert _is_broad(handler)


def test_is_broad_false_for_value_error():
    handler = _parse_handler("try:\n    pass\nexcept ValueError:\n    pass")
    assert not _is_broad(handler)


# ── _body_is_silent ───────────────────────────────────────────────────────────

def test_body_is_silent_pass():
    handler = _parse_handler("try:\n    pass\nexcept Exception:\n    pass")
    assert _body_is_silent(handler.body)


def test_body_is_silent_dummy_assign():
    handler = _parse_handler("try:\n    pass\nexcept Exception as e:\n    _ = str(e)")
    assert _body_is_silent(handler.body)


def test_body_not_silent_with_log():
    handler = _parse_handler(
        "try:\n    pass\nexcept Exception as e:\n    print(e)"
    )
    assert not _body_is_silent(handler.body)


# ── _body_has_reraise ─────────────────────────────────────────────────────────

def test_body_has_reraise():
    handler = _parse_handler(
        "try:\n    pass\nexcept Exception:\n    raise"
    )
    assert _body_has_reraise(handler.body)


def test_body_no_reraise():
    handler = _parse_handler(
        "try:\n    pass\nexcept Exception:\n    pass"
    )
    assert not _body_has_reraise(handler.body)


# ── _body_has_reraise_with_cause ──────────────────────────────────────────────

def test_reraise_with_cause():
    handler = _parse_handler(
        "try:\n    pass\nexcept Exception as e:\n    raise RuntimeError('x') from e"
    )
    assert _body_has_reraise_with_cause(handler.body)


def test_reraise_without_cause():
    handler = _parse_handler(
        "try:\n    pass\nexcept Exception as e:\n    raise RuntimeError('x')"
    )
    assert not _body_has_reraise_with_cause(handler.body)


# ── _has_new_raise ────────────────────────────────────────────────────────────

def test_has_new_raise_returns_line():
    handler = _parse_handler(
        "try:\n    pass\nexcept Exception:\n    raise RuntimeError('x')"
    )
    result = _has_new_raise(handler.body)
    assert result is not None


def test_has_new_raise_none_for_bare_raise():
    handler = _parse_handler(
        "try:\n    pass\nexcept Exception:\n    raise"
    )
    assert _has_new_raise(handler.body) is None


def test_has_new_raise_none_with_cause():
    handler = _parse_handler(
        "try:\n    pass\nexcept Exception as e:\n    raise RuntimeError('x') from e"
    )
    assert _has_new_raise(handler.body) is None


# ── _scan_file ────────────────────────────────────────────────────────────────

def test_scan_bare_except(tmp_path):
    src = """\
        try:
            pass
        except:
            pass
    """
    f = _write(tmp_path, "a.py", src)
    findings = _scan_file(f, "a.py", _ALL_PATTERNS, 3)
    patterns = {fi.pattern for fi in findings}
    assert "bare_except" in patterns


def test_scan_silent_except(tmp_path):
    src = """\
        try:
            x = 1
        except ValueError:
            pass
    """
    f = _write(tmp_path, "b.py", src)
    findings = _scan_file(f, "b.py", _ALL_PATTERNS, 3)
    patterns = {fi.pattern for fi in findings}
    assert "silent_except" in patterns


def test_scan_broad_except_without_reraise(tmp_path):
    src = """\
        try:
            x = 1
        except Exception:
            print("oops")
    """
    f = _write(tmp_path, "c.py", src)
    findings = _scan_file(f, "c.py", _ALL_PATTERNS, 3)
    patterns = {fi.pattern for fi in findings}
    assert "broad_except" in patterns


def test_scan_broad_except_with_reraise_not_flagged(tmp_path):
    src = """\
        try:
            x = 1
        except Exception:
            raise
    """
    f = _write(tmp_path, "d.py", src)
    findings = _scan_file(f, "d.py", _ALL_PATTERNS, 3)
    patterns = {fi.pattern for fi in findings}
    assert "broad_except" not in patterns


def test_scan_reraise_as_new(tmp_path):
    src = """\
        try:
            x = 1
        except ValueError as e:
            raise RuntimeError("wrapped")
    """
    f = _write(tmp_path, "e.py", src)
    findings = _scan_file(f, "e.py", _ALL_PATTERNS, 3)
    patterns = {fi.pattern for fi in findings}
    assert "reraise_as_new" in patterns


def test_scan_reraise_with_cause_not_flagged(tmp_path):
    src = """\
        try:
            x = 1
        except ValueError as e:
            raise RuntimeError("wrapped") from e
    """
    f = _write(tmp_path, "f.py", src)
    findings = _scan_file(f, "f.py", _ALL_PATTERNS, 3)
    patterns = {fi.pattern for fi in findings}
    assert "reraise_as_new" not in patterns


def test_scan_overly_nested(tmp_path):
    src = """\
        try:
            try:
                try:
                    try:
                        pass
                    except:
                        pass
                except:
                    pass
            except:
                pass
        except:
            pass
    """
    f = _write(tmp_path, "g.py", src)
    findings = _scan_file(f, "g.py", _ALL_PATTERNS, 3)
    patterns = {fi.pattern for fi in findings}
    assert "overly_nested" in patterns


def test_scan_clean_file(tmp_path):
    src = """\
        try:
            x = 1
        except ValueError as e:
            raise RuntimeError("wrapped") from e
    """
    f = _write(tmp_path, "h.py", src)
    findings = _scan_file(f, "h.py", _ALL_PATTERNS, 3)
    # Only reraise_as_new — but with cause it's clean, just silent? No:
    # the body has raise so not silent; no broad; no bare; no string; no nested
    patterns = {fi.pattern for fi in findings}
    assert "bare_except" not in patterns
    assert "silent_except" not in patterns
    assert "broad_except" not in patterns


# ── _exception_audit (integration) ────────────────────────────────────────────

def test_audit_text_output(tmp_path):
    src = """\
        try:
            pass
        except:
            pass
    """
    _write(tmp_path, "z.py", src)
    ctx = _ctx(tmp_path)
    result = _exception_audit(ctx)
    assert "bare_except" in result.lower() or "BARE EXCEPT" in result


def test_audit_json_output(tmp_path):
    src = """\
        try:
            pass
        except:
            pass
    """
    _write(tmp_path, "z.py", src)
    ctx = _ctx(tmp_path)
    result = _exception_audit(ctx, format="json")
    data = json.loads(result)
    assert "findings" in data
    assert data["total_findings"] >= 1


def test_audit_min_severity_filters(tmp_path):
    src = """\
        try:
            try:
                try:
                    try:
                        pass
                    except ValueError:
                        pass
                except ValueError:
                    pass
            except ValueError:
                pass
        except ValueError:
            pass
    """
    _write(tmp_path, "nested.py", src)
    ctx = _ctx(tmp_path)
    # overly_nested is low severity; min_severity=high should filter it out
    result_high = _exception_audit(ctx, min_severity="high")
    # No high findings from this code
    assert "OVERLY NESTED" not in result_high


def test_audit_pattern_filter(tmp_path):
    src = """\
        try:
            pass
        except:
            pass
    """
    _write(tmp_path, "z.py", src)
    ctx = _ctx(tmp_path)
    result = _exception_audit(ctx, patterns="broad_except")
    # bare_except should not appear when only broad_except requested
    assert "bare_except" not in result.lower()


def test_audit_invalid_min_severity(tmp_path):
    ctx = _ctx(tmp_path)
    result = _exception_audit(ctx, min_severity="extreme")
    assert "Unknown min_severity" in result


def test_audit_invalid_pattern(tmp_path):
    ctx = _ctx(tmp_path)
    result = _exception_audit(ctx, patterns="nonexistent_pattern")
    assert "Unknown patterns" in result


def test_audit_no_findings(tmp_path):
    src = """\
        def foo():
            return 1
    """
    _write(tmp_path, "clean.py", src)
    ctx = _ctx(tmp_path)
    result = _exception_audit(ctx)
    assert "No exception handling anti-patterns found" in result


def test_audit_path_filter(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    src_bad = """\
        try:
            pass
        except:
            pass
    """
    src_clean = "x = 1\n"
    _write(tmp_path, "bad.py", src_bad)
    _write(tmp_path, "sub/clean.py", src_clean)
    ctx = _ctx(tmp_path)
    result = _exception_audit(ctx, path="sub")
    assert "No exception handling anti-patterns found" in result


# ── get_tools ─────────────────────────────────────────────────────────────────

def test_get_tools_returns_one():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "exception_audit"


def test_get_tools_handler_callable():
    tools = get_tools()
    assert callable(tools[0].handler)


def test_get_tools_schema_valid():
    tools = get_tools()
    schema = tools[0].schema
    assert schema["name"] == "exception_audit"
    assert "parameters" in schema
    props = schema["parameters"]["properties"]
    assert "path" in props
    assert "patterns" in props
    assert "min_severity" in props
    assert "format" in props
    assert "max_nest_depth" in props
