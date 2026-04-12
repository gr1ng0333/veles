"""
Ouroboros — Focused Agent.

Lightweight agent for external-project tasks (focused workers).
Unlike the full OuroborosAgent:
- Uses build_focused_messages() — no identity/scratchpad/chat history
- Applies tool whitelist from task["tool_whitelist"]
- Always sends TG notification on completion
- Supports DEPLOY / CODE / FULL presets + custom comma-separated lists
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import queue
import time
import traceback
import threading
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

from ouroboros.utils import (
    utc_now_iso, append_jsonl, truncate_for_log,
    get_git_info,
)
from ouroboros.llm import LLMClient, model_transport
from ouroboros.model_modes import sync_mode_env_from_state
from ouroboros.tools import ToolRegistry
from ouroboros.tools.registry import ToolContext, FOCUSED_TOOL_PRESETS
from ouroboros.focused_context import build_focused_messages
from ouroboros.loop import run_llm_loop


def _resolve_whitelist(tools_spec: Optional[str]) -> Optional[List[str]]:
    """Resolve tools_spec to a list of allowed tool names (or None = all).

    tools_spec can be:
      - None / "" / "FULL"  → no restriction
      - "DEPLOY" / "CODE"   → use preset
      - "run_shell,git_status,..."  → explicit comma-separated list
    """
    if not tools_spec or tools_spec.strip().upper() == "FULL":
        return None
    spec = tools_spec.strip().upper()
    if spec in FOCUSED_TOOL_PRESETS:
        preset = FOCUSED_TOOL_PRESETS[spec]
        return list(preset) if preset is not None else None
    # Treat as comma-separated list
    names = [n.strip() for n in tools_spec.split(",") if n.strip()]
    return names if names else None


class FocusedAgent:
    """Isolated agent for focused worker tasks."""

    def __init__(self, repo_dir: str, drive_root: str, event_queue: Any = None):
        self.repo_dir = pathlib.Path(repo_dir)
        self.drive_root = pathlib.Path(drive_root)
        self._event_queue = event_queue
        self._pending_events: List[Dict[str, Any]] = []

        self.llm = LLMClient()
        self.tools = ToolRegistry(repo_dir=self.repo_dir, drive_root=self.drive_root)

        self._log_boot()

    def _log_boot(self) -> None:
        try:
            git_branch, git_sha = get_git_info(self.repo_dir)
            append_jsonl(self.drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "focused_worker_boot",
                "pid": os.getpid(),
                "git_branch": git_branch,
                "git_sha": git_sha,
            })
        except Exception:
            pass

    def _emit_progress(self, text: str) -> None:
        if not text:
            return
        evt = {
            "type": "send_message",
            "chat_id": self._current_chat_id,
            "text": text,
            "is_progress": True,
            "ts": utc_now_iso(),
        }
        self._pending_events.append(evt)
        if self._event_queue is not None:
            try:
                self._event_queue.put_nowait(evt)
            except Exception:
                pass

    def handle_task(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        sync_mode_env_from_state()
        start_time = time.time()
        self._pending_events = []
        self._current_chat_id = int(task.get("chat_id") or 0) or None
        task_id = str(task.get("id") or "")
        drive_logs = self.drive_root / "logs"

        # --- Tool whitelist ---
        tool_whitelist_spec = str(task.get("tool_whitelist") or "FULL")
        whitelist = _resolve_whitelist(tool_whitelist_spec)
        if whitelist is not None:
            self.tools.set_whitelist(whitelist)
        else:
            self.tools.clear_whitelist()

        # --- Build context ---
        requested_model = str(
            task.get("model") or "codex/gpt-5.4"
        ).strip()
        write_transport = model_transport(requested_model) if requested_model else None

        ctx = ToolContext(
            repo_dir=self.repo_dir,
            drive_root=self.drive_root,
            pending_events=self._pending_events,
            current_chat_id=self._current_chat_id,
            current_task_type="focused",
            emit_progress_fn=self._emit_progress,
            task_id=task_id,
            write_transport=write_transport,
            event_queue=self._event_queue,
        )
        self.tools.set_context(ctx)

        # Build minimal focused messages (no identity/scratchpad/chat history)
        messages = build_focused_messages(
            task=task,
            system_prompt=str(task.get("system_prompt") or "You are a focused autonomous agent. Complete the given task efficiently and report results."),
            project_context=str(task.get("project_context") or ""),
            repo_dir=self.repo_dir,
            drive_root=self.drive_root,
        )

        # Budget
        budget_remaining = None
        try:
            state_path = self.drive_root / "state" / "state.json"
            state_data = json.loads(state_path.read_text())
            total_budget = float(os.environ.get("TOTAL_BUDGET", "1"))
            spent = float(state_data.get("spent_usd", 0))
            if total_budget > 0:
                budget_remaining = max(0, total_budget - spent)
        except Exception:
            pass

        # --- LLM loop ---
        usage: Dict[str, Any] = {}
        llm_trace: Dict[str, Any] = {"assistant_notes": [], "tool_calls": []}
        text = ""

        initial_effort = os.environ.get("OUROBOROS_REASONING_EFFORT", "").strip().lower() or "high"

        try:
            text, usage, llm_trace = run_llm_loop(
                messages=messages,
                tools=self.tools,
                llm=self.llm,
                drive_logs=drive_logs,
                emit_progress=self._emit_progress,
                incoming_messages=queue.Queue(),
                task_type="focused",
                task_id=task_id,
                budget_remaining_usd=budget_remaining,
                event_queue=self._event_queue,
                initial_effort=initial_effort,
                drive_root=self.drive_root,
            )
        except Exception as e:
            tb = traceback.format_exc()
            append_jsonl(drive_logs / "events.jsonl", {
                "ts": utc_now_iso(), "type": "focused_task_error",
                "task_id": task_id, "error": repr(e),
                "traceback": truncate_for_log(tb, 2000),
            })
            text = f"⚠️ Error: {type(e).__name__}: {e}"

        if not isinstance(text, str) or not text.strip():
            text = "⚠️ Model returned an empty response."

        runtime_sec = time.time() - start_time
        cost_usd = float(usage.get("cost_usd") or 0.0)
        rounds = int(usage.get("rounds") or 0)

        # --- Mandatory TG notification ---
        if self._current_chat_id:
            task_desc = str(task.get("text") or "")[:100]
            summary = (
                f"✅ Focused task `{task_id}` done in {int(runtime_sec)}s "
                f"· {rounds} rounds · ${cost_usd:.3f}\n\n"
                f"**Task:** {task_desc}\n\n"
                f"**Result:**\n{text[:1500]}"
            )
            self._pending_events.append({
                "type": "send_message",
                "chat_id": self._current_chat_id,
                "text": summary,
                "format": "markdown",
                "is_progress": False,
                "ts": utc_now_iso(),
            })

        # Store result
        try:
            results_dir = self.drive_root / "task_results"
            results_dir.mkdir(parents=True, exist_ok=True)
            result_file = results_dir / f"{task_id}.json"
            result_data = {
                "task_id": task_id,
                "status": "completed",
                "result": text,
                "cost_usd": cost_usd,
                "rounds": rounds,
                "runtime_sec": round(runtime_sec, 1),
                "ts": utc_now_iso(),
            }
            tmp = results_dir / f"{task_id}.json.tmp"
            tmp.write_text(json.dumps(result_data, ensure_ascii=False), encoding="utf-8")
            os.rename(str(tmp), str(result_file))
        except Exception as e:
            log.warning("Failed to store focused task result: %s", e)

        # --- Emit task_done for supervisor ---
        self._pending_events.append({
            "type": "task_done",
            "task_id": task_id,
            "task_type": "focused",
            "ok": True,
            "total_rounds": rounds,
            "cost_usd": cost_usd,
            "response_len": len(text),
            "response_text": text[:500],
            "ts": utc_now_iso(),
        })

        return list(self._pending_events)
