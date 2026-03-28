"""Shell tools: run_shell."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso, append_jsonl, truncate_for_log
from ouroboros.tools.git import _acquire_copilot_write_lock

log = logging.getLogger(__name__)


def _try_parse_python_list_string(s: str) -> Optional[List[str]]:
    """Try to parse a Python-style list string like `[grep, -n, pattern, file.py]`.

    LLMs sometimes produce this malformed format — elements without quotes.
    json.loads and ast.literal_eval both fail on it; this handles the gap.
    Returns list if parseable, None otherwise.
    """
    stripped = s.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        return None
    inner = stripped[1:-1].strip()
    if not inner:
        return None

    # Try JSON first (handles properly quoted elements like ['git', 'add'])
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except (json.JSONDecodeError, ValueError):
        pass

    # Heuristic split: split by comma, strip each element of whitespace and quotes.
    # Handles: [grep, -n, pattern, file.py] → ['grep', '-n', 'pattern', 'file.py']
    # Also handles: ['git', 'add', '-A'] via JSON above; this handles unquoted only.
    parts = [p.strip().strip("'\"").strip() for p in inner.split(",")]
    parts = [p for p in parts if p]
    if not parts:
        return None

    # Sanity: if any part still starts/ends with [ or ] it's malformed — fall back
    if any(p.startswith("[") or p.endswith("]") for p in parts):
        return None

    return parts


def _run_shell(ctx: ToolContext, cmd, cwd: str = "") -> str:
    # Recover from LLM sending cmd as JSON string instead of list
    if isinstance(cmd, str):
        raw_cmd = cmd
        warning = "run_shell_cmd_string"

        # 1. Try Python-list-string parser first (handles unquoted lists like [grep, -n, ...])
        recovered = _try_parse_python_list_string(cmd)
        if recovered is not None:
            cmd = recovered
            warning = "run_shell_cmd_string_python_list_recovered"
        else:
            # 2. Try JSON parsing
            try:
                parsed = json.loads(cmd)
                if isinstance(parsed, list):
                    cmd = parsed
                    warning = "run_shell_cmd_string_json_list_recovered"
                elif isinstance(parsed, str):
                    try:
                        cmd = shlex.split(parsed)
                    except ValueError:
                        cmd = parsed.split()
                    warning = "run_shell_cmd_string_json_string_split"
                else:
                    try:
                        cmd = shlex.split(cmd)
                    except ValueError:
                        cmd = cmd.split()
                    warning = "run_shell_cmd_string_json_non_list_split"
            except Exception:
                # 3. Last resort: shlex split
                try:
                    cmd = shlex.split(raw_cmd)
                except ValueError:
                    cmd = raw_cmd.split()
                warning = "run_shell_cmd_string_split_fallback"

        try:
            append_jsonl(ctx.drive_path("logs") / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "tool_warning",
                "tool": "run_shell",
                "warning": warning,
                "cmd_preview": truncate_for_log(raw_cmd, 500),
            })
        except Exception:
            log.debug("Failed to log run_shell warning to events.jsonl", exc_info=True)
            pass

    if not isinstance(cmd, list):
        return "⚠️ SHELL_ARG_ERROR: cmd must be a list of strings."
    cmd = [str(x) for x in cmd]

    shell_text = ' ' + ' '.join(cmd).lower() + ' '
    mutating_markers = (
        ' git add ', ' git commit ', ' git push ', ' git tag ', ' git reset ', ' git checkout ',
        ' git cherry-pick ', ' apply_patch ', ' patch ', ' sed -i', ' perl -pi', ' mv ', ' cp ',
        ' rm ', ' mkdir ', ' touch ', ' python - <<', ' python3 - <<'
    )
    likely_mutating = any(tok in shell_text for tok in mutating_markers)
    if likely_mutating:
        msg = _acquire_copilot_write_lock(ctx)
        if msg:
            return msg
        ctx.write_attempted = True

    work_dir = ctx.repo_dir
    if cwd and cwd.strip() not in ("", ".", "./"):
        candidate = (ctx.repo_dir / cwd).resolve()
        if candidate.exists() and candidate.is_dir():
            work_dir = candidate

    try:
        res = subprocess.run(
            cmd, cwd=str(work_dir),
            capture_output=True, text=True, timeout=120,
        )
        out = res.stdout + ("\n--- STDERR ---\n" + res.stderr if res.stderr else "")
        if len(out) > 50000:
            out = out[:25000] + "\n...(truncated)...\n" + out[-25000:]
        prefix = f"exit_code={res.returncode}\n"
        return prefix + out
    except subprocess.TimeoutExpired:
        return "⚠️ TIMEOUT: command exceeded 120s."
    except Exception as e:
        return f"⚠️ SHELL_ERROR: {e}"


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("run_shell", {
            "name": "run_shell",
            "description": "Run a shell command (list of args) inside the repo. Returns stdout+stderr.",
            "parameters": {"type": "object", "properties": {
                "cmd": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string", "default": ""},
            }, "required": ["cmd"]},
        }, _run_shell, is_code_tool=True),
    ]
