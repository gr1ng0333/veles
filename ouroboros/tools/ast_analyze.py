"""ast_analyze — structural AST analysis of Python source files.

Growth tool: understand module structure, function signatures, complexity,
and call relationships without reading the full file content.

Capabilities:
- Per-file breakdown: imports, classes, methods, functions with signatures
- Cyclomatic complexity estimate (branches: if/for/while/try/except/with/assert)
- Call graph: which functions/methods call which other names
- Directory summary: rank files by total complexity / size
- Filter by complexity threshold, include/exclude private symbols
"""

from __future__ import annotations

import ast
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

# ── constants ─────────────────────────────────────────────────────────────────

_DEFAULT_REPO = os.environ.get("REPO_DIR", "/opt/veles")
_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv",
              ".mypy_cache", ".pytest_cache", "dist", "build"}
_MAX_FILES_DIR = 200   # cap for directory mode


# ── AST helpers ───────────────────────────────────────────────────────────────

def _complexity(node: ast.AST) -> int:
    """Estimate cyclomatic complexity: count decision/loop/exception branch nodes."""
    count = 0
    for n in ast.walk(node):
        if isinstance(n, (
            ast.If, ast.For, ast.While, ast.With,
            ast.Try, ast.ExceptHandler,
            ast.Assert, ast.comprehension,
        )):
            count += 1
        # Boolean operators add branches
        elif isinstance(n, ast.BoolOp):
            count += len(n.values) - 1
    return count


def _param_names(args: ast.arguments) -> List[str]:
    """Extract function parameter names (excluding self/cls)."""
    names = []
    all_args = args.posonlyargs + args.args + args.kwonlyargs
    if args.vararg:
        all_args_names = [a.arg for a in all_args]
    else:
        all_args_names = [a.arg for a in all_args]

    # Build with defaults/annotations summary
    result = []
    for i, arg in enumerate(args.posonlyargs + args.args):
        if arg.arg in ("self", "cls"):
            continue
        ann = f": {ast.unparse(arg.annotation)}" if arg.annotation else ""
        result.append(f"{arg.arg}{ann}")
    for arg in args.kwonlyargs:
        if arg.arg in ("self", "cls"):
            continue
        ann = f": {ast.unparse(arg.annotation)}" if arg.annotation else ""
        result.append(f"{arg.arg}{ann}")
    if args.vararg:
        result.append(f"*{args.vararg.arg}")
    if args.kwarg:
        result.append(f"**{args.kwarg.arg}")
    return result


def _return_annotation(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    if node.returns:
        try:
            return ast.unparse(node.returns)
        except Exception:
            return "?"
    return ""


def _docstring(node: ast.AST) -> str:
    """Extract first docstring from a function/class/module."""
    try:
        doc = ast.get_docstring(node)  # type: ignore[arg-type]
        if doc:
            # first line only, truncated
            first = doc.split("\n")[0][:120]
            return first
    except Exception:
        pass
    return ""


def _calls_in(node: ast.AST) -> List[str]:
    """Collect all Call names (flat names and attr chains) inside a node."""
    calls: List[str] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            try:
                calls.append(ast.unparse(n.func).split("(")[0])
            except Exception:
                pass
    # Deduplicate preserving order
    seen: set = set()
    result: List[str] = []
    for c in calls:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


def _analyze_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    include_calls: bool,
    include_private: bool,
) -> Optional[Dict[str, Any]]:
    name = node.name
    if not include_private and name.startswith("_") and not name.startswith("__"):
        return None

    is_async = isinstance(node, ast.AsyncFunctionDef)
    params = _param_names(node.args)
    ret = _return_annotation(node)
    lines = (node.end_lineno or node.lineno) - node.lineno + 1
    cx = _complexity(node)
    doc = _docstring(node)

    entry: Dict[str, Any] = {
        "name": name,
        "async": is_async,
        "params": params,
        "return": ret,
        "line": node.lineno,
        "lines": lines,
        "complexity": cx,
    }
    if doc:
        entry["doc"] = doc
    if include_calls:
        entry["calls"] = _calls_in(node)
    return entry


