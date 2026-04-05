"""test_coverage_map — per-function test coverage heuristics via AST.

No pytest-cov or instrumented run required. Analyses source and test files
using AST to determine whether each public function/method has test coverage.

Three coverage signals (checked in order):

  1. **Direct name match** — ``test_foo`` or ``test_ClassName_foo`` exists
     in any test file that can logically cover this module.
  2. **Call reference** — the function name is called (``Name`` node, Load context)
     or stored in a variable inside a test function body.
  3. **Import reference** — the function is explicitly imported inside a test
     file (``from module import foo``).

Coverage status per function:
  covered   — at least one signal found
  uncovered — no signal found
  skipped   — private (starts with ``_``) and ``include_private=False``

Module-level summary:
  - total public functions, covered count, coverage %
  - top uncovered functions (highest priority to add tests for)

Typical uses:
    test_coverage_map(target="ouroboros/tools/dep_cycles.py")
    test_coverage_map(target="ouroboros/tools/", status="uncovered")
    test_coverage_map(target="ouroboros/loop_runtime.py", include_private=True)
    test_coverage_map(target="ouroboros/tools/change_impact.py", format="json")
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
from typing import Any, Dict, List, Optional, Set, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

_REPO_DIR = pathlib.Path(os.environ.get("OUROBOROS_REPO_DIR", "/opt/veles"))

_SKIP_DIRS = {
    "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
    "node_modules", ".venv", "venv", "dist", "build",
}


# ── File helpers ──────────────────────────────────────────────────────────────

def _collect_py_files(root: pathlib.Path) -> List[pathlib.Path]:
    """Recursively collect .py files, skipping noise dirs."""
    if root.is_file() and root.suffix == ".py":
        return [root]
    files: List[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        for fname in sorted(filenames):
            if fname.endswith(".py"):
                files.append(pathlib.Path(dirpath) / fname)
    return files


def _resolve_target(target: str, repo_dir: pathlib.Path) -> Optional[pathlib.Path]:
    """Return absolute path for target string (file or directory)."""
    # Direct absolute
    abs_path = pathlib.Path(target)
    if abs_path.is_absolute() and (abs_path.exists()):
        return abs_path

    # Relative to repo
    candidate = repo_dir / target
    if candidate.exists():
        return candidate

    # Try inside ouroboros/
    candidate2 = repo_dir / "ouroboros" / target
    if candidate2.exists():
        return candidate2

    return None


def _rel(path: pathlib.Path, repo_dir: pathlib.Path) -> str:
    try:
        return str(path.relative_to(repo_dir))
    except ValueError:
        return str(path)


# ── AST analysis: source functions ───────────────────────────────────────────

class FunctionInfo:
    __slots__ = ("name", "line", "is_method", "class_name", "is_private")

    def __init__(
        self,
        name: str,
        line: int,
        is_method: bool = False,
        class_name: str = "",
    ) -> None:
        self.name = name
        self.line = line
        self.is_method = is_method
        self.class_name = class_name
        self.is_private = name.startswith("_")


def _extract_functions(path: pathlib.Path) -> List[FunctionInfo]:
    """Extract all top-level functions and class methods from a .py file."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=str(path))
    except (SyntaxError, OSError):
        return []

    results: List[FunctionInfo] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            results.append(FunctionInfo(
                name=node.name,
                line=node.lineno,
                is_method=False,
                class_name="",
            ))
        elif isinstance(node, ast.ClassDef):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    results.append(FunctionInfo(
                        name=child.name,
                        line=child.lineno,
                        is_method=True,
                        class_name=node.name,
                    ))

    return results


# ── AST analysis: test file signals ──────────────────────────────────────────

