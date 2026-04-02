"""Runtime implementation for run_llm_loop.

Extracted from ouroboros.loop to keep that module compact and maintainable.
"""

from __future__ import annotations

import os
import pathlib
import queue
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

PROMPT_TOKEN_GUARD_THRESHOLD = 40000

from ouroboros.context import compact_tool_history, compact_tool_history_llm
from ouroboros.copilot_proxy import CopilotServerCooldownError, should_reset_session, summarize_session_for_reset, COPILOT_SESSION_ROUND_LIMIT
from ouroboros.llm import LLMClient, normalize_reasoning_effort, model_transport, reasoning_rank
from ouroboros.model_modes import execution_style_for_active_mode, get_runtime_diagnostics, max_rounds_for_active_mode, tools_enabled_for_active_mode
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


MAX_SESSION_RESETS = 10
COPILOT_MAX_ROUNDS = COPILOT_SESSION_ROUND_LIMIT * MAX_SESSION_RESETS  # 280


def _copilot_max_rounds_cap(default_max_rounds: int, active_model: str) -> int:
    if model_transport(active_model) != "copilot":
        return default_max_rounds
    # Ensure Copilot loop can run for at least MAX_SESSION_RESETS full sessions
    return max(default_max_rounds, COPILOT_MAX_ROUNDS)


def _consume_force_user_initiator(state: Dict[str, Any]) -> bool:
    flag = bool(state.get("force_user_initiator"))
    state["force_user_initiator"] = False
    return flag


def _enforce_evolution_copilot_reasoning(*, task_type: str, active_model: str, active_effort: str) -> str:
    """Force high reasoning for Copilot Sonnet/Opus during evolution runs."""
    if str(task_type or "").strip().lower() != "evolution":
        return active_effort
    normalized_model = str(active_model or "").strip().lower()
    if normalized_model not in {"copilot/claude-sonnet-4.6", "copilot/claude-opus-4.6"}:
        return active_effort
    normalized_effort = normalize_reasoning_effort(active_effort, default="medium")
    if reasoning_rank(normalized_effort) != reasoning_rank("high"):
        return "high"
    return normalized_effort


EVOLUTION_COPILOT_REQUEST_DELAY_SEC = float(os.environ.get("EVOLUTION_COPILOT_REQUEST_DELAY_SEC", "4.0"))


def _maybe_sleep_before_evolution_copilot_request(
    *,
    task_type: str,
    active_model: str,
    round_idx: int,
    phase: str,
) -> None:
    if str(task_type or "").strip().lower() != "evolution":
        return
    if model_transport(active_model) != "copilot":
        return
    if phase == "primary" and round_idx <= 1:
        return
    delay_sec = max(0.0, EVOLUTION_COPILOT_REQUEST_DELAY_SEC)
    if delay_sec <= 0:
        return
    time.sleep(delay_sec)


