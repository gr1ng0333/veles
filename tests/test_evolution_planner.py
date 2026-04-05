"""Tests for evolution_planner tool — action-candidate synthesis."""
from __future__ import annotations

import json
import pathlib
from unittest.mock import patch, MagicMock

import pytest

from ouroboros.tools.evolution_planner import (
    _candidates_from_patterns,
    _candidates_from_hot_spots,
    _candidates_from_test_gaps,
    _candidates_observability,
    _focus_boost,
    _rank_and_deduplicate,
    _score_candidate,
    _format_text,
    _evolution_plan,
    get_tools,
)
from ouroboros.tools.registry import ToolContext, ToolEntry


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_ctx(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path)


def _sample_patterns(n: int = 3) -> list:
    all_patterns = [
        {"class": "tool timeout on commit/push", "count": 200, "root_cause": "bad timeout cap"},
        {"class": "pre-push smoke failure", "count": 120, "root_cause": "slow pytest run"},
        {"class": "SSH / remote execution error", "count": 114, "root_cause": "poor command scoping"},
        {"class": "Copilot 500 / HTTP error", "count": 5, "root_cause": "brittle retry"},
        {"class": "loop round exhaustion", "count": 3, "root_cause": "hardcoded limit"},
    ]
    return all_patterns[:n]


def _sample_spots(n: int = 3) -> list:
    return [
        {
            "path": "ouroboros/loop_runtime.py",
            "churn": 15,
            "lines": 1200,
            "hot_score": 0.95,
            "oversized_functions": [{"name": "_run_round", "start_line": 200, "length": 220}],
        },
        {
            "path": "ouroboros/context.py",
            "churn": 8,
            "lines": 900,
            "hot_score": 0.72,
            "oversized_functions": [],
        },
        {
            "path": "ouroboros/tools/search.py",
            "churn": 4,
            "lines": 600,
            "hot_score": 0.45,
            "oversized_functions": [],
        },
    ][:n]


# ── Pattern candidates ────────────────────────────────────────────────────────

class TestCandidatesFromPatterns:
    def test_generates_candidates(self):
        cands = _candidates_from_patterns(_sample_patterns(3))
        assert len(cands) == 3

    def test_kind_is_fix_pattern(self):
        cands = _candidates_from_patterns(_sample_patterns(1))
        assert all(c["kind"] == "fix_pattern" for c in cands)

    def test_timeout_maps_to_git_tool(self):
        cands = _candidates_from_patterns([
            {"class": "tool timeout on commit/push", "count": 200, "root_cause": "timeouts"}
        ])
        assert "git.py" in cands[0]["target"]

    def test_smoke_maps_to_test_smoke(self):
        cands = _candidates_from_patterns([
            {"class": "pre-push smoke failure", "count": 120, "root_cause": "slow pytest"}
        ])
        assert "test_smoke" in cands[0]["target"]

    def test_ssh_maps_to_ssh_targets(self):
        cands = _candidates_from_patterns([
            {"class": "SSH / remote execution error", "count": 114, "root_cause": "poor scoping"}
        ])
        assert "ssh_targets" in cands[0]["target"]

    def test_empty_patterns(self):
        assert _candidates_from_patterns([]) == []

    def test_pattern_count_in_candidate(self):
        cands = _candidates_from_patterns([
            {"class": "tool timeout on commit/push", "count": 42, "root_cause": ""}
        ])
        assert cands[0]["pattern_count"] == 42


# ── Hot spot candidates ───────────────────────────────────────────────────────

class TestCandidatesFromHotSpots:
    def test_generates_refactor_candidates(self):
        cands = _candidates_from_hot_spots(_sample_spots(2))
        kinds = [c["kind"] for c in cands]
        assert all(k == "refactor" for k in kinds)

    def test_oversized_function_triggers_decompose(self):
        cands = _candidates_from_hot_spots(_sample_spots(1))
        assert len(cands) == 1
        assert "_run_round" in cands[0]["title"]

    def test_no_candidates_for_small_files(self):
        tiny_spot = {"path": "foo.py", "churn": 1, "lines": 100, "hot_score": 0.1,
                     "oversized_functions": []}
        cands = _candidates_from_hot_spots([tiny_spot])
        # Too small to qualify
        assert cands == []

    def test_no_duplicate_targets(self):
        same_path_spots = [
            {"path": "ouroboros/loop_runtime.py", "churn": 15, "lines": 1200,
             "hot_score": 0.95, "oversized_functions": []},
            {"path": "ouroboros/loop_runtime.py", "churn": 15, "lines": 1200,
             "hot_score": 0.95, "oversized_functions": []},
        ]
        cands = _candidates_from_hot_spots(same_path_spots)
        targets = [c["target"] for c in cands]
        assert len(targets) == len(set(targets))