def _analyze_class(
    node: ast.ClassDef,
    include_calls: bool,
    include_private: bool,
) -> Dict[str, Any]:
    name = node.name
    bases = [ast.unparse(b) for b in node.bases]
    doc = _docstring(node)
    lines = (node.end_lineno or node.lineno) - node.lineno + 1
    cx = _complexity(node)

    methods: List[Dict[str, Any]] = []
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = _analyze_function(item, include_calls, include_private)
            if fn is not None:
                methods.append(fn)

    entry: Dict[str, Any] = {
        "name": name,
        "bases": bases,
        "line": node.lineno,
        "lines": lines,
        "complexity": cx,
        "methods": methods,
    }
    if doc:
        entry["doc"] = doc
    return entry


def _analyze_imports(tree: ast.Module) -> List[str]:
    """Collect import module names from the top-level of a module."""
    imports: List[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = ", ".join(a.name for a in node.names[:5])
            if len(node.names) > 5:
                names += f", ... (+{len(node.names) - 5})"
            imports.append(f"from {mod} import {names}")
    return imports


def _analyze_file(
    path: Path,
    include_calls: bool,
    include_private: bool,
    min_complexity: int,
    sort_by: str,
) -> Dict[str, Any]:
    """Full AST analysis of a single .py file."""
    t0 = time.monotonic()
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"error": f"read error: {e}", "file": str(path)}

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return {"error": f"SyntaxError: {e}", "file": str(path)}

    imports = _analyze_imports(tree)
    classes: List[Dict[str, Any]] = []
    functions: List[Dict[str, Any]] = []
    module_doc = _docstring(tree)

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            cls = _analyze_class(node, include_calls, include_private)
            if cls["complexity"] >= min_complexity or any(
                m["complexity"] >= min_complexity for m in cls["methods"]
            ):
                classes.append(cls)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = _analyze_function(node, include_calls, include_private)
            if fn is not None and fn["complexity"] >= min_complexity:
                functions.append(fn)

    # Sort
    def sort_key(x: Dict) -> Any:
        if sort_by == "complexity":
            return -x.get("complexity", 0)
        elif sort_by == "size":
            return -x.get("lines", 0)
        elif sort_by == "name":
            return x.get("name", "")
        return x.get("line", 0)

    classes.sort(key=sort_key)
    functions.sort(key=sort_key)
    for cls in classes:
        cls["methods"].sort(key=sort_key)

    # Summary stats
    all_fns = functions + [m for c in classes for m in c["methods"]]
    total_lines = sum(f.get("lines", 0) for f in all_fns)
    total_cx = sum(f.get("complexity", 0) for f in all_fns)
    max_cx = max((f.get("complexity", 0) for f in all_fns), default=0)
    high_cx = [f for f in all_fns if f.get("complexity", 0) >= 10]

    elapsed = time.monotonic() - t0

    result: Dict[str, Any] = {
        "file": str(path),
        "lines_total": source.count("\n") + 1,
        "elapsed_sec": round(elapsed, 4),
        "summary": {
            "classes": len(classes),
            "functions": len(functions),
            "methods": sum(len(c["methods"]) for c in classes),
            "total_function_lines": total_lines,
            "total_complexity": total_cx,
            "max_complexity": max_cx,
            "high_complexity_symbols": [f["name"] for f in high_cx],
        },
        "imports": imports,
        "classes": classes,
        "functions": functions,
    }
    if module_doc:
        result["module_doc"] = module_doc
    return result


