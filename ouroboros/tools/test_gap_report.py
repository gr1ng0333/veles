"""test_gap_report — prioritized test-coverage gaps ranked by risk.

Combines five signals to answer "which untested functions need tests most urgently?":

  1. Coverage gap   — function has no test coverage (primary gate: +30)
  2. Complexity     — current cyclomatic complexity (high = harder without tests: +≤20)
  3. Trend          — rising complexity in recent commits (getting riskier: +8)
  4. Churn          — file changed frequently in last 30 days (+≤10)
  5. Module risk    — CRITICAL/HIGH/MEDIUM tier by path keywords (+15/10/5)

Risk score (0–83 theoretical max per function):
  +30  uncovered (base gate)
  +≤20 complexity × 2 (capped)
  +8   rising trend  |  +5  volatile trend
  +≤10 churn (capped at 5 commits = 10 pts)
  +15/10/5/0 module tier CRITICAL/HIGH/MEDIUM/LOW

Only uncovered functions appear in the ranked output.
Covered functions are counted in the summary only.

Usage:
    test_gap_report()                           # scan ouroboros/, top 15
    test_gap_report(path="ouroboros/tools/")    # narrower scope
    test_gap_report(top_k=10, commits=20)       # wider trend window
    test_gap_report(include_private=True)       # include _ functions
    test_gap_report(format="json")              # machine-readable
"""
from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

# ── Import signal helpers from sister tools ────────────────────────────────────
from ouroboros.tools.test_coverage_map import (
    _check_coverage_signals,
    _collect_py_files,
    _extract_functions,
    _find_test_files,
)
from ouroboros.tools.complexity_trend import (
    _extract_function_complexities,
    _git_log_shas,
    _git_show_file,
)
from ouroboros.tools.hot_spots import _git_churn

_REPO_DIR = pathlib.Path(os.environ.get("OUROBOROS_REPO_DIR", "/opt/veles"))

# ── Risk tier classification ───────────────────────────────────────────────────
_CRITICAL_KEYWORDS = frozenset({
    "agent", "loop", "context", "llm", "supervisor",
    "registry", "safety", "memory",
})
_HIGH_KEYWORDS = frozenset({
    "tools", "copilot_proxy", "codex_proxy",
    "loop_runtime", "loop_copilot",
})

_TIER_BONUS: Dict[str, int] = {
    "CRITICAL": 15,
    "HIGH": 10,
    "MEDIUM": 5,
    "LOW": 0,
}
_TREND_BONUS: Dict[str, int] = {
    "rising": 8,
    "volatile": 5,
    "falling": 0,
    "stable": 0,
    "": 0,
}
_TREND_ICONS: Dict[str, str] = {
    "rising": "🔺",
    "falling": "🔻",
    "volatile": "↕️ ",
    "stable": "  ",
    "": "  ",
}


def _classify_module_risk(rel_path: str) -> str:
    """Return CRITICAL/HIGH/MEDIUM/LOW for a file path."""
    parts = set(rel_path.replace("/", ".").replace(".py", "").split("."))
    if parts & _CRITICAL_KEYWORDS:
        return "CRITICAL"
    if parts & _HIGH_KEYWORDS:
        return "HIGH"
    return "MEDIUM"


def _trend_label(old_cx: int, new_cx: int) -> str:
    """Simple two-point trend: stable if |Δ| < 2, rising if Δ > 0, falling otherwise."""
    delta = new_cx - old_cx
    if abs(delta) < 2:
        return "stable"
    return "rising" if delta > 0 else "falling"


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class GapEntry:
    """A single test gap entry for one function."""
    file: str
    function: str
    class_name: str
    line: int
    complexity: int          # current cyclomatic complexity
    old_complexity: int      # complexity N commits ago (0 if no trend data)
    delta: int               # complexity change (positive = rising)
    trend: str               # rising / falling / stable / volatile / ""
    churn: int               # git commit count for file in churn window
    module_tier: str         # CRITICAL / HIGH / MEDIUM / LOW
    risk_score: float        # combined risk score
    reasons: List[str] = field(default_factory=list)


# ── Core analysis ──────────────────────────────────────────────────────────────

