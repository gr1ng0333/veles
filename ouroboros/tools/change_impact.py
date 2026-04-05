"""change_impact — blast-radius analysis for a module or file change.

Growth tool: answers "if I modify X, what else is at risk?" in one call.

Given a file path or module name, computes:
  - direct dependents (modules that import X)
  - transitive dependents (full reverse-reachability, BFS)
  - test coverage mapping (which test files cover X and its dependents)
  - risk tiers: CRITICAL (core/loop/agent), HIGH (tools), MEDIUM, LOW

Use before modifying any module to understand blast radius and which
tests to run. Complements semantic_diff (what changed) and
dependency_graph (forward deps). Together they form a full change-safety pipeline.

Examples:
    change_impact(target="ouroboros/tools/registry.py")
    change_impact(target="tools.registry")
    change_impact(target="ouroboros/loop_runtime.py", depth=3)
    change_impact(target="ouroboros/context.py", format="json")
"""
from __future__ import annotations

import json
import os
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ouroboros.tools.dependency_graph import _build_graph, _module_name
from ouroboros.tools.registry import ToolContext, ToolEntry

_REPO_DIR = Path(os.environ.get("OUROBOROS_REPO_DIR", "/opt/veles"))

# Risk tier classification by path keywords
_CRITICAL_KEYWORDS = {"agent", "loop", "context", "llm", "supervisor", "registry", "safety", "memory"}
_HIGH_KEYWORDS = {"tools", "copilot_proxy", "codex_proxy", "loop_runtime", "loop_copilot"}


def _classify_risk(module: str) -> str:
    parts = set(module.replace("/", ".").split("."))
    if parts & _CRITICAL_KEYWORDS:
        return "CRITICAL"
    if parts & _HIGH_KEYWORDS:
        return "HIGH"
    return "MEDIUM"


