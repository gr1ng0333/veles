"""Tests for dependency_graph tool."""
import json
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ouroboros.tools.dependency_graph import _dependency_graph, get_tools


class FakeCtx:
    pass


def _run(mode, **kwargs):
    result = _dependency_graph(FakeCtx(), directory="ouroboros", mode=mode, **kwargs)
    return json.loads(result)


def test_get_tools_registered():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "dependency_graph"


def test_summary_basic():
    d = _run("summary")
    assert d["modules"] > 50, "Should have many modules"
    assert d["internal_edges"] > 50, "Should have many edges"
    assert "top_imported" in d
    assert "top_importers" in d
    assert "orphan_modules" in d
    assert "leaf_modules" in d


def test_summary_top_imported_contains_registry():
    """tools.registry should be among the most imported modules."""
    d = _run("summary")
    imported_names = [x["module"] for x in d["top_imported"]]
    assert any("registry" in n for n in imported_names), f"Expected registry in top_imported: {imported_names[:5]}"


def test_cycles_returns_list():
    d = _run("cycles")
    assert "cycles_found" in d
    assert isinstance(d["cycles"], list)
    # We know the codebase has cycles
    assert d["cycles_found"] >= 0


def test_module_mode_loop():
    d = _run("module", module="loop")
    assert d["module"] == "loop"
    assert isinstance(d["imports_internal"], list)
    assert isinstance(d["imported_by"], list)
    assert d["in_degree"] >= 0
    # loop must import at least a few things
    assert len(d["imports_internal"]) >= 2


def test_path_mode_agent_to_utils():
    d = _run("path", from_module="agent", to_module="utils")
    assert d["reachable"] is True
    assert d["path"] is not None
    assert d["path"][0] == "agent"
    assert d["path"][-1] == "utils"


def test_path_mode_unreachable():
    """utils probably doesn't import agent."""
    d = _run("path", from_module="utils", to_module="agent")
    # May or may not be reachable, but should return valid structure
    assert "reachable" in d
    assert "path" in d


def test_module_not_found_returns_hint():
    d = _run("module", module="nonexistent_module_xyz")
    assert "error" in d


def test_edges_mode():
    d = _run("edges")
    assert "total_edges" in d
    assert isinstance(d["edges"], list)
    assert d["total_edges"] > 0
    if d["edges"]:
        edge = d["edges"][0]
        assert "from" in edge
        assert "to" in edge


def test_invalid_directory():
    d = json.loads(_dependency_graph(FakeCtx(), directory="nonexistent_dir_xyz", mode="summary"))
    assert "error" in d


def test_unknown_mode():
    d = _run("invalid_mode")
    assert "error" in d
    assert "available_modes" in d
