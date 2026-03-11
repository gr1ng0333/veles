from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from supervisor.state import load_state, save_state


DEFAULT_AUX_LIGHT_MODEL = "qwen/qwen3-coder:free"


@dataclass(frozen=True)
class ModelMode:
    key: str
    model: str
    label: str
    description: str
    max_rounds: int
    tools_enabled: bool
    intended_use: str
    execution_style: str


@dataclass(frozen=True)
class ModeRuntimePolicy:
    mode_key: str
    main_model: str
    max_rounds: int
    tools_enabled: bool
    intended_use: str
    execution_style: str
    aux_light_model: str

MODEL_MODES: Dict[str, ModelMode] = {
    "codex": ModelMode(
        key="codex",
        model="codex/gpt-5.4",
        label="GPT-5.4 Codex",
        description="повседневный основной режим",
        max_rounds=200,
        tools_enabled=True,
        intended_use="основная рабочая модель",
        execution_style="loop",
    ),
    "haiku": ModelMode(
        key="haiku",
        model="copilot/claude-haiku-4.5",
        label="Claude Haiku 4.5",
        description="быстрый и дешёвый рабочий режим",
        max_rounds=10,
        tools_enabled=True,
        intended_use="короткие рабочие задачи и дешёвый fallback-режим",
        execution_style="loop",
    ),
    "sonnet": ModelMode(
        key="sonnet",
        model="anthropic/claude-sonnet-4.6",
        label="Claude Sonnet 4.6",
        description="разговорный режим",
        max_rounds=1,
        tools_enabled=False,
        intended_use="разговор и одноходовый ответ",
        execution_style="one_shot",
    ),
    "opus": ModelMode(
        key="opus",
        model="anthropic/claude-opus-4.6",
        label="Claude Opus 4.6",
        description="режим планирования",
        max_rounds=1,
        tools_enabled=False,
        intended_use="подробное планирование и one-shot анализ",
        execution_style="one_shot",
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
    os.environ.setdefault("OUROBOROS_MODEL_LIGHT", DEFAULT_AUX_LIGHT_MODEL)
    return active


def bootstrap_mode_env() -> ModelMode:
    return apply_mode_env(get_active_mode())


def get_aux_light_model() -> str:
    return os.environ.get("OUROBOROS_MODEL_LIGHT", "").strip() or DEFAULT_AUX_LIGHT_MODEL


def get_runtime_policy(st: Optional[Dict[str, Any]] = None) -> ModeRuntimePolicy:
    mode = get_active_mode(st)
    return ModeRuntimePolicy(
        mode_key=mode.key,
        main_model=mode.model,
        max_rounds=mode.max_rounds,
        tools_enabled=mode.tools_enabled,
        intended_use=mode.intended_use,
        execution_style=mode.execution_style,
        aux_light_model=get_aux_light_model(),
    )


def mode_summary_text() -> str:
    policy = get_runtime_policy()
    lines = [
        "🔧 Current model mode:",
        f"• Mode: {policy.mode_key}",
        f"• Main: {policy.main_model}",
        f"• Rounds limit: {policy.max_rounds}",
        f"• Tools: {'on' if policy.tools_enabled else 'off'}",
        f"• Execution: {policy.execution_style}",
        f"• Purpose: {policy.intended_use}",
        f"• Aux light model: {policy.aux_light_model}",
    ]
    if policy.mode_key == "codex":
        try:
            from ouroboros.codex_proxy import get_accounts_status
            statuses = get_accounts_status()
            active = next((acc for acc in statuses if acc.get("is_active")), None)
            if active is not None:
                lines.append(f"• Account: acc{int(active.get('index', 0))}")
                lines.append(
                    f"• Limits: 5h={int(active.get('usage_5h', 0))} 7d={int(active.get('usage_7d', 0))}"
                )
        except Exception:
            pass
    return "\n".join(lines)


def tools_enabled_for_active_mode() -> bool:
    return get_runtime_policy().tools_enabled


def max_rounds_for_active_mode() -> int:
    return get_runtime_policy().max_rounds



def execution_style_for_active_mode() -> str:
    return get_runtime_policy().execution_style
