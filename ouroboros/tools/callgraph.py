"""callgraph — function-level call graph analysis.

Where change_impact and dep_cycles work at the module/import level,
callgraph works at the **function** level: who calls whom within the codebase.

Answers two key questions:
  - "If I modify function X, which functions will be affected?" (callers, reverse graph)
  - "What does function X depend on internally?"  (callees, forward graph)

Works by parsing AST Call expressions inside every function body across all
scanned Python files.  Callee names are resolved on a best-effort basis
(simple attribute access chains + bare names); dynamic / computed calls are
ignored.

Examples:
    callgraph(function="_build_graph")            # both callers and callees
    callgraph(function="_build_graph", direction="callers")
    callgraph(function="execute", depth=2)        # transitive callers, depth 2
    callgraph(path="ouroboros/tools/registry.py") # full graph for one file
    callgraph(path="ouroboros/tools/", function="get_tools")
    callgraph(format="json")                      # machine-readable
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

_REPO_DIR = pathlib.Path(os.environ.get("OUROBOROS_REPO_DIR", "/opt/veles"))

_SKIP_DIRS = {
    "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
    "node_modules", ".venv", "venv", "dist", "build",
}

# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class FuncNode:
    """Fully-qualified function node in the call graph."""
    key: str            # "file.py::ClassName.method" or "file.py::func"
    file: str           # relative path
    name: str           # bare function/method name
    class_name: str     # parent class name, or ""
    line: int


@dataclass
class CallGraph:
    """Bidirectional call graph."""
    nodes: Dict[str, FuncNode] = field(default_factory=dict)
    # callee_key → set[caller_key]
    callers: Dict[str, Set[str]] = field(default_factory=lambda: defaultdict(set))
    # caller_key → set[callee_key]
    callees: Dict[str, Set[str]] = field(default_factory=lambda: defaultdict(set))


# ── AST helpers ───────────────────────────────────────────────────────────────

def _call_name(node: ast.Call) -> Optional[str]:
    """
    Extract the called function name from a Call AST node.
    Returns a best-effort string like 'func', 'obj.method', 'cls.method',
    or None if too dynamic (subscript, *args, etc.).
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        # a.b.c(…) → "a.b.c"
        parts: List[str] = [func.attr]
        cur: Any = func.value
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _collect_py_files(root: pathlib.Path) -> List[pathlib.Path]:
    if root.is_file() and root.suffix == ".py":
        return [root]
    files: List[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in sorted(filenames):
            if fname.endswith(".py"):
                files.append(pathlib.Path(dirpath) / fname)
    return files


def _rel(path: pathlib.Path, repo_dir: pathlib.Path) -> str:
    try:
        return str(path.relative_to(repo_dir))
    except ValueError:
        return str(path)


# ── Graph builder ─────────────────────────────────────────────────────────────

def _build_callgraph(py_files: List[pathlib.Path], repo_dir: pathlib.Path) -> CallGraph:
    """
    Parse all files, extract function definitions and their call expressions,
    build a bidirectional call graph.

    Node keys are  "rel/path.py::ClassName.method"  or  "rel/path.py::func".
    Calls are resolved by bare name first (within-file lookup), then by
    attribute tail (e.g. 'self.helper' → 'helper').
    """
    cg = CallGraph()

    # ── Pass 1: collect all function defs  ────────────────────────────────
    # file_funcs: rel_path → {bare_name → [node_key, ...]}  (many same-name fns)
    file_funcs: Dict[str, Dict[str, List[str]]] = {}
    # name_to_keys: bare_name → [node_key]  (global, may have collisions)
    name_to_keys: Dict[str, List[str]] = defaultdict(list)

    for py_file in py_files:
        try:
            src = py_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src, filename=str(py_file))
        except (SyntaxError, OSError):
            continue

        rel = _rel(py_file, repo_dir)
        file_funcs[rel] = defaultdict(list)

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                cls_name = node.name
                for item in ast.walk(node):
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        # Only direct methods — not nested funcs inside methods
                        # (Walk gives us all, so filter by parent later)
                        # Simple approach: register all methods found in class body
                        if any(item is m for m in ast.walk(node) if
                               isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                               and m is item):
                            fn_node = FuncNode(
                                key=f"{rel}::{cls_name}.{item.name}",
                                file=rel,
                                name=item.name,
                                class_name=cls_name,
                                line=item.lineno,
                            )
                            cg.nodes[fn_node.key] = fn_node
                            file_funcs[rel][item.name].append(fn_node.key)
                            name_to_keys[item.name].append(fn_node.key)

        # Top-level functions
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn_node = FuncNode(
                    key=f"{rel}::{node.name}",
                    file=rel,
                    name=node.name,
                    class_name="",
                    line=node.lineno,
                )
                cg.nodes[fn_node.key] = fn_node
                file_funcs[rel][node.name].append(fn_node.key)
                name_to_keys[node.name].append(fn_node.key)

    # ── Pass 2: collect calls inside each function ─────────────────────────
    for py_file in py_files:
        try:
            src = py_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src, filename=str(py_file))
        except (SyntaxError, OSError):
            continue

        rel = _rel(py_file, repo_dir)

        def _process_func_body(
            fn_node_key: str,
            func_body: ast.AST,
        ) -> None:
            for node in ast.walk(func_body):
                if isinstance(node, ast.Call):
                    raw = _call_name(node)
                    if raw is None:
                        continue
                    # bare name: "foo"
                    bare = raw.split(".")[-1]
                    # Try exact match in same file first
                    candidates: List[str] = file_funcs.get(rel, {}).get(bare, [])
                    # Also check global name map
                    if not candidates:
                        candidates = name_to_keys.get(bare, [])
                    # Deduplicate: if only one candidate, use it; if multiple, add all
                    for callee_key in candidates:
                        if callee_key != fn_node_key:
                            cg.callees[fn_node_key].add(callee_key)
                            cg.callers[callee_key].add(fn_node_key)

        # Walk class methods
        for cls_node in ast.walk(tree):
            if isinstance(cls_node, ast.ClassDef):
                for item in cls_node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        key = f"{rel}::{cls_node.name}.{item.name}"
                        if key in cg.nodes:
                            _process_func_body(key, item)

        # Walk top-level functions
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = f"{rel}::{node.name}"
                if key in cg.nodes:
                    _process_func_body(key, node)

    return cg


