"""
Ouroboros — LLM tool loop.

Core loop: send messages to LLM, execute tool calls, repeat until final response.
Extracted from agent.py to keep the agent thin.
"""

from __future__ import annotations

import json
import os
import pathlib
import queue
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from typing import Any, Callable, Dict, List, Optional, Tuple

import logging

from ouroboros.llm import LLMClient, normalize_reasoning_effort, add_usage, model_transport, transport_model_name
from ouroboros.tools.registry import ToolRegistry
from ouroboros.context import compact_tool_history, compact_tool_history_llm
from ouroboros.pricing import estimate_cost as _estimate_cost
from ouroboros.utils import utc_now_iso, append_jsonl, truncate_for_log, sanitize_tool_args_for_log, sanitize_tool_result_for_log, estimate_tokens, sanitize_owner_facing_text
from ouroboros.antistagnation import (
    build_forced_finalize_reason,
    inject_stagnation_self_check,
    load_antistagnation_config,
    should_force_round_finalize,
    stagnation_action,
)

log = logging.getLogger(__name__)

READ_ONLY_PARALLEL_TOOLS = frozenset({
    "repo_read", "repo_list",
    "drive_read", "drive_list",
    "web_search", "codebase_digest", "chat_history",
})

# Stateful browser tools require thread-affinity (Playwright sync uses greenlet)
STATEFUL_BROWSER_TOOLS = frozenset({"browse_page", "browser_action", "send_browser_screenshot"})

# Default tool result cap (used for Copilot, OpenRouter, and unknown transports)
_TOOL_RESULT_MAX = 15_000

# Aggressive caps for Codex transport (no prompt caching → every token costs full price every round)
_CODEX_TOOL_RESULT_CAPS = {
    "run_shell":          2_000,
    "git_diff":           4_000,
    "git_status":         1_500,
    "repo_read":          3_000,
    "repo_list":          1_500,
    "repo_write_commit":  1_000,
    "repo_commit_push":   1_000,
    "web_search":         3_000,
    "research_run":       3_000,
    "deep_research":      4_000,
    "browse_page":        3_000,
    "codebase_digest":    4_000,
    "codebase_health":    2_000,
    "vps_health_check":   2_000,
    "multi_model_review": 4_000,
    "_default":           2_500,
}

def _truncate_tool_result(result: Any, tool_name: str = "", transport: str = "") -> str:
    """
    Hard-cap tool result string.
    For Codex transport uses per-tool aggressive caps (no prompt caching).
    For other transports uses the default 15,000 char cap.
    """
    result_str = str(result)
    if transport == "codex":
        cap = _CODEX_TOOL_RESULT_CAPS.get(tool_name, _CODEX_TOOL_RESULT_CAPS["_default"])
    else:
        cap = _TOOL_RESULT_MAX
    if len(result_str) <= cap:
        return result_str
    original_len = len(result_str)
    return result_str[:cap] + f"\n... (truncated from {original_len} to {cap} chars [{transport or 'default'}])"

def _execute_single_tool(
    tools: ToolRegistry,
    tc: Dict[str, Any],
    drive_logs: pathlib.Path,
    task_id: str = "",
) -> Dict[str, Any]:
    """
    Execute a single tool call and return all needed info.

    Returns dict with: tool_call_id, fn_name, result, is_error, args_for_log, is_code_tool
    """
    fn_name = tc["function"]["name"]
    tool_call_id = tc["id"]
    is_code_tool = fn_name in tools.CODE_TOOLS

    # Parse arguments
    try:
        args = json.loads(tc["function"]["arguments"] or "{}")
    except (json.JSONDecodeError, ValueError) as e:
        result = f"⚠️ TOOL_ARG_ERROR: Could not parse arguments for '{fn_name}': {e}"
        return {
            "tool_call_id": tool_call_id,
            "fn_name": fn_name,
            "result": result,
            "is_error": True,
            "args_for_log": {},
            "is_code_tool": is_code_tool,
        }

    args_for_log = sanitize_tool_args_for_log(fn_name, args if isinstance(args, dict) else {})

    # Execute tool
    tool_ok = True
    try:
        result = tools.execute(fn_name, args)
    except Exception as e:
        tool_ok = False
        result = f"⚠️ TOOL_ERROR ({fn_name}): {type(e).__name__}: {e}"
        append_jsonl(drive_logs / "events.jsonl", {
            "ts": utc_now_iso(), "type": "tool_error", "task_id": task_id,
            "tool": fn_name, "args": args_for_log, "error": repr(e),
        })

    # Log tool execution (sanitize secrets from result before persisting)
    append_jsonl(drive_logs / "tools.jsonl", {
        "ts": utc_now_iso(), "tool": fn_name, "task_id": task_id,
        "args": args_for_log,
        "result_preview": sanitize_tool_result_for_log(truncate_for_log(result, 2000)),
    })

    is_error = (not tool_ok) or str(result).startswith("⚠️")

    return {
        "tool_call_id": tool_call_id,
        "fn_name": fn_name,
        "result": result,
        "is_error": is_error,
        "args_for_log": args_for_log,
        "is_code_tool": is_code_tool,
    }


