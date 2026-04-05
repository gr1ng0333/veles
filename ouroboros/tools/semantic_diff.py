"""semantic_diff — function/class-level change analysis between two git refs.

Shows what changed *functionally* between two git revisions: which functions
and classes were added, removed, or modified across all Python files in the
repo. Goes beyond raw git diff to answer "what behaviour changed?"
"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import textwrap
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class FuncInfo:
    name: str
    lineno: int
    end_lineno: int
    is_method: bool
    parent_class: Optional[str]

    @property
    def full_name(self) -> str:
        if self.parent_class:
            return f"{self.parent_class}.{self.name}"
        return self.name

    @property
    def line_count(self) -> int:
        return max(1, self.end_lineno - self.lineno + 1)


@dataclass
class ClassInfo:
    name: str
    lineno: int
    end_lineno: int
    method_names: Set[str] = field(default_factory=set)


@dataclass
class ModuleSnapshot:
    functions: Dict[str, FuncInfo] = field(default_factory=dict)
    classes: Dict[str, ClassInfo] = field(default_factory=dict)


# ── AST parsing ───────────────────────────────────────────────────────────────

def _parse_source(source: str) -> ModuleSnapshot:
    """Extract function and class signatures from Python source."""
    snap = ModuleSnapshot()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return snap

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            methods: Set[str] = set()
            for item in ast.walk(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item is not node:
                    # only direct methods (one level down)
                    methods.add(item.name)
            snap.classes[node.name] = ClassInfo(
                name=node.name,
                lineno=node.lineno,
                end_lineno=getattr(node, "end_lineno", node.lineno),
                method_names=methods,
            )

    # Walk top-level and class-level functions separately
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", node.lineno)
        # Determine parent class
        parent: Optional[str] = None
        for cls_node in ast.walk(tree):
            if not isinstance(cls_node, ast.ClassDef):
                continue
            cls_end = getattr(cls_node, "end_lineno", cls_node.lineno)
            if cls_node.lineno <= node.lineno <= cls_end:
                parent = cls_node.name
                break
        fi = FuncInfo(
            name=node.name,
            lineno=node.lineno,
            end_lineno=end,
            is_method=parent is not None,
            parent_class=parent,
        )
        snap.functions[fi.full_name] = fi

    return snap


# ── Git helpers ────────────────────────────────────────────────────────────────

def _git_show(repo_dir: pathlib.Path, ref: str, path: str) -> Optional[str]:
    """Return file content at git ref, or None if not present."""
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
        cwd=str(repo_dir),
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _changed_py_files(
    repo_dir: pathlib.Path, ref_a: str, ref_b: str
) -> List[str]:
    """Return list of .py paths changed between ref_a and ref_b."""
    result = subprocess.run(
        ["git", "diff", "--name-only", ref_a, ref_b, "--", "*.py"],
        capture_output=True,
        text=True,
        cwd=str(repo_dir),
    )
    if result.returncode != 0:
        return []
    return [
        p.strip() for p in result.stdout.splitlines()
        if p.strip().endswith(".py") and not p.strip().startswith("build/")
    ]


def _resolve_ref(repo_dir: pathlib.Path, ref: str) -> str:
    """Resolve a ref to a SHA (best-effort)."""
    r = subprocess.run(
        ["git", "rev-parse", "--short", ref],
        capture_output=True, text=True, cwd=str(repo_dir),
    )
    return r.stdout.strip() if r.returncode == 0 else ref


# ── Diff computation ──────────────────────────────────────────────────────────

@dataclass
class FileDiff:
    path: str
    added_funcs: List[str] = field(default_factory=list)
    removed_funcs: List[str] = field(default_factory=list)
    modified_funcs: List[str] = field(default_factory=list)
    added_classes: List[str] = field(default_factory=list)
    removed_classes: List[str] = field(default_factory=list)
    file_added: bool = False
    file_removed: bool = False

    @property
    def total_changes(self) -> int:
        return (
            len(self.added_funcs)
            + len(self.removed_funcs)
            + len(self.modified_funcs)
            + len(self.added_classes)
            + len(self.removed_classes)
            + int(self.file_added)
            + int(self.file_removed)
        )

    def is_empty(self) -> bool:
        return self.total_changes == 0


def _compare_snapshots(
    path: str,
    snap_a: Optional[ModuleSnapshot],
    snap_b: Optional[ModuleSnapshot],
) -> FileDiff:
    fd = FileDiff(path=path)

    if snap_a is None and snap_b is not None:
        fd.file_added = True
        fd.added_funcs = sorted(snap_b.functions.keys())
        fd.added_classes = sorted(snap_b.classes.keys())
        return fd

    if snap_a is not None and snap_b is None:
        fd.file_removed = True
        fd.removed_funcs = sorted(snap_a.functions.keys())
        fd.removed_classes = sorted(snap_a.classes.keys())
        return fd

    if snap_a is None or snap_b is None:
        return fd

    # Functions
    names_a = set(snap_a.functions.keys())
    names_b = set(snap_b.functions.keys())
    fd.added_funcs = sorted(names_b - names_a)
    fd.removed_funcs = sorted(names_a - names_b)

    # Modified: same name, different line count (a proxy for body change)
    for name in names_a & names_b:
        fa = snap_a.functions[name]
        fb = snap_b.functions[name]
        if fa.line_count != fb.line_count:
            fd.modified_funcs.append(name)
    fd.modified_funcs.sort()

    # Classes
    cls_a = set(snap_a.classes.keys())
    cls_b = set(snap_b.classes.keys())
    fd.added_classes = sorted(cls_b - cls_a)
    fd.removed_classes = sorted(cls_a - cls_b)

    return fd


# ── Aggregation and formatting ─────────────────────────────────────────────────

def _analyse(
    repo_dir: pathlib.Path,
    ref_a: str,
    ref_b: str,
    path_filter: Optional[str],
    max_files: int,
) -> Tuple[List[FileDiff], Dict[str, int]]:
    changed = _changed_py_files(repo_dir, ref_a, ref_b)
    if path_filter:
        changed = [p for p in changed if path_filter in p]
    changed = changed[:max_files]

    diffs: List[FileDiff] = []
    totals: Dict[str, int] = {
        "added_funcs": 0,
        "removed_funcs": 0,
        "modified_funcs": 0,
        "added_classes": 0,
        "removed_classes": 0,
        "files_added": 0,
        "files_removed": 0,
    }

    for path in changed:
        src_a = _git_show(repo_dir, ref_a, path)
        src_b = _git_show(repo_dir, ref_b, path)
        snap_a = _parse_source(src_a) if src_a is not None else None
        snap_b = _parse_source(src_b) if src_b is not None else None
        fd = _compare_snapshots(path, snap_a, snap_b)
        if not fd.is_empty():
            diffs.append(fd)
        totals["added_funcs"] += len(fd.added_funcs)
        totals["removed_funcs"] += len(fd.removed_funcs)
        totals["modified_funcs"] += len(fd.modified_funcs)
        totals["added_classes"] += len(fd.added_classes)
        totals["removed_classes"] += len(fd.removed_classes)
        totals["files_added"] += int(fd.file_added)
        totals["files_removed"] += int(fd.file_removed)

    # Sort by most changes first
    diffs.sort(key=lambda d: d.total_changes, reverse=True)
    return diffs, totals


def _format_text(
    diffs: List[FileDiff],
    totals: Dict[str, int],
    ref_a: str,
    ref_b: str,
    sha_a: str,
    sha_b: str,
) -> str:
    lines: List[str] = []
    lines.append(f"## Semantic Diff  {sha_a} → {sha_b}")
    lines.append(
        f"Summary: +{totals['added_funcs']} funcs  "
        f"-{totals['removed_funcs']} funcs  "
        f"~{totals['modified_funcs']} modified  "
        f"+{totals['added_classes']} classes  "
        f"-{totals['removed_classes']} classes  "
        f"({totals['files_added']} files added, {totals['files_removed']} files removed)"
    )
    lines.append("")

    for fd in diffs:
        marker = ""
        if fd.file_added:
            marker = " [NEW FILE]"
        elif fd.file_removed:
            marker = " [DELETED]"
        lines.append(f"### {fd.path}{marker}  ({fd.total_changes} changes)")
        if fd.added_classes:
            lines.append(f"  + classes: {', '.join(fd.added_classes)}")
        if fd.removed_classes:
            lines.append(f"  - classes: {', '.join(fd.removed_classes)}")
        if fd.added_funcs:
            # Wrap long lists
            wrapped = textwrap.wrap(", ".join(fd.added_funcs), width=80)
            lines.append("  + funcs: " + wrapped[0])
            for part in wrapped[1:]:
                lines.append("           " + part)
        if fd.removed_funcs:
            wrapped = textwrap.wrap(", ".join(fd.removed_funcs), width=80)
            lines.append("  - funcs: " + wrapped[0])
            for part in wrapped[1:]:
                lines.append("           " + part)
        if fd.modified_funcs:
            wrapped = textwrap.wrap(", ".join(fd.modified_funcs), width=80)
            lines.append("  ~ modified: " + wrapped[0])
            for part in wrapped[1:]:
                lines.append("              " + part)
        lines.append("")

    if not diffs:
        lines.append("No semantic changes detected.")

    return "\n".join(lines).rstrip()


def _format_json(
    diffs: List[FileDiff],
    totals: Dict[str, int],
    ref_a: str,
    ref_b: str,
    sha_a: str,
    sha_b: str,
) -> Dict[str, Any]:
    return {
        "ref_a": ref_a,
        "ref_b": ref_b,
        "sha_a": sha_a,
        "sha_b": sha_b,
        "totals": totals,
        "files": [
            {
                "path": fd.path,
                "file_added": fd.file_added,
                "file_removed": fd.file_removed,
                "added_funcs": fd.added_funcs,
                "removed_funcs": fd.removed_funcs,
                "modified_funcs": fd.modified_funcs,
                "added_classes": fd.added_classes,
                "removed_classes": fd.removed_classes,
            }
            for fd in diffs
        ],
    }


# ── Main handler ──────────────────────────────────────────────────────────────

def _semantic_diff(
    ctx: ToolContext,
    ref_a: str = "HEAD~1",
    ref_b: str = "HEAD",
    path_filter: str = "",
    max_files: int = 50,
    format: str = "text",
) -> Any:
    """Compare two git refs and report function/class-level changes.

    Args:
        ref_a: Base git ref (default: HEAD~1).
        ref_b: Target git ref (default: HEAD).
        path_filter: Optional substring to filter file paths (e.g. 'ouroboros/tools').
        max_files: Maximum number of changed files to analyse (default 50).
        format: Output format — 'text' (default) or 'json'.

    Returns:
        Structured report of added/removed/modified functions and classes.
    """
    import json as _json

    repo_dir = pathlib.Path(ctx.repo_dir if ctx and ctx.repo_dir else "/opt/veles")
    sha_a = _resolve_ref(repo_dir, ref_a)
    sha_b = _resolve_ref(repo_dir, ref_b)
    diffs, totals = _analyse(repo_dir, ref_a, ref_b, path_filter or None, max_files)

    if format == "json":
        return {"result": _json.dumps(_format_json(diffs, totals, ref_a, ref_b, sha_a, sha_b), indent=2)}

    return {"result": _format_text(diffs, totals, ref_a, ref_b, sha_a, sha_b)}


# ── Tool registration ─────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="semantic_diff",
            schema={
                "name": "semantic_diff",
                "description": (
                    "Compare two git revisions and show what changed functionally: "
                    "added/removed/modified functions and classes per file. "
                    "Answers 'what behaviour changed?' between versions or commits. "
                    "More informative than raw git diff for evolution review."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ref_a": {
                            "type": "string",
                            "description": "Base git ref (branch, tag, SHA). Default: HEAD~1.",
                        },
                        "ref_b": {
                            "type": "string",
                            "description": "Target git ref. Default: HEAD.",
                        },
                        "path_filter": {
                            "type": "string",
                            "description": "Optional substring to filter file paths (e.g. 'ouroboros/tools').",
                        },
                        "max_files": {
                            "type": "integer",
                            "description": "Max changed files to analyse (default 50).",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format (default: text).",
                        },
                    },
                    "required": [],
                },
            },
            handler=lambda ctx, **kw: _semantic_diff(ctx, **kw),
            is_code_tool=True,
        )
    ]
