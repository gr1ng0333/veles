"""health_report — Project-wide codebase health dashboard.

Orchestrates seven existing analysis tools into a single A–F health grade
with a prioritised action list.  One call replaces ~10 separate tool calls
and manual aggregation.

Dimensions scored (each 0-100, then weighted average):
  security     (weight 3) — high-sev findings from security_scan
  cycles       (weight 2) — circular imports from dep_cycles
  debt         (weight 2) — tech debt density from tech_debt
  exceptions   (weight 1) — exception anti-patterns from exception_audit
  docs         (weight 1) — docstring coverage from doc_coverage
  types        (weight 1) — type annotation coverage from type_coverage
  todos        (weight 0) — informational only (not scored)

Grade thresholds:
  A  90–100  clean
  B  75–89   minor issues
  C  60–74   needs attention
  D  40–59   significant problems
  F  0–39    critical state

Parameters
----------
path        : str, optional — limit scan to a subdirectory (default: full repo)
format      : "text" | "json" (default "text")
max_actions : int — max action items to surface (default 10)
quick       : bool — skip slow scans (doc/type coverage); faster but less complete

Examples
--------
    health_report()                          # full project dashboard
    health_report(path="ouroboros/")         # focus on agent core
    health_report(format="json")             # machine-readable
    health_report(quick=True)                # fast overview
    health_report(max_actions=5)             # executive summary
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

_REPO_DIR = Path(os.environ.get("REPO_DIR", "/opt/veles"))


# ── Action item ───────────────────────────────────────────────────────────────

@dataclass(order=True)
class _Action:
    """A single actionable health recommendation."""

    priority: float = field(compare=True)  # 1-10 (higher = more urgent)
    dimension: str = field(compare=False)
    severity: str = field(compare=False)   # "critical" | "high" | "medium" | "low"
    action: str = field(compare=False)
    location: str = field(compare=False, default="")
    detail: str = field(compare=False, default="")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "priority": round(self.priority, 1),
            "dimension": self.dimension,
            "severity": self.severity,
            "action": self.action,
            "location": self.location,
            "detail": self.detail,
        }


# ── Dimension result ──────────────────────────────────────────────────────────

@dataclass
class _Dimension:
    name: str
    score: float        # 0–100
    weight: int
    summary: str        # one-line human summary
    actions: List[_Action] = field(default_factory=list)
    error: str = ""


# ── Scoring helpers ───────────────────────────────────────────────────────────

_GRADE_THRESHOLDS = [
    (90, "A", "✅"),
    (75, "B", "🟢"),
    (60, "C", "🟡"),
    (40, "D", "🟠"),
    (0,  "F", "🔴"),
]


def _grade(score: float) -> Tuple[str, str]:
    for threshold, letter, icon in _GRADE_THRESHOLDS:
        if score >= threshold:
            return letter, icon
    return "F", "🔴"


def _bar(score: float, width: int = 20) -> str:
    filled = round(score / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _sev_priority(sev: str) -> float:
    return {"critical": 9.5, "high": 7.5, "medium": 5.0, "low": 2.5}.get(sev.lower(), 3.0)


# ── Dimension: security ───────────────────────────────────────────────────────

def _scan_security(repo_root: Path, path: Optional[str]) -> _Dimension:
    actions: List[_Action] = []
    try:
        from ouroboros.tools.security_scan import (
            _collect_py_files,
            _scan_file as _sec_scan_file,
            _ALL_CATEGORIES,
        )
        py_files = _collect_py_files(repo_root, path, skip_tests=True)
        for fpath in py_files:
            try:
                rel = str(fpath.relative_to(repo_root))
            except ValueError:
                rel = str(fpath)
            try:
                findings = _sec_scan_file(fpath, rel, _ALL_CATEGORIES.copy(), 0)
            except Exception:
                continue
            for f in findings:
                actions.append(_Action(
                    priority=_sev_priority(f.severity),
                    dimension="security",
                    severity=f.severity,
                    action=f.message,
                    location=f"{f.file}:{f.line}",
                    detail=f.snippet[:120] if f.snippet else "",
                ))
    except ImportError:
        return _Dimension("security", 50.0, 3, "security_scan unavailable", error="ImportError")
    except Exception as exc:
        return _Dimension("security", 50.0, 3, f"scan error: {exc}", error=str(exc))

    n = len(actions)
    n_high = sum(1 for a in actions if a.severity in ("critical", "high"))
    n_med  = sum(1 for a in actions if a.severity == "medium")

    # Score: start at 100, penalise per finding
    score = max(0.0, 100.0 - n_high * 12 - n_med * 4 - (n - n_high - n_med) * 1)

    summary = (
        "No security issues found" if n == 0
        else f"{n} issue(s): {n_high} high/critical, {n_med} medium"
    )
    return _Dimension("security", score, 3, summary, actions=actions)


# ── Dimension: cycles ─────────────────────────────────────────────────────────

def _scan_cycles(repo_root: Path, path: Optional[str]) -> _Dimension:
    actions: List[_Action] = []
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
        ouroboros_root = repo_root / "ouroboros"
        supervisor_root = repo_root / "supervisor"
        py_files: List[Path] = []
        for pkg_root in [ouroboros_root, supervisor_root]:
            if pkg_root.exists():
                py_files.extend(_collect_py_files(pkg_root))

        if not py_files:
            return _Dimension("cycles", 100.0, 2, "No Python packages found")

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
            severity = _classify_severity(scc).lower()
            back_edge = _find_back_edge_line(cycle_path, adj)
            hint = _build_hint(cycle_path, back_edge)

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
            actions.append(_Action(
                priority=_sev_priority(severity),
                dimension="cycles",
                severity=severity,
                action=f"Break import cycle: {chain}",
                location=loc,
                detail=hint,
            ))
    except ImportError:
        return _Dimension("cycles", 50.0, 2, "dep_cycles unavailable", error="ImportError")
    except Exception as exc:
        return _Dimension("cycles", 50.0, 2, f"scan error: {exc}", error=str(exc))

    n = len(actions)
    n_critical = sum(1 for a in actions if a.severity == "critical")
    n_high = sum(1 for a in actions if a.severity == "high")

    score = max(0.0, 100.0 - n_critical * 15 - n_high * 8 - (n - n_critical - n_high) * 4)
    summary = "No circular imports" if n == 0 else f"{n} cycle(s): {n_critical} critical, {n_high} high"
    return _Dimension("cycles", score, 2, summary, actions=actions)


# ── Dimension: debt ───────────────────────────────────────────────────────────

def _scan_debt(repo_root: Path, path: Optional[str]) -> _Dimension:
    actions: List[_Action] = []
    try:
        from ouroboros.tools.tech_debt import _scan_codebase
        debt = _scan_codebase(repo_root, path, None, "low")
    except ImportError:
        return _Dimension("debt", 50.0, 2, "tech_debt unavailable", error="ImportError")
    except Exception as exc:
        return _Dimension("debt", 50.0, 2, f"scan error: {exc}", error=str(exc))

    for item in debt.get("high_complexity", []):
        actions.append(_Action(
            priority=8.0,
            dimension="debt",
            severity="high",
            action=f"Decompose {item['function']}() — cyclomatic={item.get('complexity',0)}",
            location=f"{item['file']}:{item['line']}",
        ))
    for item in debt.get("oversized_functions", []):
        actions.append(_Action(
            priority=6.5,
            dimension="debt",
            severity="medium",
            action=f"Split {item['function']}() — {item.get('lines',0)} lines",
            location=f"{item['file']}:{item['line']}",
        ))
    for item in debt.get("god_objects", []):
        actions.append(_Action(
            priority=7.0,
            dimension="debt",
            severity="high",
            action=f"Decompose class {item['class']} — {item.get('method_count',0)} methods",
            location=f"{item['file']}:{item['line']}",
        ))
    for item in debt.get("deep_nesting", []):
        actions.append(_Action(
            priority=5.5,
            dimension="debt",
            severity="medium",
            action=f"Flatten {item['function']}() — nesting depth={item.get('depth',0)}",
            location=f"{item['file']}:{item['line']}",
        ))
    for item in debt.get("too_many_params", []):
        actions.append(_Action(
            priority=4.5,
            dimension="debt",
            severity="low",
            action=f"Reduce params in {item['function']}() — {item.get('param_count',0)} params",
            location=f"{item['file']}:{item['line']}",
        ))

    n = len(actions)
    n_high = sum(1 for a in actions if a.severity in ("high", "critical"))
    score = max(0.0, 100.0 - n_high * 8 - (n - n_high) * 3)
    summary = "No debt issues" if n == 0 else f"{n} debt item(s): {n_high} high priority"
    return _Dimension("debt", score, 2, summary, actions=actions)


# ── Dimension: exceptions ─────────────────────────────────────────────────────

def _scan_exceptions(repo_root: Path, path: Optional[str]) -> _Dimension:
    actions: List[_Action] = []
    try:
        from ouroboros.tools.exception_audit import _scan_codebase as _exc_scan
        findings = _exc_scan(repo_root, path, None, None, "low", 5)
    except ImportError:
        return _Dimension("exceptions", 80.0, 1, "exception_audit unavailable", error="ImportError")
    except Exception as exc:
        return _Dimension("exceptions", 80.0, 1, f"scan error: {exc}", error=str(exc))

    for f in findings:
        sev = f.get("severity", "medium")
        cat = f.get("category", "unknown")
        func = f.get("function", "?")
        file_ = f.get("file", "")
        line = f.get("line", 0)
        actions.append(_Action(
            priority=_sev_priority(sev),
            dimension="exceptions",
            severity=sev,
            action=f"Fix {cat} in {func}()",
            location=f"{file_}:{line}",
        ))

    n = len(actions)
    n_high = sum(1 for a in actions if a.severity in ("high", "critical"))
    score = max(0.0, 100.0 - n_high * 6 - (n - n_high) * 2)
    summary = "No exception issues" if n == 0 else f"{n} issue(s): {n_high} high priority"
    return _Dimension("exceptions", score, 1, summary, actions=actions)


# ── Dimension: docs ───────────────────────────────────────────────────────────

def _scan_docs(repo_root: Path, path: Optional[str]) -> _Dimension:
    try:
        from ouroboros.tools.doc_coverage import _scan_codebase as _doc_scan
        result = _doc_scan(
            repo_root,
            path or "ouroboros/",
            skip_private=True,
            categories=None,
        )
    except ImportError:
        return _Dimension("docs", 70.0, 1, "doc_coverage unavailable", error="ImportError")
    except Exception as exc:
        return _Dimension("docs", 70.0, 1, f"scan error: {exc}", error=str(exc))

    summary_data = result.get("summary", {})
    total = summary_data.get("total", 0)
    documented = summary_data.get("documented", 0)
    pct = (documented / total * 100) if total > 0 else 100.0

    actions: List[_Action] = []
    # Surface top missing docs by file coverage
    for file_info in sorted(
        result.get("files", []),
        key=lambda f: f.get("coverage_pct", 100),
    )[:5]:
        file_pct = file_info.get("coverage_pct", 100)
        if file_pct < 50:
            actions.append(_Action(
                priority=3.0,
                dimension="docs",
                severity="low",
                action=f"Add docstrings to {file_info['file']} ({file_pct:.0f}% coverage)",
                location=file_info["file"],
            ))

    summary = f"{pct:.0f}% docstring coverage ({documented}/{total} items documented)"
    return _Dimension("docs", pct, 1, summary, actions=actions)


# ── Dimension: types ──────────────────────────────────────────────────────────

def _scan_types(repo_root: Path, path: Optional[str]) -> _Dimension:
    try:
        from ouroboros.tools.type_coverage import _scan_codebase as _type_scan
        result = _type_scan(
            repo_root,
            path or "ouroboros/",
            skip_private=True,
        )
    except ImportError:
        return _Dimension("types", 70.0, 1, "type_coverage unavailable", error="ImportError")
    except Exception as exc:
        return _Dimension("types", 70.0, 1, f"scan error: {exc}", error=str(exc))

    summary_data = result.get("summary", {})
    fully_typed = summary_data.get("fully_typed", 0)
    total_funcs = summary_data.get("total_functions", 0)
    pct = (fully_typed / total_funcs * 100) if total_funcs > 0 else 100.0

    actions: List[_Action] = []
    # Surface files with worst annotation coverage
    for file_info in sorted(
        result.get("files", []),
        key=lambda f: f.get("coverage_pct", 100),
    )[:5]:
        file_pct = file_info.get("coverage_pct", 100)
        if file_pct < 30:
            actions.append(_Action(
                priority=2.5,
                dimension="types",
                severity="low",
                action=f"Add type annotations to {file_info['file']} ({file_pct:.0f}% typed)",
                location=file_info["file"],
            ))

    summary = f"{pct:.0f}% functions fully typed ({fully_typed}/{total_funcs})"
    return _Dimension("types", pct, 1, summary, actions=actions)


# ── Dimension: todos (informational) ─────────────────────────────────────────

def _scan_todos(repo_root: Path, path: Optional[str]) -> _Dimension:
    try:
        from ouroboros.tools.todo_scanner import _scan_codebase as _todo_scan
        result = _todo_scan(repo_root, path, None, "low")
    except ImportError:
        return _Dimension("todos", 100.0, 0, "todo_scanner unavailable", error="ImportError")
    except Exception as exc:
        return _Dimension("todos", 100.0, 0, f"scan error: {exc}", error=str(exc))

    todos = result if isinstance(result, list) else result.get("todos", [])
    n = len(todos)
    n_high = sum(1 for t in todos if t.get("priority") == "high")

    actions: List[_Action] = []
    for t in todos[:5]:
        if t.get("priority") == "high":
            actions.append(_Action(
                priority=4.0,
                dimension="todos",
                severity="medium",
                action=f"Resolve {t.get('tag','TODO')}: {t.get('text','')[:60]}",
                location=f"{t.get('file','')}:{t.get('line','')}",
            ))

    summary = f"{n} annotation(s): {n_high} high priority" if n > 0 else "No TODO/FIXME annotations"
    return _Dimension("todos", 100.0, 0, summary, actions=actions)


# ── Aggregate ─────────────────────────────────────────────────────────────────

def _weighted_score(dimensions: List[_Dimension]) -> float:
    total_weight = sum(d.weight for d in dimensions if d.weight > 0)
    if total_weight == 0:
        return 100.0
    weighted_sum = sum(d.score * d.weight for d in dimensions if d.weight > 0)
    return round(weighted_sum / total_weight, 1)


# ── Formatters ────────────────────────────────────────────────────────────────

def _format_text(
    dimensions: List[_Dimension],
    overall_score: float,
    path: Optional[str],
    max_actions: int,
) -> str:
    letter, icon = _grade(overall_score)
    lines: List[str] = []

    header = f"## Codebase Health Report"
    if path:
        header += f" — {path}"
    lines.append(header)
    lines.append("")

    # Overall score bar
    bar = _bar(overall_score)
    lines.append(f"  Overall  {icon} {letter}   [{bar}]  {overall_score:.0f}/100")
    lines.append("")

    # Per-dimension table
    lines.append("  Dimension    Score   Grade  Summary")
    lines.append("  " + "─" * 70)
    dim_order = ["security", "cycles", "debt", "exceptions", "docs", "types", "todos"]
    dim_map = {d.name: d for d in dimensions}
    for dname in dim_order:
        d = dim_map.get(dname)
        if d is None:
            continue
        dletter, dicon = _grade(d.score) if d.weight > 0 else ("-", "ℹ️")
        if d.weight == 0:
            dletter, dicon = "─", "ℹ️"
        score_str = f"{d.score:.0f}" if d.weight > 0 else " ─ "
        weight_tag = f"(×{d.weight})" if d.weight > 0 else "(info)"
        lines.append(
            f"  {dname:<12} {score_str:>5}   {dicon}{dletter:<3} "
            f"{weight_tag:<7}  {d.summary}"
        )
        if d.error:
            lines.append(f"             ⚠ {d.error}")

    lines.append("")

    # Top actions
    all_actions: List[_Action] = []
    for d in dimensions:
        all_actions.extend(d.actions)
    all_actions.sort(key=lambda a: -a.priority)
    top_actions = all_actions[:max_actions]

    if top_actions:
        lines.append(f"  Top {len(top_actions)} Action Item(s):")
        lines.append("  " + "─" * 70)
        for i, a in enumerate(top_actions, 1):
            sev_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}.get(
                a.severity.lower(), "⚪"
            )
            loc_part = f"  @ {a.location}" if a.location else ""
            lines.append(f"  {i:2}. {sev_icon}[{a.dimension:<10}] {a.action}")
            if loc_part:
                lines.append(f"      {loc_part}")
        lines.append("")
        remaining = len(all_actions) - len(top_actions)
        if remaining > 0:
            lines.append(f"  … and {remaining} more action(s). Use max_actions= to see more.")
    else:
        lines.append("  ✅ No action items — codebase is clean!")

    return "\n".join(lines)


def _format_json(
    dimensions: List[_Dimension],
    overall_score: float,
    path: Optional[str],
    max_actions: int,
) -> str:
    letter, icon = _grade(overall_score)

    all_actions: List[_Action] = []
    for d in dimensions:
        all_actions.extend(d.actions)
    all_actions.sort(key=lambda a: -a.priority)

    return json.dumps(
        {
            "overall_score": overall_score,
            "overall_grade": letter,
            "path": path or "",
            "dimensions": [
                {
                    "name": d.name,
                    "score": round(d.score, 1),
                    "weight": d.weight,
                    "grade": _grade(d.score)[0] if d.weight > 0 else "─",
                    "summary": d.summary,
                    "action_count": len(d.actions),
                    "error": d.error,
                }
                for d in dimensions
            ],
            "top_actions": [a.to_dict() for a in all_actions[:max_actions]],
            "total_actions": len(all_actions),
        },
        indent=2,
    )


# ── Main entry ────────────────────────────────────────────────────────────────

def _health_report(
    ctx: ToolContext,
    path: Optional[str] = None,
    format: str = "text",
    max_actions: int = 10,
    quick: bool = False,
) -> str:
    repo_root = ctx.repo_dir

    # Run dimension scanners
    dims: List[_Dimension] = [
        _scan_security(repo_root, path),
        _scan_cycles(repo_root, path),
        _scan_debt(repo_root, path),
        _scan_exceptions(repo_root, path),
    ]
    if not quick:
        dims += [
            _scan_docs(repo_root, path),
            _scan_types(repo_root, path),
        ]
    dims.append(_scan_todos(repo_root, path))

    overall = _weighted_score(dims)

    if format == "json":
        return _format_json(dims, overall, path, max_actions)
    return _format_text(dims, overall, path, max_actions)


# ── Tool registration ──────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="health_report",
            schema={
                "name": "health_report",
                "description": (
                    "Project-wide codebase health dashboard. Orchestrates security_scan, "
                    "dep_cycles, tech_debt, exception_audit, doc_coverage, type_coverage, "
                    "and todo_scanner into a single A–F grade with ranked action list. "
                    "Replaces ~10 separate tool calls and manual aggregation."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Limit scan to a subdirectory (e.g. 'ouroboros')",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format (default: text)",
                        },
                        "max_actions": {
                            "type": "integer",
                            "description": "Max action items to surface (default 10)",
                        },
                        "quick": {
                            "type": "boolean",
                            "description": "Skip slow doc/type coverage scans (default false)",
                        },
                    },
                },
            },
            handler=lambda ctx, **kw: _health_report(ctx, **kw),
        )
    ]