def _extract_last_assistant_content(messages: List[Dict[str, Any]]) -> Optional[str]:
    """Find the last non-empty assistant message content from message history."""
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if content and content.strip():
                return content.strip()
    return None


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
    interaction_id: Optional[str] = None,
) -> Optional[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    if round_idx <= max_rounds:
        return None
    finish_reason = f"⚠️ Task exceeded MAX_ROUNDS ({max_rounds}). Consider decomposing into subtasks via schedule_task."

    # For Copilot: do NOT call LLM again — the thread is likely exhausted
    # and will return 400. Instead, use last assistant content as final response.
    transport = model_transport(active_model)
    if transport == "copilot":
        last_content = _extract_last_assistant_content(messages)
        final_text = last_content or finish_reason
        log.info("Copilot hard round limit reached (round %d/%d), returning last assistant content", round_idx, max_rounds)
        return final_text, accumulated_usage, llm_trace

    # For non-Copilot transports: try to get a summary from LLM
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
            interaction_id=interaction_id,
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

COPILOT_WRAP_UP_ROUNDS_BEFORE = 2  # inject wrap-up N rounds before session boundary


def _maybe_inject_copilot_wrap_up(
    *,
    round_idx: int,
    max_rounds: int,
    active_model: str,
    messages: List[Dict[str, Any]],
    llm_trace: Dict[str, Any],
    wrap_up_injected: bool,
) -> bool:
    """For Copilot: inject a wrap-up warning N rounds before the session boundary.

    With session reset every COPILOT_SESSION_ROUND_LIMIT rounds, fires at
    round 26 and 27 of each session (not at the global max_rounds cap).
    For non-Copilot transports, fires N rounds before the global max_rounds.
    """
    if wrap_up_injected:
        return True
    if model_transport(active_model) != "copilot":
        # Non-Copilot: keep original behaviour (N rounds before hard limit)
        wrap_up_at = max_rounds - COPILOT_WRAP_UP_ROUNDS_BEFORE
        if round_idx < wrap_up_at:
            return False
        remaining = max_rounds - round_idx
        warn = (
            f"⚠️ [WRAP-UP] Осталось {remaining} раундов до принудительного завершения (лимит {max_rounds}). "
            "Прямо сейчас: сохрани scratchpad (update_scratchpad), допиши что важно, сформируй финальный ответ. "
            "Следующий раунд может быть последним."
        )
        messages.append({"role": "system", "content": warn})
        llm_trace["assistant_notes"].append(f"wrap_up_at_round_{round_idx}")
        return True
    # Copilot: trigger based on position within current 28-round session
    session_round = round_idx % COPILOT_SESSION_ROUND_LIMIT
    if session_round == 0:
        # round_idx is exactly at a session boundary (shouldn't wrap-up now)
        return False
    rounds_until_boundary = COPILOT_SESSION_ROUND_LIMIT - session_round
    if rounds_until_boundary > COPILOT_WRAP_UP_ROUNDS_BEFORE:
        return False
    warn = (
        f"⚠️ [COPILOT SESSION BOUNDARY] Через {rounds_until_boundary} раунда(ов) произойдёт session reset "
        f"(шаг {session_round}/{COPILOT_SESSION_ROUND_LIMIT}). "
        "Контекст будет суммаризован и задача продолжится в новой сессии. "
        "Доведи текущий шаг до стабильной точки: сохрани scratchpad, зафиксируй промежуточные результаты. "
        "Незавершённые tool calls или несохранённые данные будут потеряны."
    )
    messages.append({"role": "system", "content": warn})
    llm_trace["assistant_notes"].append(f"copilot_session_boundary_at_round_{round_idx}")
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
    task_type = getattr(ctx, "current_task_type", None) or ""
    active_effort = _enforce_evolution_copilot_reasoning(
        task_type=task_type,
        active_model=active_model,
        active_effort=active_effort,
    )
    transport = model_transport(active_model)
    # Copilot has prefix caching — less aggressive compaction
    keep_recent = 30 if transport == "copilot" else 16

    if pending_compaction is not None:
        messages = compact_tool_history_llm(messages, keep_recent=pending_compaction)
        ctx._pending_compaction = None
    elif task_type == "evolution" and round_idx > 4:
        messages = compact_tool_history(messages, keep_recent=keep_recent)
    elif round_idx >= 8 and round_idx % 8 == 0:
        messages = compact_tool_history(messages, keep_recent=keep_recent)
    elif round_idx > 3 and len(messages) > 40:
        messages = compact_tool_history(messages, keep_recent=keep_recent)

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


def _is_copilot_timeout_error(exc: Exception) -> bool:
    """Return True for Copilot infrastructure errors that warrant a model fallback."""
    msg = str(exc)
    if isinstance(exc, RuntimeError) and "All Copilot accounts exhausted" in msg:
        return True
    if "timed out" in msg.lower() or "TimeoutError" in type(exc).__name__:
        return True
    if "IncompleteRead" in type(exc).__name__ or "IncompleteRead" in msg:
        return True
    # HTTP 5xx from urllib
    import urllib.error
    if isinstance(exc, urllib.error.HTTPError) and exc.code >= 500:
        return True
    if isinstance(exc, (urllib.error.URLError, OSError, ConnectionError)):
        return True
    return False


def _is_transport_timeout_error(exc: Exception, transport: str) -> bool:
    """Unified transport-aware timeout/error check."""
    if transport == "copilot":
        return _is_copilot_timeout_error(exc)
    return _is_codex_timeout_error(exc)


def _consume_last_llm_error(accumulated_usage: Dict[str, Any], model: str) -> Optional[str]:
    err_model = accumulated_usage.get("_last_llm_error_model")
    err_text = accumulated_usage.get("_last_llm_error")
    if err_model != model:
        return None
    accumulated_usage["_last_llm_error"] = None
    accumulated_usage["_last_llm_error_model"] = None
    return err_text or None


def _build_fallback_candidates(active_model: str, transport: str) -> List[str]:
    if transport == "copilot":
        chain = {
            "copilot/claude-opus-4.6": "copilot/claude-sonnet-4.6",
            "copilot/claude-sonnet-4.6": "copilot/claude-haiku-4.5",
            "copilot/claude-haiku-4.5": None,
        }
        candidates: List[str] = []
        seen = {active_model}
        current = chain.get(active_model)
        while current and current not in seen:
            candidates.append(current)
            seen.add(current)
            current = chain.get(current)
        if "codex/gpt-5.4" not in seen:
            candidates.append("codex/gpt-5.4")
        return candidates

    fallback_list_raw = os.environ.get(
        "OUROBOROS_MODEL_FALLBACK_LIST",
        "google/gemini-2.5-pro-preview,openai/o3,anthropic/claude-sonnet-4.6",
    )
    return [m.strip() for m in fallback_list_raw.split(",") if m.strip() and m.strip() != active_model]


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
    interaction_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    primary_exc: Optional[Exception] = None
    transport = model_transport(active_model)

    primary_force_user_initiator = bool(accumulated_usage.pop("_force_user_initiator", False))

    def _call_candidate(model: str, phase: str) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[Exception]]:
        force_user_initiator = primary_force_user_initiator if phase.startswith("primary") else False
        candidate_transport = model_transport(model)
        try:
            if candidate_transport == "copilot":
                _maybe_sleep_before_evolution_copilot_request(
                    task_type=task_type,
                    active_model=model,
                    round_idx=round_idx,
                    phase=phase,
                )
            msg, _ = _call_llm_with_retry(
                llm,
                messages,
                model,
                tool_schemas,
                active_effort,
                max_retries,
                drive_logs,
                task_id,
                round_idx,
                event_queue,
                accumulated_usage,
                task_type,
                interaction_id=interaction_id,
                force_user_initiator=force_user_initiator,
            )
            return msg, _consume_last_llm_error(accumulated_usage, model), None
        except Exception as exc:
            return None, _consume_last_llm_error(accumulated_usage, model), exc

    while True:
        msg, primary_error_text, primary_exc = _call_candidate(active_model, "primary")
        if msg is not None:
            return msg
        if transport == "copilot" and isinstance(primary_exc, CopilotServerCooldownError):
            cooldown_sec = int(primary_exc.cooldown_sec or 60)
            append_jsonl(drive_logs / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "copilot_server_cooldown",
                "task_id": task_id,
                "round": round_idx,
                "model": active_model,
                "interaction_id": interaction_id,
                "account_idx": primary_exc.account_idx,
                "status_code": primary_exc.status_code,
                "cooldown_sec": cooldown_sec,
            })
            emit_progress(
                f"⚠️ Copilot {active_model} вернул {primary_exc.status_code} на acc#{primary_exc.account_idx}. "
                f"Ставлю cooldown на {cooldown_sec}с и повторяю этот же раунд."
            )
            time.sleep(cooldown_sec)
            continue
        primary_reason = f"timeout/error: {primary_exc or primary_error_text}" if (primary_exc or primary_error_text) else "empty response"
        if primary_exc is None and not _is_transport_timeout_error(RuntimeError(primary_reason), transport):
            return None
        break

    fallback_candidates = _build_fallback_candidates(active_model, transport)
    if not fallback_candidates:
        if primary_exc is not None:
            raise primary_exc
        return None

    previous_model = active_model
    previous_reason = f"timeout/error: {primary_exc or primary_error_text}" if (primary_exc or primary_error_text) else "empty response"
    last_exc: Optional[Exception] = primary_exc or (RuntimeError(primary_error_text) if primary_error_text else None)

    for fallback_model in fallback_candidates:
        if model_transport(previous_model) == "copilot" and model_transport(fallback_model) == "codex":
            emit_progress("↪️ Copilot exhausted/unstable — передаю этот же раунд в Codex.")

        emit_progress(f"⚡ Fallback: {previous_model} → {fallback_model} ({previous_reason})")
        log.warning("Falling back from %s to %s: %s", previous_model, fallback_model, previous_reason)

        msg, fallback_error_text, fallback_exc = _call_candidate(fallback_model, "fallback")
        if msg is not None:
            return msg

        previous_model = fallback_model
        previous_reason = f"timeout/error: {fallback_exc or fallback_error_text}" if (fallback_exc or fallback_error_text) else "empty response"
        if fallback_exc is not None:
            last_exc = fallback_exc
        elif fallback_error_text:
            last_exc = RuntimeError(fallback_error_text)

    if last_exc is not None:
        if primary_exc is not None and last_exc is not primary_exc:
            raise primary_exc from last_exc
        raise last_exc
    if primary_exc is not None:
        raise primary_exc
    return None


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