def _build_reverse_graph(adjacency: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    """Invert adjacency: target → set of modules that import it."""
    rev: Dict[str, Set[str]] = defaultdict(set)
    for src, targets in adjacency.items():
        for tgt in targets:
            rev[tgt].add(src)
    return dict(rev)


def _bfs_transitive(reverse: Dict[str, Set[str]], start: str, depth: int) -> Dict[str, int]:
    """BFS over reverse graph. Returns {module: distance} up to depth."""
    visited: Dict[str, int] = {start: 0}
    queue: deque = deque([(start, 0)])
    while queue:
        node, d = queue.popleft()
        if d >= depth:
            continue
        for dependent in sorted(reverse.get(node, [])):
            if dependent not in visited and not dependent.startswith("[ext]"):
                visited[dependent] = d + 1
                queue.append((dependent, d + 1))
    return visited


def _find_test_files(repo_dir: Path, module_short: str) -> List[str]:
    """Find test files that likely cover a module by name heuristic."""
    # module_short: e.g. "registry" or "tools.registry"
    short = module_short.split(".")[-1]
    candidates = []
    tests_dir = repo_dir / "tests"
    if not tests_dir.exists():
        return []
    for f in sorted(tests_dir.glob("test_*.py")):
        stem = f.stem  # e.g. "test_registry"
        if stem == f"test_{short}" or short in stem:
            candidates.append(str(f.relative_to(repo_dir)))
    return candidates


def _resolve_target(target: str, repo_dir: Path) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolve target (file path or dotted module) to (module_name, file_path).
    Returns (None, None) if not found.
    """
    # Try as file path first
    if "/" in target or target.endswith(".py"):
        p = repo_dir / target
        if not p.exists():
            # try without repo prefix
            candidates = list(repo_dir.rglob(Path(target).name))
            if candidates:
                p = candidates[0]
            else:
                return None, None
        # Determine containing package root
        for pkg_root in [repo_dir / "ouroboros", repo_dir / "supervisor", repo_dir]:
            if str(p).startswith(str(pkg_root)):
                mod = _module_name(p, pkg_root)
                return mod, str(p.relative_to(repo_dir))
        return _module_name(p, repo_dir), str(p.relative_to(repo_dir))

    # Try as dotted module name
    # Build module map from ouroboros + supervisor
    for pkg_root in [repo_dir / "ouroboros", repo_dir / "supervisor"]:
        if not pkg_root.exists():
            continue
        for f in pkg_root.rglob("*.py"):
            mod = _module_name(f, pkg_root)
            short_mod = ".".join(mod.split(".")[1:]) if "." in mod else mod
            if mod == target or short_mod == target or mod.endswith("." + target) or mod == target.split(".")[-1]:
                return mod, str(f.relative_to(repo_dir))

    return None, None


def _change_impact(
    ctx: ToolContext,
    target: str = "",
    depth: int = 5,
    directory: str = "ouroboros",
    format: str = "text",
    show_test_map: bool = True,
) -> str:
    """Compute blast radius of changing a module."""
    if not target:
        return json.dumps({"error": "target is required (file path or module name)"}, indent=2)

    repo_dir = _REPO_DIR.resolve()
    pkg_root = (repo_dir / directory).resolve()
    if not pkg_root.exists():
        return json.dumps({"error": f"Directory not found: {directory}"}, indent=2)

    # Build forward graph
    adjacency, all_nodes = _build_graph(pkg_root, include_external=False)
    reverse = _build_reverse_graph(adjacency)

    # Resolve target to module name
    mod_name, file_path = _resolve_target(target, repo_dir)
    if mod_name is None:
        # Try matching by suffix in known nodes
        suffix = target.replace("/", ".").replace(".py", "").split(".")[-1]
        candidates = [n for n in all_nodes if n.split(".")[-1] == suffix]
        if candidates:
            mod_name = candidates[0]
            file_path = target
        else:
            return json.dumps({
                "error": f"Cannot resolve target: '{target}'",
                "hint": [n for n in sorted(all_nodes) if suffix.lower() in n.lower()][:10],
            }, indent=2)

    # Strip package prefix for graph lookup (graph uses relative names)
    # Graph keys look like "agent", "tools.registry", etc.
    # mod_name might be "ouroboros.tools.registry" or "tools.registry"
    def _strip_pkg(name: str) -> str:
        if name.startswith(f"{directory}."):
            return name[len(directory) + 1:]
        return name

    graph_key = _strip_pkg(mod_name)

    # Direct dependents (who directly imports target)
    direct_deps = sorted(reverse.get(graph_key, []))

    # Transitive dependents (BFS up to depth)
    transitive_map = _bfs_transitive(reverse, graph_key, depth)
    # Remove self
    transitive_map.pop(graph_key, None)

    # Classify by distance
    distance_1 = {m for m, d in transitive_map.items() if d == 1}
    distance_2plus = {m: d for m, d in transitive_map.items() if d > 1}

    # All affected modules
    all_affected = sorted(transitive_map.keys())

    # Risk classification
    risk_counts: Dict[str, int] = defaultdict(int)
    risk_items: Dict[str, List[str]] = defaultdict(list)
    for m in all_affected:
        risk = _classify_risk(m)
        risk_counts[risk] += 1
        risk_items[risk].append(m)

    # Overall risk score
    critical_count = risk_counts.get("CRITICAL", 0)
    high_count = risk_counts.get("HIGH", 0)
    if critical_count > 0:
        overall_risk = "CRITICAL"
    elif high_count > 3:
        overall_risk = "HIGH"
    elif len(all_affected) > 10:
        overall_risk = "HIGH"
    elif len(all_affected) > 5:
        overall_risk = "MEDIUM"
    else:
        overall_risk = "LOW"

    # Test coverage mapping
    test_files: List[str] = []
    if show_test_map:
        # Tests for the target module itself
        test_files.extend(_find_test_files(repo_dir, graph_key))
        # Tests for affected modules (up to depth=1 only to avoid noise)
        for m in sorted(distance_1):
            t = _find_test_files(repo_dir, m)
            for tf in t:
                if tf not in test_files:
                    test_files.append(tf)

    # Also: test_smoke always relevant
    smoke = "tests/test_smoke.py"
    if smoke not in test_files:
        test_files.insert(0, smoke)

    result = {
        "target": graph_key,
        "file": file_path or target,
        "overall_risk": overall_risk,
        "direct_dependents": direct_deps,
        "direct_count": len(direct_deps),
        "transitive_count": len(all_affected),
        "blast_radius": {
            "depth_1": sorted(distance_1),
            "depth_2plus": sorted(distance_2plus.keys()),
        },
        "risk_breakdown": {
            "CRITICAL": sorted(risk_items.get("CRITICAL", [])),
            "HIGH": sorted(risk_items.get("HIGH", [])),
            "MEDIUM": sorted(risk_items.get("MEDIUM", [])),
            "LOW": sorted(risk_items.get("LOW", [])),
        },
        "recommended_tests": test_files,
        "depth_searched": depth,
    }

    if format == "json":
        return json.dumps(result, ensure_ascii=False, indent=2)

    # Text format
    lines = [
        f"change_impact: {graph_key}",
        f"  file        : {result['file']}",
        f"  risk        : {overall_risk}  ({len(all_affected)} modules affected transitively)",
        "",
        f"Direct dependents ({len(direct_deps)}):",
    ]
    for m in direct_deps:
        lines.append(f"  - {m}  [{_classify_risk(m)}]")

    if distance_2plus:
        lines.append(f"\nTransitive dependents (depth 2-{depth}, {len(distance_2plus)}):")
        for m in sorted(distance_2plus, key=lambda x: (distance_2plus[x], x)):
            lines.append(f"  d{distance_2plus[m]} {m}  [{_classify_risk(m)}]")

    if critical_count > 0 or high_count > 0:
        lines.append("\n⚠️  Risk items:")
        for m in sorted(risk_items.get("CRITICAL", [])):
            lines.append(f"  CRITICAL  {m}")
        for m in sorted(risk_items.get("HIGH", []))[:10]:
            lines.append(f"  HIGH      {m}")

    lines.append(f"\nRecommended tests to run ({len(test_files)}):")
    for t in test_files:
        lines.append(f"  pytest {t}")

    return "\n".join(lines)


def get_tools() -> List[ToolEntry]:
    return [ToolEntry(
        name="change_impact",
        schema={
            "name": "change_impact",
            "description": (
                "Blast-radius analysis: given a module or file, shows which other modules "
                "depend on it (directly and transitively), risk tiers (CRITICAL/HIGH/MEDIUM/LOW), "
                "and which tests to run before committing. "
                "Use before modifying any module to understand change scope. "
                "Complements semantic_diff (what changed) and dependency_graph (forward deps)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "File path (e.g. 'ouroboros/tools/registry.py') or dotted module name (e.g. 'tools.registry').",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Max transitive depth for BFS traversal. Default: 5.",
                    },
                    "directory": {
                        "type": "string",
                        "description": "Package root directory relative to repo. Default: 'ouroboros'.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["text", "json"],
                        "description": "Output format. Default: text.",
                    },
                    "show_test_map": {
                        "type": "boolean",
                        "description": "Include recommended test files. Default: true.",
                    },
                },
                "required": ["target"],
            },
        },
        handler=_change_impact,
    )]
