"""duplicate_code — AST-based code clone detector.

Detects duplicate and near-duplicate function bodies (copy-paste code) in the
Python codebase.  Two levels of detection:

  Type-1 (exact)      — byte-for-byte identical function bodies after stripping
                        whitespace and comments.  Indicates pure copy-paste.

  Type-2 (normalized) — bodies that become identical after renaming all local
                        variables/args to canonical positional tokens (_v0, _v1
                        …) and replacing literal values with _STR / _NUM.
                        Indicates copy-paste with minor renaming.

Each clone group reports:
  - clone_type    — "exact" or "normalized"
  - body_lines    — median body line count
  - instances     — list of {file, line, name, end_line} for every duplicate

Filters:
  path            — limit to a subdirectory or single file
  min_lines       — ignore function bodies shorter than this (default 5)
  min_group_size  — only report groups with >= N instances (default 2)
  clone_type      — "exact" | "normalized" | "all" (default "all")
  format          — "text" | "json" (default "text")

Examples:
    duplicate_code()                          # full scan, text
    duplicate_code(path="ouroboros/")         # one directory
    duplicate_code(min_lines=10)              # only substantial clones
    duplicate_code(clone_type="exact")        # pure copy-paste only
    duplicate_code(format="json")
"""

from __future__ import annotations

import ast
import copy
import hashlib
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

_CLONE_TYPE_EXACT = "exact"
_CLONE_TYPE_NORMALIZED = "normalized"
_ALL_CLONE_TYPES = [_CLONE_TYPE_EXACT, _CLONE_TYPE_NORMALIZED]


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


# ── AST normalization ─────────────────────────────────────────────────────────

def _strip_locations(node: ast.AST) -> None:
    """Zero out all source-location fields so they don't affect the dump."""
    for n in ast.walk(node):
        for attr in ("lineno", "col_offset", "end_lineno", "end_col_offset"):
            if hasattr(n, attr):
                setattr(n, attr, 0)


