"""Tests for change_impact tool — blast-radius analysis."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ouroboros.tools.change_impact import _change_impact, get_tools, _classify_risk, _build_reverse_graph


# ── Fixtures ────────────────────────────────────────────────────────────────


class FakeCtx:
    pass


def _run(target, **kwargs):
    return _change_impact(FakeCtx(), target=target, **kwargs)


def _run_json(target, **kwargs):
    result = _run(target, format="json", **kwargs)
    return json.loads(result)


# ── Registration ─────────────────────────────────────────────────────────────


def test_get_tools_registered():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "change_impact"


def test_schema_has_required_target():
    tools = get_tools()
    schema = tools[0].schema
    assert "target" in schema["parameters"]["properties"]
    assert "target" in schema["parameters"].get("required", [])


# ── Input validation ─────────────────────────────────────────────────────────


def test_empty_target_returns_error():
    result = _run("")
    data = json.loads(result)
    assert "error" in data


def test_unknown_target_returns_error_with_hint():
    result = _run("nonexistent_module_xyz_abc")
    data = json.loads(result)
    assert "error" in data


# ── Registry (critical hub) ──────────────────────────────────────────────────


def test_registry_is_critical():
    data = _run_json("ouroboros/tools/registry.py")
    assert data["overall_risk"] == "CRITICAL"


def test_registry_has_many_direct_dependents():
    data = _run_json("tools.registry")
    assert data["direct_count"] >= 50, "registry should have 50+ direct dependents"


def test_registry_transitive_count():
    data = _run_json("ouroboros/tools/registry.py")
    assert data["transitive_count"] >= 50


def test_registry_recommended_tests_includes_smoke():
    data = _run_json("ouroboros/tools/registry.py")
    assert any("test_smoke" in t for t in data["recommended_tests"])


# ── Leaf / low-impact module ─────────────────────────────────────────────────


def test_leaf_module_has_low_direct_count():
    """time_tools should have few or no dependents."""
    data = _run_json("tools.time_tools")
    # time_tools is a leaf — almost nothing imports it
    assert data["direct_count"] <= 5


def test_leaf_module_risk_is_not_critical():
    data = _run_json("tools.time_tools")
    assert data["overall_risk"] != "CRITICAL"


# ── Result structure ─────────────────────────────────────────────────────────


def test_json_output_has_required_keys():
    data = _run_json("ouroboros/tools/registry.py")
    required = {
        "target", "file", "overall_risk", "direct_dependents",
        "direct_count", "transitive_count", "blast_radius",
        "risk_breakdown", "recommended_tests", "depth_searched",
    }
    assert required.issubset(data.keys())


def test_blast_radius_has_depth_keys():
    data = _run_json("tools.registry")
    assert "depth_1" in data["blast_radius"]
    assert "depth_2plus" in data["blast_radius"]


def test_risk_breakdown_has_all_tiers():
    data = _run_json("tools.registry")
    for tier in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        assert tier in data["risk_breakdown"]


def test_direct_dependents_is_list_of_strings():
    data = _run_json("tools.registry")
    assert isinstance(data["direct_dependents"], list)
    for item in data["direct_dependents"]:
        assert isinstance(item, str)


# ── Text format ──────────────────────────────────────────────────────────────


def test_text_format_contains_target():
    result = _run("tools.registry", format="text")
    assert "tools.registry" in result
    assert "change_impact" in result


def test_text_format_contains_risk_label():
    result = _run("tools.registry", format="text")
    assert "CRITICAL" in result or "HIGH" in result or "MEDIUM" in result or "LOW" in result


def test_text_format_recommends_tests():
    result = _run("tools.registry", format="text")
    assert "pytest" in result


# ── Depth parameter ──────────────────────────────────────────────────────────


def test_depth_1_smaller_than_depth_5():
    d1 = _run_json("tools.registry", depth=1)
    d5 = _run_json("tools.registry", depth=5)
    # depth=1: only direct dependents counted in transitive
    # depth=5: more transitivity
    assert d5["transitive_count"] >= d1["transitive_count"]


def test_depth_respected_in_output():
    data = _run_json("tools.registry", depth=2)
    assert data["depth_searched"] == 2


# ── Risk classification helper ────────────────────────────────────────────────


def test_classify_risk_critical():
    assert _classify_risk("agent") == "CRITICAL"
    assert _classify_risk("loop") == "CRITICAL"
    assert _classify_risk("context") == "CRITICAL"
    assert _classify_risk("registry") == "CRITICAL"


def test_classify_risk_high():
    assert _classify_risk("tools.some_tool") == "HIGH"
    assert _classify_risk("loop_runtime") == "HIGH"


def test_classify_risk_medium():
    assert _classify_risk("digest_scheduler") == "MEDIUM"
    assert _classify_risk("reflection") == "MEDIUM"


# ── Reverse graph helper ─────────────────────────────────────────────────────


def test_build_reverse_graph_basic():
    adj = {"a": {"b", "c"}, "b": {"c"}}
    rev = _build_reverse_graph(adj)
    assert "a" in rev.get("b", set())
    assert "a" in rev.get("c", set())
    assert "b" in rev.get("c", set())


def test_build_reverse_graph_empty():
    rev = _build_reverse_graph({})
    assert rev == {}


# ── show_test_map=False ───────────────────────────────────────────────────────


def test_show_test_map_false_still_has_smoke():
    """Even with show_test_map=False, smoke should still be included."""
    data = _run_json("tools.registry", show_test_map=False)
    # smoke is always prepended
    assert any("test_smoke" in t for t in data["recommended_tests"])
