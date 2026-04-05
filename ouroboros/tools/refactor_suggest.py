"""refactor_suggest — synthesis tool: actionable refactoring suggestions.

Aggregates signals from six analysis tools into a single prioritised list
of concrete, effort-weighted actions.  Reuses the internal scan functions
from the existing tools directly — no re-implementation, no JSON round-trips.

Sources used
------------
  dep_cycles       → circular import chains (break back-edge)
  dead_code        → unused imports and dead private symbols (delete)
  duplicate_code   → exact and normalised code clones (extract helper)
  security_scan    → security anti-patterns (fix immediately)
  tech_debt        → oversized/complex/deeply-nested functions (refactor)
  exception_audit  → exception handling anti-patterns (tighten)

Each finding is converted to a ``_Suggestion`` with:
  priority   — 1 (lowest) … 10 (highest)
  category   — source category
  effort     — "low" | "medium" | "high"  (expected effort to fix)
  impact     — "low" | "medium" | "high"  (benefit when fixed)
  action     — concrete one-line description of what to do
  location   — "file:line" string (or "" if file-level)

Priority is calculated from severity × impact / effort and capped at 10.

Filters
-------
  path         — limit all scans to a subdirectory or file
  max_results  — return at most N suggestions (default 20)
  focus        — "security" | "cycles" | "debt" | "dead" | "duplication"
                 | "exceptions" | "all"  (default "all")
  min_priority — only return suggestions with priority >= this (default 1)
  format       — "text" | "json"  (default "text")

Examples
--------
    refactor_suggest()                          # top 20 across all sources
    refactor_suggest(focus="security")          # security issues only
    refactor_suggest(min_priority=7)            # only high-priority items
    refactor_suggest(max_results=5)             # executive summary
    refactor_suggest(path="ouroboros/tools/")   # limit to one directory
    refactor_suggest(format="json")             # machine-readable output
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

_REPO_DIR = Path(os.environ.get("REPO_DIR", "/opt/veles"))

# ── Suggestion dataclass ──────────────────────────────────────────────────────

@dataclass(order=True)
class _Suggestion:
    """A single actionable refactor recommendation."""

    priority: float = field(compare=True)
    category: str = field(compare=False)
    effort: str = field(compare=False)
    impact: str = field(compare=False)
    action: str = field(compare=False)
    location: str = field(compare=False, default="")
    details: str = field(compare=False, default="")
    source: str = field(compare=False, default="")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "priority": round(self.priority, 1),
            "category": self.category,
            "effort": self.effort,
            "impact": self.impact,
            "action": self.action,
            "location": self.location,
            "details": self.details,
            "source": self.source,
        }


_EFFORT_MULT = {"low": 1.2, "medium": 1.0, "high": 0.8}
_IMPACT_MULT = {"low": 0.8, "medium": 1.0, "high": 1.2}
_SEV_BASE = {"low": 2, "medium": 4, "high": 7, "critical": 10}


def _score(severity: str, impact: str, effort: str) -> float:
    """Compute priority 1-10 from severity, impact, effort."""
    sev = _SEV_BASE.get(severity.lower(), 2)
    imp = _IMPACT_MULT.get(impact, 1.0)
    eff = _EFFORT_MULT.get(effort, 1.0)
    return min(10.0, round(sev * imp * eff, 1))


# ── Source 1: dep_cycles ──────────────────────────────────────────────────────

def _collect_dep_cycles(repo_root: Path, path: Optional[str]) -> List[_Suggestion]:
    try:
        from ouroboros.tools.dep_cycles import (
            _collect_py_files,
            _build_import_graph,
            _tarjan_scc,
            _find_shortest_cycle,
            _find_back_edge_line,
            _build_hint,
            _classify_severity,
        )
    except ImportError:
        return []

    suggestions: List[_Suggestion] = []

    ouroboros_root = repo_root / "ouroboros"
    supervisor_root = repo_root / "supervisor"
    py_files: List[Path] = []
    for pkg_root in [ouroboros_root, supervisor_root]:
        if pkg_root.exists():
            py_files.extend(_collect_py_files(pkg_root))

    if not py_files:
        return []

    scan_root = ouroboros_root if ouroboros_root.exists() else repo_root
    adj, mod_map = _build_import_graph(py_files, scan_root)
    all_nodes = set(mod_map.keys())

    if path:
        clean = path.rstrip("/").lstrip("/").replace("/", ".").replace(".py", "")
        pkg = scan_root.name + "."
        if clean.startswith(pkg):
            clean = clean[len(pkg):]
        all_nodes = {n for n in all_nodes if n.startswith(clean) or clean in n}

    sccs = _tarjan_scc(all_nodes, adj)
    for scc in sccs:
        scc_set = set(scc)
        cycle_path = _find_shortest_cycle(scc_set, adj)
        severity = _classify_severity(scc)
        back_edge = _find_back_edge_line(cycle_path, adj)
        hint = _build_hint(cycle_path, back_edge)

        sev_lower = severity.lower()
        effort = "medium"
        impact = "high" if sev_lower == "critical" else "medium"
        priority = _score(sev_lower, impact, effort)

        loc = ""
        if back_edge:
            src_file = str(mod_map.get(back_edge[0], ""))
            if src_file:
                try:
                    loc = str(Path(src_file).relative_to(repo_root))
                except ValueError:
                    loc = src_file
                loc = f"{loc}:{back_edge[2]}"

        chain = " → ".join(m.split(".")[-1] for m in cycle_path)
        suggestions.append(_Suggestion(
            priority=priority,
            category="circular_import",
            effort=effort,
            impact=impact,
            action=f"Break import cycle: {chain}",
            location=loc,
            details=hint,
            source="dep_cycles",
        ))

    return suggestions


# ── Source 2: dead_code ───────────────────────────────────────────────────────

def _collect_dead_code(repo_root: Path, path: Optional[str]) -> List[_Suggestion]:
    try:
        from ouroboros.tools.dead_code import _scan_codebase
    except ImportError:
        return []

    suggestions: List[_Suggestion] = []
    try:
        dead, _ = _scan_codebase(repo_root, path, None)
    except Exception:
        return []

    for item in dead.get("unused_imports", []):
        suggestions.append(_Suggestion(
            priority=_score("low", "low", "low"),
            category="unused_import",
            effort="low",
            impact="low",
            action=f"Remove unused import: {item['stmt']}",
            location=f"{item['file']}:{item['line']}",
            details=f"Name '{item['name']}' is imported but never referenced",
            source="dead_code",
        ))

    for item in dead.get("dead_privates", []):
        suggestions.append(_Suggestion(
            priority=_score("low", "low", "low"),
            category="dead_symbol",
            effort="low",
            impact="low",
            action=f"Delete dead private {item['kind']}: {item['name']}",
            location=f"{item['file']}:{item['line']}",
            details=f"'{item['name']}' is never called or imported elsewhere",
            source="dead_code",
        ))

    return suggestions


# ── Source 3: duplicate_code ──────────────────────────────────────────────────

def _collect_duplicates(repo_root: Path, path: Optional[str]) -> List[_Suggestion]:
    try:
        from ouroboros.tools.duplicate_code import (
            _collect_py_files,
            _extract_functions,
            _find_clones,
        )
    except ImportError:
        return []

    suggestions: List[_Suggestion] = []
    py_files = _collect_py_files(repo_root, path)
    all_records: list = []
    for fpath in py_files:
        try:
            rel = str(fpath.relative_to(repo_root))
        except ValueError:
            rel = str(fpath)
        all_records.extend(_extract_functions(fpath, rel))

    try:
        groups = _find_clones(all_records, min_lines=5, min_group_size=2, clone_type="all")
    except Exception:
        return []

    for grp in groups:
        ctype = grp.get("clone_type", "normalized")
        n = grp.get("instance_count", 2)
        body_lines = grp.get("body_lines", 0)
        instances = grp.get("instances", [])

        severity = "medium" if ctype == "exact" else "low"
        impact = "medium"
        effort = "medium" if body_lines < 30 else "high"
        priority = _score(severity, impact, effort)

        names = ", ".join(inst.get("name", "?") + "()" for inst in instances[:3])
        loc = f"{instances[0]['file']}:{instances[0]['line']}" if instances else ""
        suggestions.append(_Suggestion(
            priority=priority,
            category="duplicate_code",
            effort=effort,
            impact=impact,
            action=f"Extract shared helper from {n}× duplicate {ctype} functions: {names}",
            location=loc,
            details=(
                f"~{body_lines} lines duplicated {n} times. "
                "Extract to a shared helper function and replace all instances."
            ),
            source="duplicate_code",
        ))

    return suggestions


# ── Source 4: security_scan ───────────────────────────────────────────────────

def _collect_security(repo_root: Path, path: Optional[str]) -> List[_Suggestion]:
    try:
        from ouroboros.tools.security_scan import (
            _collect_py_files,
            _scan_file as _sec_scan_file,
            _ALL_CATEGORIES,
            _CATEGORY_SEVERITY,
        )
    except ImportError:
        return []

    suggestions: List[_Suggestion] = []
    skip_tests = False
    py_files = _collect_py_files(repo_root, path, skip_tests)
    enabled = _ALL_CATEGORIES.copy()
    min_level = 0  # all severities

    for fpath in py_files:
        try:
            rel = str(fpath.relative_to(repo_root))
        except ValueError:
            rel = str(fpath)
        try:
            source = fpath.read_text(encoding="utf-8", errors="replace")
            lines = source.splitlines()
            findings = _sec_scan_file(fpath, rel, enabled, min_level)
        except Exception:
            continue

        for f in findings:
            sev = f.severity
            effort = "low" if sev == "low" else "medium"
            impact = "high" if sev == "high" else ("medium" if sev == "medium" else "low")
            priority = _score(sev, impact, effort)
            suggestions.append(_Suggestion(
                priority=priority,
                category=f"security:{f.category}",
                effort=effort,
                impact=impact,
                action=f"[{sev.upper()}] {f.message}",
                location=f"{f.file}:{f.line}",
                details=f.snippet,
                source="security_scan",
            ))

    return suggestions


# ── Source 5: tech_debt ───────────────────────────────────────────────────────

def _collect_tech_debt(repo_root: Path, path: Optional[str]) -> List[_Suggestion]:
    try:
        from ouroboros.tools.tech_debt import _scan_codebase
    except ImportError:
        return []

    suggestions: List[_Suggestion] = []
    try:
        debt = _scan_codebase(repo_root, path, None, "low")
    except Exception:
        return []

    for item in debt.get("high_complexity", []):
        cx = item.get("complexity", 0)
        priority = _score("high", "high", "high")
        suggestions.append(_Suggestion(
            priority=priority,
            category="high_complexity",
            effort="high",
            impact="high",
            action=(
                f"Decompose {item['function']}() "
                f"(cyclomatic={cx}) — extract sub-functions or simplify branches"
            ),
            location=f"{item['file']}:{item['line']}",
            details=f"Cyclomatic complexity {cx} — BIBLE P5 threshold is 10",
            source="tech_debt",
        ))

    for item in debt.get("deep_nesting", []):
        depth = item.get("depth", 0)
        priority = _score("high", "high", "medium")
        suggestions.append(_Suggestion(
            priority=priority,
            category="deep_nesting",
            effort="medium",
            impact="high",
            action=(
                f"Flatten nesting in {item['function']}() "
                f"(depth={depth}) — early returns or extract inner logic"
            ),
            location=f"{item['file']}:{item['line']}",
            details=f"Nesting depth {depth} exceeds threshold {5}",
            source="tech_debt",
        ))

    for item in debt.get("oversized_functions", []):
        lines_count = item.get("lines", 0)
        effort = "high" if lines_count > 300 else "medium"
        priority = _score("medium", "high", effort)
        suggestions.append(_Suggestion(
            priority=priority,
            category="oversized_function",
            effort=effort,
            impact="high",
            action=(
                f"Split {item['function']}() ({lines_count}L) "
                "into focused sub-functions"
            ),
            location=f"{item['file']}:{item['line']}",
            details=f"{lines_count} lines — BIBLE P5 limit is 150",
            source="tech_debt",
        ))

    for item in debt.get("god_objects", []):
        methods = item.get("method_count", 0)
        priority = _score("high", "high", "high")
        suggestions.append(_Suggestion(
            priority=priority,
            category="god_object",
            effort="high",
            impact="high",
            action=(
                f"Decompose class {item['class']} "
                f"({methods} methods) — violates SRP"
            ),
            location=f"{item['file']}:{item['line']}",
            details=f"{methods} methods exceeds threshold 20",
            source="tech_debt",
        ))

    for item in debt.get("too_many_params", []):
        nparams = item.get("param_count", 0)
        priority = _score("medium", "medium", "medium")
        suggestions.append(_Suggestion(
            priority=priority,
            category="too_many_params",
            effort="medium",
            impact="medium",
            action=(
                f"Reduce params in {item['function']}() "
                f"({nparams} params) — introduce a config dataclass"
            ),
            location=f"{item['file']}:{item['line']}",
            details=f"{nparams} parameters — BIBLE P5 limit is 8",
            source="tech_debt",
        ))

    return suggestions


# ── Source 6: exception_audit ─────────────────────────────────────────────────

def _collect_exception_audit(repo_root: Path, path: Optional[str]) -> List[_Suggestion]:
    try:
        from ouroboros.tools.exception_audit import (
            _collect_py_files,
            _scan_file as _exc_scan_file,
            _ALL_PATTERNS,
        )
    except ImportError:
        return []

    _SEVERITY_MAP = {
        "bare_except": "high",
        "silent_except": "high",
        "string_exception": "high",
        "broad_except": "medium",
        "reraise_as_new": "medium",
        "overly_nested": "low",
    }

    suggestions: List[_Suggestion] = []
    py_files = _collect_py_files(repo_root, path)
    enabled = set(_ALL_PATTERNS)
    max_nest_depth = 4

    for fpath in py_files:
        try:
            rel = str(fpath.relative_to(repo_root))
        except ValueError:
            rel = str(fpath)
        try:
            findings = _exc_scan_file(fpath, rel, enabled, max_nest_depth)
        except Exception:
            continue

        for f in findings:
            sev = _SEVERITY_MAP.get(f.pattern, "medium")
            effort = "low"
            impact = "high" if sev == "high" else "medium"
            priority = _score(sev, impact, effort)
            # exception_audit _Finding has .detail (not .message/.snippet)
            detail = getattr(f, "detail", "") or ""
            message = getattr(f, "message", detail) or detail
            suggestions.append(_Suggestion(
                priority=priority,
                category=f"exception:{f.pattern}",
                effort=effort,
                impact=impact,
                action=f"Fix {f.pattern}: {message or f.pattern}",
                location=f"{f.file}:{f.line}",
                details=detail,
                source="exception_audit",
            ))

    return suggestions


# ── Focus filter ──────────────────────────────────────────────────────────────

_FOCUS_SOURCES = {
    "security": {"security_scan"},
    "cycles": {"dep_cycles"},
    "debt": {"tech_debt"},
    "dead": {"dead_code"},
    "duplication": {"duplicate_code"},
    "exceptions": {"exception_audit"},
    "all": None,
}


# ── Main aggregator ───────────────────────────────────────────────────────────

def _refactor_suggest(
    ctx: ToolContext,
    path: Optional[str] = None,
    max_results: int = 20,
    focus: str = "all",
    min_priority: float = 1.0,
    format: str = "text",
) -> str:
    if focus not in _FOCUS_SOURCES:
        valid = ", ".join(sorted(_FOCUS_SOURCES))
        return f"Unknown focus: {focus!r}. Valid: {valid}"

    repo_root = Path(ctx.repo_dir if ctx and ctx.repo_dir else _REPO_DIR)
    allowed_sources = _FOCUS_SOURCES.get(focus)

    all_suggestions: List[_Suggestion] = []

    def _maybe_collect(fn, src_name: str) -> None:
        if allowed_sources is None or src_name in allowed_sources:
            try:
                all_suggestions.extend(fn(repo_root, path))
            except Exception:
                pass

    _maybe_collect(_collect_security, "security_scan")
    _maybe_collect(_collect_dep_cycles, "dep_cycles")
    _maybe_collect(_collect_tech_debt, "tech_debt")
    _maybe_collect(_collect_exception_audit, "exception_audit")
    _maybe_collect(_collect_duplicates, "duplicate_code")
    _maybe_collect(_collect_dead_code, "dead_code")

    # Filter and sort
    filtered = [s for s in all_suggestions if s.priority >= min_priority]
    filtered.sort(key=lambda s: -s.priority)

    # Deduplicate by (location, category) to avoid same finding from multiple passes
    seen: set = set()
    deduped: List[_Suggestion] = []
    for s in filtered:
        key = (s.location, s.category, s.action[:60])
        if key not in seen:
            seen.add(key)
            deduped.append(s)

    top = deduped[:max_results]

    if format == "json":
        return json.dumps(
            {
                "total_found": len(deduped),
                "returned": len(top),
                "filters": {
                    "path": path,
                    "focus": focus,
                    "min_priority": min_priority,
                    "max_results": max_results,
                },
                "suggestions": [s.to_dict() for s in top],
            },
            ensure_ascii=False,
            indent=2,
        )

    return _format_text(top, len(deduped), path, focus, min_priority, max_results)


# ── Text formatter ────────────────────────────────────────────────────────────

_PRIORITY_ICON = {
    range(9, 11): "🔴",
    range(7, 9): "🟠",
    range(5, 7): "🟡",
    range(1, 5): "🔵",
}


def _priority_icon(p: float) -> str:
    ip = int(p)
    for rng, icon in _PRIORITY_ICON.items():
        if ip in rng:
            return icon
    return "⚪"


def _format_text(
    top: List[_Suggestion],
    total_found: int,
    path: Optional[str],
    focus: str,
    min_priority: float,
    max_results: int,
) -> str:
    lines: List[str] = []
    filter_parts: List[str] = []
    if path:
        filter_parts.append(f"path={path}")
    if focus != "all":
        filter_parts.append(f"focus={focus}")
    if min_priority > 1:
        filter_parts.append(f"min_priority={min_priority}")
    filter_str = (", " + ", ".join(filter_parts)) if filter_parts else ""

    lines.append(
        f"## Refactor Suggestions — top {len(top)} of {total_found} found{filter_str}"
    )
    lines.append(
        "   Priority 1–10 | effort: low/med/high | impact: low/med/high\n"
    )

    if not top:
        lines.append("✅ No refactoring suggestions found matching the specified filters.")
        return "\n".join(lines)

    for i, s in enumerate(top, 1):
        icon = _priority_icon(s.priority)
        lines.append(
            f"{i:2}. {icon} [{s.priority:.1f}] {s.action}"
        )
        if s.location:
            lines.append(f"     📍 {s.location}")
        lines.append(
            f"     effort={s.effort} impact={s.impact} source={s.source}"
        )
        if s.details and s.details.strip() and s.details.strip() != s.action.strip():
            detail_short = s.details.strip()[:120]
            lines.append(f"     💬 {detail_short}")
        lines.append("")

    if total_found > max_results:
        lines.append(
            f"ℹ️  {total_found - max_results} more suggestions not shown "
            f"(increase max_results or add min_priority filter)"
        )

    return "\n".join(lines)


# ── Tool registration ─────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="refactor_suggest",
            schema={
                "name": "refactor_suggest",
                "description": (
                    "Synthesis tool: aggregates signals from dep_cycles, dead_code, "
                    "duplicate_code, security_scan, tech_debt, and exception_audit "
                    "into a single priority-ranked list of actionable refactoring "
                    "suggestions. Priority 1–10 based on severity × impact / effort. "
                    "Use to decide WHERE to refactor next."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Limit scan to a subdirectory or file (relative to repo root)",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum suggestions to return (default 20)",
                        },
                        "focus": {
                            "type": "string",
                            "enum": [
                                "security", "cycles", "debt",
                                "dead", "duplication", "exceptions", "all",
                            ],
                            "description": "Limit to one source type (default 'all')",
                        },
                        "min_priority": {
                            "type": "number",
                            "description": "Only return suggestions with priority >= this (1–10, default 1)",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format (default 'text')",
                        },
                    },
                },
            },
            handler=_refactor_suggest,
        )
    ]
