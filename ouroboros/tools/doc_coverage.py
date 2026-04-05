"""doc_coverage — AST-based docstring coverage scanner.

Reports where public API elements (modules, classes, functions/methods)
are missing docstrings.  Good docstring coverage is the other half of
"readable public API" — type_coverage covers *what* types are used,
doc_coverage covers *why* things exist.

Reports three categories:
  - missing_module_docstrings   — .py files with no module-level docstring
  - missing_class_docstrings    — class definitions without a docstring
  - missing_function_docstrings — function/method definitions without a docstring

Metrics per file and codebase summary:
  - total_items      — all scannable elements (modules + classes + functions)
  - documented_items — elements that have a docstring
  - coverage_pct     — documented_items / total_items * 100

Filters:
  path              — limit scan to a subdirectory or single file
  category          — one of missing_module_docstrings /
                      missing_class_docstrings /
                      missing_function_docstrings
  min_missing       — only show files with >= N missing docstrings
  skip_private      — skip dunder and single-underscore-prefixed names
  format            — text (default) or json

Examples:
    doc_coverage()                                  # full scan, text
    doc_coverage(path="ouroboros/")                 # limit to one dir
    doc_coverage(skip_private=True)                 # public API only
    doc_coverage(category="missing_function_docstrings")
    doc_coverage(format="json")
    doc_coverage(min_missing=3)                     # only high-gap files
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

# ── Constants ─────────────────────────────────────────────────────────────────

_REPO_DIR = Path(os.environ.get("REPO_DIR", "/opt/veles"))

_SKIP_DIRS = {
    "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
    "node_modules", ".venv", "venv", "dist", "build",
}

_CAT_MODULE = "missing_module_docstrings"
_CAT_CLASS = "missing_class_docstrings"
_CAT_FUNCTION = "missing_function_docstrings"
_ALL_CATEGORIES = [_CAT_MODULE, _CAT_CLASS, _CAT_FUNCTION]

_SELF_NAMES = {"self", "cls"}


# ── File collection ────────────────────────────────────────────────────────────

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


# ── AST helpers ────────────────────────────────────────────────────────────────

def _has_docstring(node: ast.AST) -> bool:
    """Return True if the node's body starts with a string constant (docstring)."""
    body = getattr(node, "body", [])
    if not body:
        return False
    first = body[0]
    return (
        isinstance(first, ast.Expr)
        and isinstance(getattr(first, "value", None), ast.Constant)
        and isinstance(first.value.value, str)
    )


def _is_private(name: str) -> bool:
    """Return True for dunder or single-underscore-prefixed names."""
    return name.startswith("_")


def _qualified_name(class_stack: List[str], func_name: str) -> str:
    if class_stack:
        return ".".join(class_stack) + "." + func_name
    return func_name


# ── Per-file scanner ───────────────────────────────────────────────────────────

class _FileSummary:
    """Aggregated docstring stats for one file."""

    __slots__ = (
        "total_items",
        "documented_items",
        "missing_module",
        "missing_class_items",
        "missing_function_items",
    )

    def __init__(self) -> None:
        self.total_items: int = 0
        self.documented_items: int = 0
        self.missing_module: bool = False
        self.missing_class_items: List[Dict[str, Any]] = []
        self.missing_function_items: List[Dict[str, Any]] = []


