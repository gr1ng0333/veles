"""type_coverage — AST-based missing type annotation scanner.

Scans Python source files and reports functions/methods that are missing
type annotations on their parameters or return value.  Good type coverage
makes code more readable, enables static analysis tools, and helps the LLM
understand intent without reading the body.

Reports three categories:
  - missing_param_annotations  — function/method parameters that have no
    annotation (excludes `self` and `cls`)
  - missing_return_annotations — function/method definitions that have no
    return type annotation
  - missing_both               — convenience aggregation of the above two
    (each entry appears once even if it lacks both params AND return)

Metrics per file and summary at codebase level:
  - total_defs      — all function/method definitions found
  - annotated_defs  — defs that are fully annotated (all params + return)
  - coverage_pct    — annotated_defs / total_defs * 100

Filters:
  path              — limit to a subdirectory or single file
  category          — one of missing_param_annotations / missing_return_annotations
  min_missing       — only show files with >= N missing annotations
  skip_private      — skip dunder and single-underscore-prefixed names
  format            — text (default) or json

Examples:
    type_coverage()                            # full scan, text
    type_coverage(path="ouroboros/")           # limit to one dir
    type_coverage(skip_private=True)           # public API only
    type_coverage(category="missing_return_annotations")
    type_coverage(format="json")
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

_CATEGORY_MISSING_PARAM = "missing_param_annotations"
_CATEGORY_MISSING_RETURN = "missing_return_annotations"
_ALL_CATEGORIES = [_CATEGORY_MISSING_PARAM, _CATEGORY_MISSING_RETURN]

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

def _is_private(name: str) -> bool:
    """Return True for dunder or single-underscore names."""
    return name.startswith("_")


def _qualified_name(class_stack: List[str], func_name: str) -> str:
    if class_stack:
        return ".".join(class_stack) + "." + func_name
    return func_name


# ── Per-file scanner ──────────────────────────────────────────────────────────

class _DefInfo:
    """Data class for a single function/method definition."""
    __slots__ = (
        "file", "line", "qualified_name", "missing_params", "missing_return",
    )

    def __init__(
        self,
        file: str,
        line: int,
        qualified_name: str,
        missing_params: List[str],
        missing_return: bool,
    ) -> None:
        self.file = file
        self.line = line
        self.qualified_name = qualified_name
        self.missing_params = missing_params
        self.missing_return = missing_return


def _scan_file(
    path: Path,
    rel_path: str,
    skip_private: bool,
) -> List[_DefInfo]:
    """Return all annotation-gap records for one file."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    results: List[_DefInfo] = []
    class_stack: List[str] = []

    def _visit(node: ast.AST) -> None:
        nonlocal class_stack

        if isinstance(node, ast.ClassDef):
            class_stack.append(node.name)
            for child in ast.iter_child_nodes(node):
                _visit(child)
            class_stack.pop()
            return

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if skip_private and _is_private(node.name):
                # Still recurse into it for nested classes/functions
                for child in ast.iter_child_nodes(node):
                    _visit(child)
                return

            qname = _qualified_name(class_stack, node.name)
            args = node.args

            # Collect params missing annotations (excluding self/cls)
            missing_params: List[str] = []
            all_args = (
                args.posonlyargs
                + args.args
                + args.kwonlyargs
                + ([args.vararg] if args.vararg else [])
                + ([args.kwarg] if args.kwarg else [])
            )
            for arg in all_args:
                if arg.arg in _SELF_NAMES:
                    continue
                if arg.annotation is None:
                    missing_params.append(arg.arg)

            missing_return = node.returns is None

            if missing_params or missing_return:
                results.append(
                    _DefInfo(
                        file=rel_path,
                        line=node.lineno,
                        qualified_name=qname,
                        missing_params=missing_params,
                        missing_return=missing_return,
                    )
                )

            # Recurse into nested defs
            for child in ast.iter_child_nodes(node):
                _visit(child)
            return

        for child in ast.iter_child_nodes(node):
            _visit(child)

    _visit(tree)
    return results


