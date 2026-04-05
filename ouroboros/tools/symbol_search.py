"""symbol_search — AST-based cross-reference search for any symbol.

Answers "where is X defined and where is it used?" for functions, classes,
variables, constants, and imported names across the entire codebase.

Complements:
  - callgraph   — function *call* graph (who calls whom)
  - dead_code   — finds symbols defined but never used
  - code_search — text/regex search (no AST understanding)

symbol_search uses the AST so it is:
  - name-exact (no false positives from substrings in comments/strings)
  - context-aware (distinguishes definitions from usages)
  - import-aware (tracks where symbols are imported from)

Examples:
    symbol_search(symbol="ToolEntry")          # all defs + usages
    symbol_search(symbol="get_tools", kind="def")
    symbol_search(symbol="_REPO_DIR", kind="use")
    symbol_search(symbol="execute", path="ouroboros/tools/")
    symbol_search(symbol="LLMClient", format="json")
    symbol_search(symbol="COPILOT_TOKEN", context_lines=2)
"""

from __future__ import annotations

import ast
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

# ── Constants ─────────────────────────────────────────────────────────────────

_REPO_DIR = Path(os.environ.get("REPO_DIR", "/opt/veles"))

_SKIP_DIRS = {
    "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
    "node_modules", ".venv", "venv", "dist", "build",
}

_ALL_KINDS = ("def", "use")

# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class SymbolMatch:
    """A single occurrence of a symbol in a file."""
    file: str
    line: int
    col: int
    kind: str           # "function_def" | "class_def" | "variable_def" |
                        # "import_def" | "usage" | "attribute_usage" | "import_usage"
    context: str        # source snippet for the matching line(s)
    symbol: str         # the exact matched name


@dataclass
class SymbolReport:
    symbol: str
    definitions: List[SymbolMatch] = field(default_factory=list)
    usages: List[SymbolMatch] = field(default_factory=list)


# ── File collection ───────────────────────────────────────────────────────────

def _collect_py_files(root: Path, subpath: Optional[str] = None) -> List[Path]:
    target = root
    if subpath:
        candidate = root / subpath.lstrip("/")
        if candidate.exists():
            target = candidate

    if target.is_file() and target.suffix == ".py":
        return [target]

    files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(str(target)):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        for fname in sorted(filenames):
            if fname.endswith(".py"):
                files.append(Path(dirpath) / fname)
    return files


# ── Source line helper ────────────────────────────────────────────────────────

def _get_context(src_lines: List[str], lineno: int, context_lines: int = 1) -> str:
    """Return source context around a 1-indexed line number."""
    idx = lineno - 1
    start = max(0, idx - context_lines)
    end = min(len(src_lines), idx + context_lines + 1)
    chunk = src_lines[start:end]
    # Mark the target line with a leading arrow
    target_offset = idx - start
    marked = []
    for i, line in enumerate(chunk):
        prefix = "→ " if i == target_offset else "  "
        marked.append(f"{prefix}{start + i + 1:4d}: {line.rstrip()}")
    return "\n".join(marked)


# ── AST scanner ───────────────────────────────────────────────────────────────

