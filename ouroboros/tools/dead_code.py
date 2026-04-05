"""dead_code — AST-based unused import and dead private symbol detector.

Finds two categories of dead code across the codebase in a single call:

  1. unused_imports — import statements whose imported names are never
     referenced within the same file.  Excludes:
     - star imports (from X import *)
     - TYPE_CHECKING-guarded imports (annotation-only)
     - names exported via __all__
     - the bare underscore (_)

  2. dead_privates — top-level private functions/classes (prefixed with _,
     but NOT dunder __name__) that are:
     - never referenced by a Name node anywhere in their own file
     - never imported from any other file in the repo

Dead code is pure waste: it confuses reading, wastes tokens in LLM context
windows, and hides bugs behind names that look important.  Finding and deleting
it is one of the highest-leverage refactors per line changed.

Complements tech_debt (structural issues), change_impact (blast-radius),
and semantic_diff (what changed between refs).

Examples:
    dead_code()                              # full scan, text output
    dead_code(path="ouroboros/tools/")       # limit to subdirectory
    dead_code(category="unused_imports")     # one category only
    dead_code(format="json")                 # machine-readable output
    dead_code(min_per_file=2)                # only files with >= 2 issues
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

# ── Constants ─────────────────────────────────────────────────────────────────

_REPO_DIR = Path(os.environ.get("REPO_DIR", "/opt/veles"))

_SKIP_DIRS = {
    "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
    "node_modules", ".venv", "venv", "dist", "build",
}

_ALL_CATEGORIES = ["unused_imports", "dead_privates"]

_CATEGORY_DESCRIPTIONS = {
    "unused_imports": "Import statements whose name is never used in the same file",
    "dead_privates": "Private (_name) functions/classes never called locally or imported externally",
}


# ── File collection ───────────────────────────────────────────────────────────

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


# ── AST helpers ───────────────────────────────────────────────────────────────

def _load_names(tree: ast.Module) -> Set[str]:
    """Collect all Name nodes with Load context (actual usages, not assignments)."""
    used: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            # Capture root of attribute chains: os.path → 'os' is used
            curr: ast.expr = node
            while isinstance(curr, ast.Attribute):
                curr = curr.value
            if isinstance(curr, ast.Name) and isinstance(curr.ctx, ast.Load):
                used.add(curr.id)
    return used


def _all_export_names(tree: ast.Module) -> Set[str]:
    """Names listed in __all__ — always considered 'used' (re-exported)."""
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                names.add(elt.value)
    return names


def _type_checking_names(tree: ast.Module) -> Set[str]:
    """Names imported inside 'if TYPE_CHECKING:' blocks (annotation-only)."""
    names: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_tc = (
            (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
            or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")
        )
        if not is_tc:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Import):
                for alias in child.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(child, ast.ImportFrom):
                for alias in child.names:
                    if alias.name != "*":
                        names.add(alias.asname or alias.name)
    return names


# ── Unused import scanner ─────────────────────────────────────────────────────

def _scan_unused_imports(
    rel_path: str,
    tree: ast.Module,
) -> List[Dict[str, Any]]:
    """Return list of unused import items for a single file."""
    exported = _all_export_names(tree)
    tc_names = _type_checking_names(tree)
    used_names = _load_names(tree)

    # Collect all imported (name, lineno, stmt_text) pairs
    imported: List[Tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # "import os.path" → used as "os" unless aliased
                used_name = alias.asname or alias.name.split(".")[0]
                stmt = f"import {alias.name}"
                if alias.asname:
                    stmt += f" as {alias.asname}"
                imported.append((used_name, node.lineno, stmt))

        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue  # star imports: can't analyse
                used_name = alias.asname or alias.name
                module = node.module or ""
                stmt = f"from {module} import {alias.name}"
                if alias.asname:
                    stmt += f" as {alias.asname}"
                imported.append((used_name, node.lineno, stmt))

    results: List[Dict[str, Any]] = []
    for name, lineno, stmt in imported:
        if name in exported:
            continue  # __all__ re-export
        if name in tc_names:
            continue  # annotation-only import
        if name == "_":
            continue  # intentional discard placeholder
        if name.startswith("__") and name.endswith("__"):
            continue  # dunder
        if name not in used_names:
            results.append({
                "file": rel_path,
                "line": lineno,
                "name": name,
                "stmt": stmt,
            })

    return results


# ── Dead private scanner ──────────────────────────────────────────────────────

def _scan_dead_privates(
    rel_path: str,
    tree: ast.Module,
    externally_used: Set[str],
) -> List[Dict[str, Any]]:
    """Return list of dead private symbol items for a single file."""
    exported = _all_export_names(tree)
    used_names = _load_names(tree)

    # Top-level private defs only (not methods inside classes)
    defs: List[Tuple[str, int, str]] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_") and not node.name.startswith("__"):
                defs.append((node.name, node.lineno, "function"))
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_") and not node.name.startswith("__"):
                defs.append((node.name, node.lineno, "class"))

    results: List[Dict[str, Any]] = []
    for name, lineno, kind in defs:
        if name in exported:
            continue  # re-exported via __all__
        if name in externally_used:
            continue  # imported from another file in the repo
        # FunctionDef.name is a string attribute, NOT a Name node —
        # so all Name(id=name, Load) hits in used_names are genuine usages.
        if name in used_names:
            continue  # called / referenced within the file
        results.append({
            "file": rel_path,
            "line": lineno,
            "name": name,
            "kind": kind,
        })

    return results


# ── Cross-file private usage ──────────────────────────────────────────────────

def _collect_externally_used_privates(py_files: List[Path]) -> Set[str]:
    """Scan all files for 'from X import _name' to find externally imported privates."""
    used: Set[str] = set()
    for path in py_files:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                name = alias.name
                if name.startswith("_") and not name.startswith("__"):
                    used.add(name)
                if alias.asname and alias.asname.startswith("_") and not alias.asname.startswith("__"):
                    used.add(alias.asname)
    return used


# ── Codebase scanner ──────────────────────────────────────────────────────────

def _scan_codebase(
    repo_root: Path,
    subpath: Optional[str],
    category: Optional[str],
) -> Tuple[Dict[str, List[Dict[str, Any]]], int]:
    """Aggregate dead-code items across all files."""
    # For subpath scans, still build cross-file external set from whole repo
    all_py_files = _collect_py_files(repo_root)
    scan_files = _collect_py_files(repo_root, subpath)

    want_imports = not category or category == "unused_imports"
    want_privates = not category or category == "dead_privates"

    externally_used: Set[str] = set()
    if want_privates:
        externally_used = _collect_externally_used_privates(all_py_files)

    aggregated: Dict[str, List[Dict[str, Any]]] = {c: [] for c in _ALL_CATEGORIES}

    for path in scan_files:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except Exception:
            continue

        try:
            rel = str(path.relative_to(repo_root))
        except ValueError:
            rel = str(path)

        if want_imports:
            aggregated["unused_imports"].extend(
                _scan_unused_imports(rel, tree)
            )
        if want_privates:
            aggregated["dead_privates"].extend(
                _scan_dead_privates(rel, tree, externally_used)
            )

    aggregated["unused_imports"].sort(key=lambda x: (x["file"], x["line"]))
    aggregated["dead_privates"].sort(key=lambda x: (x["file"], x["line"]))

    return aggregated, len(scan_files)


# ── Text formatter ────────────────────────────────────────────────────────────

def _format_text(
    dead: Dict[str, List[Dict]],
    total_files: int,
    category: Optional[str],
    path: Optional[str],
    min_per_file: int,
) -> str:
    lines: List[str] = []

    total_items = sum(len(v) for v in dead.values())
    filter_note = ""
    if path:
        filter_note += f", path={path}"
    if category:
        filter_note += f", category={category}"

    lines.append(f"## Dead Code Report — {total_files} files scanned{filter_note}")
    lines.append(f"   {total_items} items across {len([c for c in _ALL_CATEGORIES if dead.get(c)])} categories\n")

    for cat in _ALL_CATEGORIES:
        items = dead.get(cat, [])
        if not items:
            continue

        desc = _CATEGORY_DESCRIPTIONS.get(cat, cat)
        lines.append(f"🔍 **{cat}** ({len(items)}) — {desc}")

        if min_per_file > 1:
            # Group by file, filter to files with >= min_per_file hits
            from collections import defaultdict
            by_file: Dict[str, List] = defaultdict(list)
            for item in items:
                by_file[item["file"]].append(item)
            display_items = [
                item
                for f_items in by_file.values()
                if len(f_items) >= min_per_file
                for item in f_items
            ]
        else:
            display_items = items

        shown = display_items[:15]
        for item in shown:
            if cat == "unused_imports":
                lines.append(
                    f"   {item['file']}:{item['line']}  {item['stmt']}"
                )
            elif cat == "dead_privates":
                lines.append(
                    f"   {item['file']}:{item['line']}  {item['kind']} {item['name']}"
                )

        if len(display_items) > 15:
            lines.append(f"   ... and {len(display_items) - 15} more")
        lines.append("")

    if total_items == 0:
        lines.append("✅ No dead code found matching the specified filters.")

    return "\n".join(lines)


# ── Tool entry point ──────────────────────────────────────────────────────────

def _dead_code(
    ctx: ToolContext,
    path: Optional[str] = None,
    category: Optional[str] = None,
    format: str = "text",
    min_per_file: int = 1,
) -> str:
    """Scan for dead code: unused imports and unreferenced private symbols."""
    if category and category not in _ALL_CATEGORIES:
        return (
            f"Unknown category: {category!r}. "
            f"Valid: {', '.join(_ALL_CATEGORIES)}"
        )

    repo_root = Path(ctx.repo_dir if ctx and ctx.repo_dir else _REPO_DIR)
    dead, total_files = _scan_codebase(repo_root, path, category)

    if format == "json":
        total_items = sum(len(v) for v in dead.values())
        summary: Dict[str, int] = {c: len(dead[c]) for c in _ALL_CATEGORIES}
        return json.dumps(
            {
                "total_files": total_files,
                "total_items": total_items,
                "summary": summary,
                "dead": dead,
                "filters": {
                    "path": path,
                    "category": category,
                    "min_per_file": min_per_file,
                },
            },
            ensure_ascii=False,
            indent=2,
        )

    return _format_text(dead, total_files, category, path, min_per_file)


# ── Tool registration ─────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="dead_code",
            schema={
                "name": "dead_code",
                "description": (
                    "AST-based dead code detector for the Veles codebase. "
                    "Finds two categories: (1) unused_imports — import statements "
                    "whose names are never referenced in the same file; "
                    "(2) dead_privates — top-level private functions/classes (_name) "
                    "never called locally or imported by any other module. "
                    "Use before refactoring to find safe deletions. "
                    "Complements tech_debt (structural debt) and change_impact (blast-radius)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Subdirectory or file to scan (relative to repo root). "
                                "Default: entire repo."
                            ),
                        },
                        "category": {
                            "type": "string",
                            "enum": _ALL_CATEGORIES,
                            "description": (
                                "Limit to one category: 'unused_imports' or 'dead_privates'. "
                                "Default: both."
                            ),
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format. Default: text.",
                        },
                        "min_per_file": {
                            "type": "integer",
                            "description": (
                                "Only show files that have at least this many issues "
                                "in a category. Default: 1 (show all)."
                            ),
                        },
                    },
                    "required": [],
                },
            },
            handler=_dead_code,
            is_code_tool=True,
        )
    ]
