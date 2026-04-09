"""
Focused context builder for focused workers.

Minimal 3-block context — no identity, no scratchpad, no chat history.
Only what's needed for isolated external-project work.

Block 0: system_prompt (static, passed at creation)
Block 1: task description + project_context
Block 2: dynamic (last N tool calls from this task)
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any, Dict, List, Optional

from ouroboros.utils import utc_now_iso, read_text, clip_text, get_git_info

log = logging.getLogger(__name__)

# Defaults
DEFAULT_MAX_TOOL_HISTORY = 20  # last N tool events in Block 2
FOCUSED_CONTEXT_SOFT_CAP = 80_000  # tokens — generous but not full Veles context


def build_focused_messages(
    task: Dict[str, Any],
    system_prompt: str,
    project_context: str = "",
    repo_dir: Optional[pathlib.Path] = None,
    drive_root: Optional[pathlib.Path] = None,
    max_tool_history: int = DEFAULT_MAX_TOOL_HISTORY,
) -> List[Dict[str, Any]]:
    """Build minimal LLM messages for a focused worker.

    Returns OpenAI-compatible messages list:
      [{"role": "system", "content": ...}, {"role": "user", "content": ...}]
    """
    # --- Block 0: static system prompt ---
    system_parts = [system_prompt.strip()]
    system_parts.append("\n\nLanguage: respond in Russian unless the task explicitly requires English.")
    system_content = "\n".join(system_parts)

    # --- Block 1: task + project context ---
    task_text = str(task.get("text") or "").strip()
    user_parts: List[str] = []

    user_parts.append(f"## Task\n\n{task_text}")

    if project_context:
        user_parts.append(f"## Project Context\n\n{clip_text(project_context, 30_000)}")

    # Runtime info (git, time)
    runtime_lines: List[str] = [f"utc_now: {utc_now_iso()}"]
    if repo_dir:
        try:
            branch, sha = get_git_info(repo_dir)
            runtime_lines.append(f"git_branch: {branch}")
            runtime_lines.append(f"git_head: {sha}")
        except Exception:
            pass
    user_parts.append("## Runtime\n\n" + "\n".join(runtime_lines))

    # --- Block 2: dynamic tool history from this task ---
    task_id = str(task.get("id") or "").strip()
    if drive_root and task_id:
        tool_history = _load_tool_history(drive_root, task_id, max_tool_history)
        if tool_history:
            user_parts.append(f"## Recent tool calls\n\n{tool_history}")

    user_content = "\n\n".join(user_parts)
    user_content = clip_text(user_content, FOCUSED_CONTEXT_SOFT_CAP)

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]

    # Copilot billing protection: trailing system message
    messages.append({"role": "system", "content": "Continue working on the task."})

    return messages


def _load_tool_history(drive_root: pathlib.Path, task_id: str, limit: int) -> str:
    """Load last N tool call events for this task_id from tools.jsonl."""
    tools_path = drive_root / "logs" / "tools.jsonl"
    if not tools_path.exists():
        return ""
    try:
        lines: List[str] = []
        with tools_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    if str(ev.get("task_id") or "") == task_id:
                        lines.append(line)
                except Exception:
                    continue
        tail = lines[-limit:]
        if not tail:
            return ""
        parts = []
        for raw in tail:
            try:
                ev = json.loads(raw)
                tool = ev.get("tool") or ev.get("name") or "?"
                status = "✓" if not ev.get("error") else "✗"
                result_snippet = str(ev.get("result") or ev.get("error") or "")[:200]
                parts.append(f"{status} {tool}: {result_snippet}")
            except Exception:
                parts.append(raw[:120])
        return "\n".join(parts)
    except Exception as e:
        log.debug("Failed to load tool history: %s", e)
        return ""
