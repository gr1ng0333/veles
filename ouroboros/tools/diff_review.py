"""diff_review — automated code review for git diffs.

Runs security, exception-handling, type annotation, and docstring checks
focused on functions and classes that appear in a git diff.

Unlike full-codebase scanners, diff_review is **focused**: findings are
reported only for code that changed, making it ideal as a pre-commit gate.

Input
-----
ref          — git ref or diff spec: "HEAD" (uncommitted changes),
               "HEAD~1" (last commit), "main..veles", "abc123..def456"
               (default: "HEAD")
path         — limit review to a subdirectory or file (relative to repo root)
checks       — comma-separated list of check groups:
               security, exceptions, types, docs, complexity
               (default: all five groups)
min_severity — "low" | "medium" | "high"  (default: low = show all)
format       — "text" | "json"             (default: text)

Check groups
------------
security     — eval/exec on dynamic input, subprocess(shell=True),
               hardcoded secrets, pickle deserialization         [high]
exceptions   — bare_except, broad_except without re-raise,
               silent_except (pass only)                         [high/medium]
types        — missing parameter / return type annotations on
               new or modified public functions                  [low]
docs         — missing docstrings on new public functions
               and classes                                       [low]
complexity   — cyclomatic complexity > 10 in changed functions   [medium]

Examples
--------
    diff_review()                         # uncommitted changes, all checks
    diff_review(ref="HEAD~1")             # last commit
    diff_review(ref="main..veles")        # branch diff
    diff_review(checks="security,exceptions")
    diff_review(min_severity="medium")
    diff_review(format="json")
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

# ── Constants ──────────────────────────────────────────────────────────────────

_REPO_DIR = Path(os.environ.get("REPO_DIR", "/opt/veles"))

_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}
_SEVERITY_ICON = {"high": "🔴", "medium": "🟡", "low": "🔵"}

_ALL_CHECKS = {"security", "exceptions", "types", "docs", "complexity"}

# Cyclomatic complexity threshold: report when CC > this value
_COMPLEXITY_THRESHOLD = 10

# Broad exception bases
_BROAD_BASES = {"Exception", "BaseException"}

# ── Finding dataclass ──────────────────────────────────────────────────────────


@dataclass
class _Finding:
    file: str
    line: int
    check: str          # check group name
    severity: str
    message: str
    function: str = ""  # enclosing function/class name, if known

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "function": self.function,
        }


# ── Diff parsing ───────────────────────────────────────────────────────────────

def _run_git_diff(ref: str, path: Optional[str], repo_dir: Path) -> str:
    """Return raw unified diff output for the given ref."""
    cmd: List[str]
    if ".." in ref or ref == "HEAD":
        if ref == "HEAD":
            # Uncommitted changes (staged + unstaged)
            cmd = ["git", "diff", "HEAD"]
            # If nothing, try staged only
        cmd = ["git", "diff", ref]
    else:
        # Single commit: diff against its parent
        cmd = ["git", "diff", f"{ref}^", ref]

    if path:
        cmd += ["--", path]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        output = ""

    # If diff is empty (e.g., ref = HEAD with no uncommitted changes), try HEAD~1..HEAD
    if not output.strip() and ref == "HEAD":
        try:
            cmd2 = ["git", "diff", "HEAD~1..HEAD"]
            if path:
                cmd2 += ["--", path]
            result2 = subprocess.run(
                cmd2,
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result2.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    return output


_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def _parse_changed_lines(diff_text: str) -> Dict[str, Set[int]]:
    """Parse unified diff and return {rel_path: set_of_added_line_numbers}."""
    result: Dict[str, Set[int]] = {}
    if not diff_text.strip():
        return result

    # Split into per-file sections
    sections = re.split(r"^diff --git ", diff_text, flags=re.MULTILINE)
    for section in sections:
        if not section.strip():
            continue

        # Find +++ b/file line
        file_match = _DIFF_FILE_RE.search(section)
        if not file_match:
            continue
        rel_path = file_match.group(1)
        if not rel_path.endswith(".py"):
            continue

        added_lines: Set[int] = set()

        # Find all hunks and parse added lines
        hunk_parts = re.split(r"^@@ ", section, flags=re.MULTILINE)
        for hunk_part in hunk_parts[1:]:  # skip header
            # Parse the hunk header
            hunk_header_match = re.match(r"-\d+(?:,\d+)? \+(\d+)(?:,(\d+))?", hunk_part)
            if not hunk_header_match:
                continue
            start_line = int(hunk_header_match.group(1))
            current_line = start_line

            # Walk through hunk lines
            rest = hunk_part[hunk_header_match.end():]
            for raw_line in rest.splitlines():
                if raw_line.startswith("+"):
                    added_lines.add(current_line)
                    current_line += 1
                elif raw_line.startswith("-"):
                    pass  # removed line, don't advance target counter
                else:
                    current_line += 1

        if added_lines:
            result.setdefault(rel_path, set()).update(added_lines)

    return result


# ── AST helpers ────────────────────────────────────────────────────────────────

def _get_func_line_range(node: ast.FunctionDef | ast.AsyncFunctionDef) -> Tuple[int, int]:
    """Return (start_line, end_line) for a function node."""
    start = node.lineno
    end = getattr(node, "end_lineno", node.lineno)
    return start, end


def _overlaps(func_start: int, func_end: int, changed_lines: Set[int]) -> bool:
    return any(func_start <= ln <= func_end for ln in changed_lines)


def _cyclomatic_complexity(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Compute approximated cyclomatic complexity for a function node."""
    cc = 1  # base
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.While, ast.For, ast.ExceptHandler,
                               ast.With, ast.AsyncWith, ast.AsyncFor)):
            cc += 1
        elif isinstance(child, ast.BoolOp):
            cc += len(child.values) - 1
        elif isinstance(child, ast.comprehension):
            if child.ifs:
                cc += len(child.ifs)
    return cc