def _compute_gaps(
    repo_dir: pathlib.Path,
    scan_path: pathlib.Path,
    commits: int,
    include_private: bool,
) -> Tuple[List[GapEntry], Dict[str, Any]]:
    """
    Scan all Python files under scan_path and compute test gap data.
    Returns (gap_entries_all_sorted, stats_dict).
    """
    stats: Dict[str, Any] = {
        "files_scanned": 0,
        "total_public_functions": 0,
        "covered": 0,
        "uncovered": 0,
        "skipped_private": 0,
    }

    # ── 1. Git churn (file-level, 30-day window) ───────────────────────────
    churn_map: Dict[str, int] = _git_churn(repo_dir, days=30)

    # ── 2. Commit SHAs for trend analysis ──────────────────────────────────
    shas = _git_log_shas(repo_dir, min(commits, 30))
    sha_oldest: Optional[str] = shas[-1] if len(shas) >= 2 else None

    # ── 3. Collect source files (skip test files) ──────────────────────────
    source_files = _collect_py_files(scan_path)
    source_files = [f for f in source_files if not f.name.startswith("test_")]
    stats["files_scanned"] = len(source_files)

    gaps: List[GapEntry] = []

    for source_file in source_files:
        try:
            rel_path = str(source_file.relative_to(repo_dir))
        except ValueError:
            rel_path = str(source_file)

        # ── Coverage signals ───────────────────────────────────────────────
        functions = _extract_functions(source_file)
        test_files = _find_test_files(source_file, repo_dir)

        # ── Current complexity (from HEAD source) ──────────────────────────
        try:
            source_text = source_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        current_cx: Dict[str, int] = _extract_function_complexities(source_text)

        # ── Old complexity (only for files with recent churn) ──────────────
        old_cx_map: Dict[str, int] = {}
        if sha_oldest is not None and churn_map.get(rel_path, 0) > 0:
            old_source = _git_show_file(repo_dir, sha_oldest, rel_path)
            if old_source:
                old_cx_map = _extract_function_complexities(old_source)

        # ── Module risk tier (file-level, fixed for all functions) ─────────
        tier = _classify_module_risk(rel_path)
        tier_bonus = _TIER_BONUS.get(tier, 0)
        file_churn = churn_map.get(rel_path, 0)
        churn_bonus = min(file_churn * 2, 10)

        for func in functions:
            # Skip dunder methods — framework hooks
            if func.name.startswith("__") and func.name.endswith("__"):
                continue

            if func.is_private and not include_private:
                stats["skipped_private"] += 1
                continue

            stats["total_public_functions"] += 1

            covered, _signal = _check_coverage_signals(func, test_files)
            if covered:
                stats["covered"] += 1
                continue

            stats["uncovered"] += 1

            # Qualified name for complexity lookup (handle class methods)
            qual_name = (
                f"{func.class_name}.{func.name}" if func.class_name else func.name
            )
            cx_new = current_cx.get(qual_name, current_cx.get(func.name, 0))
            cx_old = old_cx_map.get(qual_name, old_cx_map.get(func.name, cx_new))
            delta = cx_new - cx_old
            trend = _trend_label(cx_old, cx_new) if old_cx_map else ""

            # ── Risk score ─────────────────────────────────────────────────
            score = 30.0                          # base: uncovered
            score += min(cx_new * 2, 20)          # complexity bonus (capped at 20)
            score += _TREND_BONUS.get(trend, 0)   # trend bonus
            score += churn_bonus                  # churn bonus
            score += tier_bonus                   # module tier bonus

            # ── Reasons (human-readable) ───────────────────────────────────
            reasons: List[str] = ["no test coverage"]
            if cx_new >= 10:
                reasons.append(f"high complexity ({cx_new})")
            elif cx_new >= 5:
                reasons.append(f"moderate complexity ({cx_new})")
            if trend == "rising":
                reasons.append(f"complexity rising (+{delta})")
            elif trend == "volatile":
                reasons.append("complexity volatile")
            if file_churn >= 5:
                reasons.append(f"high churn ({file_churn}× / 30d)")
            elif file_churn >= 2:
                reasons.append(f"churn {file_churn}× / 30d")
            if tier in ("CRITICAL", "HIGH"):
                reasons.append(f"{tier} module")

            gaps.append(GapEntry(
                file=rel_path,
                function=func.name,
                class_name=func.class_name,
                line=func.line,
                complexity=cx_new,
                old_complexity=cx_old,
                delta=delta,
                trend=trend,
                churn=file_churn,
                module_tier=tier,
                risk_score=score,
                reasons=reasons,
            ))

    gaps.sort(key=lambda g: (-g.risk_score, g.file, g.line))
    return gaps, stats


# ── Text formatter ─────────────────────────────────────────────────────────────

_TIER_SHORT: Dict[str, str] = {
    "CRITICAL": "CRIT",
    "HIGH": "HIGH",
    "MEDIUM": "MED ",
    "LOW": "LOW ",
}


