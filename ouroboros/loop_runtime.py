"""Runtime implementation for run_llm_loop.

Extracted from ouroboros.loop to keep that module compact and maintainable.
"""

from __future__ import annotations

import os
import pathlib
import queue
from typing import Any, Callable, Dict, List, Optional, Tuple

PROMPT_TOKEN_GUARD_THRESHOLD = 40000

from ouroboros.context import compact_tool_history, compact_tool_history_llm
from ouroboros.llm import LLMClient, normalize_reasoning_effort
from ouroboros.tools.registry import ToolRegistry
from ouroboros.utils import append_jsonl, utc_now_iso
from ouroboros.antistagnation import (
    build_forced_finalize_reason,
    detect_context_overflow,
    inject_stagnation_self_check,
    is_small_completion_stagnation,
    load_antistagnation_config,
    should_force_round_finalize,
    stagnation_action,
)
from ouroboros.loop import (
    log,
    _setup_dynamic_tools,
    _StatefulToolExecutor,
    _maybe_inject_self_check,
    _call_llm_with_retry,
    _drain_incoming_messages,
    _handle_text_response,
    _handle_tool_calls,
    _check_budget_limits,
    _finalize_with_summary,
)


def _maybe_handle_hard_round_limit(
    *,
    round_idx: int,
    max_rounds: int,
    messages: List[Dict[str, Any]],
    llm: LLMClient,
    active_model: str,
    active_effort: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    event_queue: Optional[queue.Queue],
    accumulated_usage: Dict[str, Any],
    llm_trace: Dict[str, Any],
    task_type: str,
) -> Optional[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    if round_idx <= max_rounds:
        return None
    finish_reason = f"⚠️ Task exceeded MAX_ROUNDS ({max_rounds}). Consider decomposing into subtasks via schedule_task."
    messages.append({"role": "system", "content": f"[ROUND_LIMIT] {finish_reason}"})
    try:
        final_msg, _ = _call_llm_with_retry(
            llm,
            messages,
            active_model,
            None,
            active_effort,
            max_retries,
            drive_logs,
            task_id,
            round_idx,
            event_queue,
            accumulated_usage,
            task_type,
        )
        if final_msg:
            return (final_msg.get("content") or finish_reason), accumulated_usage, llm_trace
        return finish_reason, accumulated_usage, llm_trace
    except Exception:
        log.warning("Failed to get final response after round limit", exc_info=True)
        return finish_reason, accumulated_usage, llm_trace


def _maybe_emit_round_warning(
    *,
    round_idx: int,
    anti,
    task_round_warn_emitted: bool,
    messages: List[Dict[str, Any]],
    llm_trace: Dict[str, Any],
) -> bool:
    if task_round_warn_emitted or round_idx < anti.task_round_warn:
        return task_round_warn_emitted
    warn = (
        f"⚠️ Round warning: task reached {round_idx} rounds (warn={anti.task_round_warn}, cap={anti.task_round_cap}). "
        "Prioritize finishing or ask for missing input."
    )
    messages.append({"role": "system", "content": f"[TASK_ROUND_WARN] {warn}"})
    llm_trace["assistant_notes"].append(warn[:320])
    return True


def _maybe_force_finalize_by_round_cap(
    *,
    round_idx: int,
    recent_progress: List[bool],
    anti,
    no_progress_rounds: int,
    messages: List[Dict[str, Any]],
    llm: LLMClient,
    active_model: str,
    active_effort: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    event_queue: Optional[queue.Queue],
    accumulated_usage: Dict[str, Any],
    llm_trace: Dict[str, Any],
    task_type: str,
) -> Optional[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    if not should_force_round_finalize(round_idx, recent_progress, anti):
        return None
    reason = build_forced_finalize_reason(
        "Task round cap reached",
        no_progress_rounds=no_progress_rounds,
        round_idx=round_idx,
    )
    final_text, accumulated_usage, _ = _finalize_with_summary(
        reason=reason,
        messages=messages,
        llm=llm,
        active_model=active_model,
        active_effort=active_effort,
        max_retries=max_retries,
        drive_logs=drive_logs,
        task_id=task_id,
        round_idx=round_idx,
        event_queue=event_queue,
        accumulated_usage=accumulated_usage,
        task_type=task_type,
    )
    llm_trace["assistant_notes"].append(reason[:320])
    return final_text, accumulated_usage, llm_trace


def _apply_context_overrides_and_compaction(
    *,
    tools: ToolRegistry,
    messages: List[Dict[str, Any]],
    round_idx: int,
    active_model: str,
    active_effort: str,
) -> Tuple[List[Dict[str, Any]], str, str]:
    ctx = tools._ctx
    if ctx.active_model_override:
        active_model = ctx.active_model_override
        ctx.active_model_override = None
    if ctx.active_effort_override:
        active_effort = normalize_reasoning_effort(ctx.active_effort_override, default=active_effort)
        ctx.active_effort_override = None

    pending_compaction = getattr(ctx, "_pending_compaction", None)
    if pending_compaction is not None:
        messages = compact_tool_history_llm(messages, keep_recent=pending_compaction)
        ctx._pending_compaction = None
    elif round_idx > 8:
        messages = compact_tool_history(messages, keep_recent=6)
    elif round_idx > 3 and len(messages) > 60:
        messages = compact_tool_history(messages, keep_recent=6)

    return messages, active_model, active_effort


def _is_codex_timeout_error(exc: Exception) -> bool:
    """Return True for Codex infrastructure errors that warrant a model fallback."""
    msg = str(exc)
    if isinstance(exc, RuntimeError) and "All Codex accounts tried" in msg:
        return True
    if "timed out" in msg.lower() or "TimeoutError" in type(exc).__name__:
        return True
    if "IncompleteRead" in type(exc).__name__ or "IncompleteRead" in msg:
        return True
    return False


def _call_llm_with_fallback(
    *,
    llm: LLMClient,
    messages: List[Dict[str, Any]],
    active_model: str,
    tool_schemas: List[Dict[str, Any]],
    active_effort: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    round_idx: int,
    event_queue: Optional[queue.Queue],
    accumulated_usage: Dict[str, Any],
    task_type: str,
    emit_progress: Callable[[str], None],
) -> Optional[Dict[str, Any]]:
    primary_exc: Optional[Exception] = None
    try:
        msg, _ = _call_llm_with_retry(
            llm,
            messages,
            active_model,
            tool_schemas,
            active_effort,
            max_retries,
            drive_logs,
            task_id,
            round_idx,
            event_queue,
            accumulated_usage,
            task_type,
        )
    except Exception as e:
        if _is_codex_timeout_error(e):
            primary_exc = e
            msg = None
        else:
            raise

    if msg is not None:
        return msg

    # --- Profile-aware fallback ---
    from ouroboros.model_profiles import (
        get_active_profile, get_profile, activate_codex_fallback,
    )
    profile = get_active_profile()
    if profile.fallback_to:
        fallback_profile = get_profile(profile.fallback_to)
        if fallback_profile:
            fb_reason_tag = "timeout" if primary_exc and "timeout" in str(primary_exc).lower() else "rate_limit"
            activate_codex_fallback(
                reason=fb_reason_tag,
                cooldown_sec=3600,
            )
            from ouroboros.model_profiles import get_codex_cooldown_remaining
            cooldown_min = get_codex_cooldown_remaining() // 60
            emit_progress(
                f"⚠️ Codex fallback activated\n"
                f"Reason: {fb_reason_tag}\n"
                f"Switched to: {fallback_profile.display_name} ({fallback_profile.model})\n"
                f"Cooldown: {cooldown_min}m\n"
                f"Auto-return to Codex after cooldown"
            )
            log.warning(
                "[fallback] %s → %s (reason: %s)",
                profile.name, fallback_profile.name, str(primary_exc)[:100] if primary_exc else "empty",
            )
            try:
                msg, _ = _call_llm_with_retry(
                    llm,
                    messages,
                    fallback_profile.model,
                    tool_schemas if fallback_profile.tools_enabled else None,
                    active_effort,
                    max_retries,
                    drive_logs,
                    task_id,
                    round_idx,
                    event_queue,
                    accumulated_usage,
                    task_type,
                )
                if msg is not None:
                    return msg
            except Exception as profile_fb_exc:
                log.warning("Profile fallback %s also failed: %s", fallback_profile.name, profile_fb_exc)

    # --- Legacy env-based fallback (last resort) ---
    fallback_list_raw = os.environ.get(
        "OUROBOROS_MODEL_FALLBACK_LIST",
        "google/gemini-2.5-pro-preview,openai/o3,anthropic/claude-sonnet-4.6",
    )
    fallback_candidates = [m.strip() for m in fallback_list_raw.split(",") if m.strip()]
    fallback_model = next((m for m in fallback_candidates if m != active_model), None)
    if fallback_model is None:
        if primary_exc is not None:
            raise primary_exc
        return None

    reason = f"timeout/error: {primary_exc}" if primary_exc else "empty response"
    emit_progress(f"⚡ Fallback: {active_model} → {fallback_model} ({reason})")
    log.warning("Falling back from %s to %s: %s", active_model, fallback_model, primary_exc or "empty response")
    try:
        msg, _ = _call_llm_with_retry(
            llm,
            messages,
            fallback_model,
            tool_schemas,
            active_effort,
            max_retries,
            drive_logs,
            task_id,
            round_idx,
            event_queue,
            accumulated_usage,
            task_type,
        )
    except Exception as fallback_exc:
        log.error("Fallback model %s also failed: %s", fallback_model, fallback_exc)
        if primary_exc is not None:
            raise primary_exc from fallback_exc
        raise
    return msg


def _update_progress_windows(
    *, recent_progress: List[bool], no_progress_rounds: int, tool_progress: bool
) -> Tuple[List[bool], int]:
    recent_progress.append(bool(tool_progress))
    if len(recent_progress) > 64:
        recent_progress = recent_progress[-64:]
    if tool_progress:
        return recent_progress, 0
    return recent_progress, no_progress_rounds + 1


def _maybe_force_finalize_by_stagnation(
    *,
    no_progress_rounds: int,
    anti,
    stagnation_check_injected: bool,
    messages: List[Dict[str, Any]],
    round_idx: int,
    llm: LLMClient,
    active_model: str,
    active_effort: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    event_queue: Optional[queue.Queue],
    accumulated_usage: Dict[str, Any],
    llm_trace: Dict[str, Any],
    task_type: str,
) -> Tuple[Optional[Tuple[str, Dict[str, Any], Dict[str, Any]]], bool]:
    action = stagnation_action(no_progress_rounds, anti, stagnation_check_injected)
    if action == "inject_self_check":
        inject_stagnation_self_check(
            messages,
            no_progress_rounds=no_progress_rounds,
            threshold=anti.stagnation_rounds,
            grace=anti.stagnation_grace,
        )
        return None, True
    if action != "force_finalize":
        return None, stagnation_check_injected

    reason = build_forced_finalize_reason(
        "Stagnation limit reached",
        no_progress_rounds=no_progress_rounds,
        round_idx=round_idx,
    )
    final_text, accumulated_usage, _ = _finalize_with_summary(
        reason=reason,
        messages=messages,
        llm=llm,
        active_model=active_model,
        active_effort=active_effort,
        max_retries=max_retries,
        drive_logs=drive_logs,
        task_id=task_id,
        round_idx=round_idx,
        event_queue=event_queue,
        accumulated_usage=accumulated_usage,
        task_type=task_type,
    )
    llm_trace["assistant_notes"].append(reason[:320])
    return (final_text, accumulated_usage, llm_trace), stagnation_check_injected




def _should_finalize_by_round_cap(round_idx: int, recent_progress: List[bool], anti) -> bool:
    return should_force_round_finalize(round_idx, recent_progress, anti)


def _handle_no_tool_call_finalize(content: Optional[str], llm_trace: Dict[str, Any], accumulated_usage: Dict[str, Any], recent_progress: List[bool]) -> Tuple[str, Dict[str, Any], Dict[str, Any], List[bool], int]:
    recent_progress.append(True)
    if len(recent_progress) > 64:
        recent_progress = recent_progress[-64:]
    final = _handle_text_response(content, llm_trace, accumulated_usage)
    return final[0], final[1], final[2], recent_progress, 0


def _get_evolution_round_limit(task_type: str, default_task_max_rounds: int) -> int:
    if task_type != "evolution":
        return default_task_max_rounds
    try:
        return max(1, int(os.environ.get("OUROBOROS_EVOLUTION_MAX_ROUNDS", "10")))
    except (ValueError, TypeError):
        log.warning("Invalid OUROBOROS_EVOLUTION_MAX_ROUNDS, falling back to 10")
        return 10


def _update_large_prompt_streak(
    task_type: str,
    current_streak: int,
    round_prompt_tokens: int,
    threshold: int = PROMPT_TOKEN_GUARD_THRESHOLD,
) -> int:
    if task_type != "evolution":
        return 0
    if round_prompt_tokens > threshold:
        return current_streak + 1
    return 0


def _should_finalize_evolution_for_prompt_tokens(
    task_type: str,
    large_prompt_streak: int,
) -> bool:
    return task_type == "evolution" and large_prompt_streak >= 2


def _append_assistant_with_tool_calls(messages: List[Dict[str, Any]], content: Optional[str], tool_calls: List[Dict[str, Any]], emit_progress: Callable[[str], None], llm_trace: Dict[str, Any]) -> None:
    messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})
    if content and content.strip():
        emit_progress(content.strip())
        llm_trace["assistant_notes"].append(content.strip()[:320])


