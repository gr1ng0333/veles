"""hot_spots — Code hot-spot analysis for directed evolution targeting.

Aggregates three independent signals:
  1. Git churn  — files changed most often in the last N days (high churn = risk + attention)
  2. Complexity — oversized modules (>1000 lines) and oversized functions (>150 lines)
  3. Pattern register — recurring error classes from task_reflections (KB patterns.md)

Combines them into a ranked list of "hot spots" — concrete files / functions
that are simultaneously risky, complex, and historically error-prone.

Why this exists:
  Every evolution task used to start with 5-10 rounds of "code archaeology" —
  reading random files to figure out where to dig. hot_spots answers that
  question immediately, cutting wasted rounds.

Usage:
    hot_spots()                       # default: 7d churn, top 10 spots
    hot_spots(days=14, top_k=5)       # 14-day churn window
    hot_spots(format="json")          # structured output
"""

from __future__ import annotations

import json
import logging
import math
import os
import pathlib
import re
import subprocess
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

_REPO_DIR = os.environ.get("REPO_DIR", "/opt/veles")
_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")

# ── Configuration ─────────────────────────────────────────────────────────────

# Files to skip — they're always churning due to release mechanics, not real risk
_CHURN_SKIP_PATTERNS = frozenset([
    "VERSION",
    "README.md",
    "pyproject.toml",
    "CHANGELOG.md",
])

# Directories to skip entirely
_SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", "node_modules", ".venv"}


# ── Signal 1: Git churn ───────────────────────────────────────────────────────

def _git_churn(repo_dir: pathlib.Path, days: int = 7) -> Dict[str, int]:
    """Count how many commits touched each file in the last N days.

    Returns {relative_path: commit_count}.
    Skips VERSION/README/pyproject (release mechanics, not real churn).
    Skips non-Python files beyond core ones.
    """
    try:
        result = subprocess.run(
            [
                "git", "log",
                f"--since={days} days ago",
                "--format=",
                "--name-only",
            ],
            capture_output=True,
            text=True,
            cwd=str(repo_dir),
            timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("hot_spots: git log failed: %s", exc)
        return {}

    counts: Counter = Counter()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Only Python files and key config files
        if not (line.endswith(".py") or line.endswith(".md") or line.endswith(".toml")):
            continue
        # Skip pure release tails
        fname = line.split("/")[-1]
        if fname in _CHURN_SKIP_PATTERNS:
            continue
        counts[line] += 1

    return dict(counts)


# ── Signal 2: Complexity ──────────────────────────────────────────────────────

def _scan_complexity(repo_dir: pathlib.Path) -> Dict[str, Any]:
    """Walk ouroboros/ and tests/ collecting module sizes and long functions.

    Returns {
        "oversized_modules": [(path, lines)],   # modules > 1000 lines
        "oversized_functions": [(path, func, start_line, length)],  # funcs > 150 lines
        "module_lines": {path: lines},           # all module sizes
    }
    """
    oversized_modules: List[Tuple[str, int]] = []
    oversized_functions: List[Tuple[str, str, int, int]] = []
    module_lines: Dict[str, int] = {}

    scan_dirs = [
        repo_dir / "ouroboros",
        repo_dir / "supervisor",
        repo_dir / "tests",
    ]

    for scan_dir in scan_dirs:
        if not scan_dir.is_dir():
            continue
        for py_file in sorted(scan_dir.rglob("*.py")):
            # Skip __pycache__
            if any(p in _SKIP_DIRS for p in py_file.parts):
                continue
            try:
                content = py_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            lines = content.splitlines()
            line_count = len(lines)
            try:
                rel_path = str(py_file.relative_to(repo_dir))
            except ValueError:
                rel_path = str(py_file)

            module_lines[rel_path] = line_count
            if line_count > 1000:
                oversized_modules.append((rel_path, line_count))

            # Function length scan
            func_starts: List[Tuple[int, str]] = []
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("def ") or stripped.startswith("async def "):
                    # Extract function name
                    m = re.match(r"(?:async\s+)?def\s+(\w+)", stripped)
                    func_name = m.group(1) if m else "?"
                    func_starts.append((i, func_name))

            for j, (start, func_name) in enumerate(func_starts):
                def_line = lines[start]
                def_indent = len(def_line) - len(def_line.lstrip())
                end = len(lines)
                for k in range(start + 1, len(lines)):
                    l = lines[k]
                    stripped_l = l.strip()
                    if not stripped_l or stripped_l.startswith("#"):
                        continue
                    line_indent = len(l) - len(l.lstrip())
                    if line_indent <= def_indent:
                        end = k
                        break
                if j + 1 < len(func_starts):
                    end = min(end, func_starts[j + 1][0])
                length = end - start
                if length > 150:
                    oversized_functions.append((rel_path, func_name, start + 1, length))

    oversized_modules.sort(key=lambda x: -x[1])
    oversized_functions.sort(key=lambda x: -x[3])

    return {
        "oversized_modules": oversized_modules,
        "oversized_functions": oversized_functions,
        "module_lines": module_lines,
    }


# ── Signal 3: Pattern register ────────────────────────────────────────────────

def _parse_pattern_register(drive_root: pathlib.Path) -> List[Dict[str, Any]]:
    """Parse knowledge/patterns.md for recurring error classes.

    Returns list of {class, count, root_cause} dicts sorted by count desc.
    patterns.md uses a markdown table format:
    | Class | Count | Evidence | Root cause | Fix |
    """
    patterns_file = drive_root / "memory" / "knowledge" / "patterns.md"
    if not patterns_file.exists():
        return []
    try:
        content = patterns_file.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("hot_spots: cannot read patterns.md: %s", exc)
        return []

    patterns: List[Dict[str, Any]] = []
    for line in content.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("| #") or line.startswith("|---") or line.startswith("| Class"):
            continue
        # Table row: | class | count | evidence | root_cause | fix |
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) < 2:
            continue
        class_name = parts[0]
        count_str = parts[1] if len(parts) > 1 else "1"
        root_cause = parts[3] if len(parts) > 3 else ""

        # Parse count: "12+" → 12, "5" → 5
        count_m = re.match(r"(\d+)", count_str)
        count = int(count_m.group(1)) if count_m else 1

        patterns.append({
            "class": class_name,
            "count": count,
            "root_cause": root_cause[:120],
        })

    patterns.sort(key=lambda x: -x["count"])
    return patterns