def _scan_file(
    py_file: Path,
    repo_root: Path,
    symbol: str,
    want_defs: bool,
    want_uses: bool,
    context_lines: int,
) -> SymbolReport:
    """Scan a single file for all occurrences of `symbol`."""
    try:
        src = py_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return SymbolReport(symbol=symbol)

    try:
        tree = ast.parse(src, filename=str(py_file))
    except SyntaxError:
        return SymbolReport(symbol=symbol)

    try:
        rel = str(py_file.relative_to(repo_root))
    except ValueError:
        rel = str(py_file)

    src_lines = src.splitlines()
    report = SymbolReport(symbol=symbol)

    # ── Walk the full AST once ────────────────────────────────────────────────
    for node in ast.walk(tree):

        # ── DEFINITIONS ──────────────────────────────────────────────────────

        if want_defs:
            # Function / async function definition
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == symbol:
                    report.definitions.append(SymbolMatch(
                        file=rel,
                        line=node.lineno,
                        col=node.col_offset,
                        kind="function_def",
                        context=_get_context(src_lines, node.lineno, context_lines),
                        symbol=symbol,
                    ))

            # Class definition
            elif isinstance(node, ast.ClassDef):
                if node.name == symbol:
                    report.definitions.append(SymbolMatch(
                        file=rel,
                        line=node.lineno,
                        col=node.col_offset,
                        kind="class_def",
                        context=_get_context(src_lines, node.lineno, context_lines),
                        symbol=symbol,
                    ))

            # Simple assignment: X = ...  or  X: int = ...
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == symbol:
                        report.definitions.append(SymbolMatch(
                            file=rel,
                            line=node.lineno,
                            col=target.col_offset,
                            kind="variable_def",
                            context=_get_context(src_lines, node.lineno, context_lines),
                            symbol=symbol,
                        ))
                    elif isinstance(target, ast.Tuple):
                        for elt in target.elts:
                            if isinstance(elt, ast.Name) and elt.id == symbol:
                                report.definitions.append(SymbolMatch(
                                    file=rel,
                                    line=node.lineno,
                                    col=elt.col_offset,
                                    kind="variable_def",
                                    context=_get_context(src_lines, node.lineno, context_lines),
                                    symbol=symbol,
                                ))

            # Annotated assignment: X: int = ...
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == symbol:
                    report.definitions.append(SymbolMatch(
                        file=rel,
                        line=node.lineno,
                        col=node.target.col_offset,
                        kind="variable_def",
                        context=_get_context(src_lines, node.lineno, context_lines),
                        symbol=symbol,
                    ))

            # Import statement: import X  /  import X as Y  /  from M import X
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    # import X → name "X" or "X.sub"
                    imported_name = alias.asname or alias.name.split(".")[0]
                    if imported_name == symbol:
                        report.definitions.append(SymbolMatch(
                            file=rel,
                            line=node.lineno,
                            col=node.col_offset,
                            kind="import_def",
                            context=_get_context(src_lines, node.lineno, context_lines),
                            symbol=symbol,
                        ))

            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported_name = alias.asname or alias.name
                    if imported_name == symbol:
                        report.definitions.append(SymbolMatch(
                            file=rel,
                            line=node.lineno,
                            col=node.col_offset,
                            kind="import_def",
                            context=_get_context(src_lines, node.lineno, context_lines),
                            symbol=symbol,
                        ))

        # ── USAGES ───────────────────────────────────────────────────────────

        if want_uses:
            # Bare name usage: foo(...)  /  x = foo  /  if foo:
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id == symbol:
                    report.usages.append(SymbolMatch(
                        file=rel,
                        line=node.lineno,
                        col=node.col_offset,
                        kind="usage",
                        context=_get_context(src_lines, node.lineno, context_lines),
                        symbol=symbol,
                    ))

            # Attribute usage: obj.symbol  (e.g. self.execute, ctx.repo_dir)
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                if node.attr == symbol:
                    report.usages.append(SymbolMatch(
                        file=rel,
                        line=node.lineno,
                        col=node.col_offset,
                        kind="attribute_usage",
                        context=_get_context(src_lines, node.lineno, context_lines),
                        symbol=symbol,
                    ))

            # Import reference: 'from M import symbol' — the symbol is *used* here
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == symbol:
                        report.usages.append(SymbolMatch(
                            file=rel,
                            line=node.lineno,
                            col=node.col_offset,
                            kind="import_usage",
                            context=_get_context(src_lines, node.lineno, context_lines),
                            symbol=symbol,
                        ))

    return report


# ── Deduplication ─────────────────────────────────────────────────────────────

def _dedup(matches: List[SymbolMatch]) -> List[SymbolMatch]:
    """Remove duplicate (file, line, kind) entries, keep first occurrence."""
    seen: set = set()
    result: List[SymbolMatch] = []
    for m in matches:
        key = (m.file, m.line, m.kind)
        if key not in seen:
            seen.add(key)
            result.append(m)
    return result


# ── Formatters ────────────────────────────────────────────────────────────────

_KIND_ICONS = {
    "function_def": "⚙️",
    "class_def": "🏛️",
    "variable_def": "📦",
    "import_def": "📥",
    "usage": "🔵",
    "attribute_usage": "🔸",
    "import_usage": "📤",
}

_KIND_LABELS = {
    "function_def": "function def",
    "class_def": "class def",
    "variable_def": "variable def",
    "import_def": "import (binding)",
    "usage": "usage",
    "attribute_usage": "attr usage",
    "import_usage": "imported by",
}


def _format_text(
    report: SymbolReport,
    total_files: int,
    kind: Optional[str],
    path: Optional[str],
    max_results: int,
    show_context: bool,
) -> str:
    lines: List[str] = []
    scope = path or "entire codebase"
    filter_note = f"  kind={kind}" if kind else ""
    total_defs = len(report.definitions)
    total_uses = len(report.usages)
    total = total_defs + total_uses

    lines.append(f"## Symbol Search: `{report.symbol}`")
    lines.append(f"   scope: {scope}{filter_note}")
    lines.append(f"   {total_files} files scanned — {total_defs} definitions, {total_uses} usages ({total} total)")
    lines.append("")

    sections: List[tuple] = []
    if kind in (None, "def"):
        sections.append(("Definitions", report.definitions))
    if kind in (None, "use"):
        sections.append(("Usages", report.usages))

    for section_name, matches in sections:
        if not matches:
            lines.append(f"**{section_name}:** none found")
            lines.append("")
            continue

        shown = matches[:max_results]
        lines.append(f"**{section_name}** ({len(matches)}{' shown: ' + str(len(shown)) if len(matches) > max_results else ''}):")

        for m in shown:
            icon = _KIND_ICONS.get(m.kind, "·")
            label = _KIND_LABELS.get(m.kind, m.kind)
            lines.append(f"  {icon} {m.file}:{m.line}  [{label}]")
            if show_context:
                for ctx_line in m.context.splitlines():
                    lines.append(f"       {ctx_line}")
        if len(matches) > max_results:
            lines.append(f"  … {len(matches) - max_results} more (use max_results to see all)")
        lines.append("")

    if total == 0:
        lines.append(f"Symbol `{report.symbol}` not found in {scope}.")
        lines.append("Tip: check spelling; symbol_search is case-sensitive.")

    return "\n".join(lines)


