"""code_search — fast structured search across the codebase.

Growth tool: native code search without shell grep timeouts.
Supports literal/regex patterns, file filters, context lines,
symbol search (def/class), and cross-file reference tracking.
Returns structured JSON with file paths, line numbers, and matches.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

# ── constants ─────────────────────────────────────────────────────────────────

_DEFAULT_REPO = os.environ.get("REPO_DIR", "/opt/veles")
_MAX_RESULTS = 500
_MAX_CONTEXT_LINES = 10
_MAX_LINE_LEN = 400  # truncate very long lines

# Extensions considered "code" for default searches
_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".go", ".rs", ".c", ".cpp", ".h",
    ".java", ".rb", ".sh", ".yaml", ".yml", ".json", ".toml",
    ".md", ".txt", ".html", ".css",
}

# Directories to skip always
_SKIP_DIRS = {
    "__pycache__", ".git", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", "dist", "build",
    ".eggs", "*.egg-info",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _should_skip_dir(name: str) -> bool:
    return name.startswith(".") or name in _SKIP_DIRS or name.endswith(".egg-info")


def _iter_files(
    root: str,
    include_ext: Optional[List[str]] = None,
    path_pattern: Optional[str] = None,
    exclude_path: Optional[str] = None,
) -> List[Path]:
    """Walk root and yield matching files."""
    root_path = Path(root)
    results: List[Path] = []

    for dirpath, dirnames, filenames in os.walk(root_path):
        # Prune skipped dirs in-place (prevents descending)
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]

        for fname in filenames:
            fp = Path(dirpath) / fname

            # Extension filter
            if include_ext:
                if fp.suffix.lower() not in include_ext:
                    continue
            else:
                if fp.suffix.lower() not in _CODE_EXTENSIONS:
                    continue

            # Path pattern filter
            rel = str(fp.relative_to(root_path))
            if path_pattern and path_pattern.lower() not in rel.lower():
                continue
            if exclude_path and exclude_path.lower() in rel.lower():
                continue

            results.append(fp)

    return sorted(results)


def _search_file(
    fp: Path,
    pattern: re.Pattern,  # type: ignore[type-arg]
    context: int,
    max_per_file: int,
    root: str,
) -> List[Dict[str, Any]]:
    """Search a single file, return list of match records."""
    try:
        text = fp.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    lines = text.splitlines()
    rel_path = str(fp.relative_to(root))
    matches: List[Dict[str, Any]] = []

    for i, line in enumerate(lines):
        if len(matches) >= max_per_file:
            break
        # Truncate very long lines before matching (but keep original for match extraction)
        display_line = line[:_MAX_LINE_LEN] + "…" if len(line) > _MAX_LINE_LEN else line
        if pattern.search(line):
            record: Dict[str, Any] = {
                "file": rel_path,
                "line": i + 1,
                "text": display_line.strip(),
            }
            if context > 0:
                ctx_before = []
                for j in range(max(0, i - context), i):
                    ctx_before.append(lines[j][:_MAX_LINE_LEN].rstrip())
                ctx_after = []
                for j in range(i + 1, min(len(lines), i + 1 + context)):
                    ctx_after.append(lines[j][:_MAX_LINE_LEN].rstrip())
                if ctx_before:
                    record["before"] = ctx_before
                if ctx_after:
                    record["after"] = ctx_after
            matches.append(record)

    return matches


def _extract_symbols(
    fp: Path,
    pattern: re.Pattern,  # type: ignore[type-arg]
    root: str,
) -> List[Dict[str, Any]]:
    """Extract function/class definitions matching the pattern."""
    try:
        text = fp.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    lines = text.splitlines()
    rel_path = str(fp.relative_to(root))
    symbols: List[Dict[str, Any]] = []

    # Match def/class/async def lines
    sym_re = re.compile(r"^(class|def|async def)\s+(\w+)")

    for i, line in enumerate(lines):
        m = sym_re.match(line.strip())
        if m:
            kind = m.group(1).replace("async def", "async_def")
            name = m.group(2)
            if pattern.search(name):
                symbols.append({
                    "file": rel_path,
                    "line": i + 1,
                    "kind": kind,
                    "name": name,
                    "signature": line.strip()[:_MAX_LINE_LEN],
                })

    return symbols


# ── main handler ──────────────────────────────────────────────────────────────

def _code_search(
    ctx: ToolContext,
    pattern: str,
    mode: str = "text",
    path_filter: str = "",
    ext_filter: str = "",
    exclude_path: str = "",
    context_lines: int = 0,
    max_results: int = 50,
    case_sensitive: bool = False,
    whole_word: bool = False,
    root: str = "",
) -> str:
    """Search the codebase for a pattern, symbol, or definition."""

    start = time.monotonic()
    root = root.strip() or _DEFAULT_REPO

    if not Path(root).exists():
        return json.dumps({"error": f"Root directory not found: {root}"}, indent=2)

    if not pattern.strip():
        return json.dumps({"error": "Pattern must not be empty."}, indent=2)

    # Clamp params
    max_results = max(1, min(max_results, _MAX_RESULTS))
    context_lines = max(0, min(context_lines, _MAX_CONTEXT_LINES))

    # Build regex
    flags = 0 if case_sensitive else re.IGNORECASE
    raw_pattern = pattern

    try:
        if mode == "symbol":
            # Symbol mode: search def/class names (regex on name only)
            compiled = re.compile(raw_pattern, flags)
        elif mode == "regex":
            compiled = re.compile(raw_pattern, flags)
        else:
            # Literal text mode
            escaped = re.escape(raw_pattern)
            if whole_word:
                escaped = r"\b" + escaped + r"\b"
            compiled = re.compile(escaped, flags)
    except re.error as e:
        return json.dumps({"error": f"Invalid regex pattern: {e}"}, indent=2)

    # Resolve extension filter
    include_ext: Optional[List[str]] = None
    if ext_filter:
        include_ext = [e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower()
                       for e in ext_filter.split(",") if e.strip()]

    # Walk files
    files = _iter_files(
        root=root,
        include_ext=include_ext,
        path_pattern=path_filter.strip() or None,
        exclude_path=exclude_path.strip() or None,
    )

    # Search
    all_matches: List[Dict[str, Any]] = []
    files_searched = 0
    files_with_matches = 0
    max_per_file = max(1, min(max_results, 20))  # up to 20 matches per file

    for fp in files:
        if len(all_matches) >= max_results:
            break
        files_searched += 1

        if mode == "symbol":
            file_matches = _extract_symbols(fp, compiled, root)
        else:
            file_matches = _search_file(fp, compiled, context_lines, max_per_file, root)

        if file_matches:
            files_with_matches += 1
            remaining = max_results - len(all_matches)
            all_matches.extend(file_matches[:remaining])

    elapsed = round(time.monotonic() - start, 3)

    result: Dict[str, Any] = {
        "pattern": pattern,
        "mode": mode,
        "files_searched": files_searched,
        "files_with_matches": files_with_matches,
        "total_matches": len(all_matches),
        "elapsed_sec": elapsed,
        "matches": all_matches,
    }

    if len(all_matches) >= max_results:
        result["truncated"] = True
        result["hint"] = "Results truncated. Use path_filter/ext_filter to narrow scope."

    return json.dumps(result, ensure_ascii=False, indent=2)


# ── registry ──────────────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="code_search",
            schema={
                "name": "code_search",
                "description": (
                    "Search the codebase for patterns, text, symbols (functions/classes), or references. "
                    "Returns structured JSON with file paths, line numbers, and match context. "
                    "Faster and more structured than run_shell grep — no timeouts, no raw text parsing. "
                    "Modes: 'text' (literal), 'regex' (full regex), 'symbol' (def/class name search). "
                    "Use for: finding function definitions, tracking API usage, locating TODO/FIXME, "
                    "cross-file reference analysis, understanding where a pattern appears."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": (
                                "Search pattern. In 'text' mode: literal string. "
                                "In 'regex' mode: Python regex. "
                                "In 'symbol' mode: regex matched against function/class names."
                            ),
                        },
                        "mode": {
                            "type": "string",
                            "description": (
                                "Search mode: 'text' (default, literal), "
                                "'regex' (Python regex in line content), "
                                "'symbol' (search def/class names)."
                            ),
                            "enum": ["text", "regex", "symbol"],
                        },
                        "path_filter": {
                            "type": "string",
                            "description": (
                                "Substring filter on relative file paths. "
                                "E.g. 'ouroboros/tools' to search only tools, 'supervisor' for supervisor code."
                            ),
                        },
                        "ext_filter": {
                            "type": "string",
                            "description": (
                                "Comma-separated file extensions to include. "
                                "E.g. '.py,.ts' or 'py,yaml'. Defaults to all code files."
                            ),
                        },
                        "exclude_path": {
                            "type": "string",
                            "description": "Substring to exclude from file paths. E.g. 'test' to skip test files.",
                        },
                        "context_lines": {
                            "type": "integer",
                            "description": "Number of lines before and after each match to include (0-10, default 0).",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum total matches to return (1-500, default 50).",
                        },
                        "case_sensitive": {
                            "type": "boolean",
                            "description": "Case-sensitive matching. Default: false (case-insensitive).",
                        },
                        "whole_word": {
                            "type": "boolean",
                            "description": "Match whole words only (text mode). Default: false.",
                        },
                        "root": {
                            "type": "string",
                            "description": "Root directory to search. Defaults to /opt/veles (the repo).",
                        },
                    },
                    "required": ["pattern"],
                },
            },
            handler=_code_search,
            timeout_sec=30,
        ),
    ]
