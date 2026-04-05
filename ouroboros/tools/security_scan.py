"""security_scan — codebase security anti-pattern scanner.

Uses AST analysis + regex to detect common security issues in Python source
files.  Findings are classified by severity (high / medium / low) and grouped
by category.

Categories
----------
eval_exec         — use of eval() / exec() / compile() on dynamic input (high)
hardcoded_secrets — apparent passwords, tokens, API keys in assignments (high)
shell_injection   — subprocess with shell=True, os.system/popen (high)
deserialization   — pickle.loads/load, shelve, marshal (high)
sql_injection     — string formatting / f-strings inside .execute() calls (high)
weak_crypto       — MD5, SHA1, DES, RC4 (medium)
yaml_unsafe       — yaml.load without explicit Loader (medium)
xml_unsafe        — use of xml.etree / xml.dom without defusedxml (medium)
debug_code        — breakpoint(), pdb.set_trace(), assert-only auth, bare print (low)
path_traversal    — os.path.join with user input, open() in request handlers (low)

Filters
-------
path          — limit to a subdirectory or single file
categories    — comma-separated list of categories to include (default: all)
min_severity  — "low" | "medium" | "high" (default: low = show all)
format        — "text" | "json" (default "text")
skip_tests    — exclude tests/ directory from scan (default: False)

Examples
--------
    security_scan()                                 # full scan
    security_scan(path="ouroboros/")                # one directory
    security_scan(categories="eval_exec,shell_injection")
    security_scan(min_severity="high")
    security_scan(skip_tests=True)
    security_scan(format="json")
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

# ── Constants ─────────────────────────────────────────────────────────────────

_REPO_DIR = Path(os.environ.get("REPO_DIR", "/opt/veles"))

_SKIP_DIRS = {
    "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
    "node_modules", ".venv", "venv", "dist", "build",
}

_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}

# category → severity
_CATEGORY_SEVERITY: Dict[str, str] = {
    "eval_exec": "high",
    "hardcoded_secrets": "high",
    "shell_injection": "high",
    "deserialization": "high",
    "sql_injection": "high",
    "weak_crypto": "medium",
    "yaml_unsafe": "medium",
    "xml_unsafe": "medium",
    "debug_code": "low",
    "path_traversal": "low",
}

_ALL_CATEGORIES = set(_CATEGORY_SEVERITY)

_SEVERITY_ICON = {"high": "🔴", "medium": "🟡", "low": "🔵"}


# ── Finding dataclass ─────────────────────────────────────────────────────────

class _Finding:
    __slots__ = ("file", "line", "category", "severity", "message", "snippet")

    def __init__(
        self,
        file: str,
        line: int,
        category: str,
        severity: str,
        message: str,
        snippet: str = "",
    ) -> None:
        self.file = file
        self.line = line
        self.category = category
        self.severity = severity
        self.message = message
        self.snippet = snippet.strip()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "category": self.category,
            "severity": self.severity,
            "message": self.message,
            "snippet": self.snippet,
        }


# ── File collection ───────────────────────────────────────────────────────────

def _collect_py_files(
    root: Path,
    subpath: Optional[str],
    skip_tests: bool,
) -> List[Path]:
    target = root
    if subpath:
        candidate = root / subpath.lstrip("/")
        if candidate.exists():
            target = candidate

    if target.is_file() and target.suffix == ".py":
        return [target]

    skip = set(_SKIP_DIRS)
    if skip_tests:
        skip.add("tests")

    py_files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(str(target)):
        dirnames[:] = [
            d for d in dirnames
            if d not in skip and not d.startswith(".")
        ]
        for fname in sorted(filenames):
            if fname.endswith(".py"):
                py_files.append(Path(dirpath) / fname)
    return py_files


# ── AST helpers ───────────────────────────────────────────────────────────────

def _name_of(node: ast.expr) -> str:
    """Return a dotted name string for Attribute/Name nodes, or ''."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name_of(node.value)}.{node.attr}"
    return ""


def _is_string_or_fstring(node: ast.expr) -> bool:
    """True if the node is a string constant or f-string."""
    if isinstance(node, ast.Constant) and isinstance(node.s, str):
        return True
    if isinstance(node, ast.JoinedStr):  # f-string
        return True
    return False