# ── Traversal helpers ──────────────────────────────────────────────────────────

def _bfs_direction(
    cg: CallGraph,
    start_keys: Set[str],
    direction: str,   # "callers" | "callees"
    depth: int,
) -> Dict[str, int]:
    """BFS in one direction. Returns {key: distance}."""
    edge_map = cg.callers if direction == "callers" else cg.callees
    visited: Dict[str, int] = {}
    for sk in start_keys:
        visited[sk] = 0
    queue: deque = deque([(k, 0) for k in start_keys])
    while queue:
        node, d = queue.popleft()
        if d >= depth:
            continue
        for nxt in sorted(edge_map.get(node, [])):
            if nxt not in visited:
                visited[nxt] = d + 1
                queue.append((nxt, d + 1))
    return visited


def _resolve_function_keys(cg: CallGraph, function: str, file_filter: Optional[str]) -> Set[str]:
    """
    Find node keys matching the requested function name.
    If file_filter is given, prefer functions from that file.
    Supports 'ClassName.method', 'method', or full key lookup.
    """
    # Exact key match
    if function in cg.nodes:
        return {function}

    results: Set[str] = set()
    bare = function.split(".")[-1]

    for key, node in cg.nodes.items():
        qualified = f"{node.class_name}.{node.name}" if node.class_name else node.name
        match = (
            node.name == function
            or qualified == function
            or node.name == bare
        )
        if match:
            if file_filter and file_filter not in node.file:
                continue
            results.add(key)

    return results


# ── Formatters ────────────────────────────────────────────────────────────────

def _format_text_focused(
    cg: CallGraph,
    focus_keys: Set[str],
    callers_map: Dict[str, int],
    callees_map: Dict[str, int],
    function: str,
    direction: str,
    depth: int,
    path_label: str,
) -> str:
    lines: List[str] = []
    lines.append(f"## Call Graph — function: {function}")
    lines.append(f"   scope: {path_label}  direction: {direction}  depth: {depth}")
    lines.append("")

    # Show focus nodes
    for fk in sorted(focus_keys):
        n = cg.nodes.get(fk)
        if n:
            lines.append(f"📍 {n.file}::{n.class_name + '.' if n.class_name else ''}{n.name}  (line {n.line})")
    lines.append("")

    if direction in ("callers", "both") and callers_map:
        direct_callers = {k for k, d in callers_map.items() if d == 1 and k not in focus_keys}
        transitive_callers = {k for k, d in callers_map.items() if d > 1 and k not in focus_keys}
        lines.append(f"Callers ({len(direct_callers)} direct, {len(transitive_callers)} transitive):")
        for k in sorted(direct_callers):
            n = cg.nodes.get(k)
            if n:
                qual = f"{n.class_name}.{n.name}" if n.class_name else n.name
                lines.append(f"  ← {n.file}::{qual}  (line {n.line})")
        for k in sorted(transitive_callers, key=lambda x: (callers_map[x], x)):
            n = cg.nodes.get(k)
            if n:
                qual = f"{n.class_name}.{n.name}" if n.class_name else n.name
                lines.append(f"  ←{'←' * (callers_map[k] - 1)} [d{callers_map[k]}] {n.file}::{qual}")
        lines.append("")

    if direction in ("callees", "both") and callees_map:
        direct_callees = {k for k, d in callees_map.items() if d == 1 and k not in focus_keys}
        transitive_callees = {k for k, d in callees_map.items() if d > 1 and k not in focus_keys}
        lines.append(f"Callees ({len(direct_callees)} direct, {len(transitive_callees)} transitive):")
        for k in sorted(direct_callees):
            n = cg.nodes.get(k)
            if n:
                qual = f"{n.class_name}.{n.name}" if n.class_name else n.name
                lines.append(f"  → {n.file}::{qual}  (line {n.line})")
        for k in sorted(transitive_callees, key=lambda x: (callees_map[x], x)):
            n = cg.nodes.get(k)
            if n:
                qual = f"{n.class_name}.{n.name}" if n.class_name else n.name
                lines.append(f"  →{'→' * (callees_map[k] - 1)} [d{callees_map[k]}] {n.file}::{qual}")
        lines.append("")

    if direction in ("callers", "both") and not callers_map:
        lines.append("Callers: none found (function may be an entry point).")
    if direction in ("callees", "both") and not callees_map:
        lines.append("Callees: none found (function calls only external/built-in code).")

    return "\n".join(lines)