# ── Scoring & aggregation ─────────────────────────────────────────────────────

def _normalize(values: List[float]) -> List[float]:
    """Min-max normalize a list of values to [0, 1]."""
    if not values:
        return values
    vmin, vmax = min(values), max(values)
    if vmax == vmin:
        return [0.5] * len(values)
    return [(v - vmin) / (vmax - vmin) for v in values]


def _compute_hot_spots(
    churn: Dict[str, int],
    complexity: Dict[str, Any],
    top_k: int = 10,
) -> List[Dict[str, Any]]:
    """Combine churn + complexity into ranked hot spots.

    Score = 0.5 * churn_norm + 0.5 * complexity_norm
    complexity_norm is based on module line count (log-scaled).

    Returns list of dicts sorted by score desc.
    """
    # All candidate paths (union of churn + oversized)
    all_paths: set = set(churn.keys())
    for path, _ in complexity["oversized_modules"]:
        all_paths.add(path)
    # Also include paths with oversized functions
    for path, func, start, length in complexity["oversized_functions"]:
        all_paths.add(path)

    if not all_paths:
        return []

    module_lines = complexity["module_lines"]
    oversized_funcs_by_file: Dict[str, List[Dict]] = defaultdict(list)
    for path, func, start, length in complexity["oversized_functions"]:
        oversized_funcs_by_file[path].append({
            "name": func,
            "start_line": start,
            "length": length,
        })

    # Build raw scores
    candidates: List[Dict[str, Any]] = []
    for path in all_paths:
        churn_count = churn.get(path, 0)
        lines = module_lines.get(path, 0)
        # log-scale complexity (50 → 0, 1000 → ~3, 5000 → ~4.3)
        complexity_score = math.log1p(max(0, lines - 50)) if lines > 50 else 0.0
        funcs = oversized_funcs_by_file.get(path, [])
        candidates.append({
            "path": path,
            "churn": churn_count,
            "lines": lines,
            "complexity_raw": complexity_score,
            "oversized_functions": funcs,
        })

    # Normalize churn and complexity independently
    churn_raw = [c["churn"] for c in candidates]
    compl_raw = [c["complexity_raw"] for c in candidates]
    churn_norm = _normalize(churn_raw)
    compl_norm = _normalize(compl_raw)

    for i, c in enumerate(candidates):
        c["churn_score"] = round(churn_norm[i], 3)
        c["complexity_score"] = round(compl_norm[i], 3)
        c["hot_score"] = round(0.5 * churn_norm[i] + 0.5 * compl_norm[i], 3)

    # Sort by combined score
    candidates.sort(key=lambda x: -x["hot_score"])
    return candidates[:top_k]


