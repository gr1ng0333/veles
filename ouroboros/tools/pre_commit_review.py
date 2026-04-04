"""pre_commit_review — fast static pre-commit checklist runner.

Growth tool: runs all 12 CHECKLISTS.md items against current staged diff
(or a specified git ref) in <1 second, without calling any LLM.

Covers:
  1  bible_compliance      — detects BIBLE.md/SYSTEM.md/identity.md mutations
  2  safety_files_intact   — safety-critical files not modified unexpectedly
  3  no_secrets            — regex scan for leaked tokens/keys in diff
  4  code_quality          — python -m py_compile on changed .py files
  5  tests_pass            — detects new module without test file
  6  version_bump          — VERSION / pyproject.toml sync + bump needed?
  7  tool_registration     — new/removed get_tools() without registry change
  8  context_building      — context.py / prompts changed without mention
  9  shrink_guard          — any file shrunk >70% vs HEAD?
  10 scratchpad_updated    — (advisory) always WARN, user must confirm
  11 knowledge_updated     — (advisory) new pattern/gotcha warranted?
  12 changelog_entry       — commit message body present?

Severity rules (from CHECKLISTS.md):
  Items 1–5:  always critical  — FAIL blocks commit
  Items 6–9:  conditional-critical — FAIL only when condition applies
  Items 10–12: advisory — WARNING only, never blocks

Overall verdict: PASS (all critical pass) | FAIL (any critical fails)

This is NOT a replacement for multi_model_review; it is a fast sanity
check you can run before every commit with near-zero cost.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.utils import _SECRET_PATTERNS, run_cmd
from ouroboros.tools.registry import ToolContext, ToolEntry

_REPO_DIR = Path(os.environ.get("OUROBOROS_REPO_DIR", "/opt/veles"))

# ── constants ─────────────────────────────────────────────────────────────────

# Safety-critical files that should never be modified silently
_SAFETY_FILES = frozenset([
    "BIBLE.md",
    "ouroboros/safety.py",
    "ouroboros/llm.py",  # billing-critical: Copilot trailing system message protection
    "ouroboros/tools/registry.py",
    "prompts/SYSTEM.md",
    "prompts/CONSCIOUSNESS.md",
])

# Identity-core files — absolute prohibition on deletion/gutting (BIBLE P2)
_IDENTITY_CORE = frozenset([
    "BIBLE.md",
    "memory/identity.md",
])

# Files relevant to context building
_CONTEXT_FILES = frozenset([
    "ouroboros/context.py",
    "prompts/SYSTEM.md",
    "prompts/BIBLE.md",
    "prompts/ARCHITECTURE.md",
    "prompts/CHECKLISTS.md",
    "prompts/CONSCIOUSNESS.md",
])


# ── git helpers ───────────────────────────────────────────────────────────────

def _git(args: List[str], cwd: Path = _REPO_DIR) -> Tuple[int, str]:
    """Run git command, return (returncode, stdout+stderr)."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=15,
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except Exception as exc:
        return 1, str(exc)


def _get_diff(staged: bool = True, ref: Optional[str] = None) -> str:
    """Get the diff to review. Priority: ref > staged."""
    if ref:
        rc, out = _git(["diff", ref + "^", ref])
        if rc == 0:
            return out
        # Maybe it's the first commit
        rc, out = _git(["show", ref])
        return out if rc == 0 else ""
    if staged:
        rc, out = _git(["diff", "--cached"])
        if rc == 0 and out:
            return out
    # Fall back to unstaged
    rc, out = _git(["diff", "HEAD"])
    return out if rc == 0 else ""


