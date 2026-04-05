"""Tests for ouroboros/tools/dead_code.py"""

from __future__ import annotations

import ast
import json
import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("REPO_DIR", "/opt/veles")

from ouroboros.tools.dead_code import (
    _ALL_CATEGORIES,
    _CATEGORY_DESCRIPTIONS,
    _collect_externally_used_privates,
    _collect_py_files,
    _dead_code,
    _load_names,
    _scan_dead_privates,
    _scan_unused_imports,
    _type_checking_names,
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
    p.write_text(textwrap.dedent(src), encoding="utf-8")
    return p


def _parse(src: str) -> ast.Module:
    return ast.parse(textwrap.dedent(src))


# ── Unit: _load_names ─────────────────────────────────────────────────────────

def test_load_names_simple():
    tree = _parse("x = os.path.join('a', 'b')")
    names = _load_names(tree)
    assert "os" in names


def test_load_names_attribute_chain():
    tree = _parse("result = a.b.c.d()")
    names = _load_names(tree)
    assert "a" in names


def test_load_names_call():
    tree = _parse("foo()\nbar(x)")
    names = _load_names(tree)
    assert "foo" in names
    assert "bar" in names
    assert "x" in names


def test_load_names_excludes_store():
    tree = _parse("myvar = 1\nmyvar2 = 2")
    names = _load_names(tree)
    assert "myvar" not in names
    assert "myvar2" not in names


# ── Unit: _type_checking_names ────────────────────────────────────────────────

def test_type_checking_names_basic():
    src = """\
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            from os import path
    """
    tree = _parse(src)
    tc = _type_checking_names(tree)
    assert "path" in tc


def test_type_checking_names_empty_outside_block():
    src = "import os\nos.getcwd()\n"
    tree = _parse(src)
    tc = _type_checking_names(tree)
    assert "os" not in tc


# ── Unit: _scan_unused_imports ────────────────────────────────────────────────

def test_unused_import_simple(tmp_path):
    src = "import os\nx = 1\n"
    tree = _parse(src)
    results = _scan_unused_imports("test.py", tree)
    assert len(results) == 1
    assert results[0]["name"] == "os"
    assert results[0]["line"] == 1


def test_used_import_not_flagged(tmp_path):
    src = "import os\nprint(os.getcwd())\n"
    tree = _parse(src)
    results = _scan_unused_imports("test.py", tree)
    assert results == []


def test_from_import_unused(tmp_path):
    src = "from pathlib import Path\nx = 1\n"
    tree = _parse(src)
    results = _scan_unused_imports("test.py", tree)
    assert len(results) == 1
    assert results[0]["name"] == "Path"


def test_from_import_used(tmp_path):
    src = "from pathlib import Path\np = Path('.')\n"
    tree = _parse(src)
    results = _scan_unused_imports("test.py", tree)
    assert results == []


def test_star_import_skipped(tmp_path):
    src = "from os import *\nx = 1\n"
    tree = _parse(src)
    results = _scan_unused_imports("test.py", tree)
    assert results == []


def test_aliased_import(tmp_path):
    src = "import numpy as np\nx = 1\n"
    tree = _parse(src)
    results = _scan_unused_imports("test.py", tree)
    assert len(results) == 1
    assert results[0]["name"] == "np"
    assert "as np" in results[0]["stmt"]


def test_type_checking_import_excluded(tmp_path):
    src = """\
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            from pathlib import Path
        x: 'Path' = None
    """
    tree = _parse(src)
    results = _scan_unused_imports("test.py", tree)
    # Path should not be reported as unused (it's under TYPE_CHECKING)
    names = [r["name"] for r in results]
    assert "Path" not in names


def test_all_export_skips_unused_report(tmp_path):
    src = "from pathlib import Path\n__all__ = ['Path']\n"
    tree = _parse(src)
    results = _scan_unused_imports("test.py", tree)
    assert all(r["name"] != "Path" for r in results)


# ── Unit: _scan_dead_privates ─────────────────────────────────────────────────

def test_dead_private_function(tmp_path):
    src = "def _helper():\n    pass\n"
    tree = _parse(src)
    results = _scan_dead_privates("test.py", tree, externally_used=set())
    assert len(results) == 1
    assert results[0]["name"] == "_helper"
    assert results[0]["kind"] == "function"


def test_used_private_not_flagged(tmp_path):
    src = "def _helper():\n    pass\n\nresult = _helper()\n"
    tree = _parse(src)
    results = _scan_dead_privates("test.py", tree, externally_used=set())
    assert results == []


def test_externally_imported_private_not_flagged(tmp_path):
    src = "def _helper():\n    pass\n"
    tree = _parse(src)
    # Simulate another file doing: from test import _helper
    results = _scan_dead_privates("test.py", tree, externally_used={"_helper"})
    assert results == []


def test_dunder_not_flagged(tmp_path):
    src = "def __hidden():\n    pass\n"
    tree = _parse(src)
    results = _scan_dead_privates("test.py", tree, externally_used=set())
    assert results == []


def test_dead_private_class(tmp_path):
    src = "class _Internal:\n    pass\n"
    tree = _parse(src)
    results = _scan_dead_privates("test.py", tree, externally_used=set())
    assert len(results) == 1
    assert results[0]["kind"] == "class"
    assert results[0]["name"] == "_Internal"


def test_private_method_not_top_level_excluded(tmp_path):
    # Private methods inside a class should NOT be reported by dead_privates
    # (only top-level defs are targeted)
    src = "class Foo:\n    def _priv(self):\n        pass\n"
    tree = _parse(src)
    results = _scan_dead_privates("test.py", tree, externally_used=set())
    assert results == []


# ── Unit: _collect_externally_used_privates ───────────────────────────────────

def test_collect_externally_used_privates(tmp_path):
    _write(tmp_path, "consumer.py", "from helper import _util\n")
    _write(tmp_path, "other.py", "x = 1\n")
    result = _collect_externally_used_privates(_collect_py_files(tmp_path))
    assert "_util" in result


def test_collect_externally_empty(tmp_path):
    _write(tmp_path, "a.py", "import os\n")
    result = _collect_externally_used_privates(_collect_py_files(tmp_path))
    assert not any(n.startswith("_") for n in result)


# ── Integration: _dead_code ───────────────────────────────────────────────────

def test_dead_code_text_output(tmp_path):
    _write(tmp_path, "a.py", "import sys\nx = 1\n")
    output = _dead_code(_ctx(tmp_path), format="text")
    assert "Dead Code Report" in output
    assert "sys" in output


def test_dead_code_json_output(tmp_path):
    _write(tmp_path, "a.py", "import sys\nx = 1\n")
    output = _dead_code(_ctx(tmp_path), format="json")
    data = json.loads(output)
    assert "dead" in data
    assert "total_files" in data
    assert "summary" in data


def test_dead_code_category_filter(tmp_path):
    _write(tmp_path, "a.py", "import sys\nx = 1\n")
    output = _dead_code(_ctx(tmp_path), category="unused_imports", format="json")
    data = json.loads(output)
    # dead_privates should be empty (filtered out)
    assert data["dead"]["dead_privates"] == []
    # unused_imports should have sys
    assert any(r["name"] == "sys" for r in data["dead"]["unused_imports"])


def test_dead_code_invalid_category(tmp_path):
    output = _dead_code(_ctx(tmp_path), category="garbage")
    assert "Unknown category" in output


def test_dead_code_no_dead_code(tmp_path):
    _write(tmp_path, "clean.py", "import os\nprint(os.getcwd())\n")
    output = _dead_code(_ctx(tmp_path), format="text")
    assert "No dead code found" in output or "0 items" in output or "Dead Code Report" in output


def test_dead_code_path_filter(tmp_path):
    subdir = tmp_path / "sub"
    subdir.mkdir()
    (subdir / "module.py").write_text("import json\nx = 1\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("import os\nos.getcwd()\n", encoding="utf-8")
    output = _dead_code(_ctx(tmp_path), path="sub", format="json")
    data = json.loads(output)
    files = [r["file"] for r in data["dead"]["unused_imports"]]
    assert any("module.py" in f for f in files)
    assert not any("other.py" in f for f in files)


# ── Tool registration ─────────────────────────────────────────────────────────

def test_get_tools_returns_one():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "dead_code"


def test_tool_schema_fields():
    schema = get_tools()[0].schema
    assert schema["name"] == "dead_code"
    assert "description" in schema
    props = schema["parameters"]["properties"]
    assert "path" in props
    assert "category" in props
    assert "format" in props
    assert "min_per_file" in props


def test_all_categories_have_descriptions():
    for cat in _ALL_CATEGORIES:
        assert cat in _CATEGORY_DESCRIPTIONS
