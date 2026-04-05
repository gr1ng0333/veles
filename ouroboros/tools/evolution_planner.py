"""evolution_plan — Synthesize signals into ranked evolution candidates.

Takes output from multiple analysis signals and produces 3-5 concrete,
prioritized action candidates for the next evolution cycle — not raw file lists,
but actionable growth moves with type, target, rationale, and estimated impact.

Signals aggregated:
  1. Hot spots (git churn + complexity from hot_spots tool)
  2. Pattern register (recurring error classes from knowledge/patterns.md)
  3. Evolution focus (cross-cycle strategic goal if set)
  4. Gap analysis (what's missing: tools, tests, observability)
  5. Module health (oversized modules that are candidates for decomposition)

Output:
  A ranked list of candidates, each with:
    - rank: 1-N
    - kind: "new_tool" | "refactor" | "fix_pattern" | "observability" | "test_coverage"
    - target: the specific file/function/class to work on
    - title: one-line description of the action
    - rationale: 2-3 sentences explaining why this is the highest leverage move
    - signals: which signals triggered this candidate
    - estimated_rounds: rough estimate of implementation effort

Usage:
    evolution_plan()                      # default: top 5 candidates
    evolution_plan(top_k=3)               # top 3 only
    evolution_plan(format="json")         # structured JSON output
    evolution_plan(focus_aligned=True)    # only candidates aligned with evolution focus
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import subprocess
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_REPO_DIR = os.environ.get("REPO_DIR", "/opt/veles")
_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")

# ── Kind definitions & priorities ─────────────────────────────────────────────

# Higher priority_base = more likely to rank high (before signal boosting)
_KIND_PRIORITY: Dict[str, int] = {
    "new_tool": 100,
    "fix_pattern": 90,
    "observability": 80,
    "refactor": 70,
    "test_coverage": 60,
}

# Max effort in rounds per kind (used to filter by focus_aligned)
_KIND_EFFORT: Dict[str, int] = {
    "new_tool": 25,
    "fix_pattern": 15,
    "observability": 20,
    "refactor": 30,
    "test_coverage": 10,
}

# ── Signal loaders ─────────────────────────────────────────────────────────────

def _load_hot_spots(repo_dir: pathlib.Path, drive_root: pathlib.Path, days: int = 7) -> Dict[str, Any]:
    """Load hot spot analysis. Returns raw data dict."""
    try:
        from ouroboros.tools.hot_spots import (
            _git_churn, _scan_complexity, _parse_pattern_register, _compute_hot_spots
        )
        churn = _git_churn(repo_dir, days=days)
        complexity = _scan_complexity(repo_dir)
        patterns = _parse_pattern_register(drive_root)
        spots = _compute_hot_spots(churn, complexity, top_k=20)
        return {
            "spots": spots,
            "patterns": patterns,
            "oversized_modules": complexity.get("oversized_modules", []),
            "oversized_functions": complexity.get("oversized_functions", []),
            "module_lines": complexity.get("module_lines", {}),
        }
    except Exception as exc:
        log.warning("evolution_plan: failed to load hot_spots data: %s", exc)
        return {
            "spots": [],
            "patterns": [],
            "oversized_modules": [],
            "oversized_functions": [],
            "module_lines": {},
        }


def _load_evolution_focus(drive_root: pathlib.Path) -> Dict[str, Any]:
    """Load current evolution focus if set."""
    try:
        from ouroboros.tools.evolution_focus import load_evolution_focus
        return load_evolution_focus(drive_root)
    except Exception:
        return {}


def _load_tool_set(repo_dir: pathlib.Path) -> List[str]:
    """Return list of currently registered tool names."""
    try:
        from ouroboros.tools.registry import ToolRegistry
        registry = ToolRegistry(repo_dir=repo_dir, drive_root=pathlib.Path(_DRIVE_ROOT))
        return registry.available_tools()
    except Exception:
        return []


def _load_test_files(repo_dir: pathlib.Path) -> List[str]:
    """Return list of test file basenames."""
    tests_dir = repo_dir / "tests"
    if not tests_dir.is_dir():
        return []
    return [f.name for f in tests_dir.glob("test_*.py")]


def _load_tools_without_tests(repo_dir: pathlib.Path) -> List[str]:
    """Find tool modules that have no corresponding test file."""
    tools_dir = repo_dir / "ouroboros" / "tools"
    if not tools_dir.is_dir():
        return []
    test_files = set(_load_test_files(repo_dir))
    untested: List[str] = []
    for py_file in sorted(tools_dir.glob("*.py")):
        name = py_file.name
        if name.startswith("_") or name == "registry.py":
            continue
        module_name = name[:-3]  # strip .py
        expected_test = f"test_{module_name}.py"
        if expected_test not in test_files:
            untested.append(module_name)
    return untested


# ── Candidate generation ───────────────────────────────────────────────────────

def _candidates_from_patterns(patterns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Generate fix_pattern candidates from top recurring error classes."""
    candidates: List[Dict[str, Any]] = []
    for p in patterns[:5]:
        cls = p.get("class", "unknown")
        count = p.get("count", 1)
        rc = p.get("root_cause", "")

        # Map pattern class to specific action
        if "timeout" in cls.lower() or "commit/push" in cls.lower():
            target = "ouroboros/tools/git.py"
            title = f"Harden commit/push timeout handling ({count}+ occurrences)"
            rationale = (
                f"'{cls}' has triggered {count}+ times. "
                f"Root: {rc[:120] if rc else 'timeout in git operations'}. "
                "Fixing this class eliminates the most common evolution blocker."
            )
        elif "smoke" in cls.lower() or "pre-push" in cls.lower():
            target = "tests/test_smoke.py"
            title = f"Reduce pre-push test flakiness ({count}+ occurrences)"
            rationale = (
                f"'{cls}' blocked {count}+ commits. "
                "Smoke tests must be fast (<5s) and deterministic. "
                "Audit test_smoke.py for slow or flaky assertions."
            )
        elif "ssh" in cls.lower() or "remote" in cls.lower():
            target = "ouroboros/tools/ssh_targets.py"
            title = f"Improve SSH error handling ({count}+ occurrences)"
            rationale = (
                f"SSH/remote errors occurred {count}+ times. "
                f"Root: {rc[:120] if rc else 'remote execution failures'}. "
                "Strengthen error capture and retry logic."
            )
        elif "copilot 500" in cls.lower() or "http error" in cls.lower():
            target = "ouroboros/loop_copilot.py"
            title = f"Improve Copilot HTTP error recovery ({count}+ occurrences)"
            rationale = (
                f"Copilot 500 errors triggered {count}+ times. "
                "Better circuit-breaker logic around HTTP 500/502/503 "
                "would prevent cascading failures."
            )
        else:
            target = "ouroboros/"
            title = f"Address pattern: {cls} ({count}+ occurrences)"
            rationale = (
                f"'{cls}' is a recurring error class with {count}+ occurrences. "
                f"Root cause: {rc[:120] if rc else 'see patterns.md'}."
            )

        candidates.append({
            "kind": "fix_pattern",
            "target": target,
            "title": title,
            "rationale": rationale,
            "signals": ["pattern_register"],
            "pattern_count": count,
            "estimated_rounds": _KIND_EFFORT["fix_pattern"],
        })
    return candidates