def _directory_summary(
    root: Path,
    include_private: bool,
    min_complexity: int,
    sort_by: str,
) -> Dict[str, Any]:
    """Summarize all .py files in a directory tree, ranked by total complexity."""
    t0 = time.monotonic()
    py_files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            if f.endswith(".py"):
                py_files.append(Path(dirpath) / f)
        if len(py_files) >= _MAX_FILES_DIR:
            break

    py_files = py_files[:_MAX_FILES_DIR]

    rows: List[Dict[str, Any]] = []
    for p in py_files:
        try:
            source = p.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(p))
        except Exception as e:
            rows.append({"file": str(p.relative_to(root)), "error": str(e)})
            continue

        total_lines = source.count("\n") + 1
        total_cx = 0
        fn_count = 0
        max_cx = 0
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cx = _complexity(node)
                total_cx += cx
                fn_count += 1
                if cx > max_cx:
                    max_cx = cx

        rows.append({
            "file": str(p.relative_to(root)),
            "lines": total_lines,
            "functions": fn_count,
            "total_complexity": total_cx,
            "max_complexity": max_cx,
        })

    # Sort
    if sort_by == "complexity":
        rows.sort(key=lambda r: -r.get("total_complexity", 0))
    elif sort_by == "size":
        rows.sort(key=lambda r: -r.get("lines", 0))
    elif sort_by == "name":
        rows.sort(key=lambda r: r.get("file", ""))
    else:
        rows.sort(key=lambda r: -r.get("total_complexity", 0))

    elapsed = time.monotonic() - t0
    return {
        "root": str(root),
        "files_analyzed": len(rows),
        "elapsed_sec": round(elapsed, 4),
        "files": rows,
    }


# ── tool handler ──────────────────────────────────────────────────────────────

def _handle_ast_analyze(ctx: ToolContext, **kwargs: Any) -> str:
    path_str: str = kwargs.get("path", "")
    mode: str = kwargs.get("mode", "overview")
    sort_by: str = kwargs.get("sort_by", "complexity")
    min_complexity: int = int(kwargs.get("min_complexity", 0))
    include_private: bool = bool(kwargs.get("include_private", True))
    include_calls: bool = bool(kwargs.get("include_calls", False))

    # Resolve path: absolute, or relative to repo, or relative to cwd
    p = Path(path_str)
    if not p.is_absolute():
        candidate = Path(ctx.repo_dir) / p
        if candidate.exists():
            p = candidate
        else:
            candidate2 = Path.cwd() / p
            if candidate2.exists():
                p = candidate2

    if not p.exists():
        return json.dumps({"error": f"path not found: {path_str}"}, ensure_ascii=False)

    if p.is_dir():
        result = _directory_summary(p, include_private, min_complexity, sort_by)
    elif p.suffix == ".py":
        result = _analyze_file(p, include_calls, include_private, min_complexity, sort_by)
    else:
        return json.dumps({"error": f"unsupported file type: {p.suffix} (only .py files supported)"})

    return json.dumps(result, ensure_ascii=False, indent=2)


# ── tool registration ──────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    schema: Dict[str, Any] = {
        "name": "ast_analyze",
        "description": (
            "Structural AST analysis of Python source files. "
            "Returns classes, methods, functions with signatures, line counts, "
            "cyclomatic complexity, and optional call graphs — without reading the full file. "
            "For directories, ranks files by total complexity. "
            "Use to understand module structure, find complex hotspots, and audit dependencies."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Path to a .py file or directory. "
                        "Can be relative to repo root (/opt/veles) or absolute."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["overview", "detail"],
                    "description": (
                        "overview (default): classes + functions with complexity. "
                        "detail: same but also includes all method bodies and sub-items."
                    ),
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["complexity", "size", "name", "line"],
                    "description": "Sort functions/methods/files by: complexity (default), size (lines), name, line number.",
                },
                "min_complexity": {
                    "type": "integer",
                    "description": "Only include symbols with complexity >= this value (default 0 = include all).",
                },
                "include_private": {
                    "type": "boolean",
                    "description": "Include _private symbols (default true). Set false to see only public API.",
                },
                "include_calls": {
                    "type": "boolean",
                    "description": "Include call graph: which names each function calls (default false). Adds detail, slightly slower.",
                },
            },
            "required": ["path"],
        },
    }
    return [ToolEntry(name="ast_analyze", schema=schema, handler=_handle_ast_analyze, is_code_tool=True)]