def _scan_file(
    path: Path,
    rel_path: str,
    skip_private: bool,
) -> Optional[_FileSummary]:
    """Scan one file and return its _FileSummary, or None on parse error."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return None

    summary = _FileSummary()
    class_stack: List[str] = []

    # ── Module docstring ────────────────────────────────────────────────────
    summary.total_items += 1
    if _has_docstring(tree):
        summary.documented_items += 1
    else:
        summary.missing_module = True

    def _visit(node: ast.AST) -> None:
        nonlocal class_stack

        if isinstance(node, ast.ClassDef):
            if not (skip_private and _is_private(node.name)):
                summary.total_items += 1
                if _has_docstring(node):
                    summary.documented_items += 1
                else:
                    summary.missing_class_items.append({
                        "file": rel_path,
                        "line": node.lineno,
                        "name": node.name,
                    })

            # Recurse into class body
            class_stack.append(node.name)
            for child in ast.iter_child_nodes(node):
                _visit(child)
            class_stack.pop()
            return

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not (skip_private and _is_private(node.name)):
                qname = _qualified_name(class_stack, node.name)
                summary.total_items += 1
                if _has_docstring(node):
                    summary.documented_items += 1
                else:
                    summary.missing_function_items.append({
                        "file": rel_path,
                        "line": node.lineno,
                        "name": qname,
                    })

            # Recurse into nested defs
            for child in ast.iter_child_nodes(node):
                _visit(child)
            return

        for child in ast.iter_child_nodes(node):
            _visit(child)

    for top_node in ast.iter_child_nodes(tree):
        _visit(top_node)

    return summary


# ── Codebase aggregator ────────────────────────────────────────────────────────

def _scan_codebase(
    repo_root: Path,
    subpath: Optional[str],
    skip_private: bool,
) -> Tuple[Dict[str, _FileSummary], int]:
    py_files = _collect_py_files(repo_root, subpath)
    per_file: Dict[str, _FileSummary] = {}

    for path in py_files:
        try:
            rel = str(path.relative_to(repo_root))
        except ValueError:
            rel = str(path)

        summary = _scan_file(path, rel, skip_private)
        if summary is not None:
            per_file[rel] = summary

    return per_file, len(py_files)


# ── Text formatter ─────────────────────────────────────────────────────────────

def _format_text(
    per_file: Dict[str, _FileSummary],
    total_files: int,
    category: Optional[str],
    path: Optional[str],
    min_missing: int,
    skip_private: bool,
) -> str:
    lines: List[str] = []

    total_items = sum(s.total_items for s in per_file.values())
    total_documented = sum(s.documented_items for s in per_file.values())
    coverage_pct = (total_documented / total_items * 100) if total_items else 100.0

    filter_notes: List[str] = []
    if path:
        filter_notes.append(f"path={path}")
    if category:
        filter_notes.append(f"category={category}")
    if skip_private:
        filter_notes.append("skip_private=True")
    filter_str = (", " + ", ".join(filter_notes)) if filter_notes else ""

    lines.append(
        f"## Docstring Coverage Report — {total_files} files scanned{filter_str}"
    )
    lines.append(
        f"   {total_documented}/{total_items} items documented  "
        f"({coverage_pct:.1f}% coverage)\n"
    )

    want_module = not category or category == _CAT_MODULE
    want_class = not category or category == _CAT_CLASS
    want_function = not category or category == _CAT_FUNCTION

    # ── Missing module docstrings ───────────────────────────────────────────
    if want_module:
        missing_modules = [
            rel for rel, s in sorted(per_file.items()) if s.missing_module
        ]
        lines.append(
            f"📄 **{_CAT_MODULE}** ({len(missing_modules)}) "
            "— .py files without a module-level docstring"
        )
        shown = missing_modules[:20]
        for rel in shown:
            lines.append(f"   {rel}")
        if len(missing_modules) > 20:
            lines.append(f"   ... and {len(missing_modules) - 20} more")
        lines.append("")

    # ── Missing class docstrings ────────────────────────────────────────────
    if want_class:
        all_class_items: List[Dict[str, Any]] = []
        for rel, s in sorted(per_file.items()):
            if min_missing > 1 and len(s.missing_class_items) < min_missing:
                continue
            all_class_items.extend(s.missing_class_items)

        lines.append(
            f"🏛️  **{_CAT_CLASS}** ({len(all_class_items)}) "
            "— class definitions without a docstring"
        )
        shown = all_class_items[:20]
        for item in shown:
            lines.append(
                f"   {item['file']}:{item['line']}  class {item['name']}"
            )
        if len(all_class_items) > 20:
            lines.append(f"   ... and {len(all_class_items) - 20} more")
        lines.append("")

    # ── Missing function docstrings ─────────────────────────────────────────
    if want_function:
        all_func_items: List[Dict[str, Any]] = []
        for rel, s in sorted(per_file.items()):
            if min_missing > 1 and len(s.missing_function_items) < min_missing:
                continue
            all_func_items.extend(s.missing_function_items)

        lines.append(
            f"🔧 **{_CAT_FUNCTION}** ({len(all_func_items)}) "
            "— function/method definitions without a docstring"
        )
        shown = all_func_items[:20]
        for item in shown:
            lines.append(
                f"   {item['file']}:{item['line']}  {item['name']}"
            )
        if len(all_func_items) > 20:
            lines.append(f"   ... and {len(all_func_items) - 20} more")
        lines.append("")

    # ── Per-file worst table ────────────────────────────────────────────────
    worst = sorted(
        (
            (rel, s)
            for rel, s in per_file.items()
            if s.total_items > 0
        ),
        key=lambda x: x[1].documented_items / x[1].total_items,
    )[:10]

    if worst:
        lines.append("📊 **Worst-covered files** (top 10 by docstring gap)")
        lines.append(f"   {'File':<55} {'Doc':>5} {'Tot':>5} {'Cov%':>6}")
        lines.append("   " + "-" * 72)
        for rel, s in worst:
            pct = (s.documented_items / s.total_items * 100) if s.total_items else 100.0
            short = rel if len(rel) <= 55 else "…" + rel[-54:]
            lines.append(
                f"   {short:<55} {s.documented_items:>5} {s.total_items:>5} {pct:>5.1f}%"
            )
        lines.append("")

    if total_items == 0:
        lines.append("✅ No scannable elements found in the specified scope.")

    return "\n".join(lines)


# ── Tool entry point ───────────────────────────────────────────────────────────

def _doc_coverage(
    ctx: ToolContext,
    path: Optional[str] = None,
    category: Optional[str] = None,
    format: str = "text",
    min_missing: int = 1,
    skip_private: bool = False,
) -> str:
    """Scan for missing docstrings: modules, classes, and functions/methods."""
    if category and category not in _ALL_CATEGORIES:
        return (
            f"Unknown category: {category!r}. "
            f"Valid: {', '.join(_ALL_CATEGORIES)}"
        )

    repo_root = Path(ctx.repo_dir if ctx and ctx.repo_dir else _REPO_DIR)
    per_file, total_files = _scan_codebase(repo_root, path, skip_private)

    if format == "json":
        total_items = sum(s.total_items for s in per_file.values())
        total_documented = sum(s.documented_items for s in per_file.values())
        coverage_pct = (total_documented / total_items * 100) if total_items else 100.0

        files_json: List[Dict[str, Any]] = []
        for rel, s in sorted(per_file.items()):
            pct = (s.documented_items / s.total_items * 100) if s.total_items else 100.0
            entry: Dict[str, Any] = {
                "file": rel,
                "total_items": s.total_items,
                "documented_items": s.documented_items,
                "coverage_pct": round(pct, 1),
            }
            if not category or category == _CAT_MODULE:
                entry["missing_module"] = s.missing_module
            if not category or category == _CAT_CLASS:
                entry["missing_class_items"] = s.missing_class_items
            if not category or category == _CAT_FUNCTION:
                entry["missing_function_items"] = s.missing_function_items
            files_json.append(entry)

        return json.dumps(
            {
                "total_files": total_files,
                "total_items": total_items,
                "documented_items": total_documented,
                "coverage_pct": round(coverage_pct, 1),
                "filters": {
                    "path": path,
                    "category": category,
                    "min_missing": min_missing,
                    "skip_private": skip_private,
                },
                "files": files_json,
            },
            ensure_ascii=False,
            indent=2,
        )

    return _format_text(per_file, total_files, category, path, min_missing, skip_private)


# ── Tool registration ──────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="doc_coverage",
            schema={
                "name": "doc_coverage",
                "description": (
                    "AST-based docstring coverage scanner. Reports which modules, "
                    "classes, and functions/methods are missing docstrings. "
                    "Complements type_coverage (annotation gaps) to give a full "
                    "picture of public-API documentation quality. "
                    "Three categories: missing_module_docstrings, "
                    "missing_class_docstrings, missing_function_docstrings. "
                    "Per-file coverage % table included."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Limit scan to a subdirectory or single file "
                                "(relative to repo root). Default: entire repo."
                            ),
                        },
                        "category": {
                            "type": "string",
                            "enum": _ALL_CATEGORIES,
                            "description": (
                                "Show only one category. "
                                "Default: all three categories."
                            ),
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format. Default: text.",
                        },
                        "min_missing": {
                            "type": "integer",
                            "description": (
                                "Only include files with >= N missing "
                                "docstrings per category. Default: 1."
                            ),
                        },
                        "skip_private": {
                            "type": "boolean",
                            "description": (
                                "Skip dunder and single-underscore-prefixed "
                                "names (focus on public API). Default: false."
                            ),
                        },
                    },
                    "required": [],
                },
            },
            execute=lambda ctx, **kw: _doc_coverage(ctx, **kw),
        )
    ]
