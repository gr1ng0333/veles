"""run_tests — structured pytest runner.

Growth tool: runs tests from within the agent loop without raw run_shell,
parses pytest output into structured JSON, and supports targeted runs,
filtering by test name/path, and automatic failure summary.

Replaces fragile shell oneliners like:
  run_shell(["bash", "-lc", "cd /opt/veles && pytest tests/ -v 2>&1 | tail -40"])

with a first-class capability that always returns machine-readable results.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

# ── constants ─────────────────────────────────────────────────────────────────

_REPO_DIR = os.environ.get("REPO_DIR", "/opt/veles")
_DEFAULT_TIMEOUT = 120  # seconds per test run


# ── helpers ───────────────────────────────────────────────────────────────────

_RESULT_RE = re.compile(
    r"(\d+) passed|(\d+) failed|(\d+) error|(\d+) warning|(\d+) skipped"
)

_FAIL_HEADER_RE = re.compile(r"^(FAILED|ERROR)\s+(tests/\S+)\s*(?:-\s*(.*))?$")
_SHORT_FAIL_RE = re.compile(r"^(E\s+.+)$")


def _parse_summary_line(summary: str) -> Dict[str, int]:
    """Parse pytest summary line into counts."""
    counts: Dict[str, int] = {"passed": 0, "failed": 0, "error": 0, "warning": 0, "skipped": 0}
    for m in _RESULT_RE.finditer(summary):
        if m.group(1):
            counts["passed"] = int(m.group(1))
        elif m.group(2):
            counts["failed"] = int(m.group(2))
        elif m.group(3):
            counts["error"] = int(m.group(3))
        elif m.group(4):
            counts["warning"] = int(m.group(4))
        elif m.group(5):
            counts["skipped"] = int(m.group(5))
    return counts


def _parse_failures(output: str, limit: int = 20) -> List[Dict[str, str]]:
    """Extract failure/error test IDs and brief error messages from pytest output."""
    failures: List[Dict[str, str]] = []

    # Split into sections by "FAILED" / "ERROR" lines in short summary
    in_failures_section = False
    for line in output.splitlines():
        stripped = line.strip()

        # Short summary section starts with "= short test summary info ="
        if "short test summary info" in stripped:
            in_failures_section = True
            continue
        if in_failures_section:
            if not stripped or stripped.startswith("="):
                continue
            m = _FAIL_HEADER_RE.match(stripped)
            if m:
                status = m.group(1)
                test_id = m.group(2).strip()
                reason = (m.group(3) or "").strip()
                if not reason:
                    # Try to find the E assertion line just before this in the full output
                    idx = output.find(test_id)
                    if idx != -1:
                        snippet = output[max(0, idx - 300): idx].splitlines()
                        for l in reversed(snippet):
                            if l.strip().startswith("E "):
                                reason = l.strip()[2:].strip()
                                break
                failures.append({"status": status, "test": test_id, "reason": reason[:200]})
                if len(failures) >= limit:
                    break

    return failures


def _build_cmd(
    paths: List[str],
    keyword: str,
    markers: str,
    verbose: bool,
    tb_style: str,
    extra_args: List[str],
) -> List[str]:
    """Assemble the pytest command."""
    cmd = ["python", "-m", "pytest"]
    if paths:
        cmd.extend(paths)
    if keyword:
        cmd.extend(["-k", keyword])
    if markers:
        cmd.extend(["-m", markers])
    if verbose:
        cmd.append("-v")
    cmd.extend(["--tb", tb_style, "--no-header", "-q"])
    cmd.extend(extra_args)
    return cmd


# ── main handler ──────────────────────────────────────────────────────────────

def _run_tests(
    ctx: ToolContext,
    paths: Optional[List[str]] = None,
    keyword: str = "",
    markers: str = "",
    verbose: bool = False,
    tb_style: str = "line",
    timeout: int = _DEFAULT_TIMEOUT,
    extra_args: Optional[List[str]] = None,
) -> str:
    """Run pytest and return structured JSON results."""

    repo_dir = str(ctx.repo_dir) if ctx.repo_dir else _REPO_DIR
    paths = paths or []
    extra_args = extra_args or []

    # Clamp timeout
    timeout = max(10, min(timeout, 300))

    cmd = _build_cmd(
        paths=paths,
        keyword=keyword,
        markers=markers,
        verbose=verbose,
        tb_style=tb_style,
        extra_args=extra_args,
    )

    try:
        proc = subprocess.run(
            cmd,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({
            "status": "timeout",
            "error": f"pytest exceeded {timeout}s",
            "cmd": " ".join(cmd),
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "error": str(e),
            "cmd": " ".join(cmd),
        }, ensure_ascii=False, indent=2)

    output = proc.stdout + proc.stderr
    exit_code = proc.returncode

    # Find summary line (last line matching "N passed" pattern)
    summary_line = ""
    for line in reversed(output.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            summary_line = line.strip()
            break

    counts = _parse_summary_line(summary_line)
    failures = _parse_failures(output) if (counts["failed"] > 0 or counts["error"] > 0) else []

    # Determine overall status
    if exit_code == 0:
        status = "passed"
    elif counts["failed"] > 0 or counts["error"] > 0:
        status = "failed"
    else:
        status = f"exit_{exit_code}"

    # Truncate raw output to last 4000 chars for context
    raw_tail = output[-4000:] if len(output) > 4000 else output

    result: Dict[str, Any] = {
        "status": status,
        "exit_code": exit_code,
        "counts": counts,
        "summary_line": summary_line,
        "failures": failures,
        "cmd": " ".join(cmd),
        "output_tail": raw_tail,
    }

    return json.dumps(result, ensure_ascii=False, indent=2)


# ── registry ──────────────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="run_tests",
            schema={
                "name": "run_tests",
                "description": (
                    "Run pytest tests and return structured JSON results. "
                    "Returns: status (passed/failed/timeout/error), per-test failure list with reasons, "
                    "and a summary of counts. "
                    "Use instead of run_shell + grep for test runs — parses output automatically. "
                    "Examples: run all tests, run specific file, filter by name keyword, run with markers."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Test paths to run. E.g. ['tests/test_smoke.py'] or "
                                "['tests/test_smoke.py::test_version_artifacts']. "
                                "Empty = run all tests."
                            ),
                        },
                        "keyword": {
                            "type": "string",
                            "description": (
                                "pytest -k expression to filter tests by name. "
                                "E.g. 'version or smoke' or 'not slow'."
                            ),
                        },
                        "markers": {
                            "type": "string",
                            "description": "pytest -m marker expression. E.g. 'not integration'.",
                        },
                        "verbose": {
                            "type": "boolean",
                            "description": "Enable verbose test output (-v). Default false.",
                        },
                        "tb_style": {
                            "type": "string",
                            "enum": ["line", "short", "long", "no"],
                            "description": "Traceback style. Default 'line' (compact).",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Max seconds to wait for pytest (default 120, max 300).",
                        },
                        "extra_args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Additional pytest arguments to pass through.",
                        },
                    },
                    "required": [],
                },
            },
            handler=_run_tests,
            timeout_sec=180,
        ),
    ]