def _candidates_from_hot_spots(spots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Generate refactor candidates from oversized hot spot modules."""
    candidates: List[Dict[str, Any]] = []
    seen_paths: set = set()

    for spot in spots[:10]:
        path = spot.get("path", "")
        lines = spot.get("lines", 0)
        churn = spot.get("churn", 0)
        hot_score = spot.get("hot_score", 0.0)
        funcs = spot.get("oversized_functions", [])

        if path in seen_paths:
            continue
        seen_paths.add(path)

        # Only flag if truly oversized or high-churn
        if lines < 800 and churn < 5 and hot_score < 0.5:
            continue

        if funcs:
            # Oversized function is a more specific target
            worst = funcs[0]
            fn_name = worst.get("name", "?")
            fn_lines = worst.get("length", 0)
            title = f"Decompose {fn_name}() in {path} ({fn_lines} lines)"
            rationale = (
                f"{path} has a {fn_lines}-line function '{fn_name}' "
                f"(churn={churn}x, hot_score={hot_score:.2f}). "
                "Long functions are hard to reason about and hide bugs. "
                "Extract into focused helpers."
            )
            target = path
        elif lines > 1000:
            title = f"Split oversized module {path} ({lines} lines)"
            rationale = (
                f"{path} has {lines} lines — exceeds the 1000-line module budget (BIBLE P5). "
                f"Churn={churn}x makes every edit riskier. "
                "Decompose into focused sub-modules."
            )
            target = path
        else:
            title = f"Reduce complexity in hot module {path}"
            rationale = (
                f"{path} is a high-churn module (churn={churn}x, score={hot_score:.2f}). "
                "Review for dead code, parameter sprawl, or logic duplication."
            )
            target = path

        candidates.append({
            "kind": "refactor",
            "target": target,
            "title": title,
            "rationale": rationale,
            "signals": ["hot_spots"],
            "hot_score": hot_score,
            "estimated_rounds": _KIND_EFFORT["refactor"],
        })

    return candidates


def _candidates_from_test_gaps(untested_modules: List[str]) -> List[Dict[str, Any]]:
    """Generate test_coverage candidates for tool modules lacking tests."""
    candidates: List[Dict[str, Any]] = []
    # Prioritize smaller, newer-looking modules
    for module in untested_modules[:6]:
        target = f"ouroboros/tools/{module}.py"
        title = f"Add tests for {module} tool"
        rationale = (
            f"ouroboros/tools/{module}.py has no test file. "
            "Untested tools are invisible to the smoke gate, "
            "meaning regressions in them go undetected until runtime."
        )
        candidates.append({
            "kind": "test_coverage",
            "target": target,
            "title": title,
            "rationale": rationale,
            "signals": ["test_gap"],
            "estimated_rounds": _KIND_EFFORT["test_coverage"],
        })
    return candidates


def _candidates_observability(
    module_lines: Dict[str, int],
    spots: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Suggest observability improvements for the most opaque modules."""
    candidates: List[Dict[str, Any]] = []

    # Look for large modules with no structured logging
    large_modules = [(p, l) for p, l in sorted(module_lines.items(), key=lambda x: -x[1])
                     if l > 500 and p.endswith(".py")]

    for path, lines in large_modules[:3]:
        # Check if any spot flagged this path
        is_hot = any(s["path"] == path for s in spots)
        if not is_hot:
            continue
        candidates.append({
            "kind": "observability",
            "target": path,
            "title": f"Add structured logging to {path}",
            "rationale": (
                f"{path} ({lines} lines) is both large and high-churn. "
                "Adding structured log points at key decision branches "
                "makes debugging evolution failures 2-5x faster."
            ),
            "signals": ["hot_spots", "module_size"],
            "estimated_rounds": _KIND_EFFORT["observability"],
        })
        if len(candidates) >= 2:
            break
    return candidates


def _focus_boost(
    candidates: List[Dict[str, Any]],
    focus: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Boost score of candidates that align with the active evolution focus."""
    if not focus:
        return candidates
    goal = focus.get("goal", "").lower()
    if not goal:
        return candidates

    # Keywords from goal → match against candidate text
    goal_keywords = set(re.findall(r"\w+", goal))
    goal_keywords -= {"the", "a", "an", "in", "to", "for", "and", "or", "of", "with"}

    for c in candidates:
        text = (c.get("title", "") + " " + c.get("rationale", "") + " " + c.get("target", "")).lower()
        matched = sum(1 for kw in goal_keywords if kw in text)
        # Boost by 20 points per matched keyword (max 60)
        c["_focus_boost"] = min(60, matched * 20)
    return candidates


# ── Ranking ────────────────────────────────────────────────────────────────────

def _score_candidate(c: Dict[str, Any]) -> float:
    """Compute final ranking score for a candidate."""
    base = _KIND_PRIORITY.get(c["kind"], 50)
    # Pattern count boosts fix_pattern candidates
    base += min(30, c.get("pattern_count", 0) // 5)
    # Hot score boosts refactor/observability
    base += c.get("hot_score", 0.0) * 20
    # Focus alignment boost
    base += c.get("_focus_boost", 0)
    return base


def _rank_and_deduplicate(candidates: List[Dict[str, Any]], top_k: int) -> List[Dict[str, Any]]:
    """Score, deduplicate by target, and return top-k."""
    # Add _focus_boost default
    for c in candidates:
        if "_focus_boost" not in c:
            c["_focus_boost"] = 0

    # Sort by score descending
    scored = sorted(candidates, key=_score_candidate, reverse=True)

    # Deduplicate: only one candidate per target file
    seen_targets: set = set()
    deduplicated: List[Dict[str, Any]] = []
    for c in scored:
        target = c.get("target", "")
        if target and target in seen_targets:
            continue
        seen_targets.add(target)
        deduplicated.append(c)
        if len(deduplicated) >= top_k:
            break

    # Add rank numbers
    for i, c in enumerate(deduplicated, 1):
        c["rank"] = i
        # Clean up internal fields
        c.pop("_focus_boost", None)
        c.pop("pattern_count", None)

    return deduplicated


# ── Formatting ─────────────────────────────────────────────────────────────────

_KIND_EMOJI = {
    "new_tool": "🔧",
    "fix_pattern": "🩹",
    "observability": "🔍",
    "refactor": "✂️",
    "test_coverage": "🧪",
}


def _format_text(
    candidates: List[Dict[str, Any]],
    focus: Dict[str, Any],
    focus_aligned_only: bool,
) -> str:
    lines: List[str] = []

    if focus:
        goal = focus.get("goal", "")
        done = focus.get("cycles_completed", 0)
        horizon = focus.get("horizon_cycles", "?")
        lines.append(f"## Evolution Focus: {goal} [{done}/{horizon} cycles]\n")

    if focus_aligned_only:
        lines.append("*(showing focus-aligned candidates only)*\n")

    if not candidates:
        lines.append("No evolution candidates found. The codebase may be in excellent shape,")
        lines.append("or the signal sources (hot_spots, patterns) returned no data.")
        return "\n".join(lines)

    lines.append(f"## Evolution Candidates (top {len(candidates)})\n")

    for c in candidates:
        rank = c["rank"]
        kind = c["kind"]
        emoji = _KIND_EMOJI.get(kind, "•")
        title = c["title"]
        target = c["target"]
        rationale = c["rationale"]
        signals = ", ".join(c.get("signals", []))
        effort = c.get("estimated_rounds", "?")

        lines.append(f"### {rank}. {emoji} [{kind}] {title}")
        lines.append(f"   **Target:** `{target}`")
        lines.append(f"   **Rationale:** {rationale}")
        lines.append(f"   **Signals:** {signals}  |  **Est. rounds:** ~{effort}")
        lines.append("")

    return "\n".join(lines)


# ── Main entrypoint ─────────────────────────────────────────────────────────────

def _evolution_plan(
    ctx: ToolContext,
    top_k: int = 5,
    format: str = "text",
    focus_aligned: bool = False,
    days: int = 7,
) -> str:
    """Generate ranked evolution candidates by synthesizing all analysis signals."""
    repo_dir = pathlib.Path(_REPO_DIR)
    drive_root = pathlib.Path(_DRIVE_ROOT)

    top_k = max(1, min(10, top_k))

    # ── Load signals ──────────────────────────────────────────────────────────
    hot_data = _load_hot_spots(repo_dir, drive_root, days=days)
    focus = _load_evolution_focus(drive_root)
    untested_modules = _load_tools_without_tests(repo_dir)

    spots = hot_data["spots"]
    patterns = hot_data["patterns"]
    oversized_modules = hot_data["oversized_modules"]
    module_lines = hot_data["module_lines"]

    # ── Generate candidate pools ──────────────────────────────────────────────
    all_candidates: List[Dict[str, Any]] = []

    # Pattern-based fixes (highest priority if many occurrences)
    all_candidates.extend(_candidates_from_patterns(patterns))

    # Refactor candidates from hot spots
    all_candidates.extend(_candidates_from_hot_spots(spots))

    # Test coverage gaps
    all_candidates.extend(_candidates_from_test_gaps(untested_modules))

    # Observability improvements for hot+large modules
    all_candidates.extend(_candidates_observability(module_lines, spots))

    # ── Apply focus boost ──────────────────────────────────────────────────────
    all_candidates = _focus_boost(all_candidates, focus)

    # ── Filter if focus_aligned ────────────────────────────────────────────────
    if focus_aligned and focus:
        all_candidates = [c for c in all_candidates if c.get("_focus_boost", 0) > 0]

    # ── Rank ──────────────────────────────────────────────────────────────────
    ranked = _rank_and_deduplicate(all_candidates, top_k=top_k)

    # ── Output ────────────────────────────────────────────────────────────────
    if format == "json":
        return json.dumps(
            {
                "candidates": ranked,
                "focus": focus,
                "meta": {
                    "top_k": top_k,
                    "total_candidates_before_dedup": len(all_candidates),
                    "hot_spots_analyzed": len(spots),
                    "patterns_analyzed": len(patterns),
                    "untested_modules": len(untested_modules),
                    "days": days,
                },
            },
            ensure_ascii=False,
            indent=2,
        )

    return _format_text(ranked, focus, focus_aligned_only=focus_aligned)


# ── Tool registration ──────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="evolution_plan",
            schema={
                "name": "evolution_plan",
                "description": (
                    "Synthesize all analysis signals into ranked evolution candidates.\n\n"
                    "Combines hot_spots (churn + complexity), pattern register "
                    "(recurring error classes), evolution focus (strategic goal), "
                    "and test coverage gaps into 3-5 concrete action items — "
                    "each with a type, target file, rationale, and effort estimate.\n\n"
                    "Use this at the START of an evolution task instead of manually "
                    "scanning code to decide what to work on.\n\n"
                    "Parameters:\n"
                    "  - top_k: number of candidates to return (default 5, max 10)\n"
                    "  - format: 'text' (default) or 'json' for structured output\n"
                    "  - focus_aligned: if True, only show candidates aligned with evolution focus\n"
                    "  - days: git churn window in days (default 7)"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "top_k": {
                            "type": "integer",
                            "description": "Number of candidates to return (default 5, max 10).",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format (default 'text').",
                        },
                        "focus_aligned": {
                            "type": "boolean",
                            "description": (
                                "If true, only return candidates aligned with the "
                                "active evolution focus goal (default false)."
                            ),
                        },
                        "days": {
                            "type": "integer",
                            "description": "Git churn window in days (default 7).",
                        },
                    },
                    "required": [],
                },
            },
            handler=_evolution_plan,
        )
    ]
