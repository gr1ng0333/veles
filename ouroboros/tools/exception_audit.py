"""exception_audit — AST-based exception handling anti-pattern scanner.

Scans Python source files for problematic exception handling patterns that
indicate silent failures, over-broad catches, or other error-handling bugs.

Six anti-pattern categories:

  bare_except         — ``except:`` with no exception type at all.  Catches
                        SystemExit, KeyboardInterrupt, and generator exits,
                        making the program very hard to kill.

  broad_except        — ``except Exception`` or ``except BaseException``
                        without a subsequent re-raise.  Swallows all
                        exceptions including unexpected ones.

  silent_except       — handler body is just ``pass`` (possibly with an
                        assignment to ``_`` or similar) with no logging or
                        re-raise.  The error disappears silently.

  reraise_as_new      — ``raise OtherError(...)`` inside an ``except``
                        block without ``from e`` — loses the original
                        traceback (anti-pattern in Python 3).

  string_exception    — ``raise "error message"`` (Python 2 remnant, still
                        causes ``TypeError`` in Python 3).

  overly_nested       — ``try`` block nested more than *N* levels deep
                        (configurable, default 3).  Usually a sign of
                        poorly structured control flow.

Each finding includes:
  file, line, pattern, severity, detail

Filters:
  path            — limit to a subdirectory or single file
  patterns        — comma-separated list of pattern names to include
                    (default: all)
  min_severity    — "low" | "medium" | "high" (default: low = show all)
  format          — "text" | "json" (default "text")
  max_nest_depth  — integer, overly_nested threshold (default 3)

Examples:
    exception_audit()                            # full scan
    exception_audit(path="ouroboros/")
    exception_audit(patterns="bare_except,silent_except")
    exception_audit(min_severity="medium")
    exception_audit(format="json")
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ouroboros.tools.registry import ToolContext, ToolEntry

# ── Constants ─────────────────────────────────────────────────────────────────

_REPO_DIR = Path(os.environ.get("REPO_DIR", "/opt/veles"))

_SKIP_DIRS = {
    "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
    "node_modules", ".venv", "venv", "dist", "build",
}

_ALL_PATTERNS = {
    "bare_except",
    "broad_except",
    "silent_except",
    "reraise_as_new",
    "string_exception",
    "overly_nested",
}

# Broad exception base names
_BROAD_BASES: Set[str] = {"Exception", "BaseException"}

_SEVERITY: Dict[str, str] = {
    "bare_except": "high",
    "broad_except": "medium",
    "silent_except": "high",
    "reraise_as_new": "medium",
    "string_exception": "high",
    "overly_nested": "low",
}

_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


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


# ── Finding record ─────────────────────────────────────────────────────────────

class _Finding:
    __slots__ = ("file", "line", "pattern", "severity", "detail")

    def __init__(
        self,
        file: str,
        line: int,
        pattern: str,
        severity: str,
        detail: str,
    ) -> None:
        self.file = file
        self.line = line
        self.pattern = pattern
        self.severity = severity
        self.detail = detail

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "pattern": self.pattern,
            "severity": self.severity,
            "detail": self.detail,
        }


# ── Helper predicates ──────────────────────────────────────────────────────────

def _exception_name(exc_type: ast.expr) -> Optional[str]:
    """Return the simple name of an exception type node, or None."""
    if isinstance(exc_type, ast.Name):
        return exc_type.id
    if isinstance(exc_type, ast.Attribute):
        return exc_type.attr
    return None


def _is_broad(handler: ast.ExceptHandler) -> bool:
    """Return True if handler catches Exception or BaseException (no type = bare)."""
    if handler.type is None:
        return False  # bare_except handled separately
    name = _exception_name(handler.type)
    return name in _BROAD_BASES


def _is_bare(handler: ast.ExceptHandler) -> bool:
    return handler.type is None


def _body_is_silent(body: List[ast.stmt]) -> bool:
    """Return True if the handler body is effectively a no-op (only pass/assignments to _)."""
    non_trivial = [
        stmt for stmt in body
        if not isinstance(stmt, ast.Pass)
        and not _is_dummy_assign(stmt)
    ]
    return len(non_trivial) == 0


def _is_dummy_assign(stmt: ast.stmt) -> bool:
    """Return True for assignments to _ or e.g. `_ = str(e)`."""
    if isinstance(stmt, ast.Assign):
        for target in stmt.targets:
            if isinstance(target, ast.Name) and target.id == "_":
                return True
    return False


def _body_has_reraise(body: List[ast.stmt]) -> bool:
    """Return True if any statement in the body is a bare `raise`."""
    for stmt in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(stmt, ast.Raise) and stmt.exc is None:
            return True
    return False


def _body_has_reraise_with_cause(body: List[ast.stmt]) -> bool:
    """Return True if any raise uses `raise X from ...`."""
    for stmt in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(stmt, ast.Raise) and stmt.cause is not None:
            return True
    return False


def _has_new_raise(body: List[ast.stmt]) -> Optional[int]:
    """Return line of first `raise X(...)` without `from` inside body, or None."""
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Raise):
            if (
                node.exc is not None        # not bare raise
                and node.cause is None      # no `from e`
            ):
                return getattr(node, "lineno", None)
    return None


# ── Per-file scanner ──────────────────────────────────────────────────────────

class _Visitor(ast.NodeVisitor):
    """Walk the AST and collect exception-handling findings."""

    def __init__(
        self,
        rel_path: str,
        enabled_patterns: Set[str],
        max_nest_depth: int,
    ) -> None:
        self.rel_path = rel_path
        self.enabled = enabled_patterns
        self.max_nest_depth = max_nest_depth
        self.findings: List[_Finding] = []
        self._try_depth = 0  # current nesting depth of try blocks

    def _add(self, line: int, pattern: str, detail: str) -> None:
        if pattern in self.enabled:
            self.findings.append(
                _Finding(
                    file=self.rel_path,
                    line=line,
                    pattern=pattern,
                    severity=_SEVERITY[pattern],
                    detail=detail,
                )
            )

    def visit_Try(self, node: ast.Try) -> None:  # noqa: N802
        self._try_depth += 1

        if "overly_nested" in self.enabled and self._try_depth > self.max_nest_depth:
            self._add(
                node.lineno,
                "overly_nested",
                f"try block nested {self._try_depth} levels deep "
                f"(threshold {self.max_nest_depth})",
            )

        for handler in node.handlers:
            self._check_handler(handler)

        self.generic_visit(node)
        self._try_depth -= 1

    # Python 3.11+ ExceptGroup / TryStar — treat same as Try
    def visit_TryStar(self, node: ast.AST) -> None:  # noqa: N802
        self.visit_Try(node)  # type: ignore[arg-type]

    def _check_handler(self, handler: ast.ExceptHandler) -> None:
        line = handler.lineno

        # ── bare except ───────────────────────────────────────────────────────
        if _is_bare(handler):
            self._add(line, "bare_except", "except: (no exception type specified)")

        # ── broad except ──────────────────────────────────────────────────────
        elif _is_broad(handler):
            exc_name = _exception_name(handler.type)  # type: ignore[arg-type]
            if not _body_has_reraise(handler.body):
                self._add(
                    line,
                    "broad_except",
                    f"except {exc_name}: without re-raise — swallows all exceptions",
                )

        # ── silent except ─────────────────────────────────────────────────────
        if _body_is_silent(handler.body):
            detail = (
                "except {}: pass — error silently discarded".format(
                    _get_exc_display(handler)
                )
            )
            self._add(line, "silent_except", detail)

        # ── reraise_as_new (lose original traceback) ──────────────────────────
        if not _is_bare(handler):
            new_raise_line = _has_new_raise(handler.body)
            if (
                new_raise_line is not None
                and not _body_has_reraise_with_cause(handler.body)
            ):
                self._add(
                    new_raise_line,
                    "reraise_as_new",
                    "raise inside except without 'from e' — original traceback lost",
                )

    def visit_Raise(self, node: ast.Raise) -> None:  # noqa: N802
        # ── string_exception ─────────────────────────────────────────────────
        if node.exc is not None and isinstance(node.exc, ast.Constant):
            if isinstance(node.exc.value, str):
                self._add(
                    node.lineno,
                    "string_exception",
                    f"raise {node.exc.value!r} — string exceptions are invalid in Python 3",
                )

        self.generic_visit(node)


def _get_exc_display(handler: ast.ExceptHandler) -> str:
    """Return a display string for the exception type(s) in a handler."""
    if handler.type is None:
        return ""
    if isinstance(handler.type, ast.Name):
        return handler.type.id
    if isinstance(handler.type, ast.Tuple):
        parts = []
        for elt in handler.type.elts:
            name = _exception_name(elt)
            if name:
                parts.append(name)
        return "(" + ", ".join(parts) + ")"
    return "..."


def _scan_file(
    path: Path,
    rel_path: str,
    enabled_patterns: Set[str],
    max_nest_depth: int,
) -> List[_Finding]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    visitor = _Visitor(rel_path, enabled_patterns, max_nest_depth)
    visitor.visit(tree)
    return visitor.findings


# ── Text formatter ─────────────────────────────────────────────────────────────

_SEVERITY_ICON = {"high": "🔴", "medium": "🟡", "low": "🔵"}
_PATTERN_LABEL = {
    "bare_except": "BARE EXCEPT",
    "broad_except": "BROAD EXCEPT",
    "silent_except": "SILENT EXCEPT",
    "reraise_as_new": "RERAISE WITHOUT CAUSE",
    "string_exception": "STRING EXCEPTION",
    "overly_nested": "OVERLY NESTED TRY",
}


def _format_text(
    findings: List[_Finding],
    total_files: int,
    total_findings: int,
    filters: Dict[str, Any],
) -> str:
    lines: List[str] = []

    filter_parts = []
    if filters.get("path"):
        filter_parts.append(f"path={filters['path']}")
    if filters.get("patterns"):
        filter_parts.append(f"patterns={filters['patterns']}")
    if filters.get("min_severity", "low") != "low":
        filter_parts.append(f"min_severity={filters['min_severity']}")
    filter_str = (", " + ", ".join(filter_parts)) if filter_parts else ""

    by_pattern: Dict[str, int] = {}
    for f in findings:
        by_pattern[f.pattern] = by_pattern.get(f.pattern, 0) + 1

    lines.append(
        f"## Exception Audit — {total_files} files scanned{filter_str}"
    )
    lines.append(f"   {total_findings} finding(s) total\n")

    if not findings:
        lines.append("✅ No exception handling anti-patterns found.")
        return "\n".join(lines)

    # Summary by pattern
    for pat in sorted(_ALL_PATTERNS):
        count = by_pattern.get(pat, 0)
        if count == 0:
            continue
        icon = _SEVERITY_ICON[_SEVERITY[pat]]
        lines.append(
            f"   {icon} {_PATTERN_LABEL[pat]:<30}  {count:>4} occurrence(s)"
        )
    lines.append("")

    # Findings grouped by severity (high first), then file
    sorted_findings = sorted(
        findings,
        key=lambda f: (
            -_SEVERITY_ORDER[f.severity],
            f.file,
            f.line,
        ),
    )

    prev_sev = None
    for finding in sorted_findings:
        if finding.severity != prev_sev:
            label = finding.severity.upper()
            icon = _SEVERITY_ICON[finding.severity]
            lines.append(f"### {icon} {label}")
            prev_sev = finding.severity

        lines.append(
            f"   {finding.file}:{finding.line}  "
            f"[{_PATTERN_LABEL.get(finding.pattern, finding.pattern)}]  "
            f"{finding.detail}"
        )

    return "\n".join(lines)


# ── Tool entry point ───────────────────────────────────────────────────────────

def _exception_audit(
    ctx: ToolContext,
    path: Optional[str] = None,
    patterns: Optional[str] = None,
    min_severity: str = "low",
    format: str = "text",
    max_nest_depth: int = 3,
) -> str:
    """Scan for exception handling anti-patterns in the Python codebase."""
    if min_severity not in _SEVERITY_ORDER:
        return (
            f"Unknown min_severity: {min_severity!r}. "
            f"Valid: low, medium, high"
        )

    # Resolve enabled patterns
    if patterns:
        requested = {p.strip() for p in patterns.split(",")}
        unknown = requested - _ALL_PATTERNS
        if unknown:
            return (
                f"Unknown patterns: {', '.join(sorted(unknown))}. "
                f"Valid: {', '.join(sorted(_ALL_PATTERNS))}"
            )
        enabled_patterns = requested
    else:
        enabled_patterns = set(_ALL_PATTERNS)

    repo_root = Path(ctx.repo_dir if ctx and ctx.repo_dir else _REPO_DIR)
    py_files = _collect_py_files(repo_root, path)

    all_findings: List[_Finding] = []
    for fpath in py_files:
        try:
            rel = str(fpath.relative_to(repo_root))
        except ValueError:
            rel = str(fpath)
        all_findings.extend(
            _scan_file(fpath, rel, enabled_patterns, max_nest_depth)
        )

    # Filter by severity
    min_sev_order = _SEVERITY_ORDER[min_severity]
    filtered = [
        f for f in all_findings
        if _SEVERITY_ORDER[f.severity] >= min_sev_order
    ]

    filters: Dict[str, Any] = {
        "path": path,
        "patterns": patterns,
        "min_severity": min_severity,
        "max_nest_depth": max_nest_depth,
    }

    if format == "json":
        return json.dumps(
            {
                "total_files": len(py_files),
                "total_findings": len(filtered),
                "filters": filters,
                "findings": [f.to_dict() for f in filtered],
            },
            ensure_ascii=False,
            indent=2,
        )

    return _format_text(filtered, len(py_files), len(filtered), filters)


# ── Tool registration ──────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="exception_audit",
            schema={
                "name": "exception_audit",
                "description": (
                    "Scan Python codebase for exception handling anti-patterns: "
                    "bare_except, broad_except (swallows all exceptions), "
                    "silent_except (pass with no log/reraise), "
                    "reraise_as_new (missing 'from e', traceback lost), "
                    "string_exception (Python 2 remnant), "
                    "overly_nested (try-in-try-in-try). "
                    "Reports file, line, severity (high/medium/low), and detail."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Limit scan to a subdirectory or file (relative to repo root)",
                        },
                        "patterns": {
                            "type": "string",
                            "description": (
                                "Comma-separated pattern names to include. "
                                "Valid: bare_except, broad_except, silent_except, "
                                "reraise_as_new, string_exception, overly_nested. "
                                "Default: all patterns."
                            ),
                        },
                        "min_severity": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                            "description": "Minimum severity to report (default: low = show all)",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format (default: text)",
                        },
                        "max_nest_depth": {
                            "type": "integer",
                            "description": "Nesting depth threshold for overly_nested pattern (default 3)",
                        },
                    },
                    "required": [],
                },
            },
            handler=_exception_audit,
        )
    ]
