"""dep_cycles — circular import detector for the Python codebase.

Uses Tarjan's Strongly Connected Components (SCC) algorithm on the
AST-based import graph to find and report circular import chains.

Circular imports are real bugs: at runtime they cause ImportError,
partially-initialised modules, or subtle attribute-not-found errors.
Even if Python loads them "successfully" today, refactoring a single
init order breaks everything.

What this tool reports:
  - Every SCC (cycle group) with 2+ modules
  - The shortest cycle path within each SCC
  - Line-level import statement that creates the cycle back-edge
  - Severity: CRITICAL (core agent/loop/registry), HIGH (tools),
    MEDIUM (everything else)
  - Actionable hint: which import to break and how

Different from ``dependency_graph(mode='cycles')``:
  - Tarjan SCC vs ad-hoc DFS coloring → finds ALL SCCs, not just 20
  - Per-cycle line numbers (exact import statement that closes the loop)
  - Severity classification
  - Actionable refactor hint per cycle
  - Text output with human-readable formatting

Examples:
    dep_cycles()                              # full scan, text output
    dep_cycles(path="ouroboros/")             # limit to subdir
    dep_cycles(min_length=3)                  # only cycles ≥ 3 modules
    dep_cycles(severity="critical")           # only CRITICAL cycles
    dep_cycles(format="json")                 # machine-readable
"""
from __future__ import annotations

import ast
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

_REPO_DIR = Path(os.environ.get("OUROBOROS_REPO_DIR", "/opt/veles"))

_SKIP_DIRS = {
    "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
    "node_modules", ".venv", "venv", "dist", "build",
}

# Severity classification by module name keywords
_CRITICAL_KEYWORDS = {
    "agent", "loop", "context", "llm", "registry", "safety",
    "memory", "supervisor", "consolidator", "consciousness",
}
_HIGH_KEYWORDS = {"tools", "copilot_proxy", "codex_proxy", "loop_runtime", "loop_copilot"}

_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}


# ── File / module helpers ─────────────────────────────────────────────────────

def _collect_py_files(root: Path, subpath: Optional[str] = None) -> List[Path]:
    target = root
    if subpath:
        candidate = root / subpath.lstrip("/")
        if candidate.exists():
            target = candidate
    if target.is_file() and target.suffix == ".py":
        return [target]
    py_files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(str(target)):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        for fname in sorted(filenames):
            if fname.endswith(".py"):
                py_files.append(Path(dirpath) / fname)
    return py_files


