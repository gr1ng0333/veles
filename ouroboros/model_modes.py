from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from supervisor.state import load_state, save_state


@dataclass(frozen=True)
class ModelMode:
    key: str
    model: str
    label: str
    description: str
    max_rounds: int
    tools_enabled: bool
    intended_use: str


MODEL_MODES: Dict[str, ModelMode] = {
    "codex": ModelMode(
        key="codex",
        model="codex/gpt-5.4",
        label="GPT-5.4 Codex",
        description="повседневный основной режим",
        max_rounds=200,
        tools_enabled=True,
        intended_use="основная рабочая модель",
    ),
    "haiku": ModelMode(
        key="haiku",
        model="anthropic/claude-haiku-4.5",
        label="Claude Haiku 4.5",
        description="быстрый и дешёвый рабочий режим",
        max_rounds=10,
        tools_enabled=True,
        intended_use="короткие рабочие задачи и дешёвый fallback-режим",
    ),
    "sonnet": ModelMode(
        key="sonnet",
        model="anthropic/claude-sonnet-4.6",
        label="Claude Sonnet 4.6",
        description="разговорный режим",
        max_rounds=1,
        tools_enabled=False,
        intended_use="разговор и одноходовый ответ",
    ),
    "opus": ModelMode(
        key="opus",
        model="anthropic/claude-opus-4.6",
        label="Claude Opus 4.6",
        description="режим планирования",
        max_rounds=1,
        tools_enabled=False,
        intended_use="подробное планирование и one-shot анализ",
    ),
}

DEFAULT_MODE_KEY = "codex"
STATE_KEY = "active_model_mode"


def _state_mode_key(st: Optional[Dict[str, Any]] = None) -> str:
    state = st if isinstance(st, dict) else load_state()
    key = str(state.get(STATE_KEY) or "").strip().lower()
    return key if key in MODEL_MODES else DEFAULT_MODE_KEY


def get_active_mode(st: Optional[Dict[str, Any]] = None) -> ModelMode:
    return MODEL_MODES[_state_mode_key(st)]


def persist_active_mode(mode_key: str) -> ModelMode:
    key = str(mode_key or "").strip().lower()
    if key not in MODEL_MODES:
        raise ValueError(f"Unknown model mode: {mode_key}")
    st = load_state()
    st[STATE_KEY] = key
    save_state(st)
    mode = MODEL_MODES[key]
    apply_mode_env(mode)
    return mode


def apply_mode_env(mode: Optional[ModelMode] = None) -> ModelMode:
    active = mode or get_active_mode()
    os.environ["OUROBOROS_MODEL"] = active.model
    os.environ["OUROBOROS_MAX_ROUNDS"] = str(active.max_rounds)
    os.environ["OUROBOROS_MODEL_TOOLS_ENABLED"] = "1" if active.tools_enabled else "0"
    os.environ.setdefault("OUROBOROS_MODEL_LIGHT", MODEL_MODES["haiku"].model)
    return active


def bootstrap_mode_env() -> ModelMode:
    return apply_mode_env(get_active_mode())


def mode_summary_text() -> str:
    mode = get_active_mode()
    light = os.environ.get("OUROBOROS_MODEL_LIGHT", MODEL_MODES["haiku"].model)
    return (
        "🔧 Current model mode:\n"
        f"• Mode: {mode.key}\n"
        f"• Main: {mode.model}\n"
        f"• Light: {light}\n"
        f"• Rounds limit: {mode.max_rounds}\n"
        f"• Tools: {'on' if mode.tools_enabled else 'off'}\n"
        f"• Purpose: {mode.intended_use}"
    )


def tools_enabled_for_active_mode() -> bool:
    return get_active_mode().tools_enabled
