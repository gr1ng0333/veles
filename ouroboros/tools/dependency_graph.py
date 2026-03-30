"""dependency_graph — AST-based import dependency analysis for the codebase.

Growth tool: answers "who depends on what" without manually reading files.

Modes:
  summary  - hub modules (top imported/importers), orphans, leaves
  cycles   - circular import detection
  module   - fans-in / fans-out for a specific module
  path     - shortest dependency path between two modules
  edges    - full edge list
"""
from __future__ import annotations
import ast, json, os
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from ouroboros.tools.registry import ToolContext, ToolEntry

_REPO_DIR = Path(os.environ.get("OUROBOROS_REPO_DIR", "/opt/veles"))


def _module_name(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else root.name


def _parse_imports(path: Path) -> List[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except SyntaxError:
        return []
    imports: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            prefix = "." * (node.level or 0)
            imports.append(prefix + node.module)
    return imports


def _build_graph(root: Path, include_external: bool = False) -> Tuple[Dict[str, Set[str]], Set[str]]:
    py_files = sorted(root.rglob("*.py"))
    module_map: Dict[str, Path] = {}
    for f in py_files:
        name = _module_name(f, root)
        module_map[name] = f

    prefix = root.name
    adjacency: Dict[str, Set[str]] = defaultdict(set)
    all_nodes: Set[str] = set()

    for mod_name, f in module_map.items():
        all_nodes.add(mod_name)
        imports = _parse_imports(f)
        for imp in imports:
            if imp.startswith("."):
                parts = mod_name.split(".")
                dots = len(imp) - len(imp.lstrip("."))
                base = ".".join(parts[:max(0, len(parts) - dots)])
                rest = imp.lstrip(".")
                resolved = f"{base}.{rest}" if rest else base
                if resolved in module_map:
                    adjacency[mod_name].add(resolved)
                    all_nodes.add(resolved)
            elif imp.startswith(prefix):
                matched = None
                for length in range(imp.count(".") + 1, 0, -1):
                    candidate = ".".join(imp.split(".")[1:length+1])
                    if candidate in module_map:
                        matched = candidate
                        break
                if matched:
                    adjacency[mod_name].add(matched)
                    all_nodes.add(matched)
            elif include_external:
                ext = "[ext]" + imp.split(".")[0]
                adjacency[mod_name].add(ext)
                all_nodes.add(ext)

    return dict(adjacency), all_nodes


def _find_cycles(adjacency: Dict[str, Set[str]]) -> List[List[str]]:
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = defaultdict(int)
    cycles: List[List[str]] = []
    path: List[str] = []

    def dfs(node: str) -> None:
        color[node] = GRAY
        path.append(node)
        for nb in sorted(adjacency.get(node, [])):
            if nb.startswith("[ext]"):
                continue
            if color[nb] == GRAY:
                idx = path.index(nb)
                cycle = path[idx:] + [nb]
                key = tuple(sorted(cycle))
                if not any(tuple(sorted(c)) == key for c in cycles):
                    cycles.append(cycle)
            elif color[nb] == WHITE:
                dfs(nb)
        path.pop()
        color[node] = BLACK

    all_nodes = set(adjacency.keys())
    for nb_set in adjacency.values():
        all_nodes.update(nb_set)
    for node in sorted(all_nodes):
        if not node.startswith("[ext]") and color[node] == WHITE:
            dfs(node)
    return cycles[:20]


def _compute_in_degree(adjacency: Dict[str, Set[str]], all_nodes: Set[str]) -> Dict[str, int]:
    in_deg: Dict[str, int] = {n: 0 for n in all_nodes}
    for targets in adjacency.values():
        for t in targets:
            if t in in_deg:
                in_deg[t] += 1
    return in_deg


def _bfs_path(adjacency: Dict[str, Set[str]], src: str, dst: str) -> Optional[List[str]]:
    if src == dst:
        return [src]
    visited = {src}
    queue: deque = deque([[src]])
    while queue:
        p = queue.popleft()
        cur = p[-1]
        for nb in sorted(adjacency.get(cur, [])):
            if nb in visited:
                continue
            np = p + [nb]
            if nb == dst:
                return np
            visited.add(nb)
            queue.append(np)
    return None


def _fans_in(adjacency: Dict[str, Set[str]], module: str) -> List[str]:
    return sorted(src for src, targets in adjacency.items() if module in targets)


def _dependency_graph(
    ctx: ToolContext,
    directory: str = "ouroboros",
    mode: str = "summary",
    module: str = "",
    from_module: str = "",
    to_module: str = "",
    include_external: bool = False,
    limit: int = 20,
) -> str:
    root = (_REPO_DIR / directory).resolve()
    if not root.exists() or not root.is_dir():
        return json.dumps({"error": f"Directory not found: {directory}"}, indent=2)

    adjacency, all_nodes = _build_graph(root, include_external=include_external)
    internal_nodes = {n for n in all_nodes if not n.startswith("[ext]")}
    in_deg = _compute_in_degree(adjacency, all_nodes)

    if mode == "summary":
        top_imported = sorted([(n, in_deg.get(n, 0)) for n in internal_nodes], key=lambda x: -x[1])[:limit]
        top_importers = sorted([(n, len(adjacency.get(n, []))) for n in internal_nodes], key=lambda x: -x[1])[:limit]
        leaves = sorted(n for n in internal_nodes if not any(not t.startswith("[ext]") for t in adjacency.get(n, [])))
        orphans = sorted(n for n in internal_nodes if in_deg.get(n, 0) == 0 and "__main__" not in n)
        total_edges = sum(len([t for t in v if not t.startswith("[ext]")]) for v in adjacency.values())
        return json.dumps({
            "directory": directory,
            "modules": len(internal_nodes),
            "internal_edges": total_edges,
            "top_imported": [{"module": n, "imported_by": c} for n, c in top_imported],
            "top_importers": [{"module": n, "imports_count": c} for n, c in top_importers],
            "leaf_modules": leaves[:limit],
            "orphan_modules": orphans[:limit],
        }, ensure_ascii=False, indent=2)

    elif mode == "cycles":
        cycles = _find_cycles(adjacency)
        return json.dumps({
            "directory": directory,
            "cycles_found": len(cycles),
            "cycles": [{"length": len(c)-1, "path": c} for c in cycles[:limit]],
        }, ensure_ascii=False, indent=2)

    elif mode == "module":
        if not module:
            return json.dumps({"error": "mode=module requires 'module' parameter"}, indent=2)
        matched = module
        if module not in internal_nodes:
            candidates = [n for n in internal_nodes if n.endswith(module.replace("/", "."))]
            if candidates:
                matched = candidates[0]
            else:
                return json.dumps({"error": f"Module '{module}' not found",
                    "hint": [n for n in sorted(internal_nodes) if module.split(".")[-1] in n][:10]}, indent=2)
        imports_int = sorted(t for t in adjacency.get(matched, []) if not t.startswith("[ext]"))
        imports_ext = sorted(t[5:] for t in adjacency.get(matched, []) if t.startswith("[ext]"))
        fans = _fans_in(adjacency, matched)
        return json.dumps({
            "module": matched,
            "imports_internal": imports_int,
            "imports_external": imports_ext if include_external else "(use include_external=true)",
            "imported_by": fans,
            "in_degree": in_deg.get(matched, 0),
            "out_degree_internal": len(imports_int),
        }, ensure_ascii=False, indent=2)

    elif mode == "path":
        if not from_module or not to_module:
            return json.dumps({"error": "mode=path requires 'from_module' and 'to_module'"}, indent=2)
        def resolve_mod(name: str) -> Optional[str]:
            if name in internal_nodes:
                return name
            c = [n for n in internal_nodes if n.endswith(name.replace("/", "."))]
            return c[0] if c else None
        src, dst = resolve_mod(from_module), resolve_mod(to_module)
        if not src:
            return json.dumps({"error": f"from_module '{from_module}' not found"}, indent=2)
        if not dst:
            return json.dumps({"error": f"to_module '{to_module}' not found"}, indent=2)
        p = _bfs_path(adjacency, src, dst)
        return json.dumps({"from": src, "to": dst, "reachable": p is not None, "path": p, "path_length": len(p)-1 if p else None}, ensure_ascii=False, indent=2)

    elif mode == "edges":
        edges = [{"from": s, "to": t} for s in sorted(adjacency) if not s.startswith("[ext]")
                 for t in sorted(adjacency[s]) if include_external or not t.startswith("[ext]")]
        return json.dumps({"directory": directory, "total_edges": len(edges), "edges": edges[:limit*10]}, ensure_ascii=False, indent=2)

    return json.dumps({"error": f"Unknown mode: {mode}", "available_modes": ["summary","cycles","module","path","edges"]}, indent=2)


def get_tools() -> List[ToolEntry]:
    return [ToolEntry(
        name="dependency_graph",
        schema={
            "name": "dependency_graph",
            "description": (
                "Analyse Python import dependencies in a directory using AST. "
                "Modes: 'summary' (hub modules, orphans, leaves, edges count), "
                "'cycles' (circular imports), 'module' (what a module imports / who imports it), "
                "'path' (shortest import path between two modules), 'edges' (full edge list). "
                "Use to understand architecture without reading files manually."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Directory to analyse relative to repo root. Default: 'ouroboros'."},
                    "mode": {"type": "string", "enum": ["summary","cycles","module","path","edges"], "description": "Analysis mode. Default: summary."},
                    "module": {"type": "string", "description": "Module name for mode=module (dotted or short suffix)."},
                    "from_module": {"type": "string", "description": "Source module for mode=path."},
                    "to_module": {"type": "string", "description": "Target module for mode=path."},
                    "include_external": {"type": "boolean", "description": "Include external library imports. Default: false."},
                    "limit": {"type": "integer", "description": "Max items to return in lists. Default: 20."},
                },
                "required": [],
            },
        },
        handler=_dependency_graph,
    )]