def _format_text(
    gaps: List[GapEntry],
    stats: Dict[str, Any],
    top_k: int,
    path_label: str,
) -> str:
    lines: List[str] = []

    total = stats["total_public_functions"]
    covered = stats["covered"]
    uncovered = stats["uncovered"]
    pct = round(100 * covered / total, 1) if total else 0.0

    lines.append(f"## Test Gap Report — {path_label}")
    lines.append(
        f"   {stats['files_scanned']} files · "
        f"{total} public functions · "
        f"covered: {covered} ({pct}%) · "
        f"gaps: {uncovered}"
    )
    lines.append("")

    shown = gaps[:top_k]

    if not shown:
        lines.append("✅  No test gaps found — all public functions have coverage!")
        return "\n".join(lines)

    lines.append(f"Top {len(shown)} gaps by risk score:\n")
    lines.append(
        f"  {'#':>2}  {'Score':>5}  {'Function':<46}  "
        f"{'CX':>4}  {'Trend':<9}  Tier  Reasons"
    )
    lines.append("  " + "─" * 104)

    for i, g in enumerate(shown, 1):
        qual = f"{g.class_name}.{g.function}" if g.class_name else g.function
        display = f"{g.file}::{qual}"
        if len(display) > 45:
            short_file = "/".join(g.file.split("/")[-2:])
            display = f"{short_file}::{qual}"
        if len(display) > 45:
            display = display[:42] + "..."

        trend_icon = _TREND_ICONS.get(g.trend, "  ")
        trend_str = trend_icon + (g.trend if g.trend else "n/a")
        tier_str = _TIER_SHORT.get(g.module_tier, "    ")
        reasons_str = ", ".join(g.reasons[:3])

        lines.append(
            f"  {i:>2}.  {g.risk_score:>5.1f}  {display:<46}  "
            f"{g.complexity:>4}  {trend_str:<9}  {tier_str}  {reasons_str}"
        )

    if len(gaps) > top_k:
        lines.append(
            f"\n  … and {len(gaps) - top_k} more gaps "
            f"(increase top_k to see them)"
        )

    lines.append("")
    lines.append(
        "Score: +30 uncovered · +≤20 complexity×2 · "
        "+8 rising · +5 volatile · +≤10 churn · +15/10/5 CRIT/HIGH/MED"
    )
    return "\n".join(lines)


# ── Handler ────────────────────────────────────────────────────────────────────

def _test_gap_report(
    ctx: ToolContext,
    path: str = "ouroboros/",
    top_k: int = 15,
    commits: int = 10,
    include_private: bool = False,
    format: str = "text",
    _repo_dir: Optional[pathlib.Path] = None,
) -> Any:
    """Prioritized test-coverage gap report ranked by multi-signal risk score.

    Args:
        path: Directory or file to scan (relative to repo root). Default: 'ouroboros/'.
        top_k: Number of top gaps to return. Default: 15, max: 50.
        commits: Recent commits to analyse for complexity trend. Default: 10.
        include_private: Include _prefixed functions. Default: false.
        format: 'text' (default) or 'json'.

    Returns:
        Ranked list of uncovered functions with risk scores and actionable reasons.
    """
    repo_dir = (_repo_dir or _REPO_DIR).resolve()

    scan_root = repo_dir / path.rstrip("/")
    if not scan_root.exists():
        msg = f"Path not found: {path}"
        if format == "json":
            return {"result": json.dumps({"error": msg})}
        return {"result": msg}

    top_k = max(1, min(50, top_k))
    commits = max(2, min(30, commits))

    gaps, stats = _compute_gaps(repo_dir, scan_root, commits, include_private)

    if format == "json":
        return {
            "result": json.dumps(
                {
                    "stats": stats,
                    "gaps": [
                        {
                            "file": g.file,
                            "function": g.function,
                            "class": g.class_name,
                            "line": g.line,
                            "complexity": g.complexity,
                            "delta": g.delta,
                            "trend": g.trend,
                            "churn": g.churn,
                            "module_tier": g.module_tier,
                            "risk_score": g.risk_score,
                            "reasons": g.reasons,
                        }
                        for g in gaps[:top_k]
                    ],
                    "total_gaps": len(gaps),
                },
                indent=2,
            )
        }

    return {"result": _format_text(gaps, stats, top_k, path)}


# ── Tool registration ──────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="test_gap_report",
            schema={
                "name": "test_gap_report",
                "description": (
                    "Prioritized test-coverage gap report ranked by multi-signal risk score.\n\n"
                    "Combines five signals to answer 'which untested functions need tests most urgently?':\n"
                    "  1. Coverage gap — no test coverage (primary gate)\n"
                    "  2. Complexity — cyclomatic complexity of the uncovered function\n"
                    "  3. Trend — rising complexity in recent commits (getting riskier)\n"
                    "  4. Churn — file changed frequently in last 30 days\n"
                    "  5. Module risk — CRITICAL/HIGH/MEDIUM tier by path\n\n"
                    "Risk score = 30 (uncovered) + complexity×2 + trend bonus + churn + tier bonus.\n"
                    "Use at the start of a test-writing cycle to know where to focus first."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Directory or file to scan (relative to repo root). "
                                "Default: 'ouroboros/'."
                            ),
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Number of top gaps to return. Default: 15, max: 50.",
                        },
                        "commits": {
                            "type": "integer",
                            "description": (
                                "Recent commits to analyse for complexity trend. "
                                "Default: 10, max: 30."
                            ),
                        },
                        "include_private": {
                            "type": "boolean",
                            "description": (
                                "Include _prefixed private functions. Default: false."
                            ),
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format. Default: text.",
                        },
                    },
                    "required": [],
                },
            },
            handler=_test_gap_report,
        )
    ]
