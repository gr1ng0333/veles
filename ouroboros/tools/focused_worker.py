"""
Focused Worker tool — spawn an isolated agent for external project tasks.

Usage:
    create_focused_worker(
        task="...",
        system_prompt="...",  # optional
        tools="CODE",         # preset: DEPLOY / CODE / FULL, or comma-separated names
        project_context="...", # optional background for the worker
        model="codex/gpt-5.4", # optional model override (default: codex/gpt-5.4)
    )

Returns: task_id for later get_task_result().
The worker sends a TG notification on completion — always.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso


def _create_focused_worker(
    ctx: ToolContext,
    task: str,
    system_prompt: str = "",
    tools: str = "FULL",
    project_context: str = "",
    model: str = "",
) -> str:
    """Schedule a focused worker task.

    - Minimal context (no identity/scratchpad/chat history)
    - Tool whitelist: DEPLOY / CODE / FULL or comma-separated list
    - Sends TG notification on completion (always)
    - Defaults to codex/gpt-5.4 unless model is explicitly overridden
    """
    task = (task or "").strip()
    if not task:
        return "⚠️ task is required — describe what the focused worker should do."

    # Default model for focused workers is always Codex
    resolved_model = (model or "").strip() or "codex/gpt-5.4"

    tid = uuid.uuid4().hex[:8]
    evt = {
        "type": "create_focused_worker",
        "task_id": tid,
        "task_text": task,
        "system_prompt": (system_prompt or "").strip(),
        "tool_whitelist": (tools or "FULL").strip(),
        "project_context": (project_context or "").strip(),
        "model": resolved_model,
        "ts": utc_now_iso(),
    }
    ctx.pending_events.append(evt)

    preset_info = tools.upper() if tools.upper() in ("DEPLOY", "CODE", "FULL") else f"custom ({tools})"
    return (
        f"Focused worker {tid} scheduled.\n"
        f"Model: {resolved_model}\n"
        f"Tools: {preset_info}\n"
        f"Task: {task[:100]}\n"
        f"Result: available via get_task_result('{tid}') · TG notification on done."
    )


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="create_focused_worker",
            schema={
                "name": "create_focused_worker",
                "description": (
                    "Spawn an isolated focused agent for external project tasks. "
                    "Runs with minimal context (no identity/scratchpad/chat history), "
                    "a tool whitelist, and always sends a TG notification on completion. "
                    "Use for tasks on external repos, deploys, or isolated code work. "
                    "Defaults to codex/gpt-5.4 unless overridden. "
                    "Returns task_id for get_task_result()."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "What the worker should do. Be specific.",
                        },
                        "system_prompt": {
                            "type": "string",
                            "description": (
                                "Optional system prompt for the worker. "
                                "If omitted, a generic agent prompt is used."
                            ),
                        },
                        "tools": {
                            "type": "string",
                            "description": (
                                "Tool preset: 'DEPLOY' (shell/git/push), "
                                "'CODE' (read/write/shell/git), 'FULL' (all tools), "
                                "or comma-separated tool names. Default: FULL."
                            ),
                        },
                        "project_context": {
                            "type": "string",
                            "description": (
                                "Optional background: repo paths, constraints, style, "
                                "anything the worker needs to know before starting."
                            ),
                        },
                        "model": {
                            "type": "string",
                            "description": (
                                "Model override for this worker. "
                                "Default: codex/gpt-5.4. "
                                "Example: 'copilot/claude-sonnet-4.6'."
                            ),
                        },
                    },
                    "required": ["task"],
                },
            },
            handler=_create_focused_worker,
            timeout_sec=30,
        ),
    ]