# ── Formatter ─────────────────────────────────────────────────────────────────

def _format_text(
    hot_spots: List[Dict[str, Any]],
    patterns: List[Dict[str, Any]],
    days: int,
) -> str:
    """Format hot spots as human-readable text."""
    lines: List[str] = []
    lines.append(f"## Hot Spots (last {days}d churn + complexity)\n")

    if not hot_spots:
        lines.append("No hot spots found (no churn + no oversized files).")
    else:
        for i, spot in enumerate(hot_spots, 1):
            path = spot["path"]
            score = spot["hot_score"]
            churn = spot["churn"]
            loc = spot["lines"]
            funcs = spot["oversized_functions"]

            func_note = ""
            if funcs:
                top_func = funcs[0]
                func_note = f"  ⚠ {top_func['name']}() {top_func['length']} lines (line {top_func['start_line']})"
                if len(funcs) > 1:
                    func_note += f" + {len(funcs)-1} more"

            lines.append(
                f"  {i:2}. [{score:.2f}] {path}  "
                f"churn={churn}x  loc={loc}"
            )
            if func_note:
                lines.append(f"      {func_note}")

    if patterns:
        lines.append("\n## Top error patterns (from pattern register)\n")
        for p in patterns[:5]:
            lines.append(
                f"  • [{p['count']:3}x] {p['class']}"
            )
            if p["root_cause"]:
                rc = p["root_cause"][:80]
                lines.append(f"           ↳ {rc}")

    return "\n".join(lines)


# ── Tool entrypoint ───────────────────────────────────────────────────────────

def _hot_spots(
    ctx: ToolContext,
    days: int = 7,
    top_k: int = 10,
    format: str = "text",
) -> str:
    """Run hot-spot analysis and return ranked results."""
    repo_dir = pathlib.Path(_REPO_DIR)
    drive_root = pathlib.Path(_DRIVE_ROOT)

    # Signal 1: Git churn
    churn = _git_churn(repo_dir, days=days)

    # Signal 2: Complexity
    complexity = _scan_complexity(repo_dir)

    # Signal 3: Pattern register
    patterns = _parse_pattern_register(drive_root)

    # Aggregate
    top_k = max(1, min(50, top_k))
    spots = _compute_hot_spots(churn, complexity, top_k=top_k)

    if format == "json":
        return json.dumps(
            {
                "hot_spots": spots,
                "patterns": patterns[:10],
                "meta": {
                    "days": days,
                    "total_churn_files": len(churn),
                    "total_py_modules": len(complexity["module_lines"]),
                    "oversized_modules": len(complexity["oversized_modules"]),
                    "oversized_functions": len(complexity["oversized_functions"]),
                },
            },
            ensure_ascii=False,
            indent=2,
        )

    return _format_text(spots, patterns, days=days)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="hot_spots",
            schema={
                "name": "hot_spots",
                "description": (
                    "Code hot-spot analysis for directed evolution targeting.\n\n"
                    "Aggregates three signals:\n"
                    "  1. Git churn — files changed most often in the last N days\n"
                    "  2. Complexity — oversized modules (>1000 lines) and functions (>150 lines)\n"
                    "  3. Pattern register — top recurring error classes from KB patterns.md\n\n"
                    "Returns a ranked list of files that are simultaneously risky, complex, "
                    "and historically error-prone. Use this at the START of an evolution task "
                    "instead of manually reading files to decide where to work.\n\n"
                    "Parameters:\n"
                    "  - days: git churn window in days (default 7)\n"
                    "  - top_k: number of hot spots to return (default 10, max 50)\n"
                    "  - format: 'text' (default) or 'json' for structured output"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {
                            "type": "integer",
                            "description": "Git churn window in days (default 7).",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Number of hot spots to return (default 10, max 50).",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format: 'text' (human-readable) or 'json' (structured).",
                        },
                    },
                    "required": [],
                },
            },
            execute=_hot_spots,
        )
    ]
