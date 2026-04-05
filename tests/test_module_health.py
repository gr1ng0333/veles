"""Tests for ouroboros/tools/module_health.py"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("REPO_DIR", "/opt/veles")

from ouroboros.tools.module_health import (
    _compute_score,
    _format_text,
    _load_names,
    _make_recommendations,
    _module_health,
    _relative,
    _resolve_file,
    _scan_churn,
    _scan_dead_code,
    _scan_tech_debt,
    _scan_tests,
    get_tools,
)
from ouroboros.tools.registry import ToolContext


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ctx(repo_dir: str) -> ToolContext:
    ctx = MagicMock(spec=ToolContext)
    ctx.repo_dir = repo_dir
    return ctx


def _write(tmp_path: Path, name: str, src: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(src), encoding="utf-8")
    return p


# ── Registration ──────────────────────────────────────────────────────────────

def test_get_tools_returns_one():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "module_health"


def test_schema_has_required_fields():
    schema = get_tools()[0].schema
    assert schema["name"] == "module_health"
    assert "description" in schema
    props = schema["parameters"]["properties"]
    assert "target" in props
    assert "days" in props
    assert "format" in props
    assert "target" in schema["parameters"].get("required", [])


# ── _resolve_file ─────────────────────────────────────────────────────────────

def test_resolve_file_direct_path(tmp_path):
    f = _write(tmp_path, "mod.py", "x = 1\n")
    result = _resolve_file("mod.py", tmp_path)
    assert result == f


def test_resolve_file_not_found(tmp_path):
    result = _resolve_file("nonexistent_xyz.py", tmp_path)
    assert result is None


def test_resolve_file_on_real_repo():
    """Resolve a real file from the actual repo."""
    repo = Path("/opt/veles")
    if not repo.exists():
        pytest.skip("Not running in /opt/veles environment")
    result = _resolve_file("ouroboros/tools/registry.py", repo)
    assert result is not None
    assert result.exists()


# ── _scan_tech_debt ───────────────────────────────────────────────────────────

def test_scan_tech_debt_clean(tmp_path):
    f = _write(tmp_path, "clean.py", "def add(a, b):\n    return a + b\n")
    debt = _scan_tech_debt(f)
    assert debt["loc"] == 2
    assert not debt["oversized_module"]
    assert debt["oversized_functions"] == []
    assert debt["high_complexity"] == []
    assert debt["too_many_params"] == []


def test_scan_tech_debt_too_many_params(tmp_path):
    params = ", ".join(f"p{i}" for i in range(10))
    f = _write(tmp_path, "many.py", f"def big({params}):\n    return 1\n")
    debt = _scan_tech_debt(f)
    assert len(debt["too_many_params"]) == 1
    assert debt["too_many_params"][0]["params"] == 10


def test_scan_tech_debt_oversized_module(tmp_path):
    # Write a file with >1000 lines
    src = "x = 1\n" * 1001
    f = tmp_path / "big.py"
    f.write_text(src)
    debt = _scan_tech_debt(f)
    assert debt["oversized_module"] is True


# ── _scan_dead_code ───────────────────────────────────────────────────────────

def test_scan_dead_code_unused_import(tmp_path):
    f = _write(tmp_path, "bad.py", "import sys\nx = 1\n")
    dead = _scan_dead_code(f, tmp_path)
    assert any(i["name"] == "sys" for i in dead["unused_imports"])


def test_scan_dead_code_used_import(tmp_path):
    f = _write(tmp_path, "ok.py", "import os\nprint(os.getcwd())\n")
    dead = _scan_dead_code(f, tmp_path)
    assert not any(i["name"] == "os" for i in dead["unused_imports"])


def test_scan_dead_code_dead_private(tmp_path):
    f = _write(tmp_path, "priv.py", "def _unused():\n    pass\n")
    dead = _scan_dead_code(f, tmp_path)
    assert any(p["name"] == "_unused" for p in dead["dead_privates"])


def test_scan_dead_code_used_private(tmp_path):
    src = "def _util():\n    pass\n\nresult = _util()\n"
    f = _write(tmp_path, "used.py", src)
    dead = _scan_dead_code(f, tmp_path)
    assert not any(p["name"] == "_util" for p in dead["dead_privates"])


# ── _scan_tests ───────────────────────────────────────────────────────────────

def test_scan_tests_finds_matching(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_registry.py").write_text("pass", encoding="utf-8")
    target = tmp_path / "ouroboros" / "tools" / "registry.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("pass", encoding="utf-8")
    found = _scan_tests(target, tmp_path)
    assert any("test_registry" in t for t in found)


def test_scan_tests_no_tests(tmp_path):
    target = tmp_path / "ouroboros" / "tools" / "unique_xyz_module.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")
    found = _scan_tests(target, tmp_path)
    assert found == []


# ── _scan_churn ───────────────────────────────────────────────────────────────

def test_scan_churn_returns_int(tmp_path):
    """On a real repo, returns a non-negative integer."""
    repo = Path("/opt/veles")
    if not repo.exists():
        pytest.skip("Not running in /opt/veles environment")
    churn = _scan_churn("ouroboros/tools/registry.py", repo, days=30)
    assert isinstance(churn, int)
    assert churn >= 0


# ── _compute_score ────────────────────────────────────────────────────────────

def _make_clean():
    debt = {"loc": 50, "oversized_module": False, "oversized_functions": [],
             "high_complexity": [], "deep_nesting": [], "too_many_params": []}
    dead = {"unused_imports": [], "dead_privates": []}
    impact = {"overall_risk": "LOW", "direct_count": 1, "transitive_count": 2}
    return debt, dead, impact


def test_compute_score_perfect():
    debt, dead, impact = _make_clean()
    score, grade, breakdown = _compute_score(debt, dead, impact, churn=0, tests=["tests/test_foo.py"])
    assert score == 100
    assert grade == "A"
    assert breakdown == []


def test_compute_score_no_tests_penalty():
    debt, dead, impact = _make_clean()
    score, grade, breakdown = _compute_score(debt, dead, impact, churn=0, tests=[])
    assert score == 90  # -10 for no tests
    assert any("no_test_file" in b for b in breakdown)


def test_compute_score_critical_impact():
    debt, dead, impact = _make_clean()
    impact["overall_risk"] = "CRITICAL"
    impact["transitive_count"] = 100
    score, grade, breakdown = _compute_score(debt, dead, impact, churn=0, tests=["t.py"])
    # -8 (CRITICAL) + -5 (>50 transitive)
    assert score == 87
    assert grade == "B"


def test_compute_score_high_churn():
    debt, dead, impact = _make_clean()
    score, grade, breakdown = _compute_score(debt, dead, impact, churn=25, tests=["t.py"])
    assert score == 92  # -8 for churn>20


def test_compute_score_many_dead_imports():
    debt, dead, impact = _make_clean()
    dead["unused_imports"] = [{"name": f"imp{i}", "line": i, "stmt": f"import imp{i}"} for i in range(8)]
    score, grade, breakdown = _compute_score(debt, dead, impact, churn=0, tests=["t.py"])
    # -8 (capped at min(10, 8))
    assert score == 92


def test_compute_score_grade_f():
    debt, dead, impact = _make_clean()
    # Pile on penalties
    debt["oversized_functions"] = [{"name": f"f{i}", "line": i, "lines": 200} for i in range(6)]
    debt["high_complexity"] = [{"name": f"g{i}", "line": i, "complexity": 20} for i in range(6)]
    debt["oversized_module"] = True
    dead["unused_imports"] = [{"name": f"i{i}", "line": i, "stmt": ""} for i in range(10)]
    impact["overall_risk"] = "CRITICAL"
    impact["transitive_count"] = 100
    score, grade, breakdown = _compute_score(debt, dead, impact, churn=25, tests=[])
    assert grade == "F"
    assert score < 40


# ── _make_recommendations ─────────────────────────────────────────────────────

def test_make_recommendations_no_tests():
    debt, dead, impact = _make_clean()
    recs = _make_recommendations(debt, dead, impact, tests=[], churn=0)
    assert any("No test file" in r for r in recs)


def test_make_recommendations_max_3():
    debt, dead, impact = _make_clean()
    debt["oversized_functions"] = [{"name": "big", "line": 1, "lines": 300}]
    debt["high_complexity"] = [{"name": "cx", "line": 1, "complexity": 20}]
    dead["unused_imports"] = [{"name": "os", "line": 1, "stmt": "import os"}]
    impact["overall_risk"] = "CRITICAL"
    impact["transitive_count"] = 100
    recs = _make_recommendations(debt, dead, impact, tests=[], churn=25)
    assert len(recs) <= 3


# ── _module_health (integration) ──────────────────────────────────────────────

def test_module_health_missing_target():
    result = _module_health(_ctx("/opt/veles"), target="")
    assert "Error" in result or "error" in result.lower()


def test_module_health_nonexistent_target():
    result = _module_health(_ctx("/opt/veles"), target="ouroboros/nonexistent_xyz.py")
    assert "Error" in result or "cannot resolve" in result.lower()


def test_module_health_text_output():
    """Integration test on a real file."""
    repo = "/opt/veles"
    if not Path(repo).exists():
        pytest.skip("Not running in /opt/veles environment")
    result = _module_health(_ctx(repo), target="ouroboros/tools/registry.py", format="text")
    assert "Module Health" in result
    assert "Score" in result
    assert "Grade" in result or any(g in result for g in ["A", "B", "C", "D", "F"])


def test_module_health_json_output():
    """Integration test — JSON format."""
    repo = "/opt/veles"
    if not Path(repo).exists():
        pytest.skip("Not running in /opt/veles environment")
    result = _module_health(_ctx(repo), target="ouroboros/tools/registry.py", format="json")
    data = json.loads(result)
    assert "score" in data
    assert "grade" in data
    assert "tech_debt" in data
    assert "dead_code" in data
    assert "change_impact" in data
    assert "churn" in data
    assert "tests" in data
    assert "recommendations" in data
    assert isinstance(data["score"], int)
    assert 0 <= data["score"] <= 100
    assert data["grade"] in ("A", "B", "C", "D", "F")


def test_module_health_small_clean_module(tmp_path):
    """A tiny clean module should get a high score."""
    f = _write(tmp_path, "tiny.py", "def add(a, b):\n    return a + b\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_tiny.py").write_text("pass\n", encoding="utf-8")

    # Patch change_impact to avoid real dependency graph
    with patch("ouroboros.tools.module_health._scan_change_impact") as mock_impact:
        mock_impact.return_value = {
            "overall_risk": "LOW", "direct_count": 0, "transitive_count": 0
        }
        result = _module_health(_ctx(str(tmp_path)), target="tiny.py", format="json")

    data = json.loads(result)
    assert data["score"] >= 90  # clean module with test → A
    assert data["grade"] == "A"
