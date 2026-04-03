"""self_audit — prescriptive codebase and operational health audit.

Single call → ranked actionable findings: what to fix next and why.

Complements descriptive tools (activity_timeline, evolution_report, memory_search)
with *prescriptive* output: concrete next actions sorted by severity.

Checks performed:
  CODE:
    1. Oversized modules (>1000 lines, BIBLE P5 violation)
    2. Oversized functions (>150 lines, BIBLE P5 signal)
    3. Core modules without test coverage
    4. Syntax-import errors in changed files

  OPERATIONAL:
    5. Recurring tool errors in recent events
    6. Tool timeouts (last 24h)
    7. Pattern register items not recently acted on
    8. No-commit evolution streaks

Usage:
    self_audit()                          # full audit, default window 24h
    self_audit(hours=6)                   # shorter event window
    self_audit(categories=["code"])       # code-only
    self_audit(format="json")             # machine-readable
"""

from __future__ import annotations

import ast
import importlib
import json
import logging
import math
import os
import pathlib
import re
import subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

_REPO_DIR = os.environ.get("OUROBOROS_REPO_DIR", "/opt/veles")
_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")

# ── Severity levels ───────────────────────────────────────────────────────────

SEVER_CRITICAL = "CRITICAL"   # blocks correct operation
SEVER_HIGH = "HIGH"           # BIBLE violation or repeated operational error
SEVER_MEDIUM = "MEDIUM"       # structural warning
SEVER_LOW = "LOW"             # advisory / cosmetic

_SEVER_ORDER = {SEVER_CRITICAL: 0, SEVER_HIGH: 1, SEVER_MEDIUM: 2, SEVER_LOW: 3}


def _sort_key(f: Dict[str, Any]) -> Tuple[int, str]:
    return (_SEVER_ORDER.get(f["severity"], 9), f["category"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_jsonl_tail(path: pathlib.Path, tail_bytes: int = 2_000_000) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    file_size = path.stat().st_size
    try:
        with path.open("rb") as f:
            if file_size > tail_bytes:
                f.seek(-tail_bytes, 2)
                f.readline()
            raw = f.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    records: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                records.append(obj)
        except json.JSONDecodeError:
            pass
    return records


def _parse_ts(ts_str: str) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _since_dt(hours: float) -> datetime:
    return datetime.now(dt_timezone.utc) - timedelta(hours=hours)


# ── Code checks ───────────────────────────────────────────────────────────────

def _count_lines(path: pathlib.Path) -> int:
    try:
        return sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))
    except Exception:
        return 0


def _check_oversized_modules(repo: pathlib.Path) -> List[Dict[str, Any]]:
    """Find Python modules > 1000 lines (BIBLE P5: module fits in one context window)."""
    findings: List[Dict[str, Any]] = []
    py_dirs = [repo / "ouroboros", repo / "supervisor"]
    for d in py_dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.py")):
            if "__pycache__" in str(p):
                continue
            n = _count_lines(p)
            if n > 1000:
                rel = str(p.relative_to(repo))
                findings.append({
                    "category": "code",
                    "severity": SEVER_HIGH,
                    "title": f"Oversized module: {rel}",
                    "detail": f"{n} lines (limit 1000, BIBLE P5)",
                    "action": f"Decompose {rel} — extract cohesive sub-module(s). Target: all files ≤1000 lines.",
                    "file": rel,
                    "lines": n,
                })
    return findings


