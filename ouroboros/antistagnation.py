from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import logging

log = logging.getLogger(__name__)


@dataclass
class AntiStagnationConfig:
    stagnation_rounds: int = 8
    stagnation_grace: int = 4
    task_round_warn: int = 250
    task_round_cap: int = 280
    extension_cap: int = 350
    extension_progress_window: int = 5
    task_max_rounds: int = 280
    small_completion_threshold: int = 100
    small_completion_max_rounds: int = 8
    context_drop_pct: int = 30


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        log.warning("Invalid %s=%r, using default %d", name, raw, default)
        return default
    return max(minimum, value)


def load_antistagnation_config() -> AntiStagnationConfig:
    return AntiStagnationConfig(
        stagnation_rounds=_env_int("OUROBOROS_STAGNATION_ROUNDS", 8),
        stagnation_grace=_env_int("OUROBOROS_STAGNATION_GRACE", 4),
        task_round_warn=_env_int("OUROBOROS_TASK_ROUND_WARN", 250),
        task_round_cap=_env_int("OUROBOROS_TASK_ROUND_CAP", 280),
        extension_cap=_env_int("OUROBOROS_TASK_ROUND_EXTENSION_CAP", 350),
        extension_progress_window=_env_int("OUROBOROS_TASK_PROGRESS_WINDOW", 5),
        task_max_rounds=_env_int("OUROBOROS_TASK_MAX_ROUNDS", 280),
        small_completion_threshold=_env_int("OUROBOROS_SMALL_COMPLETION_THRESHOLD", 100),
        small_completion_max_rounds=_env_int("OUROBOROS_SMALL_COMPLETION_MAX_ROUNDS", 8),
        context_drop_pct=_env_int("OUROBOROS_CONTEXT_DROP_PCT", 30, minimum=5),
    )


def inject_stagnation_self_check(
    messages: List[Dict[str, Any]],
    *,
    no_progress_rounds: int,
    threshold: int,
    grace: int,
) -> None:
    messages.append({
        "role": "system",
        "content": (
            "[STAGNATION_SELF_CHECK] "
            f"No meaningful progress for {no_progress_rounds} rounds (threshold={threshold}, grace={grace}). "
            "In your next assistant message, explicitly choose ONE action tag at the top: "
            "tool_needed | finalize_now | ask_owner. "
            "If tool_needed: call exactly one tool with concrete args. "
            "If finalize_now: provide concise final answer now. "
            "If ask_owner: ask one precise blocking question."
        ),
    })


def build_forced_finalize_reason(prefix: str, *, no_progress_rounds: int, round_idx: int) -> str:
    return (
        f"⚠️ {prefix} (round={round_idx}, no_progress={no_progress_rounds}). "
        "Give a concise summary: what is done, what remains, and one next best action."
    )


def compute_round_limit(recent_progress: List[bool], cap: int, extension_cap: int, progress_window: int) -> int:
    tail = recent_progress[-progress_window:] if progress_window > 0 else recent_progress[-5:]
    return extension_cap if any(tail) else cap


def should_force_round_finalize(round_idx: int, recent_progress: List[bool], cfg: AntiStagnationConfig) -> bool:
    if round_idx < cfg.task_round_cap:
        return False
    limit = compute_round_limit(recent_progress, cfg.task_round_cap, cfg.extension_cap, cfg.extension_progress_window)
    return round_idx >= limit


def stagnation_action(no_progress_rounds: int, cfg: AntiStagnationConfig, already_injected: bool) -> str:
    if no_progress_rounds >= (cfg.stagnation_rounds + cfg.stagnation_grace):
        return "force_finalize"
    if no_progress_rounds >= cfg.stagnation_rounds and not already_injected:
        return "inject_self_check"
    return "none"


def is_small_completion_stagnation(
    recent_completion_tokens: List[int],
    cfg: AntiStagnationConfig,
    *,
    task_type: str = "",
    has_tool_calls: bool = False,
) -> bool:
    """Return True if the last N rounds all had completion_tokens below threshold.

    Rounds with tool_calls are never considered stagnant (tool work is real work).
    For evolution tasks: threshold is halved (50 instead of 100).
    """
    # Any round with tool calls = real work, not stagnation
    if has_tool_calls:
        return False
    n = cfg.small_completion_max_rounds
    if len(recent_completion_tokens) < n:
        return False
    threshold = cfg.small_completion_threshold
    if task_type == "evolution":
        threshold = threshold // 2
    return all(t < threshold for t in recent_completion_tokens[-n:])


