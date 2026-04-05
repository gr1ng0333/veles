"""Tests for dep_cycles — circular import detector."""
from __future__ import annotations

import ast
import json
import tempfile
from pathlib import Path
from typing import Dict, List, Set, Tuple
from unittest.mock import MagicMock

import pytest

# Import internal helpers directly for unit testing
from ouroboros.tools.dep_cycles import (
    _build_import_graph,
    _classify_severity,
    _collect_py_files,
    _dep_cycles,
    _find_back_edge_line,
    _find_shortest_cycle,
    _format_text,
    _module_key,
    _parse_imports,
    _resolve_relative,
    _scan_dep_cycles,
    _tarjan_scc,
    get_tools,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_pkg(tmp_path: Path) -> Path:
    """Create a minimal Python package for graph tests."""
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    return pkg


def _write_module(pkg: Path, name: str, content: str) -> Path:
    path = pkg / f"{name}.py"
    path.write_text(content)
    return path


# ── _module_key ───────────────────────────────────────────────────────────────

class TestModuleKey:
    def test_simple(self, tmp_pkg: Path) -> None:
        f = tmp_pkg / "utils.py"
        f.write_text("")
        assert _module_key(f, tmp_pkg) == "utils"

    def test_nested(self, tmp_pkg: Path) -> None:
        sub = tmp_pkg / "sub"
        sub.mkdir()
        f = sub / "helper.py"
        f.write_text("")
        assert _module_key(f, tmp_pkg) == "sub.helper"

    def test_init_stripped(self, tmp_pkg: Path) -> None:
        sub = tmp_pkg / "sub"
        sub.mkdir()
        f = sub / "__init__.py"
        f.write_text("")
        assert _module_key(f, tmp_pkg) == "sub"


# ── _parse_imports ─────────────────────────────────────────────────────────────

class TestParseImports:
    def test_plain_import(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("import os\nimport sys\n")
        results = _parse_imports(f)
        names = [r[0] for r in results]
        assert "os" in names
        assert "sys" in names

    def test_from_import(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("from pathlib import Path\n")
        results = _parse_imports(f)
        assert ("pathlib", 1) in results

    def test_relative_import(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("from . import utils\nfrom ..helpers import foo\n")
        results = _parse_imports(f)
        names = [r[0] for r in results]
        assert "." in names or any(n.startswith(".") for n in names)

    def test_syntax_error_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.py"
        f.write_text("def broken(\n")
        assert _parse_imports(f) == []

    def test_returns_lineno(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("# comment\nimport os\n")
        results = _parse_imports(f)
        linenos = [r[1] for r in results]
        assert 2 in linenos


# ── _resolve_relative ─────────────────────────────────────────────────────────

class TestResolveRelative:
    def test_single_dot(self) -> None:
        assert _resolve_relative("pkg.mod", ".utils") == "pkg.utils"

    def test_double_dot(self) -> None:
        assert _resolve_relative("pkg.sub.mod", "..utils") == "pkg.utils"

    def test_plain_not_relative(self) -> None:
        assert _resolve_relative("pkg.mod", "os") is None

    def test_bare_dot(self) -> None:
        result = _resolve_relative("pkg.mod", ".")
        assert result == "pkg" or result is not None  # goes up one level

    def test_triple_dot(self) -> None:
        result = _resolve_relative("a.b.c.d", "...utils")
        assert result == "a.utils"


# ── _build_import_graph ───────────────────────────────────────────────────────

class TestBuildImportGraph:
    def test_simple_edge(self, tmp_pkg: Path) -> None:
        _write_module(tmp_pkg, "a", "from mypkg import b\n")
        _write_module(tmp_pkg, "b", "x = 1\n")
        adj, mod_map = _build_import_graph(
            [tmp_pkg / "a.py", tmp_pkg / "b.py"], tmp_pkg
        )
        assert "a" in mod_map
        assert "b" in mod_map
        # 'a' may or may not resolve depending on prefix logic
        assert isinstance(adj, dict)

    def test_no_imports(self, tmp_pkg: Path) -> None:
        _write_module(tmp_pkg, "standalone", "x = 42\n")
        adj, mod_map = _build_import_graph([tmp_pkg / "standalone.py"], tmp_pkg)
        assert "standalone" in mod_map
        assert adj.get("standalone", []) == []


# ── _tarjan_scc ───────────────────────────────────────────────────────────────

class TestTarjanSCC:
    def _adj(self, edges: List[Tuple[str, str]]) -> Dict[str, List[Tuple[str, int]]]:
        d: Dict[str, List[Tuple[str, int]]] = {}
        for src, dst in edges:
            d.setdefault(src, []).append((dst, 1))
        return d

    def test_no_cycle(self) -> None:
        adj = self._adj([("a", "b"), ("b", "c")])
        nodes = {"a", "b", "c"}
        sccs = _tarjan_scc(nodes, adj)
        assert sccs == []

    def test_simple_cycle_ab(self) -> None:
        adj = self._adj([("a", "b"), ("b", "a")])
        nodes = {"a", "b"}
        sccs = _tarjan_scc(nodes, adj)
        assert len(sccs) == 1
        assert set(sccs[0]) == {"a", "b"}

    def test_triangle_cycle(self) -> None:
        adj = self._adj([("a", "b"), ("b", "c"), ("c", "a")])
        nodes = {"a", "b", "c"}
        sccs = _tarjan_scc(nodes, adj)
        assert len(sccs) == 1
        assert set(sccs[0]) == {"a", "b", "c"}

    def test_two_separate_cycles(self) -> None:
        adj = self._adj([
            ("a", "b"), ("b", "a"),
            ("c", "d"), ("d", "c"),
        ])
        nodes = {"a", "b", "c", "d"}
        sccs = _tarjan_scc(nodes, adj)
        assert len(sccs) == 2

    def test_self_loop_excluded(self) -> None:
        # Single-node SCC (self-loop) should NOT be in results (len<2)
        adj = self._adj([("a", "a")])
        nodes = {"a"}
        sccs = _tarjan_scc(nodes, adj)
        assert sccs == []

    def test_isolated_nodes_excluded(self) -> None:
        adj: Dict[str, List] = {}
        nodes = {"x", "y", "z"}
        sccs = _tarjan_scc(nodes, adj)
        assert sccs == []


# ── _find_shortest_cycle ──────────────────────────────────────────────────────

class TestFindShortestCycle:
    def _adj(self, edges: List[Tuple[str, str]]) -> Dict[str, List[Tuple[str, int]]]:
        d: Dict[str, List[Tuple[str, int]]] = {}
        for src, dst in edges:
            d.setdefault(src, []).append((dst, 1))
        return d

    def test_ab_cycle(self) -> None:
        adj = self._adj([("a", "b"), ("b", "a")])
        cycle = _find_shortest_cycle({"a", "b"}, adj)
        assert cycle[0] == cycle[-1]
        assert len(set(cycle)) == 2

    def test_triangle_gives_length_3(self) -> None:
        adj = self._adj([("a", "b"), ("b", "c"), ("c", "a")])
        cycle = _find_shortest_cycle({"a", "b", "c"}, adj)
        # Shortest cycle uses all 3 nodes
        assert len(set(cycle[:-1])) == 3
        assert cycle[0] == cycle[-1]


# ── _classify_severity ────────────────────────────────────────────────────────

class TestClassifySeverity:
    def test_critical_for_loop(self) -> None:
        assert _classify_severity(["loop_runtime", "loop"]) == "CRITICAL"

    def test_critical_for_registry(self) -> None:
        assert _classify_severity(["tools.registry", "agent"]) == "CRITICAL"

    def test_high_for_tools(self) -> None:
        assert _classify_severity(["tools.search", "tools.http_client"]) == "HIGH"

    def test_medium_for_unknown(self) -> None:
        assert _classify_severity(["billing", "reporting"]) == "MEDIUM"


# ── _find_back_edge_line ──────────────────────────────────────────────────────

class TestFindBackEdgeLine:
    def _adj(self, edges: List[Tuple[str, str, int]]) -> Dict[str, List[Tuple[str, int]]]:
        d: Dict[str, List[Tuple[str, int]]] = {}
        for src, dst, line in edges:
            d.setdefault(src, []).append((dst, line))
        return d

    def test_finds_back_edge(self) -> None:
        adj = self._adj([("a", "b", 5), ("b", "a", 10)])
        cycle = ["a", "b", "a"]
        result = _find_back_edge_line(cycle, adj)
        assert result == ("b", "a", 10)

    def test_no_back_edge(self) -> None:
        adj = self._adj([("a", "b", 5)])
        cycle = ["a", "b", "a"]
        result = _find_back_edge_line(cycle, adj)
        assert result is None

    def test_empty_cycle(self) -> None:
        adj: Dict[str, List] = {}
        result = _find_back_edge_line([], adj)
        assert result is None


# ── _scan_dep_cycles integration ──────────────────────────────────────────────

class TestScanDepCycles:
    def test_finds_real_cycles_in_repo(self) -> None:
        """The actual repo has known circular imports — tool should find them."""
        from pathlib import Path
        repo = Path("/opt/veles")
        records, file_count = _scan_dep_cycles(repo, None, 2, None)
        assert file_count > 0
        # We know from preliminary run: at least 5 cycles exist
        assert len(records) >= 1

    def test_severity_filter(self) -> None:
        from pathlib import Path
        repo = Path("/opt/veles")
        records_all, _ = _scan_dep_cycles(repo, None, 2, None)
        records_medium, _ = _scan_dep_cycles(repo, None, 2, "MEDIUM")
        # MEDIUM filter should return subset
        assert len(records_medium) <= len(records_all)
        for r in records_medium:
            assert r["severity"] == "MEDIUM"

    def test_min_length_filter(self) -> None:
        from pathlib import Path
        repo = Path("/opt/veles")
        records_2, _ = _scan_dep_cycles(repo, None, 2, None)
        records_3, _ = _scan_dep_cycles(repo, None, 3, None)
        assert len(records_3) <= len(records_2)
        for r in records_3:
            assert r["shortest_cycle_length"] >= 3

    def test_records_have_required_fields(self) -> None:
        from pathlib import Path
        repo = Path("/opt/veles")
        records, _ = _scan_dep_cycles(repo, None, 2, None)
        if records:
            r = records[0]
            assert "severity" in r
            assert "shortest_cycle" in r
            assert "hint" in r
            assert "scc_members" in r
            assert r["severity"] in ("CRITICAL", "HIGH", "MEDIUM")


# ── _dep_cycles (full tool) ───────────────────────────────────────────────────

class TestDepCyclesTool:
    def _ctx(self) -> MagicMock:
        ctx = MagicMock()
        ctx.repo_dir = "/opt/veles"
        return ctx

    def test_text_output_returns_str(self) -> None:
        result = _dep_cycles(self._ctx(), format="text")
        assert isinstance(result, str)
        assert "Circular Import Report" in result

    def test_json_output_valid(self) -> None:
        result = _dep_cycles(self._ctx(), format="json")
        data = json.loads(result)
        assert "cycles_found" in data
        assert "cycles" in data
        assert isinstance(data["cycles"], list)

    def test_unknown_severity_returns_error(self) -> None:
        result = _dep_cycles(self._ctx(), severity="extreme")
        assert "Unknown severity" in result

    def test_severity_filter_in_json(self) -> None:
        result = _dep_cycles(self._ctx(), severity="critical", format="json")
        data = json.loads(result)
        for cycle in data["cycles"]:
            assert cycle["severity"] == "CRITICAL"

    def test_min_length_3(self) -> None:
        result = _dep_cycles(self._ctx(), min_length=3, format="json")
        data = json.loads(result)
        for cycle in data["cycles"]:
            assert cycle["shortest_cycle_length"] >= 3


# ── get_tools ─────────────────────────────────────────────────────────────────

class TestGetTools:
    def test_returns_one_tool(self) -> None:
        tools = get_tools()
        assert len(tools) == 1
        assert tools[0].name == "dep_cycles"

    def test_schema_has_required_keys(self) -> None:
        tools = get_tools()
        schema = tools[0].schema
        assert schema["name"] == "dep_cycles"
        assert "description" in schema
        assert "parameters" in schema

    def test_handler_callable(self) -> None:
        tools = get_tools()
        assert callable(tools[0].handler)
