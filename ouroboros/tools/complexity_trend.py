"""complexity_trend — cyclomatic complexity trends over git history.

Identifies functions/methods whose complexity is *changing* across the last
N commits.  Answers "what is getting harder to maintain?" rather than just
"what is complex right now?" (that's tech_debt's job).

Trend categories:
  rising    — complexity grew by >= min_delta (oldest → newest commit in window)
  falling   — complexity dropped by >= min_delta (simplification — good!)
  volatile  — swings in both directions, variance > delta (unstable code)
  stable    — high but unchanging (tech_debt reports these)

Output per function:
  - File and qualified name
  - Complexity at oldest and newest observed commit in the window
  - Delta (Δ = newest − oldest; positive = getting worse)
  - Peak complexity seen anywhere in the window
  - Trend label

The analysis only scans files that actually changed between the oldest and
newest commit in the window, so it stays fast even with commits=20+.

Usage:
    complexity_trend()                            # last 10 commits, all trends
    complexity_trend(commits=20)                  # wider window
    complexity_trend(trend="rising")              # only deteriorating functions
    complexity_trend(path="ouroboros/tools/")     # limit scope
    complexity_trend(min_delta=5)                 # bigger signal only
    complexity_trend(format="json")               # machine-readable
"""
from __future__ import annotations

import ast
import json
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

_REPO_DIR = Path("/opt/veles")

_SKIP_PREFIXES = ("tests/", "build/", "dist/", "__pycache__")
_SCAN_PREFIXES = ("ouroboros/", "supervisor/")

# ── Cyclomatic complexity ──────────────────────────────────────────────────────

def _cyclomatic(node: ast.AST) -> int:
    """Estimate cyclomatic complexity of an AST subtree (branch count)."""
    count = 0
    for n in ast.walk(node):
        if isinstance(n, (
            ast.If, ast.For, ast.While, ast.With,
            ast.Try, ast.ExceptHandler, ast.Assert,
            ast.comprehension,
        )):
            count += 1
        elif isinstance(n, ast.BoolOp):
            count += len(n.values) - 1
    return count


# ── AST extraction ─────────────────────────────────────────────────────────────

