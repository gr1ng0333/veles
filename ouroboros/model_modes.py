from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from supervisor.state import load_state, save_state


DEFAULT_AUX_LIGHT_MODEL = "qwen/qwen3-coder:free"
DEFAULT_BACKGROUND_OPENROUTER_MODEL = DEFAULT_AUX_LIGHT_MODEL
DEFAULT_CONSCIOUSNESS_CODEX_MODEL = "gpt-5.4-codex-mini"

# Valid reasoning effort levels (Codex API)
VALID_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
DEFAULT_REASONING_EFFORT = "medium"
REASONING_EFFORT_STATE_KEY = "codex_reasoning_effort"


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
    reasoning_effort: str = DEFAULT_REASONING_EFFORT  # only meaningful for codex transport


@dataclass(frozen=True)
class ModeRuntimePolicy:
    mode_key: str
    main_model: str
    max_rounds: int
    tools_enabled: bool
    intended_use: str
    execution_style: str
    aux_light_model: str
    background_model: str
    background_reasoning_effort: str
    reasoning_effort: str = DEFAULT_REASONING_EFFORT


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
        reasoning_effort=DEFAULT_REASONING_EFFORT,
    ),
    "haiku": ModelMode(
        key="haiku",
        model="copilot/claude-haiku-4.5",
        label="Claude Haiku 4.5",
        description="быстрый и дешёвый рабочий режим",
        max_rounds=30,
        tools_enabled=True,
        intended_use="короткие рабочие задачи и дешёвый fallback-режим",
        execution_style="loop",
    ),
    "sonnet": ModelMode(
        key="sonnet",
        model="copilot/claude-sonnet-4.6",
        label="Claude Sonnet 4.6",
        description="сбалансированный рабочий режим",
        max_rounds=50,
        tools_enabled=True,
        intended_use="средние задачи: написание кода, исследование, диагностика",
        execution_style="loop",
    ),
    "opus": ModelMode(
        key="opus",
        model="copilot/claude-opus-4.6",
        label="Claude Opus 4.6",
        description="мощный автономный режим для сложных задач",
        max_rounds=100,
        tools_enabled=True,
        intended_use="сложные задачи: рефакторинг, code review, архитектурные изменения, работа с репозиториями",
        execution_style="loop",
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


def get_codex_reasoning_effort(st: Optional[Dict[str, Any]] = None) -> str:
    """Return persisted codex reasoning effort (independent from model mode)."""
    state = st if isinstance(st, dict) else load_state()
    effort = str(state.get(REASONING_EFFORT_STATE_KEY) or "").strip().lower()
    return effort if effort in VALID_REASONING_EFFORTS else DEFAULT_REASONING_EFFORT


def set_codex_reasoning_effort(effort: str) -> str:
    """Persist codex reasoning effort and update env. Returns normalized effort."""
    effort = str(effort or "").strip().lower()
    if effort not in VALID_REASONING_EFFORTS:
        raise ValueError(f"Unknown reasoning effort: {effort!r}. Valid: {VALID_REASONING_EFFORTS}")
    st = load_state()
    st[REASONING_EFFORT_STATE_KEY] = effort
    save_state(st)
    os.environ["OUROBOROS_REASONING_EFFORT"] = effort
    return effort


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
    # Apply codex reasoning effort: use persisted value, fall back to mode default
    effort = get_codex_reasoning_effort()
    os.environ["OUROBOROS_REASONING_EFFORT"] = effort
    return active


def bootstrap_mode_env() -> ModelMode:
    return apply_mode_env(get_active_mode())


def sync_mode_env_from_state(st: Optional[Dict[str, Any]] = None) -> ModelMode:
    """Re-apply persisted active mode to process env.

    Needed for long-lived launcher/chat/worker processes after restarts or
    mode switches so each task starts from persisted truth, not stale env.
    """
    return apply_mode_env(get_active_mode(st))


def get_aux_light_model() -> str:
    return os.environ.get("OUROBOROS_MODEL_LIGHT", "").strip() or DEFAULT_AUX_LIGHT_MODEL


def get_background_model() -> str:
    if os.environ.get("CODEX_CONSCIOUSNESS_ACCESS") or os.environ.get("CODEX_CONSCIOUSNESS_REFRESH"):
        model_name = os.environ.get("CODEX_CONSCIOUSNESS_MODEL", DEFAULT_CONSCIOUSNESS_CODEX_MODEL).strip()
        return f"codex-consciousness/{model_name or DEFAULT_CONSCIOUSNESS_CODEX_MODEL}"
    return os.environ.get("OUROBOROS_MODEL_BACKGROUND", "").strip() or get_aux_light_model() or DEFAULT_BACKGROUND_OPENROUTER_MODEL


def get_background_reasoning_effort() -> str:
    return os.environ.get("OUROBOROS_BG_REASONING_EFFORT", "").strip().lower() or "medium"


def get_runtime_policy(st: Optional[Dict[str, Any]] = None) -> ModeRuntimePolicy:
    mode = get_active_mode(st)
    effort = get_codex_reasoning_effort(st)
    return ModeRuntimePolicy(
        mode_key=mode.key,
        main_model=mode.model,
        max_rounds=mode.max_rounds,
        tools_enabled=mode.tools_enabled,
        intended_use=mode.intended_use,
        execution_style=mode.execution_style,
        aux_light_model=get_aux_light_model(),
        background_model=get_background_model(),
        background_reasoning_effort=get_background_reasoning_effort(),
        reasoning_effort=effort,
    )


def _model_diagnostics(model: str) -> Dict[str, str]:
    from ouroboros.llm import model_transport, transport_model_name

    requested = str(model or "").strip()
    return {
        "requested_model": requested,
        "transport": model_transport(requested),
        "actual_model": transport_model_name(requested),
    }


def get_runtime_diagnostics(st: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    policy = get_runtime_policy(st)
    return {
        "mode_key": policy.mode_key,
        "execution_style": policy.execution_style,
        "tools_enabled": policy.tools_enabled,
        "max_rounds": policy.max_rounds,
        "intended_use": policy.intended_use,
        "background_reasoning_effort": policy.background_reasoning_effort,
        "reasoning_effort": policy.reasoning_effort,
        "main": _model_diagnostics(policy.main_model),
        "aux_light": _model_diagnostics(policy.aux_light_model),
        "background": _model_diagnostics(policy.background_model),
    }


def _format_model_line(label: str, entry: Dict[str, Any]) -> str:
    return (
        f"• {label}: {entry.get('requested_model', '')} "
        f"→ {entry.get('transport', '')} "
        f"→ {entry.get('actual_model', '')}"
    )


def mode_summary_text() -> str:
    policy = get_runtime_policy()
    diagnostics = get_runtime_diagnostics()
    lines = [
        "🔧 Current model mode:",
        f"• Mode: {policy.mode_key}",
        _format_model_line("Main", diagnostics["main"]),
        f"• Rounds limit: {policy.max_rounds}",
        f"• Tools: {'on' if policy.tools_enabled else 'off'}",
        f"• Execution: {policy.execution_style}",
        f"• Purpose: {policy.intended_use}",
        _format_model_line("Aux light", diagnostics["aux_light"]),
        _format_model_line("Background", diagnostics["background"]),
        f"• Background reasoning: {policy.background_reasoning_effort}",
    ]
    # Show reasoning effort for codex transport
    if policy.mode_key == "codex":
        lines.append(f"• Reasoning effort: {policy.reasoning_effort}  (change: /low /medium /high /xhigh)")
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