def _format_text_overview(
    cg: CallGraph,
    py_files: List[pathlib.Path],
    path_label: str,
    top_k: int,
) -> str:
    """Summary overview when no specific function is requested."""
    lines: List[str] = []
    total_nodes = len(cg.nodes)
    total_edges = sum(len(v) for v in cg.callees.values())

    lines.append(f"## Call Graph Overview — {path_label}")
    lines.append(f"   {total_nodes} functions · {total_edges} call edges · {len(py_files)} files")
    lines.append("")

    if total_nodes == 0:
        lines.append("No function definitions found.")
        return "\n".join(lines)

    # Most-called functions (hot spots in the call graph)
    call_counts = [(k, len(cg.callers.get(k, []))) for k in cg.nodes]
    call_counts.sort(key=lambda x: (-x[1], x[0]))

    lines.append(f"Top {min(top_k, len(call_counts))} most-called functions:")
    for key, cnt in call_counts[:top_k]:
        n = cg.nodes[key]
        qual = f"{n.class_name}.{n.name}" if n.class_name else n.name
        lines.append(f"  {cnt:>4}×  {n.file}::{qual}  (line {n.line})")

    lines.append("")

    # Functions with most callees (complex hubs)
    callee_counts = [(k, len(cg.callees.get(k, []))) for k in cg.nodes]
    callee_counts.sort(key=lambda x: (-x[1], x[0]))
    non_trivial = [(k, c) for k, c in callee_counts if c >= 5]
    if non_trivial:
        lines.append(f"Highest-fanout functions (call ≥5 others, top {min(top_k, len(non_trivial))}):")
        for key, cnt in non_trivial[:top_k]:
            n = cg.nodes[key]
            qual = f"{n.class_name}.{n.name}" if n.class_name else n.name
            lines.append(f"  {cnt:>4} callees  {n.file}::{qual}")
        lines.append("")

    lines.append('Tip: use callgraph(function="<name>") to inspect a specific function.')
    return "\n".join(lines)


# ── Handler ────────────────────────────────────────────────────────────────────