def detect_context_overflow(
    current_prompt_tokens: int,
    prev_prompt_tokens: int,
    cfg: AntiStagnationConfig,
) -> bool:
    """Return True if prompt_tokens dropped more than context_drop_pct% from previous round."""
    if prev_prompt_tokens <= 0 or current_prompt_tokens <= 0:
        return False
    drop_ratio = 1.0 - (current_prompt_tokens / prev_prompt_tokens)
    return drop_ratio > (cfg.context_drop_pct / 100.0)


# ---------------------------------------------------------------------------
# Evolution Write Anchor
# ---------------------------------------------------------------------------
# Tool names (or prefixes) that count as "write actions" — evidence that
# the agent is producing something, not just reading.
_WRITE_TOOL_PREFIXES = (
    "repo_write_commit",
    "repo_commit_push",
    "drive_write",
    "knowledge_write",
    "update_scratchpad",
    "update_identity",
    "run_shell",          # shell can write/commit — conservative: count it
    "external_repo_script",
    "plan_step_done",
    "plan_complete",
    "send_document",
    "send_owner_message",
)

# Rounds without write where we inject warnings (evolution tasks only)
_WRITE_WARN_ROUND = int(os.environ.get("OUROBOROS_WRITE_WARN_ROUND", "8"))
_WRITE_CRITICAL_ROUND = int(os.environ.get("OUROBOROS_WRITE_CRITICAL_ROUND", "15"))


def is_write_tool_call(tool_name: str) -> bool:
    """Return True if this tool call counts as a write/progress action."""
    name = (tool_name or "").lower()
    return any(name.startswith(prefix) for prefix in _WRITE_TOOL_PREFIXES)


def inject_write_anchor_deliverable(messages: List[Dict[str, Any]], task_text: str) -> None:
    """Inject round-1 deliverable declaration request for evolution tasks."""
    messages.append({
        "role": "system",
        "content": (
            "[EVOLUTION_DELIVERABLE] Before doing anything else, declare in ONE sentence: "
            "what exact file/function/test will be different after this task completes? "
            "Example: 'Deliverable: add X to ouroboros/Y.py and update tests/test_Y.py'. "
            "Then proceed. Task: " + (task_text[:400] if task_text else "(see above)")
        ),
    })


def maybe_inject_write_anchor(
    messages: List[Dict[str, Any]],
    *,
    round_idx: int,
    write_round_count: int,
    task_type: str,
    write_warn_injected: bool,
    write_critical_injected: bool,
) -> tuple[bool, bool]:
    """Inject read-only warnings if evolution task hasn't written anything yet.

    Returns updated (write_warn_injected, write_critical_injected).
    Only active for evolution tasks.
    """
    if task_type != "evolution":
        return write_warn_injected, write_critical_injected
    if write_round_count > 0:
        # Already written something — anchors no longer needed
        return write_warn_injected, write_critical_injected

    if round_idx == _WRITE_WARN_ROUND and not write_warn_injected:
        messages.append({
            "role": "system",
            "content": (
                f"[READ_ONLY_WARN] Round {round_idx}: no write/commit tool called yet. "
                "Reading without writing is not evolution. "
                "If you now understand what needs to change — make the change. "
                "If you still need more information — state exactly what is missing and why."
            ),
        })
        return True, write_critical_injected

    if round_idx == _WRITE_CRITICAL_ROUND and not write_critical_injected:
        messages.append({
            "role": "system",
            "content": (
                f"[COMMIT_REQUIRED] Round {round_idx}: still no write/commit. "
                "You MUST make a concrete change now — write a file, commit, or explicitly declare "
                "'nothing worth changing' with a one-sentence reason. "
                "Continuing to read without writing is forbidden past this point."
            ),
        })
        return write_warn_injected, True

    return write_warn_injected, write_critical_injected