class _StatefulToolExecutor:
    """Sticky single-thread executor for stateful browser tools."""
    def __init__(self):
        self._executor: Optional[ThreadPoolExecutor] = None

    def submit(self, fn, *args, **kwargs):
        """Submit work to the sticky thread. Creates executor on first call."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stateful_tool")
        return self._executor.submit(fn, *args, **kwargs)

    def reset(self):
        """Shutdown current executor and create a fresh one. Used after timeout/error."""
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def shutdown(self, wait=True, cancel_futures=False):
        """Final cleanup."""
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
            self._executor = None

def _tool_result_from_error(
    *,
    fn_name: str,
    tool_call_id: str,
    is_code_tool: bool,
    tc: Dict[str, Any],
    drive_logs: pathlib.Path,
    event_type: str,
    result_text: str,
    task_id: str = "",
    extra_event_fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    args_for_log = {}
    try:
        args = json.loads(tc["function"]["arguments"] or "{}")
        args_for_log = sanitize_tool_args_for_log(fn_name, args if isinstance(args, dict) else {})
    except Exception:
        pass

    event = {
        "ts": utc_now_iso(),
        "type": event_type,
        "task_id": task_id,
        "tool": fn_name,
        "args": args_for_log,
    }
    if extra_event_fields:
        event.update(extra_event_fields)
    append_jsonl(drive_logs / "events.jsonl", event)
    append_jsonl(drive_logs / "tools.jsonl", {
        "ts": utc_now_iso(), "tool": fn_name, "task_id": task_id,
        "args": args_for_log, "result_preview": result_text,
    })

    return {
        "tool_call_id": tool_call_id,
        "fn_name": fn_name,
        "result": result_text,
        "is_error": True,
        "args_for_log": args_for_log,
        "is_code_tool": is_code_tool,
    }

def _make_timeout_result(
    fn_name: str,
    tool_call_id: str,
    is_code_tool: bool,
    tc: Dict[str, Any],
    drive_logs: pathlib.Path,
    timeout_sec: int,
    task_id: str = "",
    reset_msg: str = "",
) -> Dict[str, Any]:
    """
    Create a timeout error result dictionary and log the timeout event.

    Args:
        reset_msg: Optional additional message (e.g., "Browser state has been reset. ")

    Returns: Dict with tool_call_id, fn_name, result, is_error, args_for_log, is_code_tool
    """
    result = (
        f"⚠️ TOOL_TIMEOUT ({fn_name}): exceeded {timeout_sec}s limit. "
        f"The tool is still running in background but control is returned to you. "
        f"{reset_msg}"
        f"Consider: shorter input, a different approach, or breaking the task into steps."
    )
    return _tool_result_from_error(
        fn_name=fn_name,
        tool_call_id=tool_call_id,
        is_code_tool=is_code_tool,
        tc=tc,
        drive_logs=drive_logs,
        event_type="tool_timeout",
        result_text=result,
        task_id=task_id,
        extra_event_fields={"timeout_sec": timeout_sec},
    )

def _make_execution_error_result(
    *,
    fn_name: str,
    tool_call_id: str,
    is_code_tool: bool,
    tc: Dict[str, Any],
    drive_logs: pathlib.Path,
    task_id: str = "",
    stage: str,
    exc: Exception,
    reset_msg: str = "",
) -> Dict[str, Any]:
    result = (
        f"⚠️ TOOL_EXECUTION_ERROR ({fn_name}): {type(exc).__name__}: {exc}. "
        f"Execution failed during {stage}. {reset_msg}"
        "The task loop stayed alive; choose a narrower tool call, another tool, or report the issue."
    )
    return _tool_result_from_error(
        fn_name=fn_name,
        tool_call_id=tool_call_id,
        is_code_tool=is_code_tool,
        tc=tc,
        drive_logs=drive_logs,
        event_type="tool_execution_error",
        result_text=result,
        task_id=task_id,
        extra_event_fields={
            "stage": stage,
            "error_type": type(exc).__name__,
            "error": repr(exc),
        },
    )

def _execute_with_timeout(
    tools: ToolRegistry,
    tc: Dict[str, Any],
    drive_logs: pathlib.Path,
    timeout_sec: int,
    task_id: str = "",
    stateful_executor: Optional[_StatefulToolExecutor] = None,
) -> Dict[str, Any]:
    """Execute one tool with hard timeout and degrade to structured error on failure."""
    fn_name = tc["function"]["name"]
    tool_call_id = tc["id"]
    is_code_tool = fn_name in tools.CODE_TOOLS
    use_stateful = stateful_executor and fn_name in STATEFUL_BROWSER_TOOLS

    # Two distinct paths: stateful (thread-sticky) vs regular (per-call)
    if use_stateful:
        # Stateful executor: submit + wait, reset on timeout/unexpected executor failures
        try:
            future = stateful_executor.submit(_execute_single_tool, tools, tc, drive_logs, task_id)
        except Exception as exc:
            stateful_executor.reset()
            return _make_execution_error_result(
                fn_name=fn_name,
                tool_call_id=tool_call_id,
                is_code_tool=is_code_tool,
                tc=tc,
                drive_logs=drive_logs,
                task_id=task_id,
                stage="submit",
                exc=exc,
                reset_msg="Browser state has been reset. ",
            )
        try:
            return future.result(timeout=timeout_sec)
        except FuturesTimeoutError:
            stateful_executor.reset()
            reset_msg = "Browser state has been reset. "
            return _make_timeout_result(
                fn_name, tool_call_id, is_code_tool, tc, drive_logs,
                timeout_sec, task_id, reset_msg
            )
        except Exception as exc:
            stateful_executor.reset()
            return _make_execution_error_result(
                fn_name=fn_name,
                tool_call_id=tool_call_id,
                is_code_tool=is_code_tool,
                tc=tc,
                drive_logs=drive_logs,
                task_id=task_id,
                stage="result",
                exc=exc,
                reset_msg="Browser state has been reset. ",
            )
    else:
        # Regular executor: explicit lifecycle to avoid shutdown(wait=True) deadlock
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            try:
                future = executor.submit(_execute_single_tool, tools, tc, drive_logs, task_id)
            except Exception as exc:
                return _make_execution_error_result(
                    fn_name=fn_name,
                    tool_call_id=tool_call_id,
                    is_code_tool=is_code_tool,
                    tc=tc,
                    drive_logs=drive_logs,
                    task_id=task_id,
                    stage="submit",
                    exc=exc,
                )
            try:
                return future.result(timeout=timeout_sec)
            except FuturesTimeoutError:
                return _make_timeout_result(
                    fn_name, tool_call_id, is_code_tool, tc, drive_logs,
                    timeout_sec, task_id, reset_msg=""
                )
            except Exception as exc:
                return _make_execution_error_result(
                    fn_name=fn_name,
                    tool_call_id=tool_call_id,
                    is_code_tool=is_code_tool,
                    tc=tc,
                    drive_logs=drive_logs,
                    task_id=task_id,
                    stage="result",
                    exc=exc,
                )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

def _handle_tool_calls(
    tool_calls: List[Dict[str, Any]],
    tools: ToolRegistry,
    drive_logs: pathlib.Path,
    task_id: str,
    stateful_executor: _StatefulToolExecutor,
    messages: List[Dict[str, Any]],
    llm_trace: Dict[str, Any],
    emit_progress: Callable[[str], None],
    transport: str = "",
) -> Tuple[int, bool]:
    """Execute tool calls and append results to messages."""
    # Parallelize only for a strict read-only whitelist; all calls wrapped with timeout.
    can_parallel = (
        len(tool_calls) > 1 and
        all(
            tc.get("function", {}).get("name") in READ_ONLY_PARALLEL_TOOLS
            for tc in tool_calls
        )
    )

    if not can_parallel:
        results = [
            _execute_with_timeout(tools, tc, drive_logs,
                                  tools.get_timeout(tc["function"]["name"]), task_id,
                                  stateful_executor)
            for tc in tool_calls
        ]
    else:
        max_workers = min(len(tool_calls), 8)
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            future_to_index = {
                executor.submit(
                    _execute_with_timeout, tools, tc, drive_logs,
                    tools.get_timeout(tc["function"]["name"]), task_id,
                    stateful_executor,
                ): idx
                for idx, tc in enumerate(tool_calls)
            }
            results = [None] * len(tool_calls)
            for future in as_completed(future_to_index):
                idx = future_to_index[future]
                tc = tool_calls[idx]
                fn_name = tc["function"]["name"]
                tool_call_id = tc["id"]
                is_code_tool = fn_name in tools.CODE_TOOLS
                try:
                    results[idx] = future.result()
                except Exception as exc:
                    results[idx] = _make_execution_error_result(
                        fn_name=fn_name,
                        tool_call_id=tool_call_id,
                        is_code_tool=is_code_tool,
                        tc=tc,
                        drive_logs=drive_logs,
                        task_id=task_id,
                        stage="parallel_result",
                        exc=exc,
                    )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    # Process results in original order
    return _process_tool_results(results, messages, llm_trace, emit_progress, transport=transport)

def _handle_text_response(
    content: Optional[str],
    llm_trace: Dict[str, Any],
    accumulated_usage: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    Handle LLM response without tool calls (final response).

    Returns: (final_text, accumulated_usage, llm_trace)
    """
    final_text = sanitize_owner_facing_text(content or "")
    if final_text.strip():
        llm_trace["assistant_notes"].append(final_text.strip()[:320])
    return final_text, accumulated_usage, llm_trace