def _exact_hash(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Hash the function body using the exact AST dump (Type-1)."""
    body_copy = copy.deepcopy(func.body)
    wrapper = ast.Module(body=body_copy, type_ignores=[])
    _strip_locations(wrapper)
    raw = ast.dump(wrapper, indent=None)
    return hashlib.sha256(raw.encode()).hexdigest()


def _normalized_hash(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Hash the function body after normalizing variable names (Type-2)."""
    body_copy = copy.deepcopy(func.body)
    wrapper = ast.Module(body=body_copy, type_ignores=[])
    _strip_locations(wrapper)

    name_map: Dict[str, str] = {}
    counter = [0]

    for node in ast.walk(wrapper):
        if isinstance(node, ast.Name):
            if node.id not in name_map:
                name_map[node.id] = f"_v{counter[0]}"
                counter[0] += 1
            node.id = name_map[node.id]
        elif isinstance(node, ast.arg):
            if node.arg not in name_map:
                name_map[node.arg] = f"_v{counter[0]}"
                counter[0] += 1
            node.arg = name_map[node.arg]
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                node.value = "_STR"
            elif isinstance(node.value, (int, float, complex)):
                node.value = 0
            elif node.value is None:
                pass  # keep None as structural
        elif isinstance(node, ast.Attribute):
            # Normalize attribute names too
            if node.attr not in name_map:
                name_map[node.attr] = f"_a{counter[0]}"
                counter[0] += 1
            node.attr = name_map[node.attr]

    raw = ast.dump(wrapper, indent=None)
    return hashlib.sha256(raw.encode()).hexdigest()


def _body_lines(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Approximate body line count."""
    if not func.body:
        return 0
    start = func.body[0].lineno if hasattr(func.body[0], "lineno") else func.lineno
    end = getattr(func, "end_lineno", start)
    return max(1, end - start + 1)


# ── Per-file extractor ─────────────────────────────────────────────────────────

class _FuncRecord:
    """Metadata + hashes for one function definition."""
    __slots__ = ("file", "line", "end_line", "name", "exact_hash", "norm_hash", "lines")

    def __init__(
        self,
        file: str,
        line: int,
        end_line: int,
        name: str,
        exact_hash: str,
        norm_hash: str,
        lines: int,
    ) -> None:
        self.file = file
        self.line = line
        self.end_line = end_line
        self.name = name
        self.exact_hash = exact_hash
        self.norm_hash = norm_hash
        self.lines = lines


def _extract_functions(path: Path, rel_path: str) -> List[_FuncRecord]:
    """Parse one file and return _FuncRecord for every function body."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    records: List[_FuncRecord] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body:
            continue

        exact = _exact_hash(node)
        norm = _normalized_hash(node)
        lines = _body_lines(node)
        end_line = getattr(node, "end_lineno", node.lineno)

        records.append(
            _FuncRecord(
                file=rel_path,
                line=node.lineno,
                end_line=end_line,
                name=node.name,
                exact_hash=exact,
                norm_hash=norm,
                lines=lines,
            )
        )

    return records


# ── Clone grouping ─────────────────────────────────────────────────────────────

def _find_clones(
    records: List[_FuncRecord],
    min_lines: int,
    min_group_size: int,
    clone_type: str,
) -> List[Dict[str, Any]]:
    """Group records by hash and return clone groups."""
    filtered = [r for r in records if r.lines >= min_lines]

    # Build groups for each clone type
    groups_exact: Dict[str, List[_FuncRecord]] = {}
    groups_norm: Dict[str, List[_FuncRecord]] = {}

    for rec in filtered:
        groups_exact.setdefault(rec.exact_hash, []).append(rec)
        groups_norm.setdefault(rec.norm_hash, []).append(rec)

    result: List[Dict[str, Any]] = []

    if clone_type in (_CLONE_TYPE_EXACT, "all"):
        for h, recs in groups_exact.items():
            if len(recs) >= min_group_size:
                result.append(_make_group(recs, _CLONE_TYPE_EXACT))

    if clone_type in (_CLONE_TYPE_NORMALIZED, "all"):
        # Only report normalized groups not already covered by exact
        exact_hashes_in_groups = {
            r.exact_hash
            for grp in result
            for inst in grp["instances"]
            # find record by file+line
            for r in filtered
            if r.file == inst["file"] and r.line == inst["line"]
        }
        for h, recs in groups_norm.items():
            if len(recs) < min_group_size:
                continue
            # Skip if all members share the same exact hash (already reported)
            exact_hashes = {r.exact_hash for r in recs}
            if len(exact_hashes) == 1 and next(iter(exact_hashes)) in {
                g["_exact_hash"] for g in result if "_exact_hash" in g
            }:
                continue
            # Skip pure-exact groups (they show up above)
            if len(exact_hashes) == 1:
                # All exact copies — skip, they're already in exact bucket
                continue
            result.append(_make_group(recs, _CLONE_TYPE_NORMALIZED))

    # Sort by group size descending, then by body_lines descending
    result.sort(key=lambda g: (-g["instance_count"], -g["body_lines"]))
    return result


def _make_group(recs: List[_FuncRecord], ctype: str) -> Dict[str, Any]:
    med_lines = sorted(r.lines for r in recs)[len(recs) // 2]
    instances = sorted(
        [
            {"file": r.file, "line": r.line, "end_line": r.end_line, "name": r.name}
            for r in recs
        ],
        key=lambda x: (x["file"], x["line"]),
    )
    grp: Dict[str, Any] = {
        "clone_type": ctype,
        "body_lines": med_lines,
        "instance_count": len(recs),
        "instances": instances,
    }
    if ctype == _CLONE_TYPE_EXACT:
        grp["_exact_hash"] = recs[0].exact_hash  # internal dedup key
    return grp


# ── Text formatter ─────────────────────────────────────────────────────────────

def _format_text(
    groups: List[Dict[str, Any]],
    total_files: int,
    total_funcs: int,
    filters: Dict[str, Any],
) -> str:
    lines: List[str] = []

    filter_parts = []
    if filters.get("path"):
        filter_parts.append(f"path={filters['path']}")
    if filters.get("min_lines", 5) != 5:
        filter_parts.append(f"min_lines={filters['min_lines']}")
    if filters.get("clone_type", "all") != "all":
        filter_parts.append(f"clone_type={filters['clone_type']}")
    filter_str = (", " + ", ".join(filter_parts)) if filter_parts else ""

    n_exact = sum(1 for g in groups if g["clone_type"] == _CLONE_TYPE_EXACT)
    n_norm = sum(1 for g in groups if g["clone_type"] == _CLONE_TYPE_NORMALIZED)
    cloned_funcs = sum(g["instance_count"] for g in groups)

    lines.append(
        f"## Duplicate Code Report — {total_files} files, {total_funcs} functions scanned{filter_str}"
    )
    lines.append(
        f"   {len(groups)} clone groups found  "
        f"({n_exact} exact, {n_norm} normalized)  "
        f"— {cloned_funcs} total duplicate instances\n"
    )

    if not groups:
        lines.append("✅ No duplicate functions found.")
        return "\n".join(lines)

    for i, grp in enumerate(groups, 1):
        icon = "🔴" if grp["clone_type"] == _CLONE_TYPE_EXACT else "🟡"
        lines.append(
            f"{icon} Group {i} — {grp['clone_type'].upper()}  "
            f"×{grp['instance_count']} instances  "
            f"~{grp['body_lines']} lines each"
        )
        for inst in grp["instances"]:
            lines.append(
                f"   {inst['file']}:{inst['line']}–{inst['end_line']}  {inst['name']}()"
            )
        lines.append("")

    return "\n".join(lines)


# ── Tool entry point ───────────────────────────────────────────────────────────

def _duplicate_code(
    ctx: ToolContext,
    path: Optional[str] = None,
    min_lines: int = 5,
    min_group_size: int = 2,
    clone_type: str = "all",
    format: str = "text",
) -> str:
    """Detect duplicate and near-duplicate function bodies (code clones)."""
    if clone_type not in (_ALL_CLONE_TYPES + ["all"]):
        return (
            f"Unknown clone_type: {clone_type!r}. "
            f"Valid: exact, normalized, all"
        )

    repo_root = Path(ctx.repo_dir if ctx and ctx.repo_dir else _REPO_DIR)
    py_files = _collect_py_files(repo_root, path)

    all_records: List[_FuncRecord] = []
    for fpath in py_files:
        try:
            rel = str(fpath.relative_to(repo_root))
        except ValueError:
            rel = str(fpath)
        all_records.extend(_extract_functions(fpath, rel))

    groups = _find_clones(all_records, min_lines, min_group_size, clone_type)

    # Strip internal keys before output
    for g in groups:
        g.pop("_exact_hash", None)

    filters = {
        "path": path,
        "min_lines": min_lines,
        "min_group_size": min_group_size,
        "clone_type": clone_type,
    }

    if format == "json":
        return json.dumps(
            {
                "total_files": len(py_files),
                "total_functions": len(all_records),
                "total_clone_groups": len(groups),
                "clone_groups": groups,
                "filters": filters,
            },
            ensure_ascii=False,
            indent=2,
        )

    return _format_text(groups, len(py_files), len(all_records), filters)


# ── Tool registration ──────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="duplicate_code",
            schema={
                "name": "duplicate_code",
                "description": (
                    "Detect duplicate and near-duplicate function bodies (code clones). "
                    "Type-1 (exact): identical AST bodies. "
                    "Type-2 (normalized): identical after renaming variables/args to "
                    "canonical tokens and replacing literals. "
                    "Reports clone groups with file/line/name for each instance."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Limit scan to a subdirectory or file (relative to repo root)",
                        },
                        "min_lines": {
                            "type": "integer",
                            "description": "Minimum function body line count to include (default 5)",
                        },
                        "min_group_size": {
                            "type": "integer",
                            "description": "Minimum instances in a clone group to report (default 2)",
                        },
                        "clone_type": {
                            "type": "string",
                            "enum": ["exact", "normalized", "all"],
                            "description": "exact=identical bodies, normalized=renamed vars, all=both (default)",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format (default text)",
                        },
                    },
                },
            },
            handler=_duplicate_code,
        )
    ]