def _changed_files(staged: bool = True, ref: Optional[str] = None) -> List[str]:
    """Return list of changed file paths."""
    if ref:
        rc, out = _git(["diff", "--name-only", ref + "^", ref])
        if rc != 0:
            rc, out = _git(["show", "--name-only", "--format=", ref])
        return [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else []
    if staged:
        rc, out = _git(["diff", "--cached", "--name-only"])
        if rc == 0 and out.strip():
            return [l.strip() for l in out.splitlines() if l.strip()]
    rc, out = _git(["diff", "--name-only", "HEAD"])
    return [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else []


def _file_size_ratio(path: Path, diff: str) -> Optional[float]:
    """
    Estimate how much of the file is being removed.
    Returns ratio of (removed lines / original lines), or None if can't determine.
    """
    if not path.exists():
        return None
    try:
        original_lines = path.read_text(encoding="utf-8", errors="replace").count("\n") + 1
        if original_lines == 0:
            return None
        removed_lines = sum(
            1 for line in diff.splitlines()
            if line.startswith("-") and not line.startswith("---")
        )
        return removed_lines / original_lines
    except Exception:
        return None


def _has_syntax_error(path: Path) -> Optional[str]:
    """Check Python file for syntax errors. Returns error message or None."""
    if not path.exists():
        return None
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        ast.parse(source, filename=str(path))
        return None
    except SyntaxError as exc:
        return f"SyntaxError at line {exc.lineno}: {exc.msg}"
    except Exception as exc:
        return str(exc)


def _get_version() -> str:
    try:
        return (_REPO_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _get_pyproject_version() -> str:
    try:
        text = (_REPO_DIR / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'version\s*=\s*"([^"]+)"', text)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _get_commit_message() -> str:
    """Get the most recent commit message (for changelog check)."""
    rc, out = _git(["log", "-1", "--format=%s%n%n%b"])
    return out if rc == 0 else ""


# ── checklist items ───────────────────────────────────────────────────────────

def _check_bible_compliance(diff: str, files: List[str]) -> Dict[str, Any]:
    """Item 1: Does the diff mutate BIBLE.md principles in a suspicious way?"""
    item = {"id": 1, "name": "bible_compliance", "severity": "critical"}

    # Direct BIBLE.md changes
    if "BIBLE.md" in files:
        # Check if Principle 0/1/2 lines are being removed
        removed_principles = [
            line[1:] for line in diff.splitlines()
            if line.startswith("-") and not line.startswith("---")
            and re.search(r'Principle [012]|Agency|Continuity|Self.Creation', line, re.I)
        ]
        if removed_principles:
            item["status"] = "FAIL"
            item["detail"] = f"BIBLE.md: {len(removed_principles)} core principle lines removed. Manual review required."
            return item
        item["status"] = "WARN"
        item["detail"] = "BIBLE.md is being modified — verify no principle direction is inverted."
        return item

    item["status"] = "PASS"
    item["detail"] = "BIBLE.md not in diff."
    return item


def _check_safety_files(files: List[str]) -> Dict[str, Any]:
    """Item 2: Safety-critical files not modified without approval."""
    item = {"id": 2, "name": "safety_files_intact", "severity": "critical"}
    touched = [f for f in files if f in _SAFETY_FILES]
    if touched:
        item["status"] = "WARN"
        item["detail"] = f"Safety-critical files modified: {touched}. Ensure explicit approval."
        return item
    item["status"] = "PASS"
    item["detail"] = "No safety-critical files in diff."
    return item


def _check_no_secrets(diff: str, files: List[str]) -> Dict[str, Any]:
    """Item 3: No leaked API keys / tokens in the diff."""
    item = {"id": 3, "name": "no_secrets", "severity": "critical"}
    hits: List[str] = []
    for match in _SECRET_PATTERNS.finditer(diff):
        # Only flag lines being ADDED (not removed)
        line_start = diff.rfind("\n", 0, match.start()) + 1
        line = diff[line_start:diff.find("\n", match.start())]
        if line.startswith("+") and not line.startswith("+++"):
            hits.append(f"  {match.group()[:40]}...")
        if len(hits) >= 3:
            break
    if hits:
        item["status"] = "FAIL"
        item["detail"] = "Potential secrets in added lines:\n" + "\n".join(hits)
        return item
    item["status"] = "PASS"
    item["detail"] = "No secret patterns detected in added lines."
    return item


def _check_code_quality(files: List[str]) -> Dict[str, Any]:
    """Item 4: Syntax errors in changed .py files."""
    item = {"id": 4, "name": "code_quality", "severity": "critical"}
    errors: List[str] = []
    for f in files:
        if not f.endswith(".py"):
            continue
        path = _REPO_DIR / f
        err = _has_syntax_error(path)
        if err:
            errors.append(f"  {f}: {err}")
    if errors:
        item["status"] = "FAIL"
        item["detail"] = "Syntax errors found:\n" + "\n".join(errors)
        return item
    py_count = sum(1 for f in files if f.endswith(".py"))
    item["status"] = "PASS"
    item["detail"] = f"All {py_count} changed .py files parsed without syntax errors."
    return item


def _check_tests(files: List[str]) -> Dict[str, Any]:
    """Item 5: New module has a test file."""
    item = {"id": 5, "name": "tests_pass", "severity": "critical"}

    # Find new tool modules (added files in ouroboros/tools/)
    new_tools = [
        f for f in files
        if f.startswith("ouroboros/tools/") and f.endswith(".py")
        and f not in ("ouroboros/tools/__init__.py", "ouroboros/tools/registry.py")
        and not f.endswith("_test.py")
    ]
    if not new_tools:
        item["status"] = "PASS"
        item["detail"] = "No new tool modules to check."
        return item

    missing_tests: List[str] = []
    for mod_path in new_tools:
        mod_name = Path(mod_path).stem
        test_candidates = [
            f"tests/test_{mod_name}.py",
            f"tests/{mod_name}_test.py",
        ]
        has_test = any(((_REPO_DIR / tc).exists() or tc in files) for tc in test_candidates)
        if not has_test:
            missing_tests.append(f"  {mod_path} → no test file found (checked {test_candidates[0]})")

    if missing_tests:
        item["status"] = "WARN"
        item["detail"] = "New tool modules without test files:\n" + "\n".join(missing_tests)
        return item

    item["status"] = "PASS"
    item["detail"] = f"All {len(new_tools)} new tool modules have test files."
    return item


def _check_version_bump(files: List[str], diff: str) -> Dict[str, Any]:
    """Item 6: VERSION updated when functional changes are present."""
    item = {"id": 6, "name": "version_bump", "severity": "conditional-critical"}

    version = _get_version()
    pyproject_ver = _get_pyproject_version()

    # Check sync
    if version and pyproject_ver and version != pyproject_ver:
        item["status"] = "FAIL"
        item["detail"] = f"VERSION ({version}) ≠ pyproject.toml ({pyproject_ver}) — sync required."
        return item

    # Check if functional .py files changed but VERSION not updated
    functional_changed = [
        f for f in files
        if f.endswith(".py") and not f.startswith("tests/")
        and f not in ("ouroboros/tools/__init__.py",)
    ]
    version_changed = "VERSION" in files or "pyproject.toml" in files

    if functional_changed and not version_changed:
        item["status"] = "WARN"
        item["detail"] = (
            f"Functional .py files changed ({len(functional_changed)}) "
            "but VERSION not updated. OK if this is a WIP commit, "
            "but final commit should bump version."
        )
        return item

    item["status"] = "PASS"
    ver_str = f"VERSION={version}" if version else "VERSION not found"
    item["detail"] = f"{ver_str}, pyproject={pyproject_ver or 'not found'}."
    return item


def _check_tool_registration(files: List[str], diff: str) -> Dict[str, Any]:
    """Item 7: New/removed get_tools() without registry change."""
    item = {"id": 7, "name": "tool_registration", "severity": "conditional-critical"}

    # Check for new tool files with get_tools()
    new_tool_files = [
        f for f in files
        if f.startswith("ouroboros/tools/") and f.endswith(".py")
        and f not in ("ouroboros/tools/__init__.py", "ouroboros/tools/registry.py")
    ]

    if not new_tool_files:
        item["status"] = "N/A"
        item["detail"] = "No tool files in diff."
        return item

    # Verify each tool file has get_tools() function
    missing_get_tools: List[str] = []
    for f in new_tool_files:
        path = _REPO_DIR / f
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
                if "def get_tools" not in content:
                    missing_get_tools.append(f)
            except Exception:
                pass

    if missing_get_tools:
        item["status"] = "FAIL"
        item["detail"] = (
            f"Tool files without get_tools() function: {missing_get_tools}"
        )
        return item

    item["status"] = "PASS"
    item["detail"] = f"All {len(new_tool_files)} tool files have get_tools()."
    return item


def _check_context_building(files: List[str]) -> Dict[str, Any]:
    """Item 8: context.py or prompts changed — note to verify."""
    item = {"id": 8, "name": "context_building", "severity": "conditional-critical"}

    touched_context = [f for f in files if f in _CONTEXT_FILES or f.startswith("prompts/")]
    if not touched_context:
        item["status"] = "N/A"
        item["detail"] = "No context-related files in diff."
        return item

    item["status"] = "WARN"
    item["detail"] = (
        f"Context-related files modified: {touched_context}. "
        "Verify system prompt assembles without error."
    )
    return item


def _check_shrink_guard(files: List[str], diff: str) -> Dict[str, Any]:
    """Item 9: Any file shrunk >70% vs HEAD?"""
    item = {"id": 9, "name": "shrink_guard", "severity": "conditional-critical"}

    # Parse diff to get per-file chunks
    shrunk: List[str] = []
    current_file: Optional[str] = None
    adds = 0
    removes = 0
    file_adds: Dict[str, int] = {}
    file_removes: Dict[str, int] = {}

    for line in diff.splitlines():
        if line.startswith("--- ") or line.startswith("+++ "):
            if line.startswith("+++ b/"):
                current_file = line[6:]
                file_adds[current_file] = 0
                file_removes[current_file] = 0
        elif line.startswith("+") and not line.startswith("+++"):
            if current_file:
                file_adds[current_file] = file_adds.get(current_file, 0) + 1
        elif line.startswith("-") and not line.startswith("---"):
            if current_file:
                file_removes[current_file] = file_removes.get(current_file, 0) + 1

    for f, removed in file_removes.items():
        path = _REPO_DIR / f
        if not path.exists():
            continue
        try:
            original_lines = path.read_text(encoding="utf-8", errors="replace").count("\n") + 1
            if original_lines > 10 and removed / original_lines > 0.70:
                ratio_pct = int(removed / original_lines * 100)
                shrunk.append(f"  {f}: {removed}/{original_lines} lines removed ({ratio_pct}%)")
        except Exception:
            pass

    if shrunk:
        item["status"] = "FAIL"
        item["detail"] = "Files shrunk >70% — possible truncation:\n" + "\n".join(shrunk)
        return item

    item["status"] = "PASS"
    item["detail"] = "No file shrunk >70%."
    return item


def _check_scratchpad(files: List[str]) -> Dict[str, Any]:
    """Item 10: (advisory) Scratchpad updated."""
    item = {"id": 10, "name": "scratchpad_updated", "severity": "advisory"}
    item["status"] = "WARN"
    item["detail"] = "Scratchpad updated? (advisory — cannot auto-verify)"
    return item


def _check_knowledge(files: List[str], diff: str) -> Dict[str, Any]:
    """Item 11: (advisory) New insight recorded in knowledge base?"""
    item = {"id": 11, "name": "knowledge_updated", "severity": "advisory"}

    # Auto-detect if there are interesting new patterns
    new_tool_files = [f for f in files if f.startswith("ouroboros/tools/") and f.endswith(".py")]
    if new_tool_files:
        item["status"] = "WARN"
        item["detail"] = (
            f"New tool(s) added: {new_tool_files}. "
            "Consider updating knowledge base with any gotchas/patterns learned."
        )
    else:
        item["status"] = "PASS"
        item["detail"] = "No new tools — knowledge update optional."
    return item


def _check_changelog(files: List[str]) -> Dict[str, Any]:
    """Item 12: (advisory) Commit message is descriptive."""
    item = {"id": 12, "name": "changelog_entry", "severity": "advisory"}
    msg = _get_commit_message()
    if not msg or len(msg.strip()) < 10:
        item["status"] = "WARN"
        item["detail"] = "No recent commit message found or it's very short."
    else:
        lines = [l.strip() for l in msg.splitlines() if l.strip()]
        if len(lines) == 1 and len(lines[0]) < 30:
            item["status"] = "WARN"
            item["detail"] = f"Commit message is very brief: '{lines[0]}'"
        else:
            item["status"] = "PASS"
            item["detail"] = f"Commit message looks descriptive ({len(lines)} lines)."
    return item


# ── main runner ───────────────────────────────────────────────────────────────

def _run_pre_commit_review(
    ctx: ToolContext,
    staged: bool = True,
    ref: Optional[str] = None,
    verbose: bool = False,
) -> str:
    diff = _get_diff(staged=staged, ref=ref)
    files = _changed_files(staged=staged, ref=ref)

    if not diff and not files:
        return (
            "⚠️ pre_commit_review: No diff found.\n"
            "Make sure you have staged changes (`git add`) or specify `ref=<commit>`."
        )

    results = [
        _check_bible_compliance(diff, files),
        _check_safety_files(files),
        _check_no_secrets(diff, files),
        _check_code_quality(files),
        _check_tests(files),
        _check_version_bump(files, diff),
        _check_tool_registration(files, diff),
        _check_context_building(files),
        _check_shrink_guard(files, diff),
        _check_scratchpad(files),
        _check_knowledge(files, diff),
        _check_changelog(files),
    ]

    # Determine overall verdict
    critical_failures = [
        r for r in results
        if r["status"] == "FAIL" and r["severity"] in ("critical", "conditional-critical")
    ]
    warnings = [r for r in results if r["status"] in ("WARN", "WARNING")]

    overall = "✅ PASS" if not critical_failures else "❌ FAIL"

    # Build output
    lines: List[str] = []
    lines.append(f"## pre_commit_review — {overall}")
    lines.append(f"Files in diff: {len(files)} | Checks: 12 | Critical failures: {len(critical_failures)} | Warnings: {len(warnings)}")
    if files:
        lines.append(f"Changed: {', '.join(files[:8])}" + (" ..." if len(files) > 8 else ""))
    lines.append("")

    # Status icons
    _icons = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️", "WARNING": "⚠️", "N/A": "➖"}

    for r in results:
        icon = _icons.get(r["status"], "❓")
        sev = r["severity"]
        sev_tag = "" if sev == "advisory" else f" [{sev}]"
        line = f"{icon} {r['id']:2d}. {r['name']}{sev_tag}: {r['status']}"
        if verbose or r["status"] in ("FAIL", "WARN"):
            line += f"\n      {r['detail']}"
        lines.append(line)

    lines.append("")
    if critical_failures:
        lines.append(f"❌ VERDICT: FAIL — {len(critical_failures)} critical issue(s) must be resolved before commit.")
        for r in critical_failures:
            lines.append(f"   • {r['name']}: {r['detail'][:100]}")
    else:
        advisory_warns = [r for r in warnings if r["severity"] == "advisory"]
        non_advisory_warns = [r for r in warnings if r["severity"] != "advisory"]
        lines.append(
            f"✅ VERDICT: PASS — no critical failures."
            + (f" {len(non_advisory_warns)} non-advisory warning(s)." if non_advisory_warns else "")
            + (f" {len(advisory_warns)} advisory reminder(s)." if advisory_warns else "")
        )

    return "\n".join(lines)


# ── tool registration ─────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="pre_commit_review",
            schema={
                "name": "pre_commit_review",
                "description": (
                    "Fast static pre-commit checklist runner. "
                    "Runs all 12 CHECKLISTS.md items (bible compliance, secrets, syntax, "
                    "shrink guard, version bump, tool registration, etc.) against staged diff "
                    "or a specific git ref. Returns PASS/FAIL verdict in <1 second, no LLM calls. "
                    "Use before every commit as a cheap sanity check. "
                    "For deep semantic review use multi_model_review instead."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "staged": {
                            "type": "boolean",
                            "description": "Review staged changes (default: true). Set false to review unstaged diff vs HEAD.",
                        },
                        "ref": {
                            "type": "string",
                            "description": "Optional git commit ref to review (e.g. 'HEAD', 'abc1234'). Overrides staged.",
                        },
                        "verbose": {
                            "type": "boolean",
                            "description": "Include detail for PASS/N/A items too (default: false, only shows FAIL/WARN detail).",
                        },
                    },
                    "required": [],
                },
            },
            handler=lambda ctx, **kw: _run_pre_commit_review(ctx, **kw),
        )
    ]
