"""LLM fallback logic extracted from loop_runtime.

Handles: transport error detection, fallback candidate list building,
and the full _call_llm_with_fallback orchestration loop.
"""

from __future__ import annotations

import os
import queue
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ouroboros.copilot_proxy import CopilotServerCooldownError
from ouroboros.llm import LLMClient, model_transport
from ouroboros.loop_copilot import maybe_sleep_before_evolution_copilot_request
from ouroboros.utils import append_jsonl, log as _rootlog, utc_now_iso

log = _rootlog.getChild("loop_fallback") if hasattr(_rootlog, "getChild") else _rootlog


# ── Error classification ───────────────────────────────────────────────────────

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
    import urllib.error

    msg = str(exc)
    if isinstance(exc, RuntimeError) and "All Copilot accounts exhausted" in msg:
        return True
    if "timed out" in msg.lower() or "TimeoutError" in type(exc).__name__:
        return True
    if "IncompleteRead" in type(exc).__name__ or "IncompleteRead" in msg:
        return True
    if isinstance(exc, urllib.error.HTTPError) and exc.code >= 500:
        return True
    if isinstance(exc, (urllib.error.URLError, OSError, ConnectionError)):
        return True
    return False


def is_transport_timeout_error(exc: Exception, transport: str) -> bool:
    """Unified transport-aware timeout/error check."""
    if transport == "copilot":
        return _is_copilot_timeout_error(exc)
    return _is_codex_timeout_error(exc)


# ── Candidate list ─────────────────────────────────────────────────────────────

def build_fallback_candidates(active_model: str, transport: str) -> List[str]:
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
    return [
        m.strip()
        for m in fallback_list_raw.split(",")
        if m.strip() and m.strip() != active_model
    ]


# ── Last error helpers ─────────────────────────────────────────────────────────

def consume_last_llm_error(
    accumulated_usage: Dict[str, Any], model: str
) -> Optional[str]:
    err_model = accumulated_usage.get("_last_llm_error_model")
    err_text = accumulated_usage.get("_last_llm_error")
    if err_model != model:
        return None
    accumulated_usage["_last_llm_error"] = None
    accumulated_usage["_last_llm_error_model"] = None
    return err_text or None


# ── Main fallback orchestrator ────────────────────────────────────────────────

def call_llm_with_fallback(
    *,
    llm: LLMClient,
    messages: List[Dict[str, Any]],
    active_model: str,
    tool_schemas: List[Dict[str, Any]],
    active_effort: str,
    max_retries: int,
    drive_logs,  # pathlib.Path
    task_id: str,
    round_idx: int,
    event_queue: Optional[queue.Queue],
    accumulated_usage: Dict[str, Any],
    task_type: str,
    emit_progress: Callable[[str], None],
    interaction_id: Optional[str] = None,
    # injected to avoid circular import
    _call_llm_with_retry_fn: Optional[Callable] = None,
) -> Optional[Dict[str, Any]]:
    """Try primary model; on failure, walk the fallback chain."""
    from ouroboros.loop import _call_llm_with_retry as _default_retry

    _call_llm_with_retry = _call_llm_with_retry_fn or _default_retry

    primary_exc: Optional[Exception] = None
    transport = model_transport(active_model)
    primary_force_user_initiator = bool(accumulated_usage.pop("_force_user_initiator", False))

    def _call_candidate(
        model: str, phase: str
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[Exception]]:
        force_user_initiator = (
            primary_force_user_initiator if phase.startswith("primary") else False
        )
        candidate_transport = model_transport(model)
        try:
            if candidate_transport == "copilot":
                maybe_sleep_before_evolution_copilot_request(
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
            return msg, consume_last_llm_error(accumulated_usage, model), None
        except Exception as exc:
            return None, consume_last_llm_error(accumulated_usage, model), exc

    while True:
        msg, primary_error_text, primary_exc = _call_candidate(active_model, "primary")
        if msg is not None:
            return msg

        if transport == "copilot" and isinstance(primary_exc, CopilotServerCooldownError):
            cooldown_sec = int(primary_exc.cooldown_sec or 60)
            append_jsonl(
                drive_logs / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "copilot_server_cooldown",
                    "task_id": task_id,
                    "round": round_idx,
                    "model": active_model,
                    "interaction_id": interaction_id,
                    "account_idx": primary_exc.account_idx,
                    "status_code": primary_exc.status_code,
                    "cooldown_sec": cooldown_sec,
                },
            )
            emit_progress(
                f"⚠️ Copilot {active_model} вернул {primary_exc.status_code} "
                f"на acc#{primary_exc.account_idx}. "
                f"Ставлю cooldown на {cooldown_sec}с и повторяю этот же раунд."
            )
            time.sleep(cooldown_sec)
            continue

        primary_reason = (
            f"timeout/error: {primary_exc or primary_error_text}"
            if (primary_exc or primary_error_text)
            else "empty response"
        )
        if primary_exc is None and not is_transport_timeout_error(
            RuntimeError(primary_reason), transport
        ):
            return None
        break

    fallback_candidates = build_fallback_candidates(active_model, transport)
    if not fallback_candidates:
        if primary_exc is not None:
            raise primary_exc
        return None

    previous_model = active_model
    previous_reason = (
        f"timeout/error: {primary_exc or primary_error_text}"
        if (primary_exc or primary_error_text)
        else "empty response"
    )
    last_exc: Optional[Exception] = primary_exc or (
        RuntimeError(primary_error_text) if primary_error_text else None
    )

    for fallback_model in fallback_candidates:
        if (
            model_transport(previous_model) == "copilot"
            and model_transport(fallback_model) == "codex"
        ):
            emit_progress("↪️ Copilot exhausted/unstable — передаю этот же раунд в Codex.")

        emit_progress(
            f"⚡ Fallback: {previous_model} → {fallback_model} ({previous_reason})"
        )
        log.warning(
            "Falling back from %s to %s: %s", previous_model, fallback_model, previous_reason
        )

        msg, fallback_error_text, fallback_exc = _call_candidate(fallback_model, "fallback")
        if msg is not None:
            return msg

        previous_model = fallback_model
        previous_reason = (
            f"timeout/error: {fallback_exc or fallback_error_text}"
            if (fallback_exc or fallback_error_text)
            else "empty response"
        )
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