# ── Test gap candidates ───────────────────────────────────────────────────────

class TestCandidatesFromTestGaps:
    def test_generates_test_coverage_kind(self):
        cands = _candidates_from_test_gaps(["budget_forecast", "captcha_solver"])
        assert all(c["kind"] == "test_coverage" for c in cands)

    def test_empty_list(self):
        assert _candidates_from_test_gaps([]) == []

    def test_target_includes_module_path(self):
        cands = _candidates_from_test_gaps(["budget_forecast"])
        assert "budget_forecast" in cands[0]["target"]

    def test_caps_at_six(self):
        many = [f"module_{i}" for i in range(20)]
        cands = _candidates_from_test_gaps(many)
        assert len(cands) <= 6


# ── Observability candidates ──────────────────────────────────────────────────

class TestCandidatesObservability:
    def test_generates_observability_kind(self):
        module_lines = {
            "ouroboros/loop_runtime.py": 1200,
            "ouroboros/context.py": 900,
        }
        spots = _sample_spots(2)
        cands = _candidates_observability(module_lines, spots)
        assert all(c["kind"] == "observability" for c in cands)

    def test_no_small_modules(self):
        module_lines = {"ouroboros/tiny.py": 50}
        cands = _candidates_observability(module_lines, [])
        assert cands == []


# ── Focus boost ───────────────────────────────────────────────────────────────

class TestFocusBoost:
    def test_boosts_matching_candidate(self):
        focus = {"goal": "improve memory search accuracy"}
        cands = [
            {"kind": "new_tool", "target": "ouroboros/tools/memory_search.py",
             "title": "Enhance memory search ranking", "rationale": ""},
            {"kind": "refactor", "target": "ouroboros/loop_runtime.py",
             "title": "Decompose _run_round", "rationale": ""},
        ]
        boosted = _focus_boost(cands, focus)
        # Memory candidate should have higher boost
        assert boosted[0]["_focus_boost"] > boosted[1]["_focus_boost"]

    def test_no_focus_no_boost(self):
        cands = [{"kind": "refactor", "target": "foo.py", "title": "Refactor", "rationale": ""}]
        result = _focus_boost(cands, {})
        assert all(c.get("_focus_boost", 0) == 0 for c in result)


# ── Ranking ───────────────────────────────────────────────────────────────────

class TestRankAndDeduplicate:
    def test_top_k_respected(self):
        cands = [
            {"kind": "fix_pattern", "target": f"file_{i}.py", "title": f"Fix {i}",
             "rationale": "", "signals": [], "_focus_boost": 0, "pattern_count": 10}
            for i in range(10)
        ]
        result = _rank_and_deduplicate(cands, top_k=3)
        assert len(result) <= 3

    def test_rank_numbers_assigned(self):
        cands = [
            {"kind": "fix_pattern", "target": "a.py", "title": "A", "rationale": "",
             "signals": [], "_focus_boost": 0, "pattern_count": 100},
        ]
        result = _rank_and_deduplicate(cands, top_k=5)
        assert result[0]["rank"] == 1

    def test_deduplicates_same_target(self):
        cands = [
            {"kind": "fix_pattern", "target": "same.py", "title": "Fix A", "rationale": "",
             "signals": [], "_focus_boost": 0, "pattern_count": 100},
            {"kind": "refactor", "target": "same.py", "title": "Refactor", "rationale": "",
             "signals": [], "_focus_boost": 0, "hot_score": 0.9},
        ]
        result = _rank_and_deduplicate(cands, top_k=5)
        targets = [c["target"] for c in result]
        assert len(targets) == len(set(targets))


# ── Format ────────────────────────────────────────────────────────────────────