# ── Codebase aggregator ────────────────────────────────────────────────────────

class _FileSummary:
    __slots__ = ("total_defs", "annotated_defs", "missing_param_items", "missing_return_items")

    def __init__(self) -> None:
        self.total_defs = 0
        self.annotated_defs = 0
        self.missing_param_items: List[Dict[str, Any]] = []
        self.missing_return_items: List[Dict[str, Any]] = []


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

        defs = _scan_file(path, rel, skip_private)

        # Count total defs by walking the AST directly
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except Exception:
            continue

        total = 0
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if skip_private and _is_private(node.name):
                    continue
                total += 1

        if total == 0:
            continue

        summary = _FileSummary()
        summary.total_defs = total

        gap_defs = {(d.file, d.line) for d in defs}
        summary.annotated_defs = total - len(defs)

        for d in defs:
            if d.missing_params:
                summary.missing_param_items.append({
                    "file": d.file,
                    "line": d.line,
                    "name": d.qualified_name,
                    "params": d.missing_params,
                })
            if d.missing_return:
                summary.missing_return_items.append({
                    "file": d.file,
                    "line": d.line,
                    "name": d.qualified_name,
                })

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

    total_defs = sum(s.total_defs for s in per_file.values())
    total_annotated = sum(s.annotated_defs for s in per_file.values())
    coverage_pct = (total_annotated / total_defs * 100) if total_defs else 100.0

    filter_notes = []
    if path:
        filter_notes.append(f"path={path}")
    if category:
        filter_notes.append(f"category={category}")
    if skip_private:
        filter_notes.append("skip_private=True")
    filter_str = (", " + ", ".join(filter_notes)) if filter_notes else ""

    lines.append(
        f"## Type Coverage Report — {total_files} files scanned{filter_str}"
    )
    lines.append(
        f"   {total_annotated}/{total_defs} defs fully annotated  "
        f"({coverage_pct:.1f}% coverage)\n"
    )

    want_param = not category or category == _CATEGORY_MISSING_PARAM
    want_return = not category or category == _CATEGORY_MISSING_RETURN

    if want_param:
        all_param_items = [
            item
            for rel, s in sorted(per_file.items())
            for item in s.missing_param_items
            if (not min_missing or 1 >= 1)  # applied at file level below
        ]
        # filter by min_missing per file
        if min_missing > 1:
            from collections import defaultdict
            counts: Dict[str, int] = defaultdict(int)
            for item in all_param_items:
                counts[item["file"]] += 1
            all_param_items = [i for i in all_param_items if counts[i["file"]] >= min_missing]

        lines.append(
            f"🔍 **{_CATEGORY_MISSING_PARAM}** ({len(all_param_items)}) "
            "— function parameters without type annotations"
        )
        shown = all_param_items[:20]
        for item in shown:
            params_str = ", ".join(item["params"])
            lines.append(
                f"   {item['file']}:{item['line']}  {item['name']}  "
                f"missing: [{params_str}]"
            )
        if len(all_param_items) > 20:
            lines.append(f"   ... and {len(all_param_items) - 20} more")
        lines.append("")

    if want_return:
        all_return_items = [
            item
            for rel, s in sorted(per_file.items())
            for item in s.missing_return_items
        ]
        if min_missing > 1:
            from collections import defaultdict
            counts2: Dict[str, int] = defaultdict(int)
            for item in all_return_items:
                counts2[item["file"]] += 1
            all_return_items = [i for i in all_return_items if counts2[i["file"]] >= min_missing]

        lines.append(
            f"🔍 **{_CATEGORY_MISSING_RETURN}** ({len(all_return_items)}) "
            "— functions without return type annotation"
        )
        shown2 = all_return_items[:20]
        for item in shown2:
            lines.append(
                f"   {item['file']}:{item['line']}  {item['name']}"
            )
        if len(all_return_items) > 20:
            lines.append(f"   ... and {len(all_return_items) - 20} more")
        lines.append("")

    # Per-file coverage table (top worst files)
    worst = sorted(
        (
            (rel, s)
            for rel, s in per_file.items()
            if s.total_defs > 0
        ),
        key=lambda x: x[1].annotated_defs / x[1].total_defs,
    )[:10]

    if worst:
        lines.append("📊 **Worst-covered files** (top 10 by annotation gap)")
        lines.append(f"   {'File':<55} {'Ann':>5} {'Tot':>5} {'Cov%':>6}")
        lines.append("   " + "-" * 72)
        for rel, s in worst:
            pct = (s.annotated_defs / s.total_defs * 100) if s.total_defs else 100.0
            short = rel if len(rel) <= 55 else "…" + rel[-54:]
            lines.append(f"   {short:<55} {s.annotated_defs:>5} {s.total_defs:>5} {pct:>5.1f}%")
        lines.append("")

    if total_defs == 0:
        lines.append("✅ No function definitions found in the specified scope.")

    return "\n".join(lines)