def _name_of(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name_of(node.value)}.{node.attr}"
    return ""


def _is_format_call(node: ast.expr) -> bool:
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return True
    if (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"):
        return True
    return False


def _is_public(name: str) -> bool:
    return not name.startswith("_")


# ── Hardcoded-secret regex (subset of security_scan) ──────────────────────────

_SECRET_RE = re.compile(
    r"""
    (?:^|[\s,=(])
    (?P<key>\w*(?:password|passwd|secret|api_?key|apikey|token|access_key|private_key|auth_token|client_secret)\w*)
    \s*=\s*
    (?P<q>['"])(?P<val>[^'"]{4,})(?P=q)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_SECRET_FP_RE = re.compile(
    r"os\.environ|getenv|environ\.get|<[A-Z_]+>|\{[A-Z_]+\}|"
    r"your[_-]?|example|placeholder|changeme|xxxxxxxx|\*+|none|false|true",
    re.IGNORECASE,
)


# ── Per-function checkers ──────────────────────────────────────────────────────

def _check_security(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    rel_path: str,
    source_lines: List[str],
    enabled: Set[str],
) -> List[_Finding]:
    if "security" not in enabled:
        return []
    findings: List[_Finding] = []
    func_name = func.name

    def add(line: int, msg: str) -> None:
        findings.append(_Finding(
            file=rel_path, line=line, check="security",
            severity="high", message=msg, function=func_name,
        ))

    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            fname = _name_of(node.func)

            # eval/exec on dynamic input
            if fname in ("eval", "exec", "compile"):
                if node.args and not isinstance(node.args[0], ast.Constant):
                    add(node.lineno, f"Dynamic {fname}() call — potential code injection")

            # subprocess(shell=True)
            if fname in (
                "subprocess.run", "subprocess.call",
                "subprocess.check_call", "subprocess.check_output",
                "subprocess.Popen",
            ):
                for kw in node.keywords:
                    if (kw.arg == "shell"
                            and isinstance(kw.value, ast.Constant)
                            and kw.value.value):
                        add(node.lineno, f"{fname}(shell=True) — use list args")

            # pickle deserialization
            if fname in ("pickle.loads", "pickle.load", "pickle.Unpickler",
                          "marshal.loads", "marshal.load"):
                add(node.lineno, f"Unsafe deserialization: {fname}()")

            # SQL injection
            if (isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("execute", "executemany")
                    and node.args and _is_format_call(node.args[0])):
                add(node.lineno, "SQL query built with string formatting — use params")

    # Hardcoded secrets (line-level regex on function body)
    start_line, end_line = _get_func_line_range(func)
    for lineno in range(start_line, end_line + 1):
        if lineno < 1 or lineno > len(source_lines):
            continue
        line = source_lines[lineno - 1]
        for m in _SECRET_RE.finditer(line):
            val = m.group("val")
            key = m.group("key")
            if not _SECRET_FP_RE.search(val) and not _SECRET_FP_RE.search(key):
                add(lineno, f"Potential hardcoded secret: '{key}' — use env vars")

    return findings


def _check_exceptions(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    rel_path: str,
    enabled: Set[str],
) -> List[_Finding]:
    if "exceptions" not in enabled:
        return []
    findings: List[_Finding] = []
    func_name = func.name

    def add(line: int, severity: str, msg: str) -> None:
        findings.append(_Finding(
            file=rel_path, line=line, check="exceptions",
            severity=severity, message=msg, function=func_name,
        ))

    for node in ast.walk(func):
        if not isinstance(node, (ast.Try,)):
            continue
        for handler in node.handlers:
            # bare except
            if handler.type is None:
                add(handler.lineno, "high", "bare except: — catches SystemExit/KeyboardInterrupt")

            # broad except without re-raise
            elif isinstance(handler.type, ast.Name) and handler.type.id in _BROAD_BASES:
                has_reraise = any(
                    isinstance(s, ast.Raise) and s.exc is None
                    for s in ast.walk(ast.Module(body=handler.body, type_ignores=[]))
                )
                if not has_reraise:
                    add(handler.lineno, "medium",
                        f"except {handler.type.id}: without re-raise — swallows all exceptions")

            # silent except (body is only pass / _ = ...)
            non_trivial = [
                s for s in handler.body
                if not isinstance(s, ast.Pass)
                and not (isinstance(s, ast.Assign)
                         and all(isinstance(t, ast.Name) and t.id == "_"
                                 for t in s.targets))
            ]
            if not non_trivial:
                exc_label = handler.type.id if isinstance(handler.type, ast.Name) else "..."
                add(handler.lineno, "high",
                    f"except {exc_label}: pass — error silently discarded")

    return findings


def _check_types(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    rel_path: str,
    enabled: Set[str],
) -> List[_Finding]:
    if "types" not in enabled:
        return []
    if not _is_public(func.name):
        return []
    findings: List[_Finding] = []
    func_name = func.name

    # Missing parameter annotations
    for arg in func.args.args + func.args.posonlyargs + func.args.kwonlyargs:
        if arg.annotation is None and arg.arg not in ("self", "cls"):
            findings.append(_Finding(
                file=rel_path, line=func.lineno, check="types",
                severity="low",
                message=f"Missing annotation for parameter '{arg.arg}'",
                function=func_name,
            ))

    # Missing return annotation
    if func.returns is None:
        findings.append(_Finding(
            file=rel_path, line=func.lineno, check="types",
            severity="low",
            message="Missing return type annotation",
            function=func_name,
        ))

    return findings


def _check_docs(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    rel_path: str,
    enabled: Set[str],
) -> List[_Finding]:
    if "docs" not in enabled:
        return []
    if not _is_public(node.name):
        return []

    has_doc = (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    )
    if has_doc:
        return []

    kind = "class" if isinstance(node, ast.ClassDef) else "function"
    return [_Finding(
        file=rel_path, line=node.lineno, check="docs",
        severity="low",
        message=f"Public {kind} '{node.name}' has no docstring",
        function=node.name,
    )]


def _check_complexity(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    rel_path: str,
    enabled: Set[str],
) -> List[_Finding]:
    if "complexity" not in enabled:
        return []
    cc = _cyclomatic_complexity(func)
    if cc <= _COMPLEXITY_THRESHOLD:
        return []
    return [_Finding(
        file=rel_path, line=func.lineno, check="complexity",
        severity="medium",
        message=f"Cyclomatic complexity {cc} > {_COMPLEXITY_THRESHOLD} — consider splitting",
        function=func.name,
    )]


# ── Per-file review ────────────────────────────────────────────────────────────

def _review_file(
    fpath: Path,
    rel_path: str,
    changed_lines: Set[int],
    enabled: Set[str],
    min_level: int,
) -> Tuple[List[_Finding], int]:
    """Review a single changed file. Returns (findings, num_functions_reviewed)."""
    try:
        source = fpath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], 0

    try:
        tree = ast.parse(source, filename=str(fpath))
    except SyntaxError:
        return [], 0

    source_lines = source.splitlines()
    findings: List[_Finding] = []
    funcs_reviewed = 0

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start, end = _get_func_line_range(node)
            if not _overlaps(start, end, changed_lines):
                continue
            funcs_reviewed += 1
            findings.extend(_check_security(node, rel_path, source_lines, enabled))
            findings.extend(_check_exceptions(node, rel_path, enabled))
            findings.extend(_check_types(node, rel_path, enabled))
            findings.extend(_check_docs(node, rel_path, enabled))
            findings.extend(_check_complexity(node, rel_path, enabled))

        elif isinstance(node, ast.ClassDef):
            # Check docs for changed classes (class body overlaps with changed lines)
            start = node.lineno
            end = getattr(node, "end_lineno", node.lineno)
            if _overlaps(start, end, changed_lines):
                findings.extend(_check_docs(node, rel_path, enabled))

    # Filter by severity
    findings = [f for f in findings if _SEVERITY_ORDER[f.severity] >= min_level]
    return findings, funcs_reviewed


# ── Formatting ─────────────────────────────────────────────────────────────────

_CHECK_LABEL = {
    "security": "SECURITY",
    "exceptions": "EXCEPTION HANDLING",
    "types": "TYPE ANNOTATIONS",
    "docs": "DOCUMENTATION",
    "complexity": "COMPLEXITY",
}


def _format_text(
    findings: List[_Finding],
    files_changed: int,
    funcs_reviewed: int,
    ref: str,
    filters: Dict[str, Any],
) -> str:
    lines: List[str] = []

    filter_parts: List[str] = []
    if filters.get("path"):
        filter_parts.append(f"path={filters['path']}")
    if filters.get("checks"):
        filter_parts.append(f"checks={filters['checks']}")
    if filters.get("min_severity", "low") != "low":
        filter_parts.append(f"min_severity={filters['min_severity']}")
    filter_str = (", " + ", ".join(filter_parts)) if filter_parts else ""

    lines.append(f"## Diff Review — ref={ref!r}{filter_str}")
    lines.append(
        f"   {files_changed} Python file(s) changed · "
        f"{funcs_reviewed} function(s) reviewed · "
        f"{len(findings)} finding(s)\n"
    )

    if not findings:
        lines.append("✅ No issues found in the changed code.")
        return "\n".join(lines)

    # Summary by check group
    by_check: Dict[str, int] = {}
    for f in findings:
        by_check[f.check] = by_check.get(f.check, 0) + 1

    lines.append("### Summary")
    for check in ("security", "exceptions", "complexity", "types", "docs"):
        count = by_check.get(check, 0)
        if count == 0:
            continue
        # Use the highest severity for this check group
        sev_counts: Dict[str, int] = {}
        for f in findings:
            if f.check == check:
                sev_counts[f.severity] = sev_counts.get(f.severity, 0) + 1
        top_sev = max(sev_counts, key=lambda s: _SEVERITY_ORDER[s])
        icon = _SEVERITY_ICON[top_sev]
        lines.append(f"   {icon} {_CHECK_LABEL.get(check, check):<25}  {count:>4} finding(s)")
    lines.append("")

    # Findings sorted by severity desc, then file, then line
    sorted_findings = sorted(
        findings,
        key=lambda f: (-_SEVERITY_ORDER[f.severity], f.file, f.line),
    )

    prev_sev = None
    for finding in sorted_findings:
        if finding.severity != prev_sev:
            icon = _SEVERITY_ICON[finding.severity]
            lines.append(f"### {icon} {finding.severity.upper()}")
            prev_sev = finding.severity

        func_part = f" [{finding.function}]" if finding.function else ""
        lines.append(
            f"   {finding.file}:{finding.line}{func_part}"
            f"  ({_CHECK_LABEL.get(finding.check, finding.check)})"
            f"  {finding.message}"
        )

    return "\n".join(lines)


# ── Tool entry point ───────────────────────────────────────────────────────────

def _diff_review(
    ctx: ToolContext,
    ref: str = "HEAD",
    path: Optional[str] = None,
    checks: Optional[str] = None,
    min_severity: str = "low",
    format: str = "text",
) -> str:
    """Run automated code review on the changed functions in a git diff."""
    if min_severity not in _SEVERITY_ORDER:
        return f"Unknown min_severity: {min_severity!r}. Valid: low, medium, high"

    min_level = _SEVERITY_ORDER[min_severity]

    if checks:
        requested = {c.strip() for c in checks.split(",")}
        unknown = requested - _ALL_CHECKS
        if unknown:
            return (
                f"Unknown checks: {', '.join(sorted(unknown))}. "
                f"Valid: {', '.join(sorted(_ALL_CHECKS))}"
            )
        enabled = requested
    else:
        enabled = set(_ALL_CHECKS)

    repo_root = Path(ctx.repo_dir if ctx and ctx.repo_dir else _REPO_DIR)

    # 1. Get diff
    diff_text = _run_git_diff(ref, path, repo_root)
    if not diff_text.strip():
        return (
            f"No diff found for ref={ref!r}"
            + (f", path={path!r}" if path else "")
            + ". Nothing to review."
        )

    # 2. Parse changed lines per file
    changed_map = _parse_changed_lines(diff_text)
    if not changed_map:
        return f"Diff found but no Python files changed (ref={ref!r})."

    # 3. Review each changed file
    all_findings: List[_Finding] = []
    total_funcs = 0

    for rel_path, changed_lines in sorted(changed_map.items()):
        fpath = repo_root / rel_path
        if not fpath.exists():
            continue
        file_findings, n_funcs = _review_file(
            fpath, rel_path, changed_lines, enabled, min_level
        )
        all_findings.extend(file_findings)
        total_funcs += n_funcs

    filters: Dict[str, Any] = {
        "path": path,
        "checks": checks,
        "min_severity": min_severity,
    }

    if format == "json":
        by_check: Dict[str, int] = {}
        by_severity: Dict[str, int] = {}
        for f in all_findings:
            by_check[f.check] = by_check.get(f.check, 0) + 1
            by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        return json.dumps(
            {
                "ref": ref,
                "files_changed": len(changed_map),
                "functions_reviewed": total_funcs,
                "total_findings": len(all_findings),
                "by_check": by_check,
                "by_severity": by_severity,
                "findings": [f.to_dict() for f in all_findings],
                "filters": filters,
            },
            ensure_ascii=False,
            indent=2,
        )

    return _format_text(all_findings, len(changed_map), total_funcs, ref, filters)


# ── Tool registration ──────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="diff_review",
            schema={
                "name": "diff_review",
                "description": (
                    "Automated code review focused on a git diff. "
                    "Runs security, exception-handling, type annotation, docstring, "
                    "and complexity checks — but only on functions that actually changed. "
                    "Ideal as a pre-commit gate or post-push sanity check. "
                    "Use ref='HEAD' for uncommitted changes, ref='HEAD~1' for the last "
                    "commit, ref='main..veles' for a branch diff."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ref": {
                            "type": "string",
                            "description": (
                                "Git ref or diff spec. Examples: 'HEAD' (uncommitted), "
                                "'HEAD~1' (last commit), 'main..veles' (branch diff), "
                                "'abc123..def456'. Default: 'HEAD'."
                            ),
                        },
                        "path": {
                            "type": "string",
                            "description": "Limit review to a subdirectory or file (relative to repo root)",
                        },
                        "checks": {
                            "type": "string",
                            "description": (
                                "Comma-separated check groups to run. "
                                "Valid: security, exceptions, types, docs, complexity. "
                                "Default: all."
                            ),
                        },
                        "min_severity": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                            "description": "Minimum severity to report (default: low = all)",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format (default: text)",
                        },
                    },
                    "required": [],
                },
            },
            handler=_diff_review,
        )
    ]