def _module_key(path: Path, root: Path) -> str:
    """Dotted module name relative to root, e.g. 'tools.registry'."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return str(path)
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else root.name


# ── Import graph builder ──────────────────────────────────────────────────────

def _parse_imports(path: Path) -> List[Tuple[str, int]]:
    """Parse all import statements. Returns (raw_module_name, lineno)."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=str(path))
    except (SyntaxError, OSError):
        return []
    results: List[Tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                results.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                prefix = "." * (node.level or 0)
                results.append((prefix + node.module, node.lineno))
    return results


def _resolve_relative(
    importer_mod: str, raw_import: str
) -> Optional[str]:
    """Resolve a relative import (e.g. '..utils') to a dotted name."""
    if not raw_import.startswith("."):
        return None
    dots = len(raw_import) - len(raw_import.lstrip("."))
    rest = raw_import.lstrip(".")
    parts = importer_mod.split(".")
    # Go up 'dots' levels
    base_parts = parts[:max(0, len(parts) - dots)]
    if rest:
        return ".".join(base_parts + [rest])
    return ".".join(base_parts) if base_parts else None


def _build_import_graph(
    py_files: List[Path],
    root: Path,
) -> Tuple[
    Dict[str, List[Tuple[str, int]]],   # adjacency: mod → [(dep_mod, lineno)]
    Dict[str, Path],                     # mod_name → file path
]:
    """Build directed import graph for all py_files under root."""
    # Build module name map first
    mod_map: Dict[str, Path] = {}
    for f in py_files:
        key = _module_key(f, root)
        mod_map[key] = f

    known = set(mod_map.keys())
    adj: Dict[str, List[Tuple[str, int]]] = defaultdict(list)

    for mod, path in mod_map.items():
        raw_imports = _parse_imports(path)
        for raw, lineno in raw_imports:
            # Try relative first
            if raw.startswith("."):
                resolved = _resolve_relative(mod, raw)
                if resolved and resolved in known:
                    adj[mod].append((resolved, lineno))
                continue

            # Absolute import: try progressively shorter prefixes
            # e.g. "ouroboros.tools.registry" → strip "ouroboros." prefix
            pkg_prefix = root.name + "."
            if raw.startswith(pkg_prefix):
                candidate = raw[len(pkg_prefix):]
                # Try full name, then each prefix
                for length in range(candidate.count(".") + 1, 0, -1):
                    dotted = ".".join(candidate.split(".")[:length])
                    if dotted in known:
                        adj[mod].append((dotted, lineno))
                        break
            else:
                # Check if any suffix matches (e.g. raw="tools.registry")
                for length in range(raw.count(".") + 1, 0, -1):
                    dotted = ".".join(raw.split(".")[:length])
                    if dotted in known:
                        adj[mod].append((dotted, lineno))
                        break

    return dict(adj), mod_map


# ── Tarjan SCC ────────────────────────────────────────────────────────────────

def _tarjan_scc(
    nodes: Set[str],
    adj: Dict[str, List[Tuple[str, int]]],
) -> List[List[str]]:
    """
    Tarjan's algorithm. Returns list of SCCs with ≥2 nodes (real cycles).
    Each SCC is a list of module names.
    """
    index_counter = [0]
    stack: List[str] = []
    on_stack: Set[str] = set()
    index: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    sccs: List[List[str]] = []

    def strongconnect(v: str) -> None:
        index[v] = lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        for w, _ in adj.get(v, []):
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])

        if lowlink[v] == index[v]:
            scc: List[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            if len(scc) >= 2:
                sccs.append(scc)

    for node in sorted(nodes):
        if node not in index:
            strongconnect(node)

    return sccs


# ── Cycle extraction ──────────────────────────────────────────────────────────

def _find_shortest_cycle(
    scc_nodes: Set[str],
    adj: Dict[str, List[Tuple[str, int]]],
) -> List[str]:
    """
    Within an SCC, find the shortest simple cycle using BFS from each node.
    Returns the path as a list (first == last element).
    """
    best: List[str] = []
    scc_set = scc_nodes

    for start in sorted(scc_nodes):
        # BFS in the subgraph induced by the SCC
        from collections import deque
        queue: deque = deque([(start, [start])])
        visited_local: Set[str] = {start}

        while queue:
            node, path = queue.popleft()
            for nxt, _ in adj.get(node, []):
                if nxt not in scc_set:
                    continue
                if nxt == start and len(path) >= 2:
                    cycle = path + [start]
                    if not best or len(cycle) < len(best):
                        best = cycle
                    # Don't break — BFS finds shortest for this start
                    continue
                if nxt not in visited_local:
                    visited_local.add(nxt)
                    queue.append((nxt, path + [nxt]))

        if best and len(best) <= 3:
            # Can't get shorter than A→B→A
            break

    return best or (sorted(scc_nodes) + [sorted(scc_nodes)[0]])


def _find_back_edge_line(
    cycle_path: List[str],
    adj: Dict[str, List[Tuple[str, int]]],
) -> Optional[Tuple[str, str, int]]:
    """
    For the closing edge (last → first in cycle), find the lineno.
    Returns (from_mod, to_mod, lineno) or None.
    """
    if len(cycle_path) < 2:
        return None
    # The "back edge" that creates the cycle is: cycle_path[-2] → cycle_path[-1]
    # but since cycle_path[-1] == cycle_path[0], the closing edge is
    # cycle_path[-2] → cycle_path[0]
    src = cycle_path[-2]
    dst = cycle_path[0]
    for dep, lineno in adj.get(src, []):
        if dep == dst:
            return (src, dst, lineno)
    return None


# ── Severity ──────────────────────────────────────────────────────────────────

def _classify_severity(scc_nodes: List[str]) -> str:
    all_parts: Set[str] = set()
    for mod in scc_nodes:
        all_parts.update(mod.replace("/", ".").split("."))
    if all_parts & _CRITICAL_KEYWORDS:
        return "CRITICAL"
    if all_parts & _HIGH_KEYWORDS:
        return "HIGH"
    return "MEDIUM"


def _build_hint(cycle_path: List[str], back_edge: Optional[Tuple[str, str, int]]) -> str:
    """Generate a short actionable refactor hint."""
    if len(cycle_path) < 2:
        return "Break cycle by extracting shared code into a new module."

    # The most common fix: move the import inside the function body
    # or extract the shared interface into a third module
    if back_edge:
        src, dst, line = back_edge
        src_short = src.split(".")[-1]
        dst_short = dst.split(".")[-1]
        return (
            f"Break back-edge: {src_short}:{line} imports {dst_short}. "
            f"Fix: move that import inside the function/method body, "
            f"or extract the shared interface into a new module."
        )

    return (
        f"Break one import in the chain "
        + " → ".join(m.split(".")[-1] for m in cycle_path)
        + ". Move it inside the function body or extract a shared module."
    )


# ── Main scanner ──────────────────────────────────────────────────────────────

def _scan_dep_cycles(
    repo_root: Path,
    subpath: Optional[str],
    min_length: int,
    severity_filter: Optional[str],
) -> Tuple[List[Dict[str, Any]], int]:
    """Scan for cycles. Returns (cycle_records, file_count)."""
    # Always scan the whole repo for graph correctness,
    # but apply subpath as a filter on which files to include as "primary"
    # For cycle detection we need the full graph — partial graphs create false positives
    ouroboros_root = repo_root / "ouroboros"
    supervisor_root = repo_root / "supervisor"

    py_files: List[Path] = []
    for pkg_root in [ouroboros_root, supervisor_root]:
        if pkg_root.exists():
            py_files.extend(_collect_py_files(pkg_root))

    if not py_files:
        py_files = _collect_py_files(repo_root)

    # If subpath given, restrict root for module naming
    scan_root = repo_root / "ouroboros" if ouroboros_root.exists() else repo_root

    adj, mod_map = _build_import_graph(py_files, scan_root)
    all_nodes = set(mod_map.keys())

    # Apply subpath filter: only consider modules within the subpath
    if subpath:
        clean = subpath.rstrip("/").lstrip("/")
        # Convert path to dotted prefix
        dotted_prefix = clean.replace("/", ".").replace(".py", "")
        # Strip known package prefix
        pkg = scan_root.name + "."
        if dotted_prefix.startswith(pkg):
            dotted_prefix = dotted_prefix[len(pkg):]
        all_nodes = {n for n in all_nodes if n.startswith(dotted_prefix) or dotted_prefix in n}

    # Run Tarjan
    sccs = _tarjan_scc(all_nodes, adj)

    records: List[Dict[str, Any]] = []
    for scc in sccs:
        scc_set = set(scc)
        cycle_path = _find_shortest_cycle(scc_set, adj)
        cycle_len = max(0, len(cycle_path) - 1)  # exclude duplicate endpoint

        if cycle_len < min_length:
            continue

        severity = _classify_severity(scc)
        if severity_filter and severity.lower() != severity_filter.lower():
            continue

        back_edge = _find_back_edge_line(cycle_path, adj)
        hint = _build_hint(cycle_path, back_edge)

        records.append({
            "scc_size": len(scc),
            "scc_members": sorted(scc),
            "shortest_cycle_length": cycle_len,
            "shortest_cycle": cycle_path,
            "back_edge": (
                {"from": back_edge[0], "to": back_edge[1], "line": back_edge[2]}
                if back_edge else None
            ),
            "severity": severity,
            "hint": hint,
        })

    # Sort by severity then cycle length
    records.sort(key=lambda r: (_SEVERITY_ORDER.get(r["severity"], 9), r["shortest_cycle_length"]))

    return records, len(py_files)


# ── Formatters ────────────────────────────────────────────────────────────────

def _format_text(
    records: List[Dict[str, Any]],
    total_files: int,
    subpath: Optional[str],
    min_length: int,
    severity_filter: Optional[str],
) -> str:
    lines: List[str] = []
    filters: List[str] = []
    if subpath:
        filters.append(f"path={subpath}")
    if min_length > 2:
        filters.append(f"min_length={min_length}")
    if severity_filter:
        filters.append(f"severity={severity_filter}")
    filter_note = (", " + ", ".join(filters)) if filters else ""

    lines.append(f"## Circular Import Report — {total_files} files scanned{filter_note}")
    lines.append(f"   {len(records)} cycle group(s) found\n")

    if not records:
        lines.append("✅ No circular imports found matching the specified filters.")
        return "\n".join(lines)

    severity_icons = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡"}

    for i, rec in enumerate(records, 1):
        sev = rec["severity"]
        icon = severity_icons.get(sev, "⚪")
        cycle = rec["shortest_cycle"]
        cycle_str = " → ".join(m.split(".")[-1] for m in cycle)

        lines.append(f"{icon} [{sev}] Cycle #{i} — {rec['shortest_cycle_length']} modules")
        lines.append(f"   Path : {cycle_str}")

        if rec["back_edge"]:
            be = rec["back_edge"]
            lines.append(
                f"   Close: {be['from'].split('.')[-1]} line {be['line']} "
                f"imports {be['to'].split('.')[-1]}"
            )

        if rec["scc_size"] > rec["shortest_cycle_length"]:
            extra = rec["scc_size"] - rec["shortest_cycle_length"]
            lines.append(
                f"   SCC  : {rec['scc_size']} modules in component "
                f"(+{extra} tangentially involved)"
            )
            lines.append(
                f"          [{', '.join(rec['scc_members'][:6])}{'...' if len(rec['scc_members']) > 6 else ''}]"
            )

        lines.append(f"   Fix  : {rec['hint']}")
        lines.append("")

    # Summary by severity
    by_sev: Dict[str, int] = defaultdict(int)
    for r in records:
        by_sev[r["severity"]] += 1
    summary_parts = [f"{by_sev[s]} {s}" for s in ["CRITICAL", "HIGH", "MEDIUM"] if s in by_sev]
    lines.append("Summary: " + ", ".join(summary_parts))

    return "\n".join(lines)


# ── Tool entry point ──────────────────────────────────────────────────────────

def _dep_cycles(
    ctx: ToolContext,
    path: Optional[str] = None,
    min_length: int = 2,
    severity: Optional[str] = None,
    format: str = "text",
) -> str:
    """Find and report circular import chains using Tarjan's SCC algorithm."""
    if severity and severity.lower() not in ("critical", "high", "medium"):
        return f"Unknown severity: {severity!r}. Valid: critical, high, medium"

    repo_root = Path(ctx.repo_dir if ctx and ctx.repo_dir else _REPO_DIR)
    records, total_files = _scan_dep_cycles(
        repo_root, path, max(2, min_length), severity
    )

    if format == "json":
        return json.dumps(
            {
                "total_files": total_files,
                "cycles_found": len(records),
                "filters": {
                    "path": path,
                    "min_length": min_length,
                    "severity": severity,
                },
                "cycles": records,
            },
            ensure_ascii=False,
            indent=2,
        )

    return _format_text(records, total_files, path, min_length, severity)


# ── Tool registration ─────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="dep_cycles",
            schema={
                "name": "dep_cycles",
                "description": (
                    "Detect circular import chains in the Python codebase using Tarjan's "
                    "Strongly Connected Components (SCC) algorithm. Reports each cycle group "
                    "with: shortest cycle path, the exact line that closes the loop, "
                    "severity (CRITICAL/HIGH/MEDIUM based on affected modules), and a "
                    "concrete refactor hint on how to break the cycle. "
                    "More thorough than dependency_graph(mode='cycles'): finds ALL SCCs, "
                    "provides line numbers, severity, and actionable hints. "
                    "Use before large refactors or when debugging ImportError issues."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Restrict to modules under this path "
                                "(e.g. 'ouroboros/tools/'). Default: full codebase."
                            ),
                        },
                        "min_length": {
                            "type": "integer",
                            "description": "Minimum cycle length to report (default 2 = A↔B).",
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["critical", "high", "medium"],
                            "description": "Filter by severity level.",
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
            handler=_dep_cycles,
        )
    ]
