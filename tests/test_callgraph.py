"""Tests for callgraph tool.

Covers:
- AST call-name extraction
- File collection
- Graph builder: top-level funcs, class methods, call edges
- BFS traversal (callers / callees / both)
- Function key resolution (bare name, qualified, partial)
- Handler: focused mode (text + json)
- Handler: overview mode (text + json)
- Edge cases: missing function, empty file, syntax error, depth limits
"""
from __future__ import annotations

import json
import pathlib
import textwrap
import ast

import pytest

from ouroboros.tools.callgraph import (
    CallGraph,
    FuncNode,
    _bfs_direction,
    _build_callgraph,
    _call_name,
    _callgraph,
    _collect_py_files,
    _rel,
    _resolve_function_keys,
)
from ouroboros.tools.registry import ToolContext


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path


@pytest.fixture()
def ctx(tmp_repo: pathlib.Path) -> ToolContext:
    (tmp_repo / "drive").mkdir()
    return ToolContext(repo_dir=tmp_repo, drive_root=tmp_repo / "drive")


def _write(root: pathlib.Path, rel: str, src: str) -> pathlib.Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(src))
    return p


# ── _call_name ─────────────────────────────────────────────────────────────────

def test_call_name_bare():
    src = "foo()"
    tree = ast.parse(src)
    call = list(ast.walk(tree))[2]  # the Call node
    assert _call_name(call) == "foo"


def test_call_name_attribute():
    src = "self.bar()"
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    result = _call_name(calls[0])
    assert result == "self.bar"


def test_call_name_nested_attribute():
    src = "a.b.c()"
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    result = _call_name(calls[0])
    assert result == "a.b.c"


def test_call_name_subscript_returns_none():
    src = "funcs[0]()"
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    result = _call_name(calls[0])
    assert result is None


# ── _collect_py_files ─────────────────────────────────────────────────────────

def test_collect_py_files_single_file(tmp_path: pathlib.Path):
    f = tmp_path / "mod.py"
    f.write_text("x = 1")
    files = _collect_py_files(f)
    assert files == [f]


def test_collect_py_files_directory(tmp_path: pathlib.Path):
    (tmp_path / "a.py").write_text("x=1")
    (tmp_path / "b.py").write_text("y=2")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "c.pyc").write_text("")
    files = _collect_py_files(tmp_path)
    names = {f.name for f in files}
    assert "a.py" in names
    assert "b.py" in names
    assert not any(f.suffix == ".pyc" for f in files)


# ── _build_callgraph ──────────────────────────────────────────────────────────

def test_build_callgraph_top_level_functions(tmp_repo: pathlib.Path):
    _write(tmp_repo, "mod.py", """\
        def foo():
            bar()

        def bar():
            pass
    """)
    cg = _build_callgraph([tmp_repo / "mod.py"], tmp_repo)
    keys = set(cg.nodes.keys())
    assert "mod.py::foo" in keys
    assert "mod.py::bar" in keys


def test_build_callgraph_class_methods(tmp_repo: pathlib.Path):
    _write(tmp_repo, "cls.py", """\
        class MyClass:
            def alpha(self):
                self.beta()

            def beta(self):
                pass
    """)
    cg = _build_callgraph([tmp_repo / "cls.py"], tmp_repo)
    keys = set(cg.nodes.keys())
    assert "cls.py::MyClass.alpha" in keys
    assert "cls.py::MyClass.beta" in keys


def test_build_callgraph_caller_edge(tmp_repo: pathlib.Path):
    _write(tmp_repo, "mod.py", """\
        def caller():
            callee()

        def callee():
            pass
    """)
    cg = _build_callgraph([tmp_repo / "mod.py"], tmp_repo)
    caller_key = "mod.py::caller"
    callee_key = "mod.py::callee"
    assert callee_key in cg.callees.get(caller_key, set())
    assert caller_key in cg.callers.get(callee_key, set())


def test_build_callgraph_no_self_edges(tmp_repo: pathlib.Path):
    _write(tmp_repo, "mod.py", """\
        def recursive():
            recursive()
    """)
    cg = _build_callgraph([tmp_repo / "mod.py"], tmp_repo)
    key = "mod.py::recursive"
    assert key not in cg.callees.get(key, set())


def test_build_callgraph_empty_file(tmp_repo: pathlib.Path):
    _write(tmp_repo, "empty.py", "")
    cg = _build_callgraph([tmp_repo / "empty.py"], tmp_repo)
    assert len(cg.nodes) == 0