def _run_one_shot_mode(
    *,
    messages: List[Dict[str, Any]],
    llm: LLMClient,
    drive_logs: pathlib.Path,
    emit_progress: Callable[[str], None],
    task_type: str,
    task_id: str,
    event_queue: Optional[queue.Queue],
    accumulated_usage: Dict[str, Any],
    llm_trace: Dict[str, Any],
    active_model: str,
    active_effort: str,
    tool_schemas: List[Dict[str, Any]],
    interaction_id: Optional[str] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    msg = _call_llm_with_fallback(
        llm=llm,
        messages=messages,
        active_model=active_model,
        tool_schemas=tool_schemas,
        active_effort=active_effort,
        max_retries=3,
        drive_logs=drive_logs,
        task_id=task_id,
        round_idx=1,
        event_queue=event_queue,
        accumulated_usage=accumulated_usage,
        task_type=task_type,
        emit_progress=emit_progress,
        interaction_id=interaction_id,
    )
    if msg is None:
        return (
            "⚠️ Failed to get a response from the model after retries/fallback. Try rephrasing your request.",
            accumulated_usage,
            llm_trace,
        )
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        llm_trace["assistant_notes"].append(
            "Mode policy blocked tool-calling in one-shot mode; returning assistant text only."
        )
    content = msg.get("content")
    final_text, final_usage, final_trace, _, _ = _handle_no_tool_call_finalize(
        content, llm_trace, accumulated_usage, []
    )
    return final_text, final_usage, final_trace


def _get_evolution_round_limit(task_type: str, default_task_max_rounds: int) -> int:
    if task_type != "evolution":
        return default_task_max_rounds
    env_limit = os.environ.get("OUROBOROS_EVOLUTION_MAX_ROUNDS", "").strip()
    if env_limit:
        try:
            return max(1, int(env_limit))
        except (ValueError, TypeError):
            log.warning("Invalid OUROBOROS_EVOLUTION_MAX_ROUNDS=%r, using transport-aware default", env_limit)
    # Transport-aware default: use active mode max_rounds capped at 80
    try:
        mode_max = int(max_rounds_for_active_mode())
        return min(max(1, mode_max), 80)
    except Exception:
        return 40


def _get_prompt_token_guard_threshold(task_type: str) -> int:
    """Return prompt token guard threshold: 120k for evolution/review, 40k otherwise."""
    if task_type in ("evolution", "review"):
        return 120_000
    return PROMPT_TOKEN_GUARD_THRESHOLD


def _update_large_prompt_streak(
    task_type: str,
    current_streak: int,
    round_prompt_tokens: int,
    threshold: int = 0,
) -> int:
    if task_type not in ("evolution", "review"):
        return 0
    effective = threshold or _get_prompt_token_guard_threshold(task_type)
    if round_prompt_tokens > effective:
        return current_streak + 1
    return 0


def _should_finalize_evolution_for_prompt_tokens(
    task_type: str,
    large_prompt_streak: int,
) -> bool:
    return task_type in ("evolution", "review") and large_prompt_streak >= 2


def _append_assistant_with_tool_calls(messages: List[Dict[str, Any]], content: Optional[str], tool_calls: List[Dict[str, Any]], emit_progress: Callable[[str], None], llm_trace: Dict[str, Any]) -> None:
    messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})
    if content and content.strip():
        emit_progress(content.strip())
        llm_trace["assistant_notes"].append(content.strip()[:320])