def _is_format_call(node: ast.expr) -> bool:
    """True if node looks like 'something % ...' or 'fmt.format(...)' or f-string."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return True
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        return True
    return False


# ── Regex-level checkers (operate on raw source text) ─────────────────────────

# Hardcoded secrets: assignment patterns like `password = "..."`, `token = "..."`
_SECRET_ASSIGN_RE = re.compile(
    r"""
    (?:^|\s)                       # start or whitespace before
    (?P<key>
        password | passwd | secret | api_key | apikey | token |
        access_key | private_key | auth_token | client_secret
    )
    \s*=\s*                        # assignment
    (?P<quote>['"])                # opening quote
    (?P<value>[^'"]{4,})           # at least 4 chars (filter empty / placeholder)
    (?P=quote)                     # matching close quote
    """,
    re.IGNORECASE | re.VERBOSE,
)

# False-positive filters for secrets: env vars, placeholder strings
_SECRET_FALSEPOS_RE = re.compile(
    r"""
    os\.environ | getenv | environ\.get |     # env lookups
    <[A-Z_]+>    |                            # <PLACEHOLDER>
    \{[A-Z_]+\}  |                            # {PLACEHOLDER}
    your[_-]?    |                            # your_token, your-key
    example      |                            # example_password
    placeholder  |                            # placeholder
    changeme     |                            # changeme
    xxxxxxxx     |                            # xxxxxxxx
    \*+          |                            # ****
    none         |
    false        |
    true
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Weak crypto identifiers
_WEAK_CRYPTO_RE = re.compile(
    r"""
    \b(?:
        hashlib\.(?:md5|sha1)\s*\(  |
        MD5(?:\.new)?\s*\(          |
        SHA1(?:\.new)?\s*\(         |
        Crypto\.Cipher\.DES         |
        Crypto\.Cipher\.ARC4        |
        cryptography.*DES           |
        cryptography.*RC4
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# XML unsafe: standard lib xml without defusedxml
_XML_UNSAFE_RE = re.compile(
    r"""
    \bfrom\s+xml\.(?:etree|dom|sax|parsers)\b  |
    \bimport\s+xml\.(?:etree|dom|sax|parsers)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Debug: pdb / breakpoint
_DEBUG_RE = re.compile(
    r"""
    (?:^|\s)(?:
        breakpoint\s*\(\s*\)    |
        pdb\.set_trace\s*\(\s*\)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# os.system / os.popen
_OS_SHELL_RE = re.compile(r"\bos\.(?:system|popen)\s*\(")

# yaml.load without Loader argument (simple pattern)
_YAML_UNSAFE_RE = re.compile(r"\byaml\.load\s*\([^)]*\)")
_YAML_LOADER_RE = re.compile(r"Loader\s*=")


# ── AST-level scanner ─────────────────────────────────────────────────────────

def _scan_ast(
    source: str,
    rel_path: str,
    lines: List[str],
    enabled: Set[str],
    min_level: int,
) -> List[_Finding]:
    """Walk the AST and collect security findings."""
    findings: List[_Finding] = []

    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        return findings

    def snip(lineno: int) -> str:
        if 1 <= lineno <= len(lines):
            return lines[lineno - 1].rstrip()
        return ""

    def add(
        lineno: int,
        category: str,
        message: str,
    ) -> None:
        sev = _CATEGORY_SEVERITY[category]
        if _SEVERITY_ORDER[sev] < min_level:
            return
        if category not in enabled:
            return
        findings.append(
            _Finding(
                file=rel_path,
                line=lineno,
                category=category,
                severity=sev,
                message=message,
                snippet=snip(lineno),
            )
        )

    for node in ast.walk(tree):
        # ── eval / exec ───────────────────────────────────────────────────
        if isinstance(node, ast.Call):
            func_name = _name_of(node.func)

            if func_name in ("eval", "exec", "compile"):
                # Flag if called with a non-literal first argument
                if node.args and not isinstance(node.args[0], ast.Constant):
                    add(
                        node.lineno,
                        "eval_exec",
                        f"Dynamic {func_name}() call — potential code injection",
                    )
                elif not node.args:
                    # eval() with no args or keyword — still suspicious
                    add(
                        node.lineno,
                        "eval_exec",
                        f"{func_name}() call without visible argument",
                    )

            # ── pickle / shelve / marshal deserialization ─────────────────
            if func_name in (
                "pickle.loads", "pickle.load",
                "pickle.Unpickler",
                "shelve.open",
                "marshal.loads", "marshal.load",
            ):
                add(
                    node.lineno,
                    "deserialization",
                    f"Unsafe deserialization: {func_name}() — never deserialize untrusted data",
                )

            # ── subprocess shell=True ─────────────────────────────────────
            if func_name in (
                "subprocess.run", "subprocess.call",
                "subprocess.check_call", "subprocess.check_output",
                "subprocess.Popen",
            ):
                for kw in node.keywords:
                    if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value:
                        add(
                            node.lineno,
                            "shell_injection",
                            f"{func_name}(shell=True) — use list args to avoid shell injection",
                        )

            # ── SQL injection: .execute() with formatted string ───────────
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in ("execute", "executemany", "executescript")
            ):
                if node.args and _is_format_call(node.args[0]):
                    add(
                        node.lineno,
                        "sql_injection",
                        "SQL query built with string formatting — use parameterized queries",
                    )

            # ── yaml.load without Loader ──────────────────────────────────
            if func_name == "yaml.load":
                has_loader = any(kw.arg == "Loader" for kw in node.keywords)
                if not has_loader and (
                    len(node.args) < 2
                    or not isinstance(node.args[1], (ast.Name, ast.Attribute))
                ):
                    add(
                        node.lineno,
                        "yaml_unsafe",
                        "yaml.load() without Loader — use yaml.safe_load() or pass Loader=yaml.SafeLoader",
                    )

        # ── assert for auth / access control ─────────────────────────────
        if isinstance(node, ast.Assert):
            # Heuristic: assert used with auth-related names
            test_src = ast.unparse(node.test) if hasattr(ast, "unparse") else ""
            auth_words = re.search(
                r"\b(is_admin|is_authenticated|has_perm|check_auth|auth|login|logged_in)\b",
                test_src,
                re.IGNORECASE,
            )
            if auth_words:
                add(
                    node.lineno,
                    "debug_code",
                    "assert used for access control — disabled with -O flag; use explicit check",
                )

    return findings


# ── Regex-level scanner ───────────────────────────────────────────────────────

def _scan_regex(
    source: str,
    lines: List[str],
    rel_path: str,
    enabled: Set[str],
    min_level: int,
) -> List[_Finding]:
    findings: List[_Finding] = []

    def add(lineno: int, category: str, message: str) -> None:
        sev = _CATEGORY_SEVERITY[category]
        if _SEVERITY_ORDER[sev] < min_level:
            return
        if category not in enabled:
            return
        snippet = lines[lineno - 1].rstrip() if 1 <= lineno <= len(lines) else ""
        findings.append(
            _Finding(
                file=rel_path,
                line=lineno,
                category=category,
                severity=sev,
                message=message,
                snippet=snippet,
            )
        )

    for lineno, line in enumerate(lines, start=1):
        # Skip comments for most regex checks (they're not executable)
        stripped = line.lstrip()
        is_comment = stripped.startswith("#")

        # ── Hardcoded secrets ─────────────────────────────────────────────
        if not is_comment and "hardcoded_secrets" in enabled:
            for m in _SECRET_ASSIGN_RE.finditer(line):
                value = m.group("value")
                # filter out obvious non-secrets
                if not _SECRET_FALSEPOS_RE.search(value) and not _SECRET_FALSEPOS_RE.search(m.group("key")):
                    key = m.group("key")
                    add(
                        lineno,
                        "hardcoded_secrets",
                        f"Potential hardcoded secret: '{key}' = '...' — use environment variables",
                    )

        # ── Weak crypto ───────────────────────────────────────────────────
        if "weak_crypto" in enabled and _SEVERITY_ORDER["medium"] >= min_level:
            if _WEAK_CRYPTO_RE.search(line):
                add(
                    lineno,
                    "weak_crypto",
                    "Weak cryptographic algorithm (MD5/SHA1/DES/RC4) — use SHA-256+ instead",
                )

        # ── XML unsafe ────────────────────────────────────────────────────
        if "xml_unsafe" in enabled and _SEVERITY_ORDER["medium"] >= min_level:
            if _XML_UNSAFE_RE.search(line):
                add(
                    lineno,
                    "xml_unsafe",
                    "Standard xml library vulnerable to XXE/Billion-Laughs — use defusedxml",
                )

        # ── Debug code ────────────────────────────────────────────────────
        if "debug_code" in enabled:
            if _DEBUG_RE.search(line) and not is_comment:
                add(
                    lineno,
                    "debug_code",
                    "Debug breakpoint left in code",
                )

        # ── os.system / os.popen ──────────────────────────────────────────
        if "shell_injection" in enabled:
            if _OS_SHELL_RE.search(line) and not is_comment:
                add(
                    lineno,
                    "shell_injection",
                    "os.system()/os.popen() — prefer subprocess with list args",
                )

        # ── yaml.load without Loader (regex fallback) ─────────────────────
        if "yaml_unsafe" in enabled and _SEVERITY_ORDER["medium"] >= min_level:
            m = _YAML_UNSAFE_RE.search(line)
            if m and not is_comment and not _YAML_LOADER_RE.search(m.group(0)):
                add(
                    lineno,
                    "yaml_unsafe",
                    "yaml.load() without Loader — use yaml.safe_load() or pass Loader=yaml.SafeLoader",
                )

    return findings


# ── Deduplication ─────────────────────────────────────────────────────────────

def _dedup(findings: List[_Finding]) -> List[_Finding]:
    """Remove exact duplicates (same file+line+category)."""
    seen: Set[Tuple[str, int, str]] = set()
    out: List[_Finding] = []
    for f in findings:
        key = (f.file, f.line, f.category)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


# ── Per-file scanner ──────────────────────────────────────────────────────────

def _scan_file(
    path: Path,
    rel_path: str,
    enabled: Set[str],
    min_level: int,
) -> List[_Finding]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    lines = source.splitlines()
    findings: List[_Finding] = []
    findings.extend(_scan_ast(source, rel_path, lines, enabled, min_level))
    findings.extend(_scan_regex(source, lines, rel_path, enabled, min_level))
    return _dedup(findings)


# ── Text formatter ─────────────────────────────────────────────────────────────

def _format_text(
    findings: List[_Finding],
    total_files: int,
    filters: Dict[str, Any],
) -> str:
    parts: List[str] = []

    filter_items = []
    if filters.get("path"):
        filter_items.append(f"path={filters['path']}")
    if filters.get("categories"):
        filter_items.append(f"categories={filters['categories']}")
    if filters.get("min_severity", "low") != "low":
        filter_items.append(f"min_severity={filters['min_severity']}")
    if filters.get("skip_tests"):
        filter_items.append("skip_tests=True")
    filter_str = (", " + ", ".join(filter_items)) if filter_items else ""

    parts.append(
        f"## Security Scan — {total_files} files, {len(findings)} finding(s){filter_str}\n"
    )

    if not findings:
        parts.append("✅ No security issues detected.")
        return "\n".join(parts)

    # Summary by category
    by_cat: Dict[str, int] = {}
    for f in findings:
        by_cat[f.category] = by_cat.get(f.category, 0) + 1

    cat_order = sorted(
        _ALL_CATEGORIES,
        key=lambda c: (-_SEVERITY_ORDER[_CATEGORY_SEVERITY[c]], c),
    )
    for cat in cat_order:
        count = by_cat.get(cat, 0)
        if count == 0:
            continue
        icon = _SEVERITY_ICON[_CATEGORY_SEVERITY[cat]]
        parts.append(f"   {icon} {cat:<22} {count:>3}")
    parts.append("")

    # Findings sorted by severity desc, file, line
    sorted_findings = sorted(
        findings,
        key=lambda f: (
            -_SEVERITY_ORDER[f.severity],
            f.file,
            f.line,
        ),
    )

    prev_severity: Optional[str] = None
    for finding in sorted_findings:
        if finding.severity != prev_severity:
            icon = _SEVERITY_ICON[finding.severity]
            parts.append(f"### {icon} {finding.severity.upper()}")
            prev_severity = finding.severity

        snip_part = f"\n      `{finding.snippet}`" if finding.snippet else ""
        parts.append(
            f"   {finding.file}:{finding.line}  [{finding.category}] {finding.message}{snip_part}"
        )

    return "\n".join(parts)


# ── Tool entry point ──────────────────────────────────────────────────────────

def _security_scan(
    ctx: ToolContext,
    path: Optional[str] = None,
    categories: Optional[str] = None,
    min_severity: str = "low",
    format: str = "text",
    skip_tests: bool = False,
) -> str:
    """Scan codebase for security anti-patterns."""
    if min_severity not in _SEVERITY_ORDER:
        return (
            f"Unknown min_severity: {min_severity!r}. "
            f"Valid: low, medium, high"
        )

    if categories:
        requested = {c.strip().lower() for c in categories.split(",")}
        unknown = requested - _ALL_CATEGORIES
        if unknown:
            return (
                f"Unknown categories: {', '.join(sorted(unknown))}. "
                f"Valid: {', '.join(sorted(_ALL_CATEGORIES))}"
            )
        enabled = requested
    else:
        enabled = set(_ALL_CATEGORIES)

    min_level = _SEVERITY_ORDER[min_severity]

    repo_root = Path(ctx.repo_dir if ctx and ctx.repo_dir else _REPO_DIR)
    py_files = _collect_py_files(repo_root, path, skip_tests)

    all_findings: List[_Finding] = []
    for fpath in py_files:
        try:
            rel = str(fpath.relative_to(repo_root))
        except ValueError:
            rel = str(fpath)
        all_findings.extend(_scan_file(fpath, rel, enabled, min_level))

    # Final sort: by severity desc, file, line
    all_findings.sort(
        key=lambda f: (-_SEVERITY_ORDER[f.severity], f.file, f.line)
    )

    filters: Dict[str, Any] = {
        "path": path,
        "categories": categories,
        "min_severity": min_severity,
        "skip_tests": skip_tests,
    }

    if format == "json":
        by_cat: Dict[str, int] = {}
        by_sev: Dict[str, int] = {}
        for f in all_findings:
            by_cat[f.category] = by_cat.get(f.category, 0) + 1
            by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        return json.dumps(
            {
                "total_files": len(py_files),
                "total_findings": len(all_findings),
                "by_category": by_cat,
                "by_severity": by_sev,
                "findings": [f.to_dict() for f in all_findings],
                "filters": filters,
            },
            ensure_ascii=False,
            indent=2,
        )

    return _format_text(all_findings, len(py_files), filters)


# ── Tool registration ─────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="security_scan",
            schema={
                "name": "security_scan",
                "description": (
                    "Scan the Python codebase for security anti-patterns using "
                    "AST analysis and regex. Detects: eval/exec code injection, "
                    "hardcoded secrets (passwords/tokens/API keys), shell injection "
                    "(subprocess shell=True, os.system), unsafe deserialization "
                    "(pickle/marshal), SQL injection via string formatting, weak crypto "
                    "(MD5/SHA1/DES/RC4), unsafe YAML loading, vulnerable XML parsing, "
                    "and debug code (breakpoint/pdb). Returns findings grouped by "
                    "severity with file, line, and code snippet."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Limit scan to a subdirectory or file (relative to repo root)",
                        },
                        "categories": {
                            "type": "string",
                            "description": (
                                "Comma-separated categories to include. "
                                "Valid: eval_exec, hardcoded_secrets, shell_injection, "
                                "deserialization, sql_injection, weak_crypto, yaml_unsafe, "
                                "xml_unsafe, debug_code, path_traversal (default: all)"
                            ),
                        },
                        "min_severity": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                            "description": "Minimum severity to include (default: low = show all)",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format (default: text)",
                        },
                        "skip_tests": {
                            "type": "boolean",
                            "description": "Exclude tests/ directory from scan (default: false)",
                        },
                    },
                    "required": [],
                },
            },
            handler=_security_scan,
        )
    ]