# ── Tool entry point ───────────────────────────────────────────────────────────

def _type_coverage(
    ctx: ToolContext,
    path: Optional[str] = None,
    category: Optional[str] = None,
    format: str = "text",
    min_missing: int = 1,
    skip_private: bool = False,
) -> str:
    """Scan for missing type annotations: parameters and/or return types."""
    if category and category not in _ALL_CATEGORIES:
        return (
            f"Unknown category: {category!r}. "
            f"Valid: {', '.join(_ALL_CATEGORIES)}"
        )

    repo_root = Path(ctx.repo_dir if ctx and ctx.repo_dir else _REPO_DIR)
    per_file, total_files = _scan_codebase(repo_root, path, skip_private)

    if format == "json":
        total_defs = sum(s.total_defs for s in per_file.values())
        total_annotated = sum(s.annotated_defs for s in per_file.values())
        coverage_pct = (total_annotated / total_defs * 100) if total_defs else 100.0

        files_json: List[Dict[str, Any]] = []
        for rel, s in sorted(per_file.items()):
            pct = (s.annotated_defs / s.total_defs * 100) if s.total_defs else 100.0
            files_json.append({
                "file": rel,
                "total_defs": s.total_defs,
                "annotated_defs": s.annotated_defs,
                "coverage_pct": round(pct, 1),
                "missing_param_items": s.missing_param_items,
                "missing_return_items": s.missing_return_items,
            })

        return json.dumps(
            {
                "total_files": total_files,
                "total_defs": total_defs,
                "annotated_defs": total_annotated,
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
            name="type_coverage",
            schema={
                "name": "type_coverage",
                "description": (
                    "AST-based missing type annotation scanner. Reports functions and "
                    "methods that lack parameter annotations or return type annotations. "
                    "Provides per-file coverage percentage and a worst-covered files table. "
                    "Categories: missing_param_annotations (parameters without types), "
                    "missing_return_annotations (functions without return type). "
                    "Use skip_private=True to focus on public API surface only."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Limit scan to a subdirectory or single file (relative to repo root).",
                        },
                        "category": {
                            "type": "string",
                            "enum": _ALL_CATEGORIES,
                            "description": (
                                "Which category to show: "
                                "'missing_param_annotations' or 'missing_return_annotations'. "
                                "Omit to show both."
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
                                "Only include files with at least this many missing annotations. "
                                "Default: 1 (show all)."
                            ),
                        },
                        "skip_private": {
                            "type": "boolean",
                            "description": (
                                "If True, skip dunder and underscore-prefixed functions/methods. "
                                "Default: False."
                            ),
                        },
                    },
                    "required": [],
                },
            },
            execute=_type_coverage,
        )
    ]