def _check_budget_limits(
    budget_remaining_usd: Optional[float],
    accumulated_usage: Dict[str, Any],
    round_idx: int,
    messages: List[Dict[str, Any]],
    llm: LLMClient,
    active_model: str,
    active_effort: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    event_queue: Optional[queue.Queue],
    llm_trace: Dict[str, Any],
    task_type: str = "task",
) -> Optional[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    """
    Check budget limits and handle budget overrun.

    Returns:
        None if budget is OK (continue loop)
        (final_text, accumulated_usage, llm_trace) if budget exceeded (stop loop)
    """
    if budget_remaining_usd is None:
        return None

    task_cost = accumulated_usage.get("cost", 0)
    budget_pct = task_cost / budget_remaining_usd if budget_remaining_usd > 0 else 1.0

    if budget_pct > 0.5:
        # Hard stop — protect the budget
        finish_reason = f"Task spent ${task_cost:.3f} (>50% of remaining ${budget_remaining_usd:.2f}). Budget exhausted."
        messages.append({"role": "system", "content": f"[BUDGET LIMIT] {finish_reason} Give your final response now."})
        try:
            final_msg, final_cost = _call_llm_with_retry(
                llm, messages, active_model, None, active_effort,
                max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type
            )
            if final_msg:
                return (final_msg.get("content") or finish_reason), accumulated_usage, llm_trace
            return finish_reason, accumulated_usage, llm_trace
        except Exception:
            log.warning("Failed to get final response after budget limit", exc_info=True)
            return finish_reason, accumulated_usage, llm_trace
    elif budget_pct > 0.3 and round_idx % 10 == 0:
        # Soft nudge every 10 rounds when spending is significant
        messages.append({"role": "system", "content": f"[INFO] Task spent ${task_cost:.3f} of ${budget_remaining_usd:.2f}. Wrap up if possible."})

    return None

def _maybe_inject_self_check(
    round_idx: int,
    max_rounds: int,
    messages: List[Dict[str, Any]],
    accumulated_usage: Dict[str, Any],
    emit_progress: Callable[[str], None],
) -> None:
    """Inject a soft self-check reminder every REMINDER_INTERVAL rounds.

    This is a cognitive feature (Bible P0: subjectivity) — the agent reflects
    on its own resource usage and strategy, not a hard kill.
    """
    REMINDER_INTERVAL = 50
    if round_idx <= 1 or round_idx % REMINDER_INTERVAL != 0:
        return
    ctx_tokens = sum(
        estimate_tokens(str(m.get("content", "")))
        if isinstance(m.get("content"), str)
        else sum(estimate_tokens(str(b.get("text", ""))) for b in m.get("content", []) if isinstance(b, dict))
        for m in messages
    )
    task_cost = accumulated_usage.get("cost", 0)
    checkpoint_num = round_idx // REMINDER_INTERVAL

    reminder = (
        f"[CHECKPOINT {checkpoint_num} — round {round_idx}/{max_rounds}]\n"
        f"📊 Context: ~{ctx_tokens} tokens | Cost so far: ${task_cost:.2f} | "
        f"Rounds remaining: {max_rounds - round_idx}\n\n"
        f"⏸️ PAUSE AND REFLECT before continuing:\n"
        f"1. Am I making real progress, or repeating the same actions?\n"
        f"2. Is my current strategy working? Should I try something different?\n"
        f"3. Is my context bloated with old tool results I no longer need?\n"
        f"   → If yes, call `compact_context` to summarize them selectively.\n"
        f"4. Have I been stuck on the same sub-problem for many rounds?\n"
        f"   → If yes, consider: simplify the approach, skip the sub-problem, or finish with what I have.\n"
        f"5. Should I just STOP and return my best result so far?\n\n"
        f"This is not a hard limit — you decide. But be honest with yourself."
    )
    messages.append({"role": "system", "content": reminder})
    emit_progress(f"🔄 Checkpoint {checkpoint_num} at round {round_idx}: ~{ctx_tokens} tokens, ${task_cost:.2f} spent")

def _setup_dynamic_tools(tools_registry, tool_schemas, messages):
    """
    Wire tool-discovery handlers onto an existing tool_schemas list.

    Creates closures for list_available_tools / enable_tools, registers them
    as handler overrides, and injects a system message advertising non-core
    tools.  Mutates tool_schemas in-place (via list.append) when tools are
    enabled, so the caller's reference stays live.

    Returns (tool_schemas, enabled_extra_set).
    """
    enabled_extra: set = set()

    def _handle_list_tools(ctx=None, **kwargs):
        non_core = tools_registry.list_non_core_tools()
        if not non_core:
            return "All tools are already in your active set."
        lines = [f"**{len(non_core)} additional tools available** (use `enable_tools` to activate):\n"]
        for t in non_core:
            lines.append(f"- **{t['name']}**: {t['description'][:120]}")
        return "\n".join(lines)

    def _handle_enable_tools(ctx=None, tools: str = "", **kwargs):
        names = [n.strip() for n in tools.split(",") if n.strip()]
        enabled, not_found = [], []
        for name in names:
            schema = tools_registry.get_schema_by_name(name)
            if schema and name not in enabled_extra:
                tool_schemas.append(schema)
                enabled_extra.add(name)
                enabled.append(name)
            elif name in enabled_extra:
                enabled.append(f"{name} (already active)")
            else:
                not_found.append(name)
        parts = []
        if enabled:
            parts.append(f"✅ Enabled: {', '.join(enabled)}")
        if not_found:
            parts.append(f"❌ Not found: {', '.join(not_found)}")
        return "\n".join(parts) if parts else "No tools specified."

    tools_registry.override_handler("list_available_tools", _handle_list_tools)
    tools_registry.override_handler("enable_tools", _handle_enable_tools)

    non_core_count = len(tools_registry.list_non_core_tools())
    if non_core_count > 0:
        messages.append({
            "role": "system",
            "content": (
                f"Note: You have {len(tool_schemas)} core tools loaded. "
                f"There are {non_core_count} additional tools available "
                f"(use `list_available_tools` to see them, `enable_tools` to activate). "
                f"Core tools cover most tasks. Enable extras only when needed."
            ),
        })

    return tool_schemas, enabled_extra

def _drain_incoming_messages(
    messages: List[Dict[str, Any]],
    incoming_messages: queue.Queue,
    drive_root: Optional[pathlib.Path],
    task_id: str,
    event_queue: Optional[queue.Queue],
    _owner_msg_seen: set,
) -> None:
    """
    Inject owner messages received during task execution.
    Drains both the in-process queue and the Drive mailbox.
    """
    # Inject owner messages received during task execution
    while not incoming_messages.empty():
        try:
            injected = incoming_messages.get_nowait()
            messages.append({"role": "user", "content": injected})
        except queue.Empty:
            break

    # Drain per-task owner messages from Drive mailbox (written by forward_to_worker tool)
    if drive_root is not None and task_id:
        from ouroboros.owner_inject import drain_owner_messages
        drive_msgs = drain_owner_messages(drive_root, task_id=task_id, seen_ids=_owner_msg_seen)
        for dmsg in drive_msgs:
            messages.append({
                "role": "user",
                "content": f"[Owner message during task]: {dmsg}",
            })
            # Log for duplicate processing detection (health invariant #5)
            if event_queue is not None:
                try:
                    event_queue.put_nowait({
                        "type": "owner_message_injected",
                        "task_id": task_id,
                        "text": dmsg[:200],
                    })
                except Exception:
                    pass

def _finalize_with_summary(
    *,
    reason: str,
    messages: List[Dict[str, Any]],
    llm: LLMClient,
    active_model: str,
    active_effort: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    round_idx: int,
    event_queue: Optional[queue.Queue],
    accumulated_usage: Dict[str, Any],
    task_type: str,
    tool_schemas: Optional[List[Dict[str, Any]]] = None,
    interaction_id: Optional[str] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    messages.append({"role": "system", "content": f"[FORCED_FINALIZE] {reason}"})
    try:
        final_msg, _ = _call_llm_with_retry(
            llm, messages, active_model, tool_schemas, active_effort,
            max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type,
            interaction_id=interaction_id,
        )
        if final_msg and (final_msg.get("content") or "").strip():
            return final_msg.get("content") or reason, accumulated_usage, {"assistant_notes": [], "tool_calls": []}
    except Exception:
        log.warning("Forced finalize failed", exc_info=True)
    return reason, accumulated_usage, {"assistant_notes": [], "tool_calls": []}

def run_llm_loop(
    messages: List[Dict[str, Any]],
    tools: ToolRegistry,
    llm: LLMClient,
    drive_logs: pathlib.Path,
    emit_progress: Callable[[str], None],
    incoming_messages: queue.Queue,
    task_type: str = "",
    task_id: str = "",
    budget_remaining_usd: Optional[float] = None,
    event_queue: Optional[queue.Queue] = None,
    initial_effort: str = "medium",
    drive_root: Optional[pathlib.Path] = None,
    persistent_executor: Optional[_StatefulToolExecutor] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """Thin wrapper delegating runtime loop implementation (keeps module compact)."""
    from ouroboros.loop_runtime import run_llm_loop_impl

    return run_llm_loop_impl(
        messages=messages,
        tools=tools,
        llm=llm,
        drive_logs=drive_logs,
        emit_progress=emit_progress,
        incoming_messages=incoming_messages,
        task_type=task_type,
        task_id=task_id,
        budget_remaining_usd=budget_remaining_usd,
        event_queue=event_queue,
        initial_effort=initial_effort,
        drive_root=drive_root,
        persistent_executor=persistent_executor,
    )

def _emit_llm_usage_event(
    event_queue: Optional[queue.Queue],
    task_id: str,
    model: str,
    usage: Dict[str, Any],
    cost: float,
    category: str = "task",
) -> None:
    """
    Emit llm_usage event to the event queue.

    Args:
        event_queue: Queue to emit events to (may be None)
        task_id: Task ID for the event
        model: Model name used for the LLM call
        usage: Usage dict from LLM response
        cost: Calculated cost for this call
        category: Budget category (task, evolution, consciousness, review, summarize, other)
    """
    if not event_queue:
        return
    try:
        event_queue.put_nowait({
            "type": "llm_usage",
            "ts": utc_now_iso(),
            "task_id": task_id,
            "model": model,
            "requested_model": model,
            "transport": model_transport(model),
            "actual_model": transport_model_name(model),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "cached_tokens": int(usage.get("cached_tokens") or 0),
            "cache_write_tokens": int(usage.get("cache_write_tokens") or 0),
            "cost": cost,
            "cost_estimated": not bool(usage.get("cost")),
            "usage": usage,
            "category": category,
        })
    except Exception:
        log.debug("Failed to put llm_usage event to queue", exc_info=True)

def _call_llm_with_retry(
    llm: LLMClient,
    messages: List[Dict[str, Any]],
    model: str,
    tools: Optional[List[Dict[str, Any]]],
    effort: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    round_idx: int,
    event_queue: Optional[queue.Queue],
    accumulated_usage: Dict[str, Any],
    task_type: str = "",
    interaction_id: Optional[str] = None,
    force_user_initiator: bool = False,
) -> Tuple[Optional[Dict[str, Any]], float]:
    """
    Call LLM with retry logic, usage tracking, and event emission.

    Returns:
        (response_message, cost) on success
        (None, 0.0) on failure after max_retries
    """
    msg = None
    last_error: Optional[Exception] = None
    accumulated_usage["_last_llm_error"] = None
    accumulated_usage["_last_llm_error_model"] = None

    for attempt in range(max_retries):
        try:
            kwargs = {"messages": messages, "model": model, "reasoning_effort": effort}
            if tools:
                kwargs["tools"] = tools
            if interaction_id:
                kwargs["interaction_id"] = interaction_id
            if force_user_initiator:
                kwargs["force_user_initiator"] = True
            resp_msg, usage = llm.chat(**kwargs)
            msg = resp_msg
            accumulated_usage["_last_llm_error"] = None
            accumulated_usage["_last_llm_error_model"] = model
            add_usage(accumulated_usage, usage)

            # Calculate cost and emit event for EVERY attempt (including retries)
            cost = float(usage.get("cost") or 0)
            if not cost:
                cost = _estimate_cost(
                    model,
                    int(usage.get("prompt_tokens") or 0),
                    int(usage.get("completion_tokens") or 0),
                    int(usage.get("cached_tokens") or 0),
                    int(usage.get("cache_write_tokens") or 0),
                )

            # Emit real-time usage event with category based on task_type
            category = task_type if task_type in ("evolution", "consciousness", "review", "summarize") else "task"
            _emit_llm_usage_event(event_queue, task_id, model, usage, cost, category)

            # Empty response = retry-worthy (model sometimes returns empty content with no tool_calls)
            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content")
            if not tool_calls and (not content or not content.strip()):
                log.warning("LLM returned empty response (no content, no tool_calls), attempt %d/%d", attempt + 1, max_retries)

                # Log raw empty response for debugging
                append_jsonl(drive_logs / "events.jsonl", {
                    "ts": utc_now_iso(), "type": "llm_empty_response",
                    "task_id": task_id,
                    "round": round_idx, "attempt": attempt + 1,
                    "model": model,
                    "raw_content": repr(content)[:500] if content else None,
                    "raw_tool_calls": repr(tool_calls)[:500] if tool_calls else None,
                    "finish_reason": msg.get("finish_reason") or msg.get("stop_reason"),
                })

                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                # Last attempt — return None to trigger "could not get response"
                return None, cost

            # Count only successful rounds
            accumulated_usage["rounds"] = accumulated_usage.get("rounds", 0) + 1

            # Store per-round token counts for stagnation/overflow detection
            accumulated_usage["_last_round_prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
            accumulated_usage["_last_round_completion_tokens"] = int(usage.get("completion_tokens") or 0)

            # Log per-round metrics
            _round_event = {
                "ts": utc_now_iso(), "type": "llm_round",
                "task_id": task_id,
                "round": round_idx, "model": model,
                "reasoning_effort": effort,
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "cached_tokens": int(usage.get("cached_tokens") or 0),
                "cache_write_tokens": int(usage.get("cache_write_tokens") or 0),
                "cost_usd": cost,
                "shadow_cost": float(usage.get("shadow_cost") or 0),
            }
            append_jsonl(drive_logs / "events.jsonl", _round_event)
            return msg, cost

        except Exception as e:
            last_error = e
            accumulated_usage["_last_llm_error"] = str(e)
            accumulated_usage["_last_llm_error_model"] = model
            append_jsonl(drive_logs / "events.jsonl", {
                "ts": utc_now_iso(), "type": "llm_api_error",
                "task_id": task_id,
                "round": round_idx, "attempt": attempt + 1,
                "model": model, "error": repr(e),
            })
            if attempt < max_retries - 1:
                time.sleep(min(2 ** attempt * 2, 30))

    return None, 0.0

def _process_tool_results(
    results: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
    llm_trace: Dict[str, Any],
    emit_progress: Callable[[str], None],
    transport: str = "",
) -> Tuple[int, bool]:
    error_count = 0
    made_progress = False
    trace_calls = llm_trace.setdefault("tool_calls", [])
    guard = llm_trace.setdefault("loop_guard_emitted", {"signatures": [], "errors": []})
    emitted_signatures = set(guard.get("signatures") or [])
    emitted_errors = set(guard.get("errors") or [])
    for exec_result in results:
        fn_name = exec_result["fn_name"]
        is_error = exec_result["is_error"]
        if is_error:
            error_count += 1
        call_signature = _build_call_signature(fn_name, exec_result["args_for_log"])
        tool_result_text = truncate_for_log(exec_result["result"], 700)
        result_fingerprint = _normalize_error_text(tool_result_text)
        error_fingerprint = _normalize_error_text(exec_result["result"]) if is_error else None

        if not is_error:
            seen_same = any(
                c.get("call_signature") == call_signature and c.get("result_fingerprint") == result_fingerprint
                for c in trace_calls
            )
            if not seen_same:
                made_progress = True

        messages.append({"role": "tool", "tool_call_id": exec_result["tool_call_id"], "content": _truncate_tool_result(exec_result["result"], tool_name=fn_name, transport=transport)})
        trace_calls.append({
            "tool": fn_name,
            "args": _safe_args(exec_result["args_for_log"]),
            "result": tool_result_text,
            "is_error": is_error,
            "call_signature": call_signature,
            "result_fingerprint": result_fingerprint,
            "error_fingerprint": error_fingerprint,
        })
        if _count_recent_repeats(trace_calls, "call_signature", call_signature) >= 4 and call_signature not in emitted_signatures:
            emit_progress("⚠️ Loop guard: same tool call repeated 4x; forcing strategy change.")
            messages.append({"role": "system", "content": "Loop guard warning: exact same tool call repeated 4 times in a row. Do not retry unchanged; change strategy, args, or tool."})
            emitted_signatures.add(call_signature)
        if error_fingerprint and _count_recent_repeats(trace_calls, "error_fingerprint", error_fingerprint) >= 3 and error_fingerprint not in emitted_errors:
            emit_progress("🚫 Loop guard: same tool error repeated 3x; pivot required.")
            messages.append({"role": "system", "content": "Loop guard critical: same tool error repeated 3 times in a row. Do not retry unchanged; pivot or report blocker."})
            emitted_errors.add(error_fingerprint)
    guard["signatures"] = list(emitted_signatures)
    guard["errors"] = list(emitted_errors)
    return error_count, made_progress

def _build_call_signature(fn_name: str, args: Any) -> str:
    payload = {"tool": fn_name, "args": _safe_args(args)}
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)

def _normalize_error_text(result: Any) -> str:
    text = result if isinstance(result, str) else repr(result)
    return " ".join(text.strip().lower().split())[:500]

def _count_recent_repeats(tool_calls: List[Dict[str, Any]], field: str, value: Optional[str]) -> int:
    if not value:
        return 0
    count = 0
    for call in reversed(tool_calls):
        if call.get(field) != value:
            break
        count += 1
    return count

def _safe_args(v: Any) -> Any:
    """Ensure args are JSON-serializable for trace logging."""
    try:
        return json.loads(json.dumps(v, ensure_ascii=False, default=str))
    except Exception:
        log.debug("Failed to serialize args for trace logging", exc_info=True)
        return {"_repr": repr(v)}



