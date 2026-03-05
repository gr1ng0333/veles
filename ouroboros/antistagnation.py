from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List

import logging

log = logging.getLogger(__name__)


@dataclass
class AntiStagnationConfig:
    stagnation_rounds: int = 8
    stagnation_grace: int = 4
    task_round_warn: int = 15
    task_round_cap: int = 30
    extension_cap: int = 50
    extension_progress_window: int = 5


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
        task_round_warn=_env_int("OUROBOROS_TASK_ROUND_WARN", 15),
        task_round_cap=_env_int("OUROBOROS_TASK_ROUND_CAP", 30),
        extension_cap=_env_int("OUROBOROS_TASK_ROUND_EXTENSION_CAP", 50),
        extension_progress_window=_env_int("OUROBOROS_TASK_PROGRESS_WINDOW", 5),
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