def _to_dict(m: SymbolMatch) -> Dict[str, Any]:
    return {
        "file": m.file,
        "line": m.line,
        "col": m.col,
        "kind": m.kind,
        "symbol": m.symbol,
        "context": m.context,
    }


# ── Tool entry point ──────────────────────────────────────────────────────────

def _symbol_search(
    ctx: ToolContext,
    symbol: str,
    path: Optional[str] = None,
    kind: Optional[str] = None,
    format: str = "text",
    context_lines: int = 1,
    max_results: int = 50,
) -> str:
    """Find all definitions and usages of a symbol across the Python codebase."""
    if not symbol or not symbol.strip():
        return "Error: `symbol` parameter is required and must be a non-empty string."

    if kind and kind not in _ALL_KINDS:
        return (
            f"Unknown kind: {kind!r}. "
            f"Valid values: {', '.join(_ALL_KINDS)} (or omit for both)"
        )

    symbol = symbol.strip()
    want_defs = kind in (None, "def")
    want_uses = kind in (None, "use")

    repo_root = Path(ctx.repo_dir if ctx and ctx.repo_dir else _REPO_DIR)
    py_files = _collect_py_files(repo_root, path)

    aggregated = SymbolReport(symbol=symbol)

    for py_file in py_files:
        file_report = _scan_file(
            py_file=py_file,
            repo_root=repo_root,
            symbol=symbol,
            want_defs=want_defs,
            want_uses=want_uses,
            context_lines=context_lines,
        )
        aggregated.definitions.extend(file_report.definitions)
        aggregated.usages.extend(file_report.usages)

    # Deduplicate and sort by file + line
    aggregated.definitions = sorted(
        _dedup(aggregated.definitions),
        key=lambda m: (m.file, m.line),
    )
    aggregated.usages = sorted(
        _dedup(aggregated.usages),
        key=lambda m: (m.file, m.line),
    )

    if format == "json":
        return json.dumps(
            {
                "symbol": symbol,
                "total_files": len(py_files),
                "definitions": [_to_dict(m) for m in aggregated.definitions],
                "usages": [_to_dict(m) for m in aggregated.usages],
                "filters": {
                    "path": path,
                    "kind": kind,
                    "context_lines": context_lines,
                    "max_results": max_results,
                },
            },
            ensure_ascii=False,
            indent=2,
        )

    show_context = context_lines > 0
    return _format_text(
        report=aggregated,
        total_files=len(py_files),
        kind=kind,
        path=path,
        max_results=max_results,
        show_context=show_context,
    )


# ── Tool registration ─────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="symbol_search",
            schema={
                "name": "symbol_search",
                "description": (
                    "AST-based cross-reference search for any Python symbol "
                    "(function, class, variable, constant, imported name). "
                    "Finds all definitions AND all usages across the codebase — "
                    "like 'Find All References' in an IDE, but name-exact (no substring "
                    "false positives). Complements callgraph (call relationships) and "
                    "dead_code (unused symbols). "
                    "Examples: symbol_search(symbol='ToolEntry'), "
                    "symbol_search(symbol='get_tools', kind='def'), "
                    "symbol_search(symbol='_REPO_DIR', kind='use'), "
                    "symbol_search(symbol='execute', path='ouroboros/tools/')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": (
                                "Symbol name to search for (case-sensitive). "
                                "Can be a function name, class name, variable name, "
                                "constant, or any Python identifier."
                            ),
                        },
                        "path": {
                            "type": "string",
                            "description": (
                                "Optional path filter — file or directory relative to repo root. "
                                "E.g. 'ouroboros/tools/', 'ouroboros/loop.py'. "
                                "If omitted, scans the entire repository."
                            ),
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["def", "use"],
                            "description": (
                                "'def' — show only definitions (function/class/variable/import bindings). "
                                "'use' — show only usages (Name loads, attribute accesses, import references). "
                                "Omit to show both (default)."
                            ),
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format. 'text' (default) for human-readable, 'json' for machine-readable.",
                        },
                        "context_lines": {
                            "type": "integer",
                            "description": (
                                "Number of source lines to show above and below each match (default: 1). "
                                "Set to 0 to suppress context and show only file:line references."
                            ),
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results per section (default: 50). Increase for exhaustive listing.",
                        },
                    },
                    "required": ["symbol"],
                },
            },
            handler=_symbol_search,
        )
    ]