def _init_antistagnation_state() -> Tuple[int, Any, int, int, List[bool], bool, bool]:
    try:
        max_rounds = max(1, int(max_rounds_for_active_mode()))
    except Exception:
        max_rounds = 200
        log.warning("Failed to resolve active mode max_rounds, defaulting to 200", exc_info=True)
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
        interaction_id=state.get("interaction_id"),
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

    state["copilot_wrap_up_injected"] = _maybe_inject_copilot_wrap_up(
        round_idx=round_idx,
        max_rounds=state["max_rounds"],
        active_model=state["active_model"],
        messages=messages,
        llm_trace=state["llm_trace"],
        wrap_up_injected=state["copilot_wrap_up_injected"],
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
        guard_threshold = _get_prompt_token_guard_threshold(task_type)
        reason = (
            f"⚠️ Evolution prompt_tokens guard triggered: prompt_tokens > "
            f"{guard_threshold} for 2 consecutive rounds. Формирую финальный ответ."
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

    if is_small_completion_stagnation(
        state["recent_completion_tokens"], state["anti"],
        task_type=task_type,
        has_tool_calls=bool(msg.get("tool_calls")),
    ):
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
        transport=model_transport(state["active_model"]),
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


def _maybe_apply_session_reset(
    *,
    state: Dict[str, Any],
    messages: List[Dict[str, Any]],
    emit_progress: Callable[[str], None],
) -> None:
    """For Copilot transport: reset session every COPILOT_SESSION_ROUND_LIMIT rounds.

    Summarizes context, creates a new interaction_id, and rebuilds messages so
    the agentic loop can continue in a fresh session without HTTP 500 errors.
    No-op for non-Copilot transports.
    """
    if model_transport(state["active_model"]) != "copilot":
        return

    interaction_id = state.get("interaction_id")
    if not should_reset_session(interaction_id):
        return

    session_resets_done = state.get("session_resets_count", 0)
    if session_resets_done >= MAX_SESSION_RESETS:
        log.warning(
            "copilot_session_reset_limit_reached resets=%d max=%d interaction=%s",
            session_resets_done, MAX_SESSION_RESETS, (interaction_id or "?")[:8],
        )
        return

    from ouroboros.llm import transport_model_name
    model_name = transport_model_name(state["active_model"])

    emit_progress(
        f"🔄 Copilot session reset #{session_resets_done + 1}/{MAX_SESSION_RESETS}: "
        f"summarizing {COPILOT_SESSION_ROUND_LIMIT} rounds..."
    )

    summary = summarize_session_for_reset(
        messages=messages,
        model=model_name,
        interaction_id=interaction_id,
    )

    if summary:
        old_interaction_id = interaction_id
        new_interaction_id = str(uuid.uuid4())
        state["interaction_id"] = new_interaction_id
        state["session_resets_count"] = session_resets_done + 1
        state["copilot_wrap_up_injected"] = False  # reset so wrap-up fires again next session

        # Rebuild messages: keep system prompt + summary as handoff context
        new_messages: List[Dict[str, Any]] = []
        for m in messages:
            if m.get("role") == "system":
                new_messages.append(m)
                break
        new_messages.append({
            "role": "assistant",
            "content": (
                "I've been working on this task. Let me review my progress and continue.\n\n"
                + summary
            ),
        })
        new_messages.append({
            "role": "user",
            "content": "Continue working on the task from where you left off. Pick up exactly where the summary indicates.",
        })
        # Last role="user" → call_copilot sets X-Initiator="user" for this first request.
        # This is intentional: the new session starts with a fresh user turn.
        messages[:] = new_messages

        log.info(
            "copilot_session_reset old=%s new=%s resets=%d/%d",
            (old_interaction_id or "?")[:8], new_interaction_id[:8],
            state["session_resets_count"], MAX_SESSION_RESETS,
        )
        emit_progress(
            f"✅ Session reset #{state['session_resets_count']} complete: "
            f"new session {new_interaction_id[:8]}"
        )
    else:
        log.warning(
            "copilot_session_reset_failed interaction=%s — continuing without reset",
            (interaction_id or "?")[:8],
        )
        emit_progress("⚠️ Session reset failed: summary unavailable, continuing without reset")


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

    _maybe_apply_session_reset(state=state, messages=messages, emit_progress=emit_progress)

    round_idx = state["round_idx"]
    force_user_initiator = _consume_force_user_initiator(state)
    if force_user_initiator:
        state["accumulated_usage"]["_force_user_initiator"] = True
    msg = _call_llm_with_fallback(
        llm=llm,
        messages=messages,
        active_model=state["active_model"],
        tool_schemas=tool_schemas,
        active_effort=state["active_effort"],
        max_retries=state["max_retries"],
        drive_logs=drive_logs,
        task_id=task_id,
        round_idx=round_idx,
        event_queue=event_queue,
        accumulated_usage=state["accumulated_usage"],
        task_type=task_type,
        emit_progress=emit_progress,
        interaction_id=state.get("interaction_id"),
    )
    if msg is None:
        return (
            "⚠️ Failed to get a response from the model after retries/fallback. Try rephrasing your request.",
            state["accumulated_usage"],
            state["llm_trace"],
        )

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
    max_rounds = _copilot_max_rounds_cap(max_rounds, active_model)

    from ouroboros.tools import tool_discovery as _td

    _td.set_registry(tools)
    tool_schemas = tools.schemas(core_only=True)
    tool_schemas, _enabled_extra_tools = _setup_dynamic_tools(tools, tool_schemas, messages)
    if not tools_enabled_for_active_mode():
        tool_schemas = []
    tools._ctx.event_queue = event_queue
    tools._ctx.task_id = task_id

    runtime_diagnostics = get_runtime_diagnostics()
    initial_effort = _enforce_evolution_copilot_reasoning(
        task_type=task_type,
        active_model=active_model,
        active_effort=initial_effort,
    )
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
        "execution_style": execution_style_for_active_mode(),
        "runtime_diagnostics": runtime_diagnostics,
        "interaction_id": str(uuid.uuid4()),
        "force_user_initiator": False,
        "copilot_wrap_up_injected": False,
        "session_resets_count": 0,
    }
    # If caller provides a persistent executor (direct-chat), reuse it and
    # do NOT shut it down when this task ends — it survives across messages.
    owns_executor = persistent_executor is None
    stateful_executor = persistent_executor or _StatefulToolExecutor()
    owner_msg_seen: set = set()

    append_jsonl(drive_logs / "events.jsonl", {
        "ts": utc_now_iso(),
        "type": "task_runtime_mode",
        "task_id": task_id,
        "task_type": task_type,
        "mode_key": runtime_diagnostics.get("mode_key"),
        "execution_style": runtime_diagnostics.get("execution_style"),
        "tools_enabled": runtime_diagnostics.get("tools_enabled"),
        "max_rounds": runtime_diagnostics.get("max_rounds"),
        "main_requested_model": runtime_diagnostics.get("main", {}).get("requested_model"),
        "main_transport": runtime_diagnostics.get("main", {}).get("transport"),
        "main_actual_model": runtime_diagnostics.get("main", {}).get("actual_model"),
        "aux_requested_model": runtime_diagnostics.get("aux_light", {}).get("requested_model"),
        "aux_transport": runtime_diagnostics.get("aux_light", {}).get("transport"),
        "aux_actual_model": runtime_diagnostics.get("aux_light", {}).get("actual_model"),
        "background_requested_model": runtime_diagnostics.get("background", {}).get("requested_model"),
        "background_transport": runtime_diagnostics.get("background", {}).get("transport"),
        "background_actual_model": runtime_diagnostics.get("background", {}).get("actual_model"),
        "background_reasoning_effort": runtime_diagnostics.get("background_reasoning_effort"),
    })

    try:
        if state["execution_style"] == "one_shot":
            return _run_one_shot_mode(
                messages=messages,
                llm=llm,
                drive_logs=drive_logs,
                emit_progress=emit_progress,
                task_type=task_type,
                task_id=task_id,
                event_queue=event_queue,
                accumulated_usage=accumulated_usage,
                llm_trace=llm_trace,
                active_model=state["active_model"],
                active_effort=state["active_effort"],
                tool_schemas=[],
                interaction_id=state.get("interaction_id"),
            )
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
        # Log Copilot session summary if applicable
        try:
            from ouroboros.llm import model_transport
            if model_transport(state["active_model"]) == "copilot":
                from ouroboros.copilot_proxy import get_session_stats
                interaction_id = state.get("interaction_id")
                if interaction_id:
                    stats = get_session_stats(interaction_id)
                    if stats:
                        log.info(
                            "copilot_session_complete id=%s rounds=%d total_prompt=%d total_compl=%d premium_requests=%d duration=%.0fs",
                            interaction_id[:8],
                            stats["rounds"],
                            stats["total_prompt_tokens"],
                            stats["total_completion_tokens"],
                            stats["premium_requests"],
                            time.time() - stats.get("started", time.time()),
                        )
        except Exception:
            log.debug("Failed to log Copilot session summary", exc_info=True)

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