def _get_function_lengths(path: pathlib.Path) -> List[Tuple[str, int, int]]:
    """Return (func_name, start_line, length) for all functions in a Python file."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except SyntaxError:
        return []
    except Exception:
        return []

    results: List[Tuple[str, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", start)
            length = end - start + 1
            results.append((node.name, start, length))
    return results


def _check_oversized_functions(repo: pathlib.Path) -> List[Dict[str, Any]]:
    """Find functions > 150 lines (BIBLE P5: signal to decompose)."""
    findings: List[Dict[str, Any]] = []
    py_dirs = [repo / "ouroboros", repo / "supervisor"]
    for d in py_dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.py")):
            if "__pycache__" in str(p):
                continue
            for fname, start, length in _get_function_lengths(p):
                if length > 150:
                    rel = str(p.relative_to(repo))
                    findings.append({
                        "category": "code",
                        "severity": SEVER_MEDIUM,
                        "title": f"Oversized function: {fname} in {rel}",
                        "detail": f"{length} lines starting at line {start} (limit 150, BIBLE P5)",
                        "action": f"Break {fname}() into smaller helpers. Each should have a single clear responsibility.",
                        "file": rel,
                        "function": fname,
                        "lines": length,
                    })
    # Limit to top 10 by size to avoid noise
    findings.sort(key=lambda f: -f["lines"])
    return findings[:10]


def _check_import_errors(repo: pathlib.Path) -> List[Dict[str, Any]]:
    """Check that all ouroboros tool modules can be imported without error."""
    findings: List[Dict[str, Any]] = []
    tools_dir = repo / "ouroboros" / "tools"
    if not tools_dir.is_dir():
        return findings

    for p in sorted(tools_dir.glob("*.py")):
        if p.name.startswith("_") or p.name == "registry.py":
            continue
        module_name = f"ouroboros.tools.{p.stem}"
        try:
            importlib.import_module(module_name)
        except ImportError as exc:
            findings.append({
                "category": "code",
                "severity": SEVER_CRITICAL,
                "title": f"Import error in tool module: {p.name}",
                "detail": str(exc)[:200],
                "action": f"Fix import in {p.name} before next evolution commit.",
                "file": str(p.relative_to(repo)),
            })
        except Exception as exc:
            # Non-import errors (e.g. SyntaxError) are also critical
            findings.append({
                "category": "code",
                "severity": SEVER_CRITICAL,
                "title": f"Load error in tool module: {p.name}",
                "detail": str(exc)[:200],
                "action": f"Fix {p.name} — module fails on import.",
                "file": str(p.relative_to(repo)),
            })

    return findings


_CORE_MODULES_NEEDING_TESTS = [
    "ouroboros/context.py",
    "ouroboros/loop.py",
    "ouroboros/loop_runtime.py",
    "ouroboros/agent.py",
    "ouroboros/reflection.py",
    "ouroboros/consolidator.py",
    "ouroboros/tools/memory_search.py",
    "ouroboros/tools/evolution_report.py",
    "ouroboros/tools/activity_timeline.py",
    "ouroboros/tools/log_query.py",
    "ouroboros/tools/code_search.py",
    "ouroboros/tools/ast_analyze.py",
]


def _check_test_coverage(repo: pathlib.Path) -> List[Dict[str, Any]]:
    """Check that key modules have corresponding test files."""
    findings: List[Dict[str, Any]] = []
    tests_dir = repo / "tests"
    for rel in _CORE_MODULES_NEEDING_TESTS:
        src = repo / rel
        if not src.exists():
            continue
        # Derive expected test file name
        stem = src.stem
        test_file = tests_dir / f"test_{stem}.py"
        if not test_file.exists():
            findings.append({
                "category": "code",
                "severity": SEVER_MEDIUM,
                "title": f"Missing tests: {rel}",
                "detail": f"No tests/test_{stem}.py found for core module {rel}",
                "action": f"Add tests/test_{stem}.py covering the main behavior of {rel}.",
                "file": rel,
            })
    return findings


# ── Operational checks ────────────────────────────────────────────────────────

def _check_tool_errors(drive: pathlib.Path, hours: float) -> List[Dict[str, Any]]:
    """Find recurring tool errors and timeouts in the recent event window."""
    findings: List[Dict[str, Any]] = []
    since = _since_dt(hours)
    events = _load_jsonl_tail(drive / "logs" / "events.jsonl")

    timeout_counts: Counter = Counter()
    error_counts: Counter = Counter()
    last_ts: Dict[str, str] = {}

    for r in events:
        dt = _parse_ts(r.get("ts", ""))
        if not dt or dt < since:
            continue
        etype = r.get("type", "")
        tool = r.get("tool", r.get("name", "?"))
        ts = r.get("ts", "")
        if etype in ("tool_timeout", "TOOL_TIMEOUT"):
            timeout_counts[tool] += 1
            last_ts[f"timeout:{tool}"] = ts
        elif etype in ("tool_error", "TOOL_ERROR"):
            error_counts[tool] += 1
            last_ts[f"error:{tool}"] = ts

    # Recurring: 2+ in window = pattern
    for tool, count in timeout_counts.most_common():
        sev = SEVER_HIGH if count >= 3 else SEVER_MEDIUM
        findings.append({
            "category": "operational",
            "severity": sev,
            "title": f"Recurring timeout: {tool} ({count}× in {hours:.0f}h)",
            "detail": f"Last: {last_ts.get(f'timeout:{tool}', '?')}",
            "action": (
                f"Investigate why {tool} times out. Increase TOOL_TIMEOUT_OVERRIDES['{tool}'] "
                f"or split the operation into smaller steps."
            ),
            "tool": tool,
            "count": count,
        })

    for tool, count in error_counts.most_common():
        if count < 2:
            continue
        sev = SEVER_HIGH if count >= 5 else SEVER_MEDIUM
        findings.append({
            "category": "operational",
            "severity": sev,
            "title": f"Recurring error: {tool} ({count}× in {hours:.0f}h)",
            "detail": f"Last: {last_ts.get(f'error:{tool}', '?')}",
            "action": f"Check error details for {tool}. Fix root cause or add error handling.",
            "tool": tool,
            "count": count,
        })

    return findings


def _check_no_commit_evolutions(drive: pathlib.Path) -> List[Dict[str, Any]]:
    """Find evolution tasks that produced no commits (stagnant cycles)."""
    findings: List[Dict[str, Any]] = []
    events = _load_jsonl_tail(drive / "logs" / "events.jsonl")
    reflections = _load_jsonl_tail(drive / "logs" / "task_reflections.jsonl")

    # Find evolution tasks with no-commit marker from reflection
    recent_reflections = reflections[-50:]  # last 50
    no_commit_tasks: List[Dict[str, Any]] = []
    for r in recent_reflections:
        goal = str(r.get("goal", ""))
        reflection = str(r.get("reflection", ""))
        if "no commit" in reflection.lower() or "no-commit" in reflection.lower():
            no_commit_tasks.append(r)

    if len(no_commit_tasks) >= 2:
        findings.append({
            "category": "operational",
            "severity": SEVER_HIGH,
            "title": f"Evolution no-commit pattern: {len(no_commit_tasks)} recent cycles without commits",
            "detail": "Recent evolution tasks reflect 'no commit' — cycles spending rounds without output",
            "action": (
                "Check what blocked commits in recent cycles (timeouts? test failures? pre-push failures?). "
                "Review pattern register for root cause."
            ),
        })

    return findings


def _check_pattern_register(drive: pathlib.Path) -> List[Dict[str, Any]]:
    """Check pattern register for high-count unresolved patterns."""
    findings: List[Dict[str, Any]] = []
    patterns_file = drive / "memory" / "knowledge" / "patterns.md"
    if not patterns_file.exists():
        return findings

    try:
        content = patterns_file.read_text(encoding="utf-8")
    except Exception:
        return findings

    # Parse table rows: | Class | Count | Evidence | Root cause | Fix |
    rows = re.findall(r"\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|", content)
    high_count: List[Tuple[str, int]] = []
    for class_name, count_str in rows:
        if class_name.strip().lower() in ("class", "---"):
            continue
        count = int(count_str)
        if count >= 3:
            high_count.append((class_name.strip(), count))

    high_count.sort(key=lambda x: -x[1])
    for class_name, count in high_count[:5]:
        findings.append({
            "category": "operational",
            "severity": SEVER_MEDIUM,
            "title": f"Persistent error pattern: '{class_name}' ({count} occurrences)",
            "detail": "Pattern register shows repeated failures of this class",
            "action": (
                f"Apply Meta-Reflection Imperative (BIBLE P2): find architectural fix that "
                f"makes the '{class_name}' class impossible, not just patches the symptom."
            ),
            "pattern": class_name,
            "count": count,
        })

    return findings


# ── Runner ────────────────────────────────────────────────────────────────────

_ALL_CATEGORIES = {"code", "operational"}


def _run_audit(
    categories: Optional[List[str]],
    hours: float,
    repo: pathlib.Path,
    drive: pathlib.Path,
) -> List[Dict[str, Any]]:
    cats = set(categories) if categories else _ALL_CATEGORIES
    findings: List[Dict[str, Any]] = []

    if "code" in cats:
        findings.extend(_check_import_errors(repo))
        findings.extend(_check_oversized_modules(repo))
        findings.extend(_check_oversized_functions(repo))
        findings.extend(_check_test_coverage(repo))

    if "operational" in cats:
        findings.extend(_check_tool_errors(drive, hours))
        findings.extend(_check_no_commit_evolutions(drive))
        findings.extend(_check_pattern_register(drive))

    findings.sort(key=_sort_key)
    return findings


# ── Formatter ─────────────────────────────────────────────────────────────────

def _format_text(findings: List[Dict[str, Any]], hours: float) -> str:
    if not findings:
        return "✅ self_audit: no issues found."

    by_severity: Dict[str, List[Dict[str, Any]]] = {}
    for f in findings:
        by_severity.setdefault(f["severity"], []).append(f)

    lines = [f"## self_audit — {len(findings)} finding(s)  (event window: {hours:.0f}h)\n"]

    icons = {SEVER_CRITICAL: "🔴", SEVER_HIGH: "🟠", SEVER_MEDIUM: "🟡", SEVER_LOW: "⚪"}

    for sev in (SEVER_CRITICAL, SEVER_HIGH, SEVER_MEDIUM, SEVER_LOW):
        group = by_severity.get(sev, [])
        if not group:
            continue
        lines.append(f"### {icons[sev]} {sev} ({len(group)})")
        for i, f in enumerate(group, 1):
            lines.append(f"  {i}. **{f['title']}**")
            lines.append(f"     {f['detail']}")
            lines.append(f"     → {f['action']}")
            lines.append("")

    # Summary line
    sev_counts = {s: len(v) for s, v in by_severity.items()}
    parts = [f"{icons[s]} {s}: {n}" for s, n in sev_counts.items() if n > 0]
    lines.append("─── " + "  ".join(parts) + " ───")
    return "\n".join(lines)


# ── Public tool ───────────────────────────────────────────────────────────────

def _self_audit(
    ctx: ToolContext,
    hours: float = 24.0,
    categories: Optional[List[str]] = None,
    format: str = "text",
) -> str:
    """Run prescriptive self-audit and return ranked actionable findings."""
    repo = ctx.repo_dir if ctx else pathlib.Path(_REPO_DIR)
    drive = ctx.drive_root if ctx else pathlib.Path(_DRIVE_ROOT)
    hours = max(1.0, min(168.0, float(hours)))
    valid_cats = [c for c in (categories or [])] or None

    findings = _run_audit(valid_cats, hours, repo, drive)

    if format == "json":
        return json.dumps(
            {"hours": hours, "count": len(findings), "findings": findings},
            ensure_ascii=False,
            indent=2,
        )

    return _format_text(findings, hours)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="self_audit",
            schema={
                "name": "self_audit",
                "description": (
                    "Prescriptive codebase and operational health audit. Single call → "
                    "ranked actionable findings: what to fix next and why.\n\n"
                    "Complements descriptive tools (activity_timeline shows what happened, "
                    "evolution_report shows what was committed). self_audit answers: "
                    "'what should I work on next?'\n\n"
                    "Code checks:\n"
                    "  - Import errors in tool modules (CRITICAL)\n"
                    "  - Oversized modules >1000 lines (BIBLE P5 violation, HIGH)\n"
                    "  - Oversized functions >150 lines (BIBLE P5 signal, MEDIUM)\n"
                    "  - Core modules without test coverage (MEDIUM)\n\n"
                    "Operational checks:\n"
                    "  - Recurring tool errors/timeouts in recent events\n"
                    "  - Evolution no-commit patterns (stagnant cycles)\n"
                    "  - High-count items in pattern register\n\n"
                    "Findings sorted by severity: CRITICAL → HIGH → MEDIUM → LOW.\n"
                    "Each finding includes a concrete next action.\n\n"
                    "Parameters:\n"
                    "- hours: event window for operational checks (default 24, max 168)\n"
                    "- categories: ['code'] or ['operational'] or both (default: both)\n"
                    "- format: 'text' (default, human-readable) or 'json'"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "hours": {
                            "type": "number",
                            "description": "Event window for operational checks in hours (default 24).",
                        },
                        "categories": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["code", "operational"]},
                            "description": "Which categories to audit. Default: both ['code', 'operational'].",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format: 'text' (default) or 'json'.",
                        },
                    },
                    "required": [],
                },
            },
            handler=lambda ctx, **kw: _self_audit(ctx, **kw),
        )
    ]
