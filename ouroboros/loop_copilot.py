"""Copilot-specific loop helpers.

Extracted from loop_runtime to keep that module under the 1000-line ceiling.
Handles: session reset, wrap-up injection, round caps, evolution delay.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from ouroboros.copilot_proxy import (
    COPILOT_SESSION_ROUND_LIMIT,
    should_reset_session,
    summarize_session_for_reset,
)
from ouroboros.llm import model_transport, normalize_reasoning_effort, reasoning_rank
from ouroboros.utils import log as _rootlog

log = _rootlog.getChild("loop_copilot") if hasattr(_rootlog, "getChild") else _rootlog

# ── Constants ──────────────────────────────────────────────────────────────────

MAX_SESSION_RESETS = 10
COPILOT_MAX_ROUNDS = COPILOT_SESSION_ROUND_LIMIT * MAX_SESSION_RESETS  # 280
COPILOT_WRAP_UP_ROUNDS_BEFORE = 2  # inject wrap-up N rounds before session boundary

EVOLUTION_COPILOT_REQUEST_DELAY_SEC = float(
    os.environ.get("EVOLUTION_COPILOT_REQUEST_DELAY_SEC", "4.0")
)


# ── Round cap helpers ──────────────────────────────────────────────────────────

def copilot_max_rounds_cap(default_max_rounds: int, active_model: str) -> int:
    """Ensure Copilot loop can run for at least MAX_SESSION_RESETS full sessions."""
    if model_transport(active_model) != "copilot":
        return default_max_rounds
    return max(default_max_rounds, COPILOT_MAX_ROUNDS)


# ── Reasoning effort ──────────────────────────────────────────────────────────

def enforce_evolution_copilot_reasoning(
    *, task_type: str, active_model: str, active_effort: str
) -> str:
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


# ── Evolution delay ───────────────────────────────────────────────────────────

def maybe_sleep_before_evolution_copilot_request(
    *, task_type: str, active_model: str, round_idx: int, phase: str
) -> None:
    if str(task_type or "").strip().lower() != "evolution":
        return
    if model_transport(active_model) != "copilot":
        return
    if phase == "primary" and round_idx <= 1:
        return
    delay_sec = max(0.0, EVOLUTION_COPILOT_REQUEST_DELAY_SEC)
    if delay_sec > 0:
        time.sleep(delay_sec)


# ── Wrap-up injection ─────────────────────────────────────────────────────────

def maybe_inject_copilot_wrap_up(
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
            f"⚠️ [WRAP-UP] Осталось {remaining} раундов до принудительного завершения "
            f"(лимит {max_rounds}). "
            "Завершай СЕЙЧАС: вызови update_scratchpad, закоммить незавершённые изменения, "
            "сформируй финальный ответ. Следующий раунд может быть последним."
        )
        messages.append({"role": "system", "content": warn})
        llm_trace["assistant_notes"].append(f"wrap_up_at_round_{round_idx}")
        return True

    # Copilot: trigger based on position within current 28-round session
    session_round = round_idx % COPILOT_SESSION_ROUND_LIMIT
    if session_round == 0:
        return False  # exactly at a boundary, no wrap-up
    rounds_until_boundary = COPILOT_SESSION_ROUND_LIMIT - session_round
    if rounds_until_boundary > COPILOT_WRAP_UP_ROUNDS_BEFORE:
        return False

    warn = (
        f"⚠️ [COPILOT SESSION BOUNDARY] Через {rounds_until_boundary} раунда(ов) произойдёт "
        f"session reset (шаг {session_round}/{COPILOT_SESSION_ROUND_LIMIT}). "
        "Контекст будет суммаризован и задача ПРОДОЛЖИТСЯ в новой сессии — "
        "НЕ считай это завершением задачи. "
        "Перед границей: вызови update_scratchpad с текущим прогрессом, "
        "закоммить незавершённые изменения, чтобы они пережили session reset."
    )
    messages.append({"role": "system", "content": warn})
    llm_trace["assistant_notes"].append(f"copilot_session_boundary_at_round_{round_idx}")
    return True


# ── Session reset ─────────────────────────────────────────────────────────────

def maybe_apply_session_reset(
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
            session_resets_done,
            MAX_SESSION_RESETS,
            (interaction_id or "?")[:8],
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
        new_messages.append(
            {
                "role": "user",
                "content": (
                    "Context from previous session:\n\n"
                    + summary
                    + "\n\n---\n\n"
                    "Continue working on the task from where you left off. "
                    "Pick up from the REMAINING/IN PROGRESS items in the summary above."
                ),
            }
        )
        new_messages.append(
            {
                "role": "system",
                "content": (
                    "Session context has been compacted. All tools remain available. "
                    "Continue execution from where the summary left off."
                ),
            }
        )
        # Structure is [system, user, system] — trailing system message ensures
        # last_role="system" → initiator="agent" → not billed as premium request.
        messages[:] = new_messages

        log.info(
            "copilot_session_reset old=%s new=%s resets=%d/%d",
            (old_interaction_id or "?")[:8],
            new_interaction_id[:8],
            state["session_resets_count"],
            MAX_SESSION_RESETS,
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