class TestFormatText:
    def test_contains_rank_and_title(self):
        candidates = [
            {
                "rank": 1,
                "kind": "fix_pattern",
                "target": "ouroboros/tools/git.py",
                "title": "Harden commit/push timeout",
                "rationale": "200+ occurrences.",
                "signals": ["pattern_register"],
                "estimated_rounds": 15,
            }
        ]
        text = _format_text(candidates, {}, False)
        assert "1." in text
        assert "fix_pattern" in text
        assert "git.py" in text

    def test_empty_candidates_returns_message(self):
        text = _format_text([], {}, False)
        assert "No evolution candidates" in text

    def test_focus_shown_when_set(self):
        focus = {"goal": "Improve memory search", "cycles_completed": 2, "horizon_cycles": 5}
        text = _format_text([], focus, False)
        assert "memory search" in text.lower()


# ── Integration ───────────────────────────────────────────────────────────────

class TestEvolutionPlanIntegration:
    def test_returns_string(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        with (
            patch("ouroboros.tools.evolution_planner._REPO_DIR", str(tmp_path)),
            patch("ouroboros.tools.evolution_planner._DRIVE_ROOT", str(tmp_path)),
            patch("ouroboros.tools.evolution_planner._load_hot_spots", return_value={
                "spots": _sample_spots(3),
                "patterns": _sample_patterns(3),
                "oversized_modules": [("ouroboros/loop_runtime.py", 1200)],
                "oversized_functions": [],
                "module_lines": {"ouroboros/loop_runtime.py": 1200},
            }),
            patch("ouroboros.tools.evolution_planner._load_evolution_focus", return_value={}),
            patch("ouroboros.tools.evolution_planner._load_tools_without_tests", return_value=[]),
        ):
            result = _evolution_plan(ctx, top_k=5, format="text")
        assert isinstance(result, str)
        assert len(result) > 50

    def test_json_format_is_valid(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        with (
            patch("ouroboros.tools.evolution_planner._REPO_DIR", str(tmp_path)),
            patch("ouroboros.tools.evolution_planner._DRIVE_ROOT", str(tmp_path)),
            patch("ouroboros.tools.evolution_planner._load_hot_spots", return_value={
                "spots": _sample_spots(2),
                "patterns": _sample_patterns(2),
                "oversized_modules": [],
                "oversized_functions": [],
                "module_lines": {},
            }),
            patch("ouroboros.tools.evolution_planner._load_evolution_focus", return_value={}),
            patch("ouroboros.tools.evolution_planner._load_tools_without_tests", return_value=[]),
        ):
            result = _evolution_plan(ctx, top_k=3, format="json")
        data = json.loads(result)
        assert "candidates" in data
        assert "meta" in data
        assert data["meta"]["top_k"] == 3

    def test_top_k_respected(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        with (
            patch("ouroboros.tools.evolution_planner._REPO_DIR", str(tmp_path)),
            patch("ouroboros.tools.evolution_planner._DRIVE_ROOT", str(tmp_path)),
            patch("ouroboros.tools.evolution_planner._load_hot_spots", return_value={
                "spots": _sample_spots(3),
                "patterns": _sample_patterns(5),
                "oversized_modules": [],
                "oversized_functions": [],
                "module_lines": {"ouroboros/loop_runtime.py": 1200},
            }),
            patch("ouroboros.tools.evolution_planner._load_evolution_focus", return_value={}),
            patch("ouroboros.tools.evolution_planner._load_tools_without_tests",
                  return_value=["module_a", "module_b"]),
        ):
            result = _evolution_plan(ctx, top_k=2, format="json")
        data = json.loads(result)
        assert len(data["candidates"]) <= 2


# ── Registry ──────────────────────────────────────────────────────────────────

class TestGetTools:
    def test_returns_single_tool_entry(self):
        tools = get_tools()
        assert len(tools) == 1
        assert isinstance(tools[0], ToolEntry)
        assert tools[0].name == "evolution_plan"

    def test_schema_has_required_params(self):
        schema = get_tools()[0].schema
        props = schema["parameters"]["properties"]
        assert "top_k" in props
        assert "format" in props
        assert "focus_aligned" in props
        assert "days" in props
