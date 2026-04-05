"""Tests for ouroboros/tools/todo_scanner.py"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from ouroboros.tools.todo_scanner import (
    _collect_py_files,
    _scan_file,
    _todo_scanner,
    _PRIORITY_ORDER,
    _TAG_PRIORITY,
    _ALL_TAGS,
    get_tools,
)
from ouroboros.tools.registry import ToolContext, ToolEntry


# ── helpers ───────────────────────────────────────────────────────────────────

def _ctx(tmp_path: Path) -> ToolContext:
    ctx = ToolContext.__new__(ToolContext)
    ctx.repo_dir = str(tmp_path)
    return ctx


def _write(tmp_path: Path, name: str, src: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(src), encoding="utf-8")
    return p


# ── tag detection ─────────────────────────────────────────────────────────────

def test_detect_todo_full_comment(tmp_path):
    _write(tmp_path, "a.py", """\
        x = 1
        # TODO: implement this
        y = 2
    """)
    findings = _scan_file(tmp_path / "a.py", "a.py", _ALL_TAGS, "low")
    assert len(findings) == 1
    assert findings[0].tag == "TODO"
    assert findings[0].line == 2
    assert "implement this" in findings[0].message


def test_detect_fixme(tmp_path):
    _write(tmp_path, "a.py", """\
        # FIXME: this is broken
        pass
    """)
    findings = _scan_file(tmp_path / "a.py", "a.py", _ALL_TAGS, "low")
    assert any(f.tag == "FIXME" for f in findings)


def test_detect_bug(tmp_path):
    _write(tmp_path, "a.py", "# BUG off-by-one error here\n")
    findings = _scan_file(tmp_path / "a.py", "a.py", _ALL_TAGS, "low")
    assert findings[0].tag == "BUG"
    assert findings[0].priority == "high"


def test_detect_hack(tmp_path):
    _write(tmp_path, "a.py", "# HACK: temporary workaround\n")
    findings = _scan_file(tmp_path / "a.py", "a.py", _ALL_TAGS, "low")
    assert findings[0].tag == "HACK"
    assert findings[0].priority == "medium"


def test_detect_xxx(tmp_path):
    _write(tmp_path, "a.py", "# XXX: this is dangerous\n")
    findings = _scan_file(tmp_path / "a.py", "a.py", _ALL_TAGS, "low")
    assert findings[0].tag == "XXX"
    assert findings[0].priority == "high"


def test_detect_optimize(tmp_path):
    _write(tmp_path, "a.py", "# OPTIMIZE: use a dict here\n")
    findings = _scan_file(tmp_path / "a.py", "a.py", _ALL_TAGS, "low")
    assert findings[0].tag == "OPTIMIZE"
    assert findings[0].priority == "low"


def test_detect_note(tmp_path):
    _write(tmp_path, "a.py", "# NOTE: important invariant\n")
    findings = _scan_file(tmp_path / "a.py", "a.py", _ALL_TAGS, "low")
    assert findings[0].tag == "NOTE"
    assert findings[0].priority == "low"


def test_detect_inline_comment(tmp_path):
    _write(tmp_path, "a.py", "result = x + 1  # TODO: handle overflow\n")
    findings = _scan_file(tmp_path / "a.py", "a.py", _ALL_TAGS, "low")
    assert len(findings) == 1
    assert findings[0].tag == "TODO"


def test_case_insensitive(tmp_path):
    _write(tmp_path, "a.py", "# todo: lowercase tag\n")
    findings = _scan_file(tmp_path / "a.py", "a.py", _ALL_TAGS, "low")
    assert findings[0].tag == "TODO"


def test_tag_with_author(tmp_path):
    _write(tmp_path, "a.py", "# TODO(alice): add validation\n")
    findings = _scan_file(tmp_path / "a.py", "a.py", _ALL_TAGS, "low")
    assert findings[0].tag == "TODO"
    assert "add validation" in findings[0].message


def test_tag_without_message(tmp_path):
    _write(tmp_path, "a.py", "# TODO\n")
    findings = _scan_file(tmp_path / "a.py", "a.py", _ALL_TAGS, "low")
    assert len(findings) == 1
    assert findings[0].tag == "TODO"


def test_no_tags_in_plain_code(tmp_path):
    _write(tmp_path, "a.py", """\
        def foo():
            x = 1
            return x
    """)
    findings = _scan_file(tmp_path / "a.py", "a.py", _ALL_TAGS, "low")
    assert findings == []


def test_multiple_tags_in_one_file(tmp_path):
    _write(tmp_path, "a.py", """\
        # TODO: add test
        x = 1  # FIXME: broken
        # HACK: workaround
    """)
    findings = _scan_file(tmp_path / "a.py", "a.py", _ALL_TAGS, "low")
    tags = {f.tag for f in findings}
    assert "TODO" in tags
    assert "FIXME" in tags
    assert "HACK" in tags
    assert len(findings) == 3


def test_line_numbers_correct(tmp_path):
    _write(tmp_path, "a.py", """\
        x = 1
        y = 2
        # TODO: line 3
        z = 3
    """)
    findings = _scan_file(tmp_path / "a.py", "a.py", _ALL_TAGS, "low")
    assert findings[0].line == 3


# ── filters ───────────────────────────────────────────────────────────────────

def test_filter_by_tag(tmp_path):
    _write(tmp_path, "a.py", """\
        # TODO: task
        # FIXME: broken
        # HACK: workaround
    """)
    findings = _scan_file(tmp_path / "a.py", "a.py", {"TODO"}, "low")
    assert all(f.tag == "TODO" for f in findings)
    assert len(findings) == 1


def test_filter_min_priority_medium(tmp_path):
    _write(tmp_path, "a.py", """\
        # NOTE: just a note
        # OPTIMIZE: speedup
        # TODO: improve
        # FIXME: broken
    """)
    findings = _scan_file(tmp_path / "a.py", "a.py", _ALL_TAGS, "medium")
    tags = {f.tag for f in findings}
    assert "NOTE" not in tags
    assert "OPTIMIZE" not in tags
    assert "TODO" in tags or "FIXME" in tags


def test_filter_min_priority_high(tmp_path):
    _write(tmp_path, "a.py", """\
        # NOTE: low priority
        # TODO: medium priority
        # FIXME: high priority
        # BUG: also high
    """)
    findings = _scan_file(tmp_path / "a.py", "a.py", _ALL_TAGS, "high")
    tags = {f.tag for f in findings}
    assert "NOTE" not in tags
    assert "TODO" not in tags
    assert "FIXME" in tags
    assert "BUG" in tags


# ── todo_scanner() tool ───────────────────────────────────────────────────────

def test_tool_text_output(tmp_path):
    _write(tmp_path, "b.py", "# TODO: do something\n")
    result = _todo_scanner(_ctx(tmp_path))
    assert "TODO Scanner" in result
    assert "TODO" in result
    assert "do something" in result


def test_tool_json_output(tmp_path):
    _write(tmp_path, "b.py", "# FIXME: broken\n")
    result = _todo_scanner(_ctx(tmp_path), format="json")
    data = json.loads(result)
    assert data["total_findings"] == 1
    assert data["findings"][0]["tag"] == "FIXME"


def test_tool_no_findings(tmp_path):
    _write(tmp_path, "c.py", "x = 1\n")
    result = _todo_scanner(_ctx(tmp_path))
    assert "0 annotation" in result
    assert "✅" in result


def test_tool_tags_filter(tmp_path):
    _write(tmp_path, "d.py", "# TODO: ok\n# FIXME: bad\n")
    result = _todo_scanner(_ctx(tmp_path), tags="FIXME")
    assert "FIXME" in result
    # TODO should not appear in findings section
    data = json.loads(_todo_scanner(_ctx(tmp_path), tags="FIXME", format="json"))
    assert all(f["tag"] == "FIXME" for f in data["findings"])


def test_tool_min_priority_filter(tmp_path):
    _write(tmp_path, "e.py", "# NOTE: low\n# FIXME: high\n")
    data = json.loads(_todo_scanner(_ctx(tmp_path), min_priority="high", format="json"))
    tags = {f["tag"] for f in data["findings"]}
    assert "NOTE" not in tags
    assert "FIXME" in tags


def test_tool_invalid_min_priority(tmp_path):
    result = _todo_scanner(_ctx(tmp_path), min_priority="critical")
    assert "Unknown min_priority" in result


def test_tool_invalid_tags(tmp_path):
    result = _todo_scanner(_ctx(tmp_path), tags="NOTEXIST")
    assert "Unknown tags" in result


def test_tool_path_filter(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    _write(sub, "f.py", "# TODO: in sub\n")
    _write(tmp_path, "g.py", "# FIXME: in root\n")
    data = json.loads(_todo_scanner(_ctx(tmp_path), path="sub", format="json"))
    assert all("sub" in f["file"] for f in data["findings"])
    assert len(data["findings"]) == 1


def test_tool_json_structure(tmp_path):
    _write(tmp_path, "h.py", "# TODO: something\n# BUG: broken\n")
    data = json.loads(_todo_scanner(_ctx(tmp_path), format="json"))
    assert "total_files" in data
    assert "total_findings" in data
    assert "by_tag" in data
    assert "findings" in data
    assert data["total_findings"] == 2


def test_tool_by_tag_summary(tmp_path):
    _write(tmp_path, "i.py", "# TODO: a\n# TODO: b\n# FIXME: c\n")
    data = json.loads(_todo_scanner(_ctx(tmp_path), format="json"))
    assert data["by_tag"]["TODO"] == 2
    assert data["by_tag"]["FIXME"] == 1


# ── collect_py_files ──────────────────────────────────────────────────────────

def test_collect_skips_pycache(tmp_path):
    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    (pycache / "x.py").write_text("# TODO: should be skipped\n")
    _write(tmp_path, "real.py", "x = 1\n")
    files = _collect_py_files(tmp_path)
    assert not any("__pycache__" in str(f) for f in files)


def test_collect_single_file(tmp_path):
    p = _write(tmp_path, "single.py", "x = 1\n")
    files = _collect_py_files(tmp_path, "single.py")
    assert files == [p]


# ── get_tools ─────────────────────────────────────────────────────────────────

def test_get_tools_returns_one_entry():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "todo_scanner"


def test_get_tools_schema_valid():
    tool = get_tools()[0]
    schema = tool.schema
    assert schema["name"] == "todo_scanner"
    assert "description" in schema
    params = schema["parameters"]["properties"]
    assert "path" in params
    assert "tags" in params
    assert "min_priority" in params
    assert "format" in params


def test_get_tools_handler_callable():
    tool = get_tools()[0]
    assert callable(tool.handler)


# ── priority invariants ───────────────────────────────────────────────────────

def test_all_tags_have_priority():
    for tag in _ALL_TAGS:
        assert tag in _TAG_PRIORITY, f"Missing priority for {tag}"
        assert _TAG_PRIORITY[tag] in _PRIORITY_ORDER


def test_priority_order_consistent():
    assert _PRIORITY_ORDER["low"] < _PRIORITY_ORDER["medium"]
    assert _PRIORITY_ORDER["medium"] < _PRIORITY_ORDER["high"]
