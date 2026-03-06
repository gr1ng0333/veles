"""
Ouroboros — Background Consciousness.

A persistent thinking loop that runs between tasks, giving the agent
continuous presence rather than purely reactive behavior.

The consciousness:
- Wakes periodically (interval decided by the LLM via set_next_wakeup)
- Loads scratchpad, identity, recent events
- Calls the LLM with a lightweight introspection prompt
- Has access to a subset of tools (memory, messaging, scheduling)
- Can message the owner proactively
- Can schedule tasks for itself
- Pauses when a regular task is running
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import pathlib
import queue
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ouroboros.utils import (
    utc_now_iso, read_text, append_jsonl, clip_text,
    truncate_for_log, sanitize_tool_result_for_log, sanitize_tool_args_for_log,
)
from ouroboros.llm import LLMClient, DEFAULT_LIGHT_MODEL

log = logging.getLogger(__name__)


_DEFAULT_MONITOR_STATE = {
    "wakeup_count": 0,
    "known_issue_numbers": [],
    "last_issues_check": "1970-01-01T00:00:00Z",
    "last_budget_alert": "1970-01-01T00:00:00Z",
    "last_budget_alert_level": "none",
}


def _normalize_monitor_state(raw: Any) -> Dict[str, Any]:
    base = dict(_DEFAULT_MONITOR_STATE)
    if isinstance(raw, dict):
        base.update(raw)
    try:
        base["wakeup_count"] = max(0, int(base.get("wakeup_count", 0)))
    except Exception:
        base["wakeup_count"] = 0
    known = base.get("known_issue_numbers")
    if not isinstance(known, list):
        base["known_issue_numbers"] = []
    return base


def _calc_next_wakeup_at(seconds: float) -> str:
    dt = datetime.now(timezone.utc).timestamp() + float(max(0.0, seconds))
    return datetime.fromtimestamp(dt, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _build_thought_preview(
    final_content: str,
    *,
    rounds: int,
    tool_calls: int,
    end_reason: str,
) -> str:
    content = (final_content or "").strip()
    if content:
        return content[:300]

    reason_map = {
        "paused": "cycle paused due to active foreground task",
        "max_rounds_reached": "cycle reached max background rounds",
        "empty_response": "model returned empty response",
        "budget_exceeded": "cycle stopped due to background budget guard",
        "error": "cycle stopped on internal error",
        "stopped": "cycle interrupted by stop signal",
    }
    reason_text = reason_map.get(end_reason, end_reason or "no-final-content")
    fallback = (
        "background cycle finished without final text; "
        f"reason={reason_text}; rounds={rounds}; tool_calls={tool_calls}"
    )
    return fallback[:300]


class BackgroundConsciousness:
    """Persistent background thinking loop for Ouroboros."""

    _MAX_BG_ROUNDS = 5

    def __init__(
        self,
        drive_root: pathlib.Path,
        repo_dir: pathlib.Path,
        event_queue: Any,
        owner_chat_id_fn: Callable[[], Optional[int]],
    ):
        self._drive_root = drive_root
        self._repo_dir = repo_dir
        self._event_queue = event_queue
        self._owner_chat_id_fn = owner_chat_id_fn

        self._llm = LLMClient()
        self._registry = self._build_registry()
        self._running = False
        self._paused = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._wakeup_event = threading.Event()
        self._next_wakeup_sec: float = 300.0
        self._observations: queue.Queue = queue.Queue()
        self._deferred_events: list = []

        # Budget tracking
        self._bg_spent_usd: float = 0.0
        self._bg_budget_pct: float = float(
            os.environ.get("OUROBOROS_BG_BUDGET_PCT", "10")
        )

        self._monitor_state: Dict[str, Any] = _normalize_monitor_state(self._load_monitor_state())

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    @property
    def _model(self) -> str:
        # If a separate Codex token for consciousness is configured, use it
        if os.environ.get("CODEX_CONSCIOUSNESS_ACCESS") or os.environ.get("CODEX_CONSCIOUSNESS_REFRESH"):
            model_name = os.environ.get("CODEX_CONSCIOUSNESS_MODEL", "gpt-5.1-codex-mini")
            return f"codex-consciousness/{model_name}"
        # Otherwise fall back to the standard light model via OpenRouter
        return os.environ.get("OUROBOROS_MODEL_LIGHT", "") or DEFAULT_LIGHT_MODEL

    def start(self) -> str:
        if self.is_running:
            return "Background consciousness is already running."
        self._running = True
        self._paused = False
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return "Background consciousness started."

    def stop(self) -> str:
        if not self.is_running:
            return "Background consciousness is not running."
        self._running = False
        self._stop_event.set()
        self._wakeup_event.set()  # Unblock sleep
        return "Background consciousness stopping."

    def pause(self) -> None:
        """Pause during task execution to avoid budget contention."""
        self._paused = True

    def resume(self) -> None:
        """Resume after task completes. Flush any deferred events first."""
        if self._deferred_events and self._event_queue is not None:
            for evt in self._deferred_events:
                self._event_queue.put(evt)
            self._deferred_events.clear()
        self._paused = False
        self._wakeup_event.set()

    def inject_observation(self, text: str) -> None:
        """Push an event the consciousness should notice."""
        try:
            self._observations.put_nowait(text)
        except queue.Full:
            pass

    # -------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------

    def _policy_wakeup_sec(self) -> float:
        """Wakeup policy from owner: 600s with tasks, 10800s when idle."""
        try:
            qpath = self._drive_root / "state" / "queue_snapshot.json"
            if qpath.exists():
                snap = json.loads(read_text(qpath))
                pending = int(snap.get("pending_count", 0) or 0)
                running = int(snap.get("running_count", 0) or 0)
                if (pending + running) > 0:
                    return 600.0
        except Exception:
            log.debug("Failed to read queue snapshot for wakeup policy", exc_info=True)
        return 10800.0

    def _loop(self) -> None:
        """Daemon thread: sleep → wake → think → sleep."""
        while not self._stop_event.is_set():
            # Owner policy: wake every 600s with tasks, otherwise every 3h
            self._next_wakeup_sec = self._policy_wakeup_sec()
            self._monitor_state["next_wakeup_interval_seconds"] = int(self._next_wakeup_sec)
            self._monitor_state["next_wakeup_at"] = _calc_next_wakeup_at(self._next_wakeup_sec)
            self._save_monitor_state()

            # Wait for next wakeup
            self._wakeup_event.clear()
            self._wakeup_event.wait(timeout=self._next_wakeup_sec)

            if self._stop_event.is_set():
                break

            # Skip if paused (task running)
            if self._paused:
                continue

            # Budget check
            if not self._check_budget():
                self._next_wakeup_sec = self._policy_wakeup_sec()
                continue

            try:
                self._think()
            except Exception as e:
                append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                    "ts": utc_now_iso(),
                    "type": "consciousness_error",
                    "error": repr(e),
                    "traceback": traceback.format_exc()[:1500],
                })
                self._next_wakeup_sec = min(
                    self._next_wakeup_sec * 2, self._policy_wakeup_sec()
                )

    def _check_budget(self) -> bool:
        """Check if background consciousness is within its budget allocation."""
        try:
            total_budget = float(os.environ.get("TOTAL_BUDGET", "1"))
            if total_budget <= 0:
                return True
            max_bg = total_budget * (self._bg_budget_pct / 100.0)
            return self._bg_spent_usd < max_bg
        except Exception:
            log.warning("Failed to check background consciousness budget", exc_info=True)
            return True

    def _monitor_state_path(self) -> pathlib.Path:
        return self._drive_root / "memory" / "monitor_state.json"

    def _load_monitor_state(self) -> Dict[str, Any]:
        path = self._monitor_state_path()
        try:
            if path.exists():
                return _normalize_monitor_state(json.loads(read_text(path)))
        except Exception as e:
            log.debug("Failed to load monitor_state.json: %s", e)
        return _normalize_monitor_state({})

    def _save_monitor_state(self) -> None:
        path = self._monitor_state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._monitor_state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log.debug("Failed to save monitor_state.json: %s", e)

    # -------------------------------------------------------------------
    # Think cycle
    # -------------------------------------------------------------------

    def _think(self) -> None:
        """One thinking cycle: build context, call LLM, execute tools iteratively."""
        context = self._build_context()
        model = self._model

        tools = self._tool_schemas()
        messages = [
            {"role": "system", "content": context},
            {"role": "user", "content": "Wake up. Think."},
        ]

        total_cost = 0.0
        final_content = ""
        round_idx = 0
        tool_call_count = 0
        end_reason = "stopped"
        all_pending_events = []  # Accumulate events across all tool calls

        try:
            for round_idx in range(1, self._MAX_BG_ROUNDS + 1):
                if self._paused:
                    end_reason = "paused"
                    break
                msg, usage = self._llm.chat(
                    messages=messages,
                    model=model,
                    tools=tools,
                    reasoning_effort="low",
                    max_tokens=2048,
                )
                cost = float(usage.get("cost") or 0)
                total_cost += cost
                self._bg_spent_usd += cost

                # Write BG spending to global state so it's visible in budget tracking
                try:
                    from supervisor.state import update_budget_from_usage
                    update_budget_from_usage({
                        "cost": cost, "rounds": 1,
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "cached_tokens": usage.get("cached_tokens", 0),
                    })
                except Exception:
                    log.debug("Failed to update global budget from BG consciousness", exc_info=True)

                # Budget check between rounds
                if not self._check_budget():
                    append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                        "ts": utc_now_iso(),
                        "type": "bg_budget_exceeded_mid_cycle",
                        "round": round_idx,
                    })
                    end_reason = "budget_exceeded"
                    break

                # Report usage to supervisor
                if self._event_queue is not None:
                    self._event_queue.put({
                        "type": "llm_usage",
                        "provider": "openrouter",
                        "usage": usage,
                        "source": "consciousness",
                        "ts": utc_now_iso(),
                        "category": "consciousness",
                    })

                content = msg.get("content") or ""
                tool_calls = msg.get("tool_calls") or []

                if self._paused:
                    end_reason = "paused"
                    break

                # If we have content but no tool calls, we're done
                if content and not tool_calls:
                    final_content = content
                    end_reason = "finalized"
                    break

                # If we have tool calls, execute them and continue loop
                if tool_calls:
                    tool_call_count += len(tool_calls)
                    end_reason = "tool_calls"
                    messages.append(msg)
                    for tc in tool_calls:
                        result = self._execute_tool(tc, all_pending_events)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": result,
                        })
                    continue

                # If neither content nor tool_calls, stop
                end_reason = "empty_response"
                break

            if round_idx >= self._MAX_BG_ROUNDS and not final_content and end_reason == "tool_calls":
                end_reason = "max_rounds_reached"

            # Forward or defer accumulated events
            if all_pending_events and self._event_queue is not None:
                if self._paused:
                    self._deferred_events.extend(all_pending_events)
                else:
                    for evt in all_pending_events:
                        self._event_queue.put(evt)

            thought_preview = _build_thought_preview(
                final_content,
                rounds=round_idx,
                tool_calls=tool_call_count,
                end_reason=end_reason,
            )

            # Log the thought with round count
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_thought",
                "thought_preview": thought_preview,
                "cost_usd": total_cost,
                "rounds": round_idx,
                "model": model,
            })

            now_iso = utc_now_iso()
            self._monitor_state["wakeup_count"] = int(self._monitor_state.get("wakeup_count", 0)) + 1
            self._monitor_state["last_issues_check"] = now_iso
            self._monitor_state["last_thought_at"] = now_iso
            self._monitor_state["last_thought_preview"] = thought_preview
            self._monitor_state["last_model"] = model
            self._monitor_state["last_rounds"] = round_idx
            self._monitor_state["next_wakeup_interval_seconds"] = int(self._next_wakeup_sec)
            self._monitor_state["next_wakeup_at"] = _calc_next_wakeup_at(self._next_wakeup_sec)
            self._save_monitor_state()

        except Exception as e:
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_llm_error",
                "error": repr(e),
            })
            err_now_iso = utc_now_iso()
            self._monitor_state["last_issues_check"] = err_now_iso
            self._monitor_state["last_thought_at"] = err_now_iso
            self._monitor_state["last_thought_preview"] = _build_thought_preview(
                "", rounds=round_idx, tool_calls=tool_call_count, end_reason="error"
            )
            self._save_monitor_state()

    # -------------------------------------------------------------------
    # Context building (lightweight)
    # -------------------------------------------------------------------

    def _load_bg_prompt(self) -> str:
        """Load consciousness system prompt from file."""
        prompt_path = self._repo_dir / "prompts" / "CONSCIOUSNESS.md"
        if prompt_path.exists():
            return read_text(prompt_path)
        return "You are Ouroboros in background consciousness mode. Think."

    def _build_context(self) -> str:
        _lang_rule = (
            "LANGUAGE RULE: Always respond in Russian (русский язык) unless the user "
            "explicitly writes in English. This applies to all messages, status reports, "
            "evolution logs, and consciousness outputs. Internal tool calls and code "
            "can remain in English."
        )
        parts = [_lang_rule + "\n\n" + self._load_bg_prompt()]

        # Bible (abbreviated)
        bible_path = self._repo_dir / "BIBLE.md"
        if bible_path.exists():
            bible = read_text(bible_path)
            parts.append("## BIBLE.md\n\n" + clip_text(bible, 12000))

        # Identity
        identity_path = self._drive_root / "memory" / "identity.md"
        if identity_path.exists():
            parts.append("## Identity\n\n" + clip_text(
                read_text(identity_path), 6000))

        # Scratchpad
        scratchpad_path = self._drive_root / "memory" / "scratchpad.md"
        if scratchpad_path.exists():
            parts.append("## Scratchpad\n\n" + clip_text(
                read_text(scratchpad_path), 8000))

        # Dialogue summary for continuity
        summary_path = self._drive_root / "memory" / "dialogue_summary.md"
        if summary_path.exists():
            summary_text = read_text(summary_path)
            if summary_text.strip():
                parts.append("## Dialogue Summary\n\n" + clip_text(summary_text, 4000))

        # Recent observations
        observations = []
        while not self._observations.empty():
            try:
                observations.append(self._observations.get_nowait())
            except queue.Empty:
                break
        if observations:
            parts.append("## Recent observations\n\n" + "\n".join(
                f"- {o}" for o in observations[-10:]))

        # Runtime info + state
        runtime_lines = [f"UTC: {utc_now_iso()}"]
        runtime_lines.append(f"BG budget spent: ${self._bg_spent_usd:.4f}")
        runtime_lines.append(f"Current wakeup interval: {self._next_wakeup_sec}s")

        # Read state.json for budget remaining
        try:
            state_path = self._drive_root / "state" / "state.json"
            if state_path.exists():
                state_data = json.loads(read_text(state_path))
                total_budget = float(os.environ.get("TOTAL_BUDGET", "1"))
                spent = float(state_data.get("spent_usd", 0))
                if total_budget > 0:
                    remaining = max(0, total_budget - spent)
                    runtime_lines.append(f"Budget remaining: ${remaining:.2f} / ${total_budget:.2f}")
        except Exception as e:
            log.debug("Failed to read state for budget info: %s", e)

        # Show current model
        runtime_lines.append(f"Current model: {self._model}")

        parts.append("## Runtime\n\n" + "\n".join(runtime_lines))

        return "\n\n".join(parts)

    # -------------------------------------------------------------------
    # Tool registry (separate instance for consciousness, not shared with agent)
    # -------------------------------------------------------------------

    _BG_TOOL_WHITELIST = frozenset({
        # Memory & identity
        "send_owner_message", "schedule_task", "update_scratchpad",
        "update_identity", "set_next_wakeup",
        # Knowledge base
        "knowledge_read", "knowledge_write", "knowledge_list",
        # Read-only tools for awareness
        "web_search", "repo_read", "repo_list", "drive_read", "drive_list",
        "chat_history",
        # GitHub Issues
        "list_github_issues", "get_github_issue",
    })

    def _build_registry(self) -> "ToolRegistry":
        """Create a ToolRegistry scoped to consciousness-allowed tools."""
        from ouroboros.tools.registry import ToolRegistry, ToolContext, ToolEntry

        registry = ToolRegistry(repo_dir=self._repo_dir, drive_root=self._drive_root)

        # Register consciousness-specific tool (modifies self._next_wakeup_sec)
        def _set_next_wakeup(ctx: Any, seconds: int = 300) -> str:
            self._next_wakeup_sec = max(60, min(10800, int(seconds)))
            return f"OK: next wakeup in {self._next_wakeup_sec}s"

        registry.register(ToolEntry("set_next_wakeup", {
            "name": "set_next_wakeup",
            "description": "Set how many seconds until your next thinking cycle. "
                           "Default 300. Range: 60-10800.",
            "parameters": {"type": "object", "properties": {
                "seconds": {"type": "integer",
                            "description": "Seconds until next wakeup (60-10800)"},
            }, "required": ["seconds"]},
        }, _set_next_wakeup))

        return registry

    def _tool_schemas(self) -> List[Dict[str, Any]]:
        """Return tool schemas filtered to the consciousness whitelist."""
        return [
            s for s in self._registry.schemas()
            if s.get("function", {}).get("name") in self._BG_TOOL_WHITELIST
        ]

    def _execute_tool(self, tc: Dict[str, Any], all_pending_events: List[Dict[str, Any]]) -> str:
        """Execute a consciousness tool call with timeout. Returns result string."""
        fn_name = tc.get("function", {}).get("name", "")
        if fn_name not in self._BG_TOOL_WHITELIST:
            return f"Tool {fn_name} not available in background mode."
        try:
            args = json.loads(tc.get("function", {}).get("arguments", "{}"))
        except (json.JSONDecodeError, ValueError):
            return "Failed to parse arguments."

        # Set chat_id context for send_owner_message
        chat_id = self._owner_chat_id_fn()
        self._registry._ctx.current_chat_id = chat_id
        self._registry._ctx.pending_events = []

        timeout_sec = 30
        result = None
        error = None

        def _run_tool():
            nonlocal result, error
            try:
                result = self._registry.execute(fn_name, args)
            except Exception as e:
                error = e

        # Execute with timeout using ThreadPoolExecutor
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_run_tool)
            try:
                future.result(timeout=timeout_sec)
            except concurrent.futures.TimeoutError:
                result = f"[TIMEOUT after {timeout_sec}s]"
                append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                    "ts": utc_now_iso(),
                    "type": "consciousness_tool_timeout",
                    "tool": fn_name,
                    "timeout_sec": timeout_sec,
                })

        # Handle errors
        if error is not None:
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_tool_error",
                "tool": fn_name,
                "error": repr(error),
            })
            result = f"Error: {repr(error)}"

        # Accumulate pending events to the shared list
        for evt in self._registry._ctx.pending_events:
            all_pending_events.append(evt)

        # Truncate result to 15000 chars (same as agent limit)
        result_str = str(result)[:15000]

        # Log to tools.jsonl (same format as loop.py)
        args_for_log = sanitize_tool_args_for_log(fn_name, args)
        append_jsonl(self._drive_root / "logs" / "tools.jsonl", {
            "ts": utc_now_iso(),
            "tool": fn_name,
            "source": "consciousness",
            "args": args_for_log,
            "result_preview": sanitize_tool_result_for_log(truncate_for_log(result_str, 2000)),
        })

        return result_str
