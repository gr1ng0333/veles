"""tech_debt — Automated technical debt scanner for the Veles codebase.

Growth tool: locates actionable structural debt items across the entire
codebase in a single call, without relying on the LLM to read files.

Debt categories detected (all AST-based, no regex heuristics):
  1. oversized_functions  — functions / methods > 150 lines (BIBLE P5 threshold)
  2. too_many_params      — functions with > 8 parameters (BIBLE P5 threshold)
  3. high_complexity      — cyclomatic complexity >= 15 (deeply branching logic)
  4. oversized_modules    — modules > 1000 lines (BIBLE P5 threshold)
  5. deep_nesting         — nesting depth > 5 (if/for/while/try/with stacks)
  6. fixme_todo           — FIXME/TODO/HACK/XXX comment lines
  7. god_objects          — classes with >= 20 methods (likely violation of SRP)

Output:
  - text: human-readable summary with per-category listings
  - json: structured data for downstream processing

Why not ast_analyze?
  ast_analyze is a per-file deep-dive tool. tech_debt is a codebase-wide
  radar: it answers "where is the debt?" not "what does this file look like?".
  Returns a ranked debt report across 100+ files in one call.

Usage:
    tech_debt()                         # full scan, text output
    tech_debt(format="json")            # structured output
    tech_debt(category="oversized_functions")  # focus on one category
    tech_debt(min_severity="high")      # only high-severity items
    tech_debt(path="ouroboros/tools/")  # limit to subdirectory
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

# ── constants ─────────────────────────────────────────────────────────────────

_REPO_DIR = os.environ.get("REPO_DIR", "/opt/veles")
_SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", "node_modules",
               ".venv", "venv", "dist", "build"}

# BIBLE P5 thresholds
_MAX_FUNCTION_LINES = 150
_MAX_PARAMS = 8
_MAX_MODULE_LINES = 1000
_MAX_NESTING = 5
_HIGH_COMPLEXITY_THRESHOLD = 15
_GOD_OBJECT_METHODS = 20

# Categories in display order
_ALL_CATEGORIES = [
    "oversized_functions",
    "too_many_params",
    "high_complexity",
    "oversized_modules",
    "deep_nesting",
    "fixme_todo",
    "god_objects",
]

_CATEGORY_DESCRIPTIONS = {
    "oversized_functions": f"Functions/methods > {_MAX_FUNCTION_LINES} lines (BIBLE P5)",
    "too_many_params": f"Functions with > {_MAX_PARAMS} parameters (BIBLE P5)",
    "high_complexity": f"Cyclomatic complexity >= {_HIGH_COMPLEXITY_THRESHOLD} (too branchy)",
    "oversized_modules": f"Modules > {_MAX_MODULE_LINES} lines (BIBLE P5)",
    "deep_nesting": f"Nesting depth > {_MAX_NESTING} (hard to read)",
    "fixme_todo": "FIXME / TODO / HACK / XXX comments",
    "god_objects": f"Classes with >= {_GOD_OBJECT_METHODS} methods (SRP violation)",
}

_SEVERITY_MAP = {
    "oversized_functions": "medium",
    "too_many_params": "medium",
    "high_complexity": "high",
    "oversized_modules": "medium",
    "deep_nesting": "high",
    "fixme_todo": "low",
    "god_objects": "high",
}


# ── AST complexity helper ─────────────────────────────────────────────────────

def _cyclomatic(node: ast.AST) -> int:
    """Estimate cyclomatic complexity (branch count)."""
    count = 0
    for n in ast.walk(node):
        if isinstance(n, (
            ast.If, ast.For, ast.While, ast.With,
            ast.Try, ast.ExceptHandler, ast.Assert,
            ast.comprehension,
        )):
            count += 1
        elif isinstance(n, ast.BoolOp):
            count += len(n.values) - 1
    return count


def _max_nesting_depth(node: ast.AST) -> int:
    """Compute maximum nesting depth of control-flow constructs."""
    _NESTING_NODES = (
        ast.If, ast.For, ast.While, ast.With, ast.Try, ast.ExceptHandler,
    )

    def _depth(n: ast.AST, current: int) -> int:
        if isinstance(n, _NESTING_NODES):
            current += 1
        max_d = current
        for child in ast.iter_child_nodes(n):
            max_d = max(max_d, _depth(child, current))
        return max_d

    return _depth(node, 0)


def _param_count(args: ast.arguments) -> int:
    """Count all parameters excluding self/cls."""
    all_args = args.posonlyargs + args.args + args.kwonlyargs
    params = [a for a in all_args if a.arg not in ("self", "cls")]
    if args.vararg:
        params.append(args.vararg)
    if args.kwarg:
        params.append(args.kwarg)
    return len(params)


# ── Per-file scanner ──────────────────────────────────────────────────────────

def _scan_file(path: Path, repo_root: Path) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """Scan a single .py file and return debt items per category."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    lines = source.splitlines()
    total_lines = len(lines)

    try:
        rel = str(path.relative_to(repo_root))
    except ValueError:
        rel = str(path)

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        # Can't parse — just report as oversized module if needed
        result: Dict[str, List[Dict]] = {c: [] for c in _ALL_CATEGORIES}
        if total_lines > _MAX_MODULE_LINES:
            result["oversized_modules"].append({
                "file": rel, "lines": total_lines,
                "note": "unparseable (SyntaxError)",
            })
        return result

    result = {c: [] for c in _ALL_CATEGORIES}

    # ── oversized_modules ──
    if total_lines > _MAX_MODULE_LINES:
        result["oversized_modules"].append({
            "file": rel,
            "lines": total_lines,
        })

    # ── FIXME/TODO/HACK/XXX ──
    todo_re = re.compile(r"#.*(FIXME|TODO|HACK|XXX)\b", re.IGNORECASE)
    for i, line in enumerate(lines, 1):
        m = todo_re.search(line)
        if m:
            tag = m.group(1).upper()
            # capture up to 80 chars of the comment
            snippet = line.strip()[:80]
            result["fixme_todo"].append({
                "file": rel,
                "line": i,
                "tag": tag,
                "snippet": snippet,
            })

    # ── god_objects ──
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        methods = [
            n for n in ast.iter_child_nodes(node)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        if len(methods) >= _GOD_OBJECT_METHODS:
            result["god_objects"].append({
                "file": rel,
                "class": node.name,
                "line": node.lineno,
                "method_count": len(methods),
            })

    # ── walk functions / methods ──
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # skip lambdas and comprehensions
        func_name = node.name
        start_line = node.lineno
        end_line = getattr(node, "end_lineno", start_line)
        func_lines = end_line - start_line + 1

        # oversized functions
        if func_lines > _MAX_FUNCTION_LINES:
            result["oversized_functions"].append({
                "file": rel,
                "function": func_name,
                "line": start_line,
                "lines": func_lines,
            })

        # too many params
        n_params = _param_count(node.args)
        if n_params > _MAX_PARAMS:
            result["too_many_params"].append({
                "file": rel,
                "function": func_name,
                "line": start_line,
                "param_count": n_params,
            })

        # high complexity
        cx = _cyclomatic(node)
        if cx >= _HIGH_COMPLEXITY_THRESHOLD:
            result["high_complexity"].append({
                "file": rel,
                "function": func_name,
                "line": start_line,
                "complexity": cx,
            })

        # deep nesting
        depth = _max_nesting_depth(node)
        if depth > _MAX_NESTING:
            result["deep_nesting"].append({
                "file": rel,
                "function": func_name,
                "line": start_line,
                "depth": depth,
            })

    return result


# ── Codebase scanner ──────────────────────────────────────────────────────────

def _collect_py_files(root: Path, subpath: Optional[str] = None) -> List[Path]:
    """Walk repo and collect .py files to scan."""
    if subpath:
        target = root / subpath.lstrip("/")
        if not target.exists():
            # Maybe it's relative without the leading dir
            target = root / subpath
        if not target.exists():
            target = root  # fallback to full scan
    else:
        target = root

    py_files: List[Path] = []

    if target.is_file() and target.suffix == ".py":
        return [target]

    for dirpath, dirnames, filenames in os.walk(str(target)):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        for fname in filenames:
            if fname.endswith(".py"):
                py_files.append(Path(dirpath) / fname)

    return py_files


def _scan_codebase(
    repo_root: Path,
    subpath: Optional[str],
    category_filter: Optional[str],
    min_severity: str,
) -> Dict[str, List[Dict[str, Any]]]:
    """Aggregate debt items across all files."""
    py_files = _collect_py_files(repo_root, subpath)

    aggregated: Dict[str, List[Dict]] = {c: [] for c in _ALL_CATEGORIES}

    severity_order = {"low": 0, "medium": 1, "high": 2}
    min_sev_val = severity_order.get(min_severity, 0)

    for path in py_files:
        file_result = _scan_file(path, repo_root)
        if file_result is None:
            continue
        for cat, items in file_result.items():
            # Apply category filter
            if category_filter and cat != category_filter:
                continue
            # Apply severity filter
            cat_sev = _SEVERITY_MAP.get(cat, "low")
            if severity_order.get(cat_sev, 0) < min_sev_val:
                continue
            aggregated[cat].extend(items)

    # Sort each category for readability
    _sort_keys = {
        "oversized_functions": lambda x: -x["lines"],
        "too_many_params": lambda x: -x["param_count"],
        "high_complexity": lambda x: -x["complexity"],
        "oversized_modules": lambda x: -x["lines"],
        "deep_nesting": lambda x: -x["depth"],
        "fixme_todo": lambda x: x["file"],
        "god_objects": lambda x: -x["method_count"],
    }
    for cat in aggregated:
        if cat in _sort_keys:
            aggregated[cat].sort(key=_sort_keys[cat])

    return aggregated


# ── Formatter ─────────────────────────────────────────────────────────────────

def _format_text(
    debt: Dict[str, List[Dict]],
    total_files: int,
    category_filter: Optional[str],
    min_severity: str,
) -> str:
    """Render debt report as human-readable text."""
    lines: List[str] = []

    # Header
    active_cats = [c for c in _ALL_CATEGORIES if debt.get(c)]
    total_items = sum(len(v) for v in debt.values())

    filter_note = ""
    if category_filter:
        filter_note = f", category={category_filter}"
    if min_severity != "low":
        filter_note += f", min_severity={min_severity}"

    lines.append(f"## Tech Debt Report — {total_files} files scanned{filter_note}")
    lines.append(f"   {total_items} debt items across {len(active_cats)} categories\n")

    for cat in _ALL_CATEGORIES:
        items = debt.get(cat, [])
        if not items:
            continue

        sev = _SEVERITY_MAP.get(cat, "low")
        sev_icon = {"high": "🔴", "medium": "🟡", "low": "⚪"}.get(sev, "⚪")
        desc = _CATEGORY_DESCRIPTIONS.get(cat, cat)

        lines.append(f"{sev_icon} **{cat}** ({len(items)})  — {desc}")

        # Show top items (cap at 10 per category for readability)
        display = items[:10]
        for item in display:
            if cat == "oversized_functions":
                lines.append(
                    f"   {item['file']}:{item['line']}  {item['function']}()  "
                    f"{item['lines']}L"
                )
            elif cat == "too_many_params":
                lines.append(
                    f"   {item['file']}:{item['line']}  {item['function']}()  "
                    f"{item['param_count']} params"
                )
            elif cat == "high_complexity":
                lines.append(
                    f"   {item['file']}:{item['line']}  {item['function']}()  "
                    f"cx={item['complexity']}"
                )
            elif cat == "oversized_modules":
                note = f"  [{item.get('note', '')}]" if item.get("note") else ""
                lines.append(f"   {item['file']}  {item['lines']}L{note}")
            elif cat == "deep_nesting":
                lines.append(
                    f"   {item['file']}:{item['line']}  {item['function']}()  "
                    f"depth={item['depth']}"
                )
            elif cat == "fixme_todo":
                lines.append(
                    f"   {item['file']}:{item['line']}  [{item['tag']}]  "
                    f"{item['snippet'][:60]}"
                )
            elif cat == "god_objects":
                lines.append(
                    f"   {item['file']}:{item['line']}  class {item['class']}  "
                    f"{item['method_count']} methods"
                )

        if len(items) > 10:
            lines.append(f"   ... and {len(items) - 10} more")
        lines.append("")

    if not active_cats:
        lines.append("✅ No debt items found matching the specified filters.")

    return "\n".join(lines)


# ── Tool entry point ──────────────────────────────────────────────────────────

def _tech_debt(
    ctx: ToolContext,
    path: Optional[str] = None,
    category: Optional[str] = None,
    min_severity: str = "low",
    format: str = "text",
) -> str:
    """Scan codebase for structural technical debt and return a ranked report."""
    repo_root = Path(ctx.repo_dir if ctx and ctx.repo_dir else _REPO_DIR)

    # Validate category
    if category and category not in _ALL_CATEGORIES:
        return (
            f"Unknown category: {category!r}. "
            f"Valid: {', '.join(_ALL_CATEGORIES)}"
        )

    # Validate severity
    if min_severity not in ("low", "medium", "high"):
        return "min_severity must be one of: low, medium, high"

    # Collect and scan files
    py_files = _collect_py_files(repo_root, path)
    total_files = len(py_files)

    debt = _scan_codebase(repo_root, path, category, min_severity)

    if format == "json":
        return json.dumps(
            {
                "total_files": total_files,
                "filters": {
                    "path": path,
                    "category": category,
                    "min_severity": min_severity,
                },
                "debt": debt,
                "summary": {
                    cat: len(items)
                    for cat, items in debt.items()
                    if items
                },
            },
            ensure_ascii=False,
            indent=2,
        )

    return _format_text(debt, total_files, category, min_severity)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="tech_debt",
            schema={
                "name": "tech_debt",
                "description": (
                    "Automated technical debt scanner for the codebase.\n\n"
                    "Detects structural debt across 7 categories:\n"
                    "  • oversized_functions — functions > 150 lines (BIBLE P5)\n"
                    "  • too_many_params — functions with > 8 parameters (BIBLE P5)\n"
                    "  • high_complexity — cyclomatic complexity >= 15\n"
                    "  • oversized_modules — modules > 1000 lines (BIBLE P5)\n"
                    "  • deep_nesting — nesting depth > 5\n"
                    "  • fixme_todo — FIXME/TODO/HACK/XXX comments\n"
                    "  • god_objects — classes with >= 20 methods\n\n"
                    "Returns a ranked report sorted by severity. "
                    "Use to find refactoring targets before evolution cycles."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Limit scan to this subdirectory or file "
                                "(relative to repo root, e.g. 'ouroboros/tools/'). "
                                "Default: scan entire repo."
                            ),
                        },
                        "category": {
                            "type": "string",
                            "enum": _ALL_CATEGORIES,
                            "description": (
                                "Focus on a single debt category. "
                                "Default: all categories."
                            ),
                        },
                        "min_severity": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                            "description": (
                                "Minimum severity to include. "
                                "low (default) = all; medium = skip fixme_todo; "
                                "high = only high_complexity, deep_nesting, god_objects."
                            ),
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": (
                                "Output format: text (default, human-readable) "
                                "or json (structured, for downstream processing)."
                            ),
                        },
                    },
                    "required": [],
                },
            },
            handler=lambda ctx, **kw: _tech_debt(ctx, **kw),
            is_code_tool=True,
        )
    ]