def test_build_callgraph_syntax_error_skipped(tmp_repo: pathlib.Path):
    _write(tmp_repo, "bad.py", "def (broken):\n    pass")
    cg = _build_callgraph([tmp_repo / "bad.py"], tmp_repo)
    assert "bad.py::broken" not in cg.nodes


def test_build_callgraph_cross_file(tmp_repo: pathlib.Path):
    _write(tmp_repo, "a.py", """\
        def helper():
            pass
    """)
    _write(tmp_repo, "b.py", """\
        def consumer():
            helper()
    """)
    cg = _build_callgraph([tmp_repo / "a.py", tmp_repo / "b.py"], tmp_repo)
    consumer_key = "b.py::consumer"
    helper_key = "a.py::helper"
    assert helper_key in cg.callees.get(consumer_key, set())
    assert consumer_key in cg.callers.get(helper_key, set())


# ── _resolve_function_keys ────────────────────────────────────────────────────

def test_resolve_bare_name(tmp_repo: pathlib.Path):
    _write(tmp_repo, "mod.py", """\
        def target():
            pass
    """)
    cg = _build_callgraph([tmp_repo / "mod.py"], tmp_repo)
    keys = _resolve_function_keys(cg, "target", None)
    assert "mod.py::target" in keys


def test_resolve_qualified_name(tmp_repo: pathlib.Path):
    _write(tmp_repo, "cls.py", """\
        class Foo:
            def bar(self):
                pass
    """)
    cg = _build_callgraph([tmp_repo / "cls.py"], tmp_repo)
    keys = _resolve_function_keys(cg, "Foo.bar", None)
    assert "cls.py::Foo.bar" in keys


def test_resolve_unknown_returns_empty(tmp_repo: pathlib.Path):
    _write(tmp_repo, "mod.py", "def alpha(): pass")
    cg = _build_callgraph([tmp_repo / "mod.py"], tmp_repo)
    keys = _resolve_function_keys(cg, "nonexistent", None)
    assert len(keys) == 0


def test_resolve_with_file_filter(tmp_repo: pathlib.Path):
    _write(tmp_repo, "a.py", "def fn(): pass")
    _write(tmp_repo, "b.py", "def fn(): pass")
    cg = _build_callgraph([tmp_repo / "a.py", tmp_repo / "b.py"], tmp_repo)
    keys = _resolve_function_keys(cg, "fn", "a.py")
    assert all("a.py" in k for k in keys)
    assert not any("b.py" in k for k in keys)


# ── _bfs_direction ────────────────────────────────────────────────────────────

def _simple_cg(tmp_repo: pathlib.Path) -> CallGraph:
    """a → b → c  (a calls b, b calls c)"""
    _write(tmp_repo, "chain.py", """\
        def a():
            b()

        def b():
            c()

        def c():
            pass
    """)
    return _build_callgraph([tmp_repo / "chain.py"], tmp_repo)


def test_bfs_callers_depth1(tmp_repo: pathlib.Path):
    cg = _simple_cg(tmp_repo)
    c_key = "chain.py::c"
    result = _bfs_direction(cg, {c_key}, "callers", 1)
    # Direct callers of c = {b}
    assert "chain.py::b" in result
    assert "chain.py::a" not in result


def test_bfs_callers_depth2(tmp_repo: pathlib.Path):
    cg = _simple_cg(tmp_repo)
    c_key = "chain.py::c"
    result = _bfs_direction(cg, {c_key}, "callers", 2)
    assert "chain.py::b" in result
    assert "chain.py::a" in result


def test_bfs_callees_depth1(tmp_repo: pathlib.Path):
    cg = _simple_cg(tmp_repo)
    a_key = "chain.py::a"
    result = _bfs_direction(cg, {a_key}, "callees", 1)
    assert "chain.py::b" in result
    assert "chain.py::c" not in result


def test_bfs_unreachable_not_included(tmp_repo: pathlib.Path):
    cg = _simple_cg(tmp_repo)
    c_key = "chain.py::c"
    result = _bfs_direction(cg, {c_key}, "callees", 3)
    # c has no callees
    assert len([k for k in result if k != c_key]) == 0


# ── Handler: focused mode ─────────────────────────────────────────────────────

def test_handler_focused_text(tmp_repo: pathlib.Path, ctx: ToolContext):
    _write(tmp_repo, "ouroboros/mod.py", """\
        def caller():
            target()

        def target():
            pass
    """)
    (tmp_repo / "ouroboros").mkdir(exist_ok=True)
    result = _callgraph(ctx, path="ouroboros/", function="target",
                        direction="callers", _repo_dir=tmp_repo)
    assert "target" in result
    assert "caller" in result