class TestFileInfo:
    """Pre-parsed test file: name-match candidates, call names, import names."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.test_func_names: Set[str] = set()   # names of test_* functions
        self.call_names: Set[str] = set()         # names called inside any test body
        self.import_names: Set[str] = set()       # names explicitly imported

        self._parse()

    def _parse(self) -> None:
        try:
            src = self.path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src, filename=str(self.path))
        except (SyntaxError, OSError):
            return

        # Top-level test function names
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test"):
                    self.test_func_names.add(node.name)
                    # Collect call names inside the test body
                    for child in ast.walk(node):
                        if isinstance(child, ast.Call):
                            self._collect_call_name(child)

            elif isinstance(node, ast.ClassDef):
                for method in ast.iter_child_nodes(node):
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if method.name.startswith("test"):
                            self.test_func_names.add(method.name)
                            for child in ast.walk(method):
                                if isinstance(child, ast.Call):
                                    self._collect_call_name(child)

        # Import names at module level
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        self.import_names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.import_names.add(alias.asname or alias.name.split(".")[0])

    def _collect_call_name(self, call: ast.Call) -> None:
        func = call.func
        if isinstance(func, ast.Name):
            self.call_names.add(func.id)
        elif isinstance(func, ast.Attribute):
            self.call_names.add(func.attr)


# ── Coverage signals ──────────────────────────────────────────────────────────

def _find_test_files(source_path: pathlib.Path, repo_dir: pathlib.Path) -> List[TestFileInfo]:
    """Find test files that are likely to test the given source file."""
    stem = source_path.stem  # e.g. "dep_cycles"
    tests_dir = repo_dir / "tests"
    if not tests_dir.exists():
        return []

    candidates: List[pathlib.Path] = []
    for f in sorted(tests_dir.glob("test_*.py")):
        # Primary match: test_<stem>.py
        if f.stem == f"test_{stem}":
            candidates.insert(0, f)  # highest priority
        elif stem in f.stem:
            candidates.append(f)

    return [TestFileInfo(p) for p in candidates]


def _check_coverage_signals(
    func: FunctionInfo,
    test_files: List[TestFileInfo],
) -> Tuple[bool, str]:
    """
    Return (is_covered, signal_description).
    Signal: "name_match", "call_ref", "import_ref", or "".
    """
    name = func.name
    class_name = func.class_name

    for tf in test_files:
        # Signal 1: direct test function name match
        # Patterns: test_foo, test_ClassName_foo, test_foo_something
        test_candidates = {
            f"test_{name}",
            f"test_{class_name}_{name}" if class_name else "",
            f"test_{name.lstrip('_')}",
        }
        test_candidates.discard("")

        for tname in tf.test_func_names:
            # Exact or prefix match
            if tname in test_candidates:
                return True, "name_match"
            # tname starts with test_name (e.g. test_foo_edge_case)
            if tname.startswith(f"test_{name}") or tname.startswith(f"test_{name.lstrip('_')}"):
                return True, "name_match"

        # Signal 2: function called inside a test body
        if name in tf.call_names:
            return True, "call_ref"

        # Signal 3: function explicitly imported
        if name in tf.import_names:
            return True, "import_ref"

    return False, ""


# ── Per-file analysis ─────────────────────────────────────────────────────────

class FunctionCoverage:
    __slots__ = ("name", "line", "class_name", "is_method",
                 "is_private", "status", "signal")

    def __init__(
        self,
        func: FunctionInfo,
        status: str,
        signal: str,
    ) -> None:
        self.name = func.name
        self.line = func.line
        self.class_name = func.class_name
        self.is_method = func.is_method
        self.is_private = func.is_private
        self.status = status   # "covered", "uncovered", "skipped"
        self.signal = signal   # "name_match", "call_ref", "import_ref", ""


def _analyse_file(
    source_path: pathlib.Path,
    repo_dir: pathlib.Path,
    include_private: bool,
    status_filter: Optional[str],
) -> Tuple[List[FunctionCoverage], List[TestFileInfo]]:
    """Analyse one file. Returns (coverage_list, test_files_used)."""
    functions = _extract_functions(source_path)
    test_files = _find_test_files(source_path, repo_dir)

    results: List[FunctionCoverage] = []
    for func in functions:
        # Skip dunder methods — they're framework hooks, not directly testable
        if func.name.startswith("__") and func.name.endswith("__"):
            continue

        if func.is_private and not include_private:
            cov = FunctionCoverage(func, status="skipped", signal="")
        else:
            covered, signal = _check_coverage_signals(func, test_files)
            cov = FunctionCoverage(
                func,
                status="covered" if covered else "uncovered",
                signal=signal,
            )
        results.append(cov)

    # Apply status filter
    if status_filter and status_filter != "all":
        results = [r for r in results if r.status == status_filter]

    return results, test_files


def _compute_stats(coverage: List[FunctionCoverage]) -> Dict[str, Any]:
    public = [c for c in coverage if c.status != "skipped"]
    covered = [c for c in public if c.status == "covered"]
    uncovered = [c for c in public if c.status == "uncovered"]
    pct = round(100 * len(covered) / len(public), 1) if public else 0.0
    return {
        "total_public": len(public),
        "covered": len(covered),
        "uncovered": len(uncovered),
        "coverage_pct": pct,
        "top_uncovered": [
            {"name": c.name, "line": c.line, "class": c.class_name}
            for c in uncovered[:5]
        ],
    }


# ── Formatters ────────────────────────────────────────────────────────────────

def _format_file_text(
    rel_path: str,
    coverage: List[FunctionCoverage],
    stats: Dict[str, Any],
    test_file_paths: List[str],
    include_private: bool,
) -> str:
    lines: List[str] = []
    pct = stats["coverage_pct"]
    grade = "A" if pct >= 90 else "B" if pct >= 75 else "C" if pct >= 60 else "D" if pct >= 40 else "F"

    lines.append(f"## Test Coverage Map — {rel_path}")
    lines.append(
        f"   Public functions: {stats['total_public']}  "
        f"Covered: {stats['covered']}  "
        f"Uncovered: {stats['uncovered']}  "
        f"Coverage: {pct}% ({grade})"
    )
    if test_file_paths:
        lines.append(f"   Test files: {', '.join(test_file_paths)}")
    else:
        lines.append("   ⚠️  No test files found for this module")
    lines.append("")

    if not coverage:
        lines.append("   (no functions match the filter)")
        return "\n".join(lines)

    # Group by class
    by_class: Dict[str, List[FunctionCoverage]] = {}
    for cov in coverage:
        key = cov.class_name or "<module>"
        by_class.setdefault(key, []).append(cov)

    signal_icons = {
        "name_match": "🟢",
        "call_ref":   "🟡",
        "import_ref": "🟡",
        "":           "🔴",
    }
    status_icons = {
        "covered":   "✅",
        "uncovered": "❌",
        "skipped":   "⏭️ ",
    }

    for group_name, items in by_class.items():
        if group_name != "<module>":
            lines.append(f"  class {group_name}:")
        for cov in items:
            icon = status_icons[cov.status]
            sig = signal_icons.get(cov.signal, "")
            prefix = "    " if group_name != "<module>" else "  "
            sig_label = f"  [{cov.signal}]" if cov.signal else ""
            private_tag = " (private)" if cov.is_private and include_private else ""
            lines.append(f"{prefix}{icon} {sig} {cov.name}  (line {cov.line}){sig_label}{private_tag}")
        lines.append("")

    if stats["top_uncovered"]:
        lines.append("### Priority: Add tests for")
        for item in stats["top_uncovered"]:
            cls = f"{item['class']}." if item["class"] else ""
            lines.append(f"  • {cls}{item['name']}  (line {item['line']})")

    return "\n".join(lines)


def _format_dir_text(
    file_reports: List[Dict[str, Any]],
    total_public: int,
    total_covered: int,
    status_filter: Optional[str],
) -> str:
    lines: List[str] = []
    pct = round(100 * total_covered / total_public, 1) if total_public else 0.0
    grade = "A" if pct >= 90 else "B" if pct >= 75 else "C" if pct >= 60 else "D" if pct >= 40 else "F"

    lines.append(f"## Test Coverage Map — directory scan")
    lines.append(
        f"   {len(file_reports)} files  "
        f"Public functions: {total_public}  "
        f"Covered: {total_covered}  "
        f"Coverage: {pct}% ({grade})"
    )
    if status_filter and status_filter != "all":
        lines.append(f"   Filter: status={status_filter}")
    lines.append("")

    for report in file_reports:
        s = report["stats"]
        file_pct = s["coverage_pct"]
        file_grade = "A" if file_pct >= 90 else "B" if file_pct >= 75 else "C" if file_pct >= 60 else "D" if file_pct >= 40 else "F"
        uncov = s["uncovered"]
        has_tests = report["has_tests"]
        tests_tag = "" if has_tests else " ⚠️ no tests"

        lines.append(
            f"  {file_grade}  {file_pct:5.1f}%  {report['rel_path']}{tests_tag}"
        )
        if uncov > 0 and s["top_uncovered"]:
            top = s["top_uncovered"][:3]
            items_str = ", ".join(
                (f"{t['class']}.{t['name']}" if t["class"] else t["name"])
                for t in top
            )
            lines.append(f"           uncovered: {items_str}")

    return "\n".join(lines)


# ── Main handler ──────────────────────────────────────────────────────────────

def _test_coverage_map(
    _ctx: Optional[ToolContext],
    *,
    target: str = "ouroboros/",
    status: str = "all",
    include_private: bool = False,
    format: str = "text",
) -> str:
    repo_dir = _REPO_DIR
    resolved = _resolve_target(target, repo_dir)
    if resolved is None:
        return f"❌ Cannot resolve target: {target!r}"

    status_filter = status if status in ("covered", "uncovered", "skipped") else None

    if resolved.is_file():
        # Single file mode
        coverage, test_files = _analyse_file(
            resolved, repo_dir, include_private, status_filter
        )
        stats = _compute_stats(coverage)
        rel_path = _rel(resolved, repo_dir)
        test_paths = [_rel(tf.path, repo_dir) for tf in test_files]

        if format == "json":
            return json.dumps(
                {
                    "target": rel_path,
                    "test_files": test_paths,
                    "stats": stats,
                    "functions": [
                        {
                            "name": c.name,
                            "line": c.line,
                            "class": c.class_name,
                            "is_method": c.is_method,
                            "is_private": c.is_private,
                            "status": c.status,
                            "signal": c.signal,
                        }
                        for c in coverage
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        return _format_file_text(rel_path, coverage, stats, test_paths, include_private)

    # Directory mode
    py_files = _collect_py_files(resolved)
    # Only source files (not test files themselves)
    src_files = [f for f in py_files if not f.name.startswith("test_")]

    file_reports: List[Dict[str, Any]] = []
    total_public = 0
    total_covered = 0

    for src in sorted(src_files):
        coverage, test_files = _analyse_file(
            src, repo_dir, include_private, status_filter
        )
        stats = _compute_stats(coverage)
        total_public += stats["total_public"]
        total_covered += stats["covered"]
        # Only include in report if there's something to show
        if stats["total_public"] > 0:
            file_reports.append(
                {
                    "rel_path": _rel(src, repo_dir),
                    "stats": stats,
                    "has_tests": bool(test_files),
                    "coverage": coverage,
                    "test_files": [_rel(tf.path, repo_dir) for tf in test_files],
                }
            )

    # Sort by coverage ascending (worst first)
    file_reports.sort(key=lambda r: r["stats"]["coverage_pct"])

    if format == "json":
        return json.dumps(
            {
                "target": _rel(resolved, repo_dir),
                "total_files": len(file_reports),
                "total_public": total_public,
                "total_covered": total_covered,
                "coverage_pct": round(100 * total_covered / total_public, 1) if total_public else 0.0,
                "files": [
                    {
                        "path": r["rel_path"],
                        "stats": r["stats"],
                        "has_tests": r["has_tests"],
                    }
                    for r in file_reports
                ],
            },
            ensure_ascii=False,
            indent=2,
        )

    return _format_dir_text(file_reports, total_public, total_covered, status_filter)


# ── Tool registration ─────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="test_coverage_map",
            schema={
                "name": "test_coverage_map",
                "description": (
                    "Per-function test coverage heuristics via AST — no pytest-cov or "
                    "instrumented run required. Analyses which public functions/methods "
                    "in a source file have corresponding tests using three signals: "
                    "(1) test_foo function exists in test files, "
                    "(2) function is called inside a test body, "
                    "(3) function is explicitly imported by a test. "
                    "Reports coverage status per function (covered/uncovered), "
                    "the signal that established coverage, and module-level stats "
                    "(coverage %, grade A–F, top uncovered functions). "
                    "Works on single files or full directories. "
                    "Use before adding tests to find the highest-priority gaps, "
                    "or after refactors to check coverage drift."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {
                            "type": "string",
                            "description": (
                                "File or directory to analyse "
                                "(e.g. 'ouroboros/tools/dep_cycles.py' or "
                                "'ouroboros/tools/'). Default: 'ouroboros/'."
                            ),
                        },
                        "status": {
                            "type": "string",
                            "enum": ["all", "covered", "uncovered"],
                            "description": (
                                "Filter output by coverage status. "
                                "Default: 'all' (show everything)."
                            ),
                        },
                        "include_private": {
                            "type": "boolean",
                            "description": (
                                "Include private functions (starting with _). "
                                "Default: false."
                            ),
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format. Default: text.",
                        },
                    },
                    "required": [],
                },
            },
            handler=_test_coverage_map,
        )
    ]
