"""Tests for coupling_map — Robert Martin coupling metrics."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ouroboros.tools.coupling_map import (
    _build_coupling_graph,
    _classify_tier,
    _collect_py_files,
    _compute_metrics,
    _coupling_map,
    _filter_records,
    _module_key,
    _parse_imports,
    _resolve_relative,
    _RIGID_CA_MIN,
    _RIGID_I,
    _UNSTABLE_I,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write(tmp: Path, rel: str, src: str) -> Path:
    p = tmp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(src))
    return p


# ── Unit: _module_key ─────────────────────────────────────────────────────────

def test_module_key_simple(tmp_path):
    f = _write(tmp_path, "foo/bar.py", "x = 1")
    assert _module_key(f, tmp_path) == "foo.bar"


def test_module_key_init(tmp_path):
    f = _write(tmp_path, "foo/__init__.py", "")
    assert _module_key(f, tmp_path) == "foo"


def test_module_key_top_level(tmp_path):
    f = _write(tmp_path, "agent.py", "")
    assert _module_key(f, tmp_path) == "agent"


# ── Unit: _parse_imports ──────────────────────────────────────────────────────

def test_parse_imports_absolute(tmp_path):
    f = _write(tmp_path, "a.py", """\
        import os
        import ouroboros.tools.registry
    """)
    imports = {name for name, _ in _parse_imports(f)}
    assert "os" in imports
    assert "ouroboros.tools.registry" in imports


def test_parse_imports_from(tmp_path):
    f = _write(tmp_path, "a.py", """\
        from ouroboros.tools import registry
    """)
    imports = {name for name, _ in _parse_imports(f)}
    assert "ouroboros.tools" in imports


def test_parse_imports_relative(tmp_path):
    f = _write(tmp_path, "tools/a.py", """\
        from . import registry
        from ..utils import helper
    """)
    imports = {name for name, _ in _parse_imports(f)}
    assert "." in imports
    assert "..utils" in imports


def test_parse_imports_syntax_error(tmp_path):
    f = _write(tmp_path, "bad.py", "def (broken:")
    assert _parse_imports(f) == []


# ── Unit: _resolve_relative ───────────────────────────────────────────────────

def test_resolve_relative_same_package():
    assert _resolve_relative("tools.registry", ".utils") == "tools.utils"


def test_resolve_relative_parent_package():
    assert _resolve_relative("tools.sub.mod", "..registry") == "tools.registry"


def test_resolve_relative_bare_dot():
    # from . import X → just the package
    result = _resolve_relative("tools.registry", ".")
    assert result == "tools"


def test_resolve_relative_non_relative():
    assert _resolve_relative("tools.registry", "os") is None


# ── Unit: _classify_tier ──────────────────────────────────────────────────────

def test_classify_tier_critical():
    assert _classify_tier("agent") == "CRITICAL"
    assert _classify_tier("loop.runtime") == "CRITICAL"
    assert _classify_tier("tools.registry") == "CRITICAL"


def test_classify_tier_high():
    assert _classify_tier("tools.search") == "HIGH"


def test_classify_tier_medium():
    assert _classify_tier("utils.helpers") == "MEDIUM"


# ── Integration: _build_coupling_graph ───────────────────────────────────────

def _make_pkg(tmp: Path) -> Path:
    """Create a small synthetic package: a → b → c, a → c."""
    root = tmp / "pkg"
    _write(root, "a.py", "from pkg import b\nfrom pkg import c\n")
    _write(root, "b.py", "from pkg import c\n")
    _write(root, "c.py", "# leaf\n")
    return root


def test_build_graph_edges(tmp_path):
    root = _make_pkg(tmp_path)
    files = _collect_py_files(root)
    fwd, ce_ext, mod_map = _build_coupling_graph(files, root)

    assert "b" in fwd.get("a", set())
    assert "c" in fwd.get("a", set())
    assert "c" in fwd.get("b", set())
    assert not fwd.get("c")  # leaf has no internal imports


def test_build_graph_no_self_loops(tmp_path):
    root = _make_pkg(tmp_path)
    files = _collect_py_files(root)
    fwd, _, mod_map = _build_coupling_graph(files, root)
    for mod, deps in fwd.items():
        assert mod not in deps, f"{mod} imports itself"


# ── Integration: _compute_metrics ────────────────────────────────────────────

def test_compute_metrics_ca_ce(tmp_path):
    root = _make_pkg(tmp_path)
    files = _collect_py_files(root)
    fwd, ce_ext, mod_map = _build_coupling_graph(files, root)
    records = _compute_metrics(fwd, ce_ext, mod_map)

    by_mod = {r["module"]: r for r in records}

    # c is the leaf — Ca=2 (imported by a and b), Ce=0
    c = by_mod["c"]
    assert c["ca"] == 2
    assert c["ce"] == 0
    assert c["instability"] == 0.0

    # a imports b and c — Ce=2; nobody imports a (in this graph) — Ca=0
    a = by_mod["a"]
    assert a["ce"] == 2
    assert a["ca"] == 0
    assert a["instability"] == 1.0


def test_compute_metrics_instability_range(tmp_path):
    root = _make_pkg(tmp_path)
    files = _collect_py_files(root)
    fwd, ce_ext, mod_map = _build_coupling_graph(files, root)
    records = _compute_metrics(fwd, ce_ext, mod_map)
    for r in records:
        if r["instability"] is not None:
            assert 0.0 <= r["instability"] <= 1.0


def test_compute_metrics_isolated(tmp_path):
    """A module with no imports and no importers is ISOLATED."""
    root = tmp_path / "pkg"
    _write(root, "alone.py", "# no imports\n")
    files = _collect_py_files(root)
    fwd, ce_ext, mod_map = _build_coupling_graph(files, root)
    records = _compute_metrics(fwd, ce_ext, mod_map)
    by_mod = {r["module"]: r for r in records}
    assert by_mod["alone"]["zone"] == "ISOLATED"
    assert by_mod["alone"]["instability"] is None


def test_compute_metrics_zone_unstable(tmp_path):
    """A module with many outgoing, zero incoming → UNSTABLE."""
    root = _make_pkg(tmp_path)
    files = _collect_py_files(root)
    fwd, ce_ext, mod_map = _build_coupling_graph(files, root)
    records = _compute_metrics(fwd, ce_ext, mod_map)
    by_mod = {r["module"]: r for r in records}
    # 'a' has I=1.0 → UNSTABLE (1.0 >= _UNSTABLE_I=0.75)
    assert by_mod["a"]["zone"] == "UNSTABLE"


# ── Integration: _filter_records ─────────────────────────────────────────────

def _make_records():
    return [
        {"module": "tools.search", "ca": 2, "ce": 10, "instability": 0.83, "zone": "UNSTABLE", "tier": "HIGH", "importers": [], "imports": []},
        {"module": "agent", "ca": 12, "ce": 1, "instability": 0.08, "zone": "RIGID", "tier": "CRITICAL", "importers": [], "imports": []},
        {"module": "utils.helper", "ca": 0, "ce": 0, "instability": None, "zone": "ISOLATED", "tier": "MEDIUM", "importers": [], "imports": []},
    ]


def test_filter_by_zone():
    records = _make_records()
    out = _filter_records(records, path=None, filter_zone="UNSTABLE", min_ce=0, min_ca=0)
    assert len(out) == 1
    assert out[0]["module"] == "tools.search"


def test_filter_by_min_ce():
    records = _make_records()
    out = _filter_records(records, path=None, filter_zone=None, min_ce=5, min_ca=0)
    assert all(r["ce"] >= 5 for r in out)


def test_filter_by_min_ca():
    records = _make_records()
    out = _filter_records(records, path=None, filter_zone=None, min_ce=0, min_ca=5)
    assert all(r["ca"] >= 5 for r in out)


def test_filter_by_path():
    records = _make_records()
    out = _filter_records(records, path="tools/", filter_zone=None, min_ce=0, min_ca=0)
    assert all("tools" in r["module"] for r in out)


# ── Integration: _coupling_map handler ───────────────────────────────────────

def test_coupling_map_text_output(tmp_path):
    _make_pkg(tmp_path)
    ctx = MagicMock()
    result = _coupling_map(ctx, format="text", _repo_dir=tmp_path)
    assert "Coupling Map" in result
    assert "Ca" in result
    assert "Ce" in result


def test_coupling_map_json_output(tmp_path):
    _make_pkg(tmp_path)
    ctx = MagicMock()
    result = _coupling_map(ctx, format="json", _repo_dir=tmp_path)
    data = json.loads(result)
    assert "records" in data
    assert "total_modules" in data
    assert isinstance(data["records"], list)


def test_coupling_map_filter_unstable(tmp_path):
    _make_pkg(tmp_path)
    ctx = MagicMock()
    result = _coupling_map(ctx, filter_zone="UNSTABLE", format="json", _repo_dir=tmp_path)
    data = json.loads(result)
    assert all(r["zone"] == "UNSTABLE" for r in data["records"])


def test_coupling_map_top_limit(tmp_path):
    _make_pkg(tmp_path)
    ctx = MagicMock()
    result = _coupling_map(ctx, top=1, format="json", _repo_dir=tmp_path)
    data = json.loads(result)
    assert len(data["records"]) <= 1


def test_coupling_map_empty_dir(tmp_path):
    """No Python files → graceful output."""
    ctx = MagicMock()
    result = _coupling_map(ctx, format="text", _repo_dir=tmp_path)
    # Should not crash; may return header with 0 modules
    assert isinstance(result, str)