def _extract_function_complexities(source: str) -> Dict[str, int]:
    """Return {qualified_name: cyclomatic_complexity} for every function in source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    # Build (start, end, class_name) for every class in the file
    class_ranges: List[Tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            end = getattr(node, "end_lineno", node.lineno)
            class_ranges.append((node.lineno, end, node.name))

    def _parent_class(lineno: int) -> Optional[str]:
        for start, end, name in class_ranges:
            if start <= lineno <= end:
                return name
        return None

    result: Dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            cx = _cyclomatic(node)
            parent = _parent_class(node.lineno)
            full_name = f"{parent}.{node.name}" if parent else node.name
            # Keep the first occurrence if somehow duplicated (shouldn't happen)
            if full_name not in result:
                result[full_name] = cx

    return result


# ── Git helpers ────────────────────────────────────────────────────────────────

def _git_log_shas(repo_dir: Path, n: int) -> List[str]:
    """Return last N commit SHAs, newest first."""
    r = subprocess.run(
        ["git", "log", "--format=%H", f"-{n}"],
        capture_output=True, text=True, cwd=str(repo_dir),
    )
    if r.returncode != 0:
        return []
    return [s.strip() for s in r.stdout.splitlines() if s.strip()]


def _git_changed_files(repo_dir: Path, sha_old: str, sha_new: str) -> List[str]:
    """Return .py files changed between sha_old and sha_new."""
    r = subprocess.run(
        ["git", "diff", "--name-only", sha_old, sha_new, "--", "*.py"],
        capture_output=True, text=True, cwd=str(repo_dir),
    )
    if r.returncode != 0:
        return []
    return [p.strip() for p in r.stdout.splitlines() if p.strip().endswith(".py")]


def _git_show_file(repo_dir: Path, sha: str, path: str) -> Optional[str]:
    """Return file content at given commit SHA; None if absent."""
    r = subprocess.run(
        ["git", "show", f"{sha}:{path}"],
        capture_output=True, text=True, cwd=str(repo_dir),
    )
    return r.stdout if r.returncode == 0 else None


def _git_short_sha(repo_dir: Path, sha: str) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "--short", sha],
        capture_output=True, text=True, cwd=str(repo_dir),
    )
    return r.stdout.strip() if r.returncode == 0 else sha[:8]


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class FunctionTrend:
    """Complexity history for a single function across the commit window."""
    file: str
    function: str
    history: List[int] = field(default_factory=list)  # complexity per commit, oldest→newest

    # ── derived properties ──

    @property
    def oldest(self) -> int:
        return self.history[0] if self.history else 0

    @property
    def newest(self) -> int:
        return self.history[-1] if self.history else 0

    @property
    def peak(self) -> int:
        return max(self.history) if self.history else 0

    @property
    def trough(self) -> int:
        return min(self.history) if self.history else 0

    @property
    def delta(self) -> int:
        """newest − oldest.  Positive = getting more complex."""
        if len(self.history) < 2:
            return 0
        return self.newest - self.oldest

    @property
    def trend_label(self) -> str:
        swing = self.peak - self.trough
        d = abs(self.delta)
        # Volatile check first: large swing even if net delta is small
        if swing >= 5 and swing > max(d, 3) * 1.5:
            return "volatile"
        if d < 2:
            return "stable"
        if self.delta > 0:
            return "rising"
        return "falling"


# ── Core analysis ──────────────────────────────────────────────────────────────

def _analyze_trends(
    repo_dir: Path,
    shas: List[str],       # newest first
    path_filter: Optional[str],
) -> List[FunctionTrend]:
    """
    Scan files changed between oldest and newest commit and track per-function
    cyclomatic complexity at every commit in the window.

    shas is ordered newest→oldest.  History arrays are built oldest→newest.
    """
    if len(shas) < 2:
        return []

    sha_newest = shas[0]
    sha_oldest = shas[-1]

    # Only scan files that actually changed in this window
    changed_files = _git_changed_files(repo_dir, sha_oldest, sha_newest)

    if path_filter:
        changed_files = [f for f in changed_files if path_filter in f]

    # Restrict to source (not tests, not build artifacts)
    changed_files = [
        f for f in changed_files
        if any(f.startswith(p) for p in _SCAN_PREFIXES)
        and not any(f.startswith(p) for p in _SKIP_PREFIXES)
    ]

    if not changed_files:
        return []

    # shas is newest-first; iterate oldest-first to build history chronologically
    shas_oldest_first = list(reversed(shas))

    # (filepath, func_name) → list of (commit_index, complexity)
    tracking: Dict[Tuple[str, str], List[Tuple[int, int]]] = defaultdict(list)

    for commit_idx, sha in enumerate(shas_oldest_first):
        for filepath in changed_files:
            source = _git_show_file(repo_dir, sha, filepath)
            if source is None:
                continue
            complexities = _extract_function_complexities(source)
            for func_name, cx in complexities.items():
                tracking[(filepath, func_name)].append((commit_idx, cx))

    total_commits = len(shas_oldest_first)
    trends: List[FunctionTrend] = []

    for (filepath, func_name), samples in tracking.items():
        if len(samples) < 2:
            continue  # only seen in one commit — no trend signal

        commit_map: Dict[int, int] = dict(samples)
        first_idx = min(commit_map)
        last_idx = max(commit_map)

        # Build dense history from first to last seen, filling gaps with
        # last known complexity (function didn't change in that commit)
        history: List[int] = []
        last_val = commit_map[first_idx]
        for i in range(first_idx, last_idx + 1):
            if i in commit_map:
                last_val = commit_map[i]
            history.append(last_val)

        trends.append(FunctionTrend(file=filepath, function=func_name, history=history))

    return trends


# ── Filtering / sorting ────────────────────────────────────────────────────────

_TREND_PRIORITY = {"rising": 4, "volatile": 3, "falling": 2, "stable": 1}


def _filter_trends(
    trends: List[FunctionTrend],
    min_delta: int,
    trend_filter: Optional[str],
    min_complexity: int,
) -> List[FunctionTrend]:
    out: List[FunctionTrend] = []
    for t in trends:
        if abs(t.delta) < min_delta:
            continue
        if trend_filter and t.trend_label != trend_filter:
            continue
        if t.peak < min_complexity:
            continue
        out.append(t)

    out.sort(
        key=lambda t: (
            -_TREND_PRIORITY.get(t.trend_label, 0),
            -abs(t.delta),
            -t.newest,
            t.file,
            t.function,
        )
    )
    return out


# ── Text formatter ─────────────────────────────────────────────────────────────

_TREND_ICONS = {
    "rising":   "🔺",
    "falling":  "🔻",
    "volatile": "↕️ ",
    "stable":   "  ",
}


def _format_text(
    trends: List[FunctionTrend],
    shas: List[str],
    short_oldest: str,
    short_newest: str,
    total_tracked: int,
) -> str:
    lines: List[str] = []

    n_rising = sum(1 for t in trends if t.trend_label == "rising")
    n_falling = sum(1 for t in trends if t.trend_label == "falling")
    n_volatile = sum(1 for t in trends if t.trend_label == "volatile")

    lines.append(
        f"## Complexity Trend  {short_oldest} → {short_newest}  ({len(shas)} commits)"
    )
    lines.append(
        f"   {total_tracked} functions tracked  |  "
        f"🔺 {n_rising} rising  🔻 {n_falling} falling  ↕️  {n_volatile} volatile"
    )
    lines.append("")

    if not trends:
        lines.append("No significant complexity changes detected in this window.")
        return "\n".join(lines)

    lines.append(f"{'Function':<52} {'Old':>4} {'New':>4} {'Δ':>5}  Peak  Trend")
    lines.append("─" * 82)

    for t in trends:
        icon = _TREND_ICONS.get(t.trend_label, "")
        delta_str = f"+{t.delta}" if t.delta > 0 else str(t.delta)

        display = f"{t.file}::{t.function}"
        if len(display) > 51:
            short_file = "/".join(t.file.split("/")[-2:])
            display = f"{short_file}::{t.function}"
        if len(display) > 51:
            display = display[:48] + "..."

        lines.append(
            f"{display:<52} {t.oldest:>4} {t.newest:>4} {delta_str:>5}  {t.peak:>4}  "
            f"{icon}{t.trend_label}"
        )

    lines.append("")
    lines.append(
        "Old/New = cyclomatic complexity at first/last seen commit  |  "
        "Δ = New−Old (+ = worse)"
    )
    return "\n".join(lines)


# ── Handler ────────────────────────────────────────────────────────────────────

def _complexity_trend(
    ctx: ToolContext,
    commits: int = 10,
    path: str = "",
    min_delta: int = 3,
    trend: str = "",
    min_complexity: int = 3,
    format: str = "text",
    _repo_dir: Optional[Path] = None,
) -> Any:
    """Track cyclomatic complexity trends across recent git commits.

    Args:
        commits: Number of recent commits to analyse (default 10, max 50).
        path: Optional path substring filter (e.g. 'ouroboros/tools/').
        min_delta: Minimum |Δ| to report (default 3).
        trend: Filter by label — 'rising', 'falling', 'volatile' or '' for all.
        min_complexity: Minimum peak complexity to include (default 3).
        format: 'text' (default) or 'json'.

    Returns:
        Report of functions with notable complexity changes.
    """
    repo_dir = (_repo_dir or _REPO_DIR).resolve()

    n = min(max(commits, 2), 50)

    shas = _git_log_shas(repo_dir, n)
    if len(shas) < 2:
        return {"result": "Not enough commits to compute trend (need ≥ 2)."}

    short_oldest = _git_short_sha(repo_dir, shas[-1])
    short_newest = _git_short_sha(repo_dir, shas[0])

    all_trends = _analyze_trends(repo_dir, shas, path or None)
    total_tracked = len(all_trends)

    filtered = _filter_trends(
        all_trends,
        min_delta=min_delta,
        trend_filter=trend or None,
        min_complexity=min_complexity,
    )

    if format == "json":
        data = {
            "commits": len(shas),
            "sha_oldest": short_oldest,
            "sha_newest": short_newest,
            "total_tracked": total_tracked,
            "functions": [
                {
                    "file": t.file,
                    "function": t.function,
                    "oldest": t.oldest,
                    "newest": t.newest,
                    "peak": t.peak,
                    "delta": t.delta,
                    "trend": t.trend_label,
                    "history": t.history,
                }
                for t in filtered
            ],
        }
        return {"result": json.dumps(data, indent=2)}

    return {"result": _format_text(filtered, shas, short_oldest, short_newest, total_tracked)}


# ── Tool registration ─────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="complexity_trend",
            schema={
                "name": "complexity_trend",
                "description": (
                    "Track cyclomatic complexity trends over recent git commits. "
                    "Shows which functions are getting more complex (rising), "
                    "being simplified (falling), or swinging erratically (volatile). "
                    "Answers 'what is getting harder to maintain?' to guide refactoring. "
                    "Complements tech_debt (which shows current state) with direction."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "commits": {
                            "type": "integer",
                            "description": (
                                "Number of recent commits to analyse (default 10, max 50)."
                            ),
                        },
                        "path": {
                            "type": "string",
                            "description": (
                                "Optional path substring filter "
                                "(e.g. 'ouroboros/tools/' or 'supervisor/')."
                            ),
                        },
                        "min_delta": {
                            "type": "integer",
                            "description": (
                                "Minimum absolute complexity change to report (default 3)."
                            ),
                        },
                        "trend": {
                            "type": "string",
                            "enum": ["rising", "falling", "volatile", ""],
                            "description": (
                                "Filter by trend type. Leave empty for all trends."
                            ),
                        },
                        "min_complexity": {
                            "type": "integer",
                            "description": (
                                "Minimum peak complexity to include (default 3, "
                                "avoids trivial noise)."
                            ),
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format (default: text).",
                        },
                    },
                    "required": [],
                },
            },
            handler=lambda ctx, **kw: _complexity_trend(ctx, **kw),
            is_code_tool=True,
        )
    ]