def _init_antistagnation_state() -> Tuple[int, Any, int, int, List[bool], bool, bool]:
    try:
        max_rounds = max(1, int(os.environ.get("OUROBOROS_MAX_ROUNDS", "200")))
    except (ValueError, TypeError):
        max_rounds = 200
        log.warning("Invalid OUROBOROS_MAX_ROUNDS, defaulting to 200")
    anti = load_antistagnation_config()
    return max_rounds, anti, 0, 0, [], False, False


def _extract_original_task(messages: List[Dict[str, Any]]) -> str:
    """Extract the original user task from the message history."""
    for m in messages:
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, str) and content.strip():
                return content.strip()[:500]
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("text", "").strip():
                        return block["text"].strip()[:500]
    return "(unknown task)"



def _finalize_due_to_reason(
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
    llm_trace: Dict[str, Any],
    task_type: str,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    final_text, accumulated_usage, _ = _finalize_with_summary(
        reason=reason,
        messages=messages,
        llm=llm,
        active_model=active_model,
        active_effort=active_effort,
        max_retries=max_retries,
        drive_logs=drive_logs,
        task_id=task_id,
        round_idx=round_idx,
        event_queue=event_queue,
        accumulated_usage=accumulated_usage,
        task_type=task_type,
    )
    llm_trace["assistant_notes"].append(reason[:320])
    return final_text, accumulated_usage, llm_trace




def _prepare_round_or_finalize(
    *,
    state: Dict[str, Any],
    messages: List[Dict[str, Any]],
    tools: ToolRegistry,
    llm: LLMClient,
    drive_logs: pathlib.Path,
    emit_progress: Callable[[str], None],
    incoming_messages: queue.Queue,
    task_type: str,
    task_id: str,
    event_queue: Optional[queue.Queue],
    drive_root: Optional[pathlib.Path],
    owner_msg_seen: set,
) -> Optional[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    round_idx = state["round_idx"]
    hard_limit = _maybe_handle_hard_round_limit(
        round_idx=round_idx,
        max_rounds=state["max_rounds"],
        messages=messages,
        llm=llm,
        active_model=state["active_model"],
        active_effort=state["active_effort"],
        max_retries=state["max_retries"],
        drive_logs=drive_logs,
        task_id=task_id,
        event_queue=event_queue,
        accumulated_usage=state["accumulated_usage"],
        llm_trace=state["llm_trace"],
        task_type=task_type,
    )
    if hard_limit is not None:
        return hard_limit

    if task_type != "consciousness" and round_idx > state["task_round_limit"]:
        limit_name = "OUROBOROS_EVOLUTION_MAX_ROUNDS" if task_type == "evolution" else "TASK_MAX_ROUNDS"
        reason = (
            f"⚠️ Task exceeded {limit_name} ({state['task_round_limit']}). "
            "Формирую финальный ответ из собранных данных."
        )
        return _finalize_due_to_reason(
            reason=reason,
            messages=messages,
            llm=llm,
            active_model=state["active_model"],
            active_effort=state["active_effort"],
            max_retries=state["max_retries"],
            drive_logs=drive_logs,
            task_id=task_id,
            round_idx=round_idx,
            event_queue=event_queue,
            accumulated_usage=state["accumulated_usage"],
            llm_trace=state["llm_trace"],
            task_type=task_type,
        )

    _maybe_inject_self_check(round_idx, state["max_rounds"], messages, state["accumulated_usage"], emit_progress)
    state["task_round_warn_emitted"] = _maybe_emit_round_warning(
        round_idx=round_idx,
        anti=state["anti"],
        task_round_warn_emitted=state["task_round_warn_emitted"],
        messages=messages,
        llm_trace=state["llm_trace"],
    )

    if _should_finalize_by_round_cap(round_idx, state["recent_progress"], state["anti"]):
        round_cap_finalize = _maybe_force_finalize_by_round_cap(
            round_idx=round_idx,
            recent_progress=state["recent_progress"],
            anti=state["anti"],
            no_progress_rounds=state["no_progress_rounds"],
            messages=messages,
            llm=llm,
            active_model=state["active_model"],
            active_effort=state["active_effort"],
            max_retries=state["max_retries"],
            drive_logs=drive_logs,
            task_id=task_id,
            event_queue=event_queue,
            accumulated_usage=state["accumulated_usage"],
            llm_trace=state["llm_trace"],
            task_type=task_type,
        )
        if round_cap_finalize is not None:
            return round_cap_finalize

    _drain_incoming_messages(messages, incoming_messages, drive_root, task_id, event_queue, owner_msg_seen)
    messages[:], state["active_model"], state["active_effort"] = _apply_context_overrides_and_compaction(
        tools=tools,
        messages=messages,
        round_idx=round_idx,
        active_model=state["active_model"],
        active_effort=state["active_effort"],
    )
    return None


def _process_llm_response_or_continue(
    *,
    state: Dict[str, Any],
    msg: Dict[str, Any],
    messages: List[Dict[str, Any]],
    tools: ToolRegistry,
    llm: LLMClient,
    drive_logs: pathlib.Path,
    emit_progress: Callable[[str], None],
    task_type: str,
    task_id: str,
    budget_remaining_usd: Optional[float],
    event_queue: Optional[queue.Queue],
    stateful_executor,
) -> Optional[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    round_idx = state["round_idx"]
    round_completion_tokens = int(state["accumulated_usage"].get("_last_round_completion_tokens") or 0)
    round_prompt_tokens = int(state["accumulated_usage"].get("_last_round_prompt_tokens") or 0)
    state["recent_completion_tokens"].append(round_completion_tokens)
    if len(state["recent_completion_tokens"]) > 16:
        state["recent_completion_tokens"] = state["recent_completion_tokens"][-16:]

    if state["prev_prompt_tokens"] > 0 and detect_context_overflow(round_prompt_tokens, state["prev_prompt_tokens"], state["anti"]):
        append_jsonl(drive_logs / "events.jsonl", {
            "ts": utc_now_iso(), "type": "context_truncated",
            "task_id": task_id, "round": round_idx,
            "prev_prompt_tokens": state["prev_prompt_tokens"],
            "current_prompt_tokens": round_prompt_tokens,
            "drop_pct": round((1.0 - round_prompt_tokens / state["prev_prompt_tokens"]) * 100, 1),
        })
        reminder = (
            f"Контекст был обрезан. Задача пользователя: {state['original_task_text']}. "
            "Сформируй финальный ответ из собранных данных."
        )
        messages.append({"role": "system", "content": f"[CONTEXT_TRUNCATED] {reminder}"})
        emit_progress(f"⚠️ Context truncated at round {round_idx}: {state['prev_prompt_tokens']} → {round_prompt_tokens} tokens")
    state["prev_prompt_tokens"] = round_prompt_tokens

    state["evolution_large_prompt_streak"] = _update_large_prompt_streak(task_type, state["evolution_large_prompt_streak"], round_prompt_tokens)
    if _should_finalize_evolution_for_prompt_tokens(task_type, state["evolution_large_prompt_streak"]):
        reason = (
            "⚠️ Evolution prompt_tokens guard triggered: prompt_tokens > "
            f"{PROMPT_TOKEN_GUARD_THRESHOLD} for 2 consecutive rounds. Формирую финальный ответ."
        )
        return _finalize_due_to_reason(
            reason=reason,
            messages=messages,
            llm=llm,
            active_model=state["active_model"],
            active_effort=state["active_effort"],
            max_retries=state["max_retries"],
            drive_logs=drive_logs,
            task_id=task_id,
            round_idx=round_idx,
            event_queue=event_queue,
            accumulated_usage=state["accumulated_usage"],
            llm_trace=state["llm_trace"],
            task_type=task_type,
        )

    if is_small_completion_stagnation(state["recent_completion_tokens"], state["anti"]):
        reason = (
            f"⚠️ Small completion stagnation: {state['anti'].small_completion_max_rounds} rounds "
            f"in a row with completion_tokens < {state['anti'].small_completion_threshold}. "
            "Формирую финальный ответ."
        )
        return _finalize_due_to_reason(
            reason=reason,
            messages=messages,
            llm=llm,
            active_model=state["active_model"],
            active_effort=state["active_effort"],
            max_retries=state["max_retries"],
            drive_logs=drive_logs,
            task_id=task_id,
            round_idx=round_idx,
            event_queue=event_queue,
            accumulated_usage=state["accumulated_usage"],
            llm_trace=state["llm_trace"],
            task_type=task_type,
        )

    tool_calls = msg.get("tool_calls") or []
    content = msg.get("content")
    if not tool_calls:
        final_text, final_usage, final_trace, state["recent_progress"], state["no_progress_rounds"] = _handle_no_tool_call_finalize(
            content, state["llm_trace"], state["accumulated_usage"], state["recent_progress"]
        )
        return final_text, final_usage, final_trace

    _append_assistant_with_tool_calls(messages, content, tool_calls, emit_progress, state["llm_trace"])
    _, tool_progress = _handle_tool_calls(
        tool_calls,
        tools,
        drive_logs,
        task_id,
        stateful_executor,
        messages,
        state["llm_trace"],
        emit_progress,
    )
    state["recent_progress"], state["no_progress_rounds"] = _update_progress_windows(
        recent_progress=state["recent_progress"],
        no_progress_rounds=state["no_progress_rounds"],
        tool_progress=tool_progress,
    )
    if tool_progress:
        state["stagnation_check_injected"] = False

    stagnation_result, state["stagnation_check_injected"] = _maybe_force_finalize_by_stagnation(
        no_progress_rounds=state["no_progress_rounds"],
        anti=state["anti"],
        stagnation_check_injected=state["stagnation_check_injected"],
        messages=messages,
        round_idx=round_idx,
        llm=llm,
        active_model=state["active_model"],
        active_effort=state["active_effort"],
        max_retries=state["max_retries"],
        drive_logs=drive_logs,
        task_id=task_id,
        event_queue=event_queue,
        accumulated_usage=state["accumulated_usage"],
        llm_trace=state["llm_trace"],
        task_type=task_type,
    )
    if stagnation_result is not None:
        return stagnation_result

    return _check_budget_limits(
        budget_remaining_usd,
        state["accumulated_usage"],
        round_idx,
        messages,
        llm,
        state["active_model"],
        state["active_effort"],
        state["max_retries"],
        drive_logs,
        task_id,
        event_queue,
        state["llm_trace"],
        task_type,
    )


def _run_single_round(
    *,
    state: Dict[str, Any],
    messages: List[Dict[str, Any]],
    tools: ToolRegistry,
    llm: LLMClient,
    drive_logs: pathlib.Path,
    emit_progress: Callable[[str], None],
    incoming_messages: queue.Queue,
    task_type: str,
    task_id: str,
    budget_remaining_usd: Optional[float],
    event_queue: Optional[queue.Queue],
    drive_root: Optional[pathlib.Path],
    tool_schemas: List[Dict[str, Any]],
    stateful_executor,
    owner_msg_seen: set,
) -> Optional[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    state["round_idx"] += 1
    prepared = _prepare_round_or_finalize(
        state=state,
        messages=messages,
        tools=tools,
        llm=llm,
        drive_logs=drive_logs,
        emit_progress=emit_progress,
        incoming_messages=incoming_messages,
        task_type=task_type,
        task_id=task_id,
        event_queue=event_queue,
        drive_root=drive_root,
        owner_msg_seen=owner_msg_seen,
    )
    if prepared is not None:
        return prepared

    round_idx = state["round_idx"]

    # --- Profile-aware tools policy ---
    from ouroboros.model_profiles import get_active_profile as _gap, switch_profile as _sp
    _cur_profile = _gap()
    effective_tools = tool_schemas if _cur_profile.tools_enabled else None

    msg = _call_llm_with_fallback(
        llm=llm,
        messages=messages,
        active_model=state["active_model"],
        tool_schemas=effective_tools,
        active_effort=state["active_effort"],
        max_retries=state["max_retries"],
        drive_logs=drive_logs,
        task_id=task_id,
        round_idx=round_idx,
        event_queue=event_queue,
        accumulated_usage=state["accumulated_usage"],
        task_type=task_type,
        emit_progress=emit_progress,
    )
    if msg is None:
        return (
            "⚠️ Failed to get a response from the model after retries/fallback. Try rephrasing your request.",
            state["accumulated_usage"],
            state["llm_trace"],
        )

    # --- Profile auto_return (e.g. opus → codex) ---
    if _cur_profile.auto_return_to and round_idx >= 1:
        log.info("[profiles] Auto-return: %s → %s", _cur_profile.name, _cur_profile.auto_return_to)
        _sp(_cur_profile.auto_return_to, manual=False)

    return _process_llm_response_or_continue(
        state=state,
        msg=msg,
        messages=messages,
        tools=tools,
        llm=llm,
        drive_logs=drive_logs,
        emit_progress=emit_progress,
        task_type=task_type,
        task_id=task_id,
        budget_remaining_usd=budget_remaining_usd,
        event_queue=event_queue,
        stateful_executor=stateful_executor,
    )
def run_llm_loop_impl(
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
    """Core LLM-with-tools loop runtime implementation."""
    active_model = llm.default_model()
    llm_trace: Dict[str, Any] = {"assistant_notes": [], "tool_calls": []}
    accumulated_usage: Dict[str, Any] = {}
    max_rounds, anti, round_idx, no_progress_rounds, recent_progress, stagnation_check_injected, task_round_warn_emitted = _init_antistagnation_state()

    # --- Profile-aware max_rounds cap ---
    from ouroboros.model_profiles import get_active_profile as _gap
    _profile = _gap()
    if _profile.max_rounds > 0:
        max_rounds = min(max_rounds, _profile.max_rounds)

    from ouroboros.tools import tool_discovery as _td

    _td.set_registry(tools)
    tool_schemas = tools.schemas(core_only=True)
    tool_schemas, _enabled_extra_tools = _setup_dynamic_tools(tools, tool_schemas, messages)
    tools._ctx.event_queue = event_queue
    tools._ctx.task_id = task_id

    state = {
        "active_model": active_model,
        "active_effort": initial_effort,
        "llm_trace": llm_trace,
        "accumulated_usage": accumulated_usage,
        "max_retries": 3,
        "max_rounds": max_rounds,
        "anti": anti,
        "round_idx": round_idx,
        "no_progress_rounds": no_progress_rounds,
        "recent_progress": recent_progress,
        "stagnation_check_injected": stagnation_check_injected,
        "task_round_warn_emitted": task_round_warn_emitted,
        "task_round_limit": _get_evolution_round_limit(task_type, anti.task_max_rounds),
        "recent_completion_tokens": [],
        "prev_prompt_tokens": 0,
        "evolution_large_prompt_streak": 0,
        "original_task_text": _extract_original_task(messages),
    }
    # If caller provides a persistent executor (direct-chat), reuse it and
    # do NOT shut it down when this task ends — it survives across messages.
    owns_executor = persistent_executor is None
    stateful_executor = persistent_executor or _StatefulToolExecutor()
    owner_msg_seen: set = set()

    try:
        while True:
            result = _run_single_round(
                state=state,
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
                drive_root=drive_root,
                tool_schemas=tool_schemas,
                stateful_executor=stateful_executor,
                owner_msg_seen=owner_msg_seen,
            )
            if result is not None:
                return result
    finally:
        if owns_executor:
            try:
                stateful_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                log.warning("Failed to shutdown stateful executor", exc_info=True)

        if drive_root is not None and task_id:
            try:
                from ouroboros.owner_inject import cleanup_task_mailbox

                cleanup_task_mailbox(drive_root, task_id)
            except Exception:
                log.debug("Failed to cleanup task mailbox", exc_info=True)