def test_handler_focused_json(tmp_repo: pathlib.Path, ctx: ToolContext):
    (tmp_repo / "ouroboros").mkdir(exist_ok=True)
    _write(tmp_repo, "ouroboros/mod.py", """\
        def alpha():
            beta()

        def beta():
            pass
    """)
    result = _callgraph(ctx, path="ouroboros/", function="beta",
                        direction="callers", format="json", _repo_dir=tmp_repo)
    data = json.loads(result)
    assert data["function"] == "beta"
    assert isinstance(data["callers"], list)


def test_handler_function_not_found_text(tmp_repo: pathlib.Path, ctx: ToolContext):
    (tmp_repo / "ouroboros").mkdir(exist_ok=True)
    _write(tmp_repo, "ouroboros/mod.py", "def real(): pass")
    result = _callgraph(ctx, path="ouroboros/", function="phantom", _repo_dir=tmp_repo)
    assert "not found" in result or "phantom" in result


def test_handler_function_not_found_json(tmp_repo: pathlib.Path, ctx: ToolContext):
    (tmp_repo / "ouroboros").mkdir(exist_ok=True)
    _write(tmp_repo, "ouroboros/mod.py", "def real(): pass")
    result = _callgraph(ctx, path="ouroboros/", function="phantom",
                        format="json", _repo_dir=tmp_repo)
    data = json.loads(result)
    assert "error" in data


def test_handler_direction_both(tmp_repo: pathlib.Path, ctx: ToolContext):
    (tmp_repo / "ouroboros").mkdir(exist_ok=True)
    _write(tmp_repo, "ouroboros/mod.py", """\
        def up():
            middle()

        def middle():
            down()

        def down():
            pass
    """)
    result = _callgraph(ctx, path="ouroboros/", function="middle",
                        direction="both", _repo_dir=tmp_repo)
    assert "Callers" in result
    assert "Callees" in result


def test_handler_depth_limits_transitive(tmp_repo: pathlib.Path, ctx: ToolContext):
    """depth=2 should include grandparent callers."""
    (tmp_repo / "ouroboros").mkdir(exist_ok=True)
    _write(tmp_repo, "ouroboros/chain.py", """\
        def a():
            b()
        def b():
            c()
        def c():
            pass
    """)
    result = _callgraph(ctx, path="ouroboros/", function="c",
                        direction="callers", depth=2, _repo_dir=tmp_repo)
    assert "a" in result
    assert "b" in result


# ── Handler: overview mode ────────────────────────────────────────────────────

def test_handler_overview_text(tmp_repo: pathlib.Path, ctx: ToolContext):
    (tmp_repo / "ouroboros").mkdir(exist_ok=True)
    _write(tmp_repo, "ouroboros/mod.py", """\
        def alpha():
            beta()
            gamma()
        def beta():
            pass
        def gamma():
            pass
    """)
    result = _callgraph(ctx, path="ouroboros/", _repo_dir=tmp_repo)
    assert "Call Graph Overview" in result
    assert "functions" in result.lower() or "×" in result


def test_handler_overview_json(tmp_repo: pathlib.Path, ctx: ToolContext):
    (tmp_repo / "ouroboros").mkdir(exist_ok=True)
    _write(tmp_repo, "ouroboros/mod.py", """\
        def x():
            y()
        def y():
            pass
    """)
    result = _callgraph(ctx, path="ouroboros/", format="json", _repo_dir=tmp_repo)
    data = json.loads(result)
    assert "total_functions" in data
    assert "functions" in data
    assert data["total_functions"] >= 2


def test_handler_path_not_found(tmp_repo: pathlib.Path, ctx: ToolContext):
    result = _callgraph(ctx, path="does/not/exist/", _repo_dir=tmp_repo)
    assert "not found" in result.lower() or "error" in result.lower()


def test_handler_file_scope(tmp_repo: pathlib.Path, ctx: ToolContext):
    """Passing a specific .py file should work too."""
    _write(tmp_repo, "standalone.py", """\
        def reader():
            writer()
        def writer():
            pass
    """)
    result = _callgraph(ctx, path="standalone.py", function="writer",
                        direction="callers", _repo_dir=tmp_repo)
    assert "reader" in result


# ── get_tools ─────────────────────────────────────────────────────────────────

def test_get_tools_returns_entry():
    from ouroboros.tools.callgraph import get_tools
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "callgraph"
    assert "callers" in tools[0].schema["description"].lower()
    assert "callees" in tools[0].schema["description"].lower()