def _callgraph(
    ctx: ToolContext,
    path: str = "ouroboros/",
    function: str = "",
    direction: str = "both",
    depth: int = 1,
    top_k: int = 20,
    format: str = "text",
    _repo_dir: Optional[pathlib.Path] = None,
) -> Any:
    """
    Function-level call graph analysis.

    Args:
        path: Directory or file to scan (relative to repo root). Default: 'ouroboros/'.
        function: Function name to focus on. If empty, shows overview.
            Supports bare name ('_build_graph'), qualified ('MyClass.method'),
            or partial match. Case-sensitive.
        direction: Which edges to show — 'callers', 'callees', or 'both'. Default: 'both'.
        depth: Transitive traversal depth. Default: 1 (direct only).
            Depth 2 = callers-of-callers, etc.
        top_k: In overview mode, how many top functions to show. Default: 20.
        format: 'text' (default) or 'json'.

    Returns:
        Call graph for the specified function, or a codebase-wide overview.
    """
    repo_dir = (_repo_dir or _REPO_DIR).resolve()
    scan_root = repo_dir / path.rstrip("/")

    if not scan_root.exists():
        msg = f"Path not found: {path}"
        if format == "json":
            return json.dumps({"error": msg})
        return msg

    if direction not in ("callers", "callees", "both"):
        direction = "both"
    depth = max(1, min(10, depth))
    top_k = max(1, min(50, top_k))

    py_files = _collect_py_files(scan_root)
    # Also include ouroboros root for cross-file resolution when scanning a subdir
    if scan_root != repo_dir / "ouroboros" and (repo_dir / "ouroboros").exists():
        all_files = _collect_py_files(repo_dir / "ouroboros")
    else:
        all_files = py_files

    cg = _build_callgraph(all_files, repo_dir)

    path_label = path

    # ── Overview mode ──────────────────────────────────────────────────────
    if not function:
        if format == "json":
            call_counts = {
                k: len(cg.callers.get(k, [])) for k in cg.nodes
            }
            callee_counts = {
                k: len(cg.callees.get(k, [])) for k in cg.nodes
            }
            # Only return functions actually in the scanned path
            py_rels = {_rel(f, repo_dir) for f in py_files}
            filtered_nodes = {
                k: {"file": n.file, "name": n.name, "class": n.class_name,
                    "line": n.line, "callers": call_counts.get(k, 0),
                    "callees": callee_counts.get(k, 0)}
                for k, n in cg.nodes.items() if n.file in py_rels
            }
            return json.dumps({
                "total_functions": len(filtered_nodes),
                "total_edges": sum(v["callees"] for v in filtered_nodes.values()),
                "functions": filtered_nodes,
            }, indent=2, ensure_ascii=False)

        return _format_text_overview(cg, py_files, path_label, top_k)

    # ── Focused mode ───────────────────────────────────────────────────────
    focus_keys = _resolve_function_keys(cg, function, path)
    if not focus_keys:
        msg = f"Function '{function}' not found in {path}. Check spelling or widen the path."
        if format == "json":
            return json.dumps({"error": msg, "function": function, "path": path})
        return msg

    callers_map: Dict[str, int] = {}
    callees_map: Dict[str, int] = {}

    if direction in ("callers", "both"):
        callers_map = _bfs_direction(cg, focus_keys, "callers", depth)
        # Remove the focus nodes themselves
        for k in focus_keys:
            callers_map.pop(k, None)

    if direction in ("callees", "both"):
        callees_map = _bfs_direction(cg, focus_keys, "callees", depth)
        for k in focus_keys:
            callees_map.pop(k, None)

    if format == "json":
        def _node_dict(k: str, dist: int) -> Dict[str, Any]:
            n = cg.nodes.get(k)
            if not n:
                return {"key": k, "distance": dist}
            return {
                "key": k,
                "file": n.file,
                "name": n.name,
                "class": n.class_name,
                "line": n.line,
                "distance": dist,
            }

        focus_list = [
            {"key": k, "file": cg.nodes[k].file, "name": cg.nodes[k].name,
             "class": cg.nodes[k].class_name, "line": cg.nodes[k].line}
            for k in sorted(focus_keys) if k in cg.nodes
        ]

        return json.dumps({
            "function": function,
            "direction": direction,
            "depth": depth,
            "focus": focus_list,
            "callers": [_node_dict(k, d) for k, d in sorted(callers_map.items(), key=lambda x: x[1])],
            "callees": [_node_dict(k, d) for k, d in sorted(callees_map.items(), key=lambda x: x[1])],
        }, indent=2, ensure_ascii=False)

    return _format_text_focused(
        cg, focus_keys, callers_map, callees_map,
        function, direction, depth, path_label,
    )


def get_tools() -> List[ToolEntry]:
    return [ToolEntry(
        name="callgraph",
        schema={
            "name": "callgraph",
            "description": (
                "Function-level call graph analysis.\n\n"
                "Where change_impact works at the module/import level, callgraph works at the "
                "function level: shows who calls a function (callers) and what it calls (callees).\n\n"
                "Usage patterns:\n"
                "  callgraph(function='_build_graph') — full bidirectional view\n"
                "  callgraph(function='execute', direction='callers', depth=2) — who ultimately triggers execute\n"
                "  callgraph(path='ouroboros/tools/registry.py') — overview of one file's call graph\n"
                "  callgraph(path='ouroboros/') — codebase-wide most-called functions\n\n"
                "Complements change_impact (module blast radius), dep_cycles (import cycles), "
                "and semantic_diff (what changed). Together they form a full change-safety pipeline."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory or file to scan (relative to repo root). Default: 'ouroboros/'.",
                    },
                    "function": {
                        "type": "string",
                        "description": (
                            "Function name to focus on. If empty, shows a codebase overview. "
                            "Supports bare name ('_build_graph'), class-qualified ('MyClass.method'), "
                            "or partial match. Case-sensitive."
                        ),
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["callers", "callees", "both"],
                        "description": "Which edges to show. Default: 'both'.",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Transitive traversal depth. Default: 1 (direct only). Max: 10.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "In overview mode, how many top functions to show. Default: 20.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["text", "json"],
                        "description": "Output format. Default: 'text'.",
                    },
                },
                "required": [],
            },
        },
        handler=_callgraph,
    )]
