"""
Model Profiles — runtime configuration for LLM routing.

Each profile defines: model string, provider route, tools policy,
max rounds, fallback policy, and display metadata.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional, Dict

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Profile definitions
# ---------------------------------------------------------------------------


@dataclass
class ModelProfile:
    name: str                          # "codex", "haiku", "sonnet", "opus"
    model: str                         # полная model string для llm.chat()
    display_name: str                  # для сообщений в чат
    tools_enabled: bool = True
    max_rounds: int = 200              # 0 = use global default
    fallback_to: Optional[str] = None  # имя профиля-fallback или None
    auto_return_to: Optional[str] = None  # после ответа вернуться на этот профиль
    description: str = ""


# Реестр профилей — НЕ инстанцировать напрямую, использовать get_profile()
PROFILES: Dict[str, ModelProfile] = {
    "codex": ModelProfile(
        name="codex",
        model="codex/gpt-5.4",
        display_name="Codex GPT-5.4",
        tools_enabled=True,
        max_rounds=0,               # глобальный лимит (200)
        fallback_to="haiku",
        description="Default working mode — Codex OAuth, full tool loop",
    ),
    "haiku": ModelProfile(
        name="haiku",
        model="copilot/claude-haiku-4.5",
        display_name="Claude Haiku 4.5",
        tools_enabled=True,
        max_rounds=10,
        fallback_to=None,           # haiku — конечная точка, нет дальнейшего fallback
        description="Fallback & lightweight working mode — Copilot Pro",
    ),
    "sonnet": ModelProfile(
        name="sonnet",
        model="copilot/claude-sonnet-4.6",
        display_name="Claude Sonnet 4.6",
        tools_enabled=False,
        max_rounds=1,
        fallback_to=None,
        description="Conversation mode — single-shot, no tools",
    ),
    "opus": ModelProfile(
        name="opus",
        model="copilot/claude-opus-4.6",
        display_name="Claude Opus 4.6",
        tools_enabled=False,
        max_rounds=1,
        fallback_to=None,
        auto_return_to="codex",     # после ответа Opus → автовозврат на Codex
        description="Strategic mode — single-shot planner / heavy analysis",
    ),
}


def get_profile(name: str) -> Optional[ModelProfile]:
    """Get profile by name. Returns None if not found."""
    return PROFILES.get(name)


def get_profile_by_model(model: str) -> Optional[ModelProfile]:
    """Reverse lookup: find profile by model string."""
    for p in PROFILES.values():
        if p.model == model:
            return p
    return None


# ---------------------------------------------------------------------------
# Active profile state (runtime, NOT env-based)
# ---------------------------------------------------------------------------

_active_profile: str = "codex"
_codex_cooldown_until: float = 0.0     # Unix timestamp
_fallback_reason: str = ""             # "timeout" | "rate_limit" | ""
_manual_switch: bool = False           # True если переключено командой, False если автоматически


def get_active_profile_name() -> str:
    """Current active profile name."""
    global _active_profile, _codex_cooldown_until, _fallback_reason
    # Проверяем: если мы на автоматическом fallback и cooldown истёк — возвращаемся
    if (not _manual_switch
            and _active_profile == "haiku"
            and _codex_cooldown_until > 0
            and time.time() >= _codex_cooldown_until):
        log.info("[profiles] Codex cooldown expired, auto-returning to codex")
        _active_profile = "codex"
        _codex_cooldown_until = 0.0
        _fallback_reason = ""
    return _active_profile


def get_active_profile() -> ModelProfile:
    """Current active profile object."""
    return PROFILES[get_active_profile_name()]


def switch_profile(name: str, manual: bool = True) -> ModelProfile:
    """
    Switch active profile.
    manual=True  → user command (/haiku, /opus, etc.)
    manual=False → automatic fallback
    """
    global _active_profile, _manual_switch, _codex_cooldown_until, _fallback_reason
    if name not in PROFILES:
        raise ValueError(f"Unknown profile: {name}")
    _active_profile = name
    _manual_switch = manual
    if manual:
        # ручное переключение сбрасывает cooldown
        _codex_cooldown_until = 0.0
        _fallback_reason = ""
    profile = PROFILES[name]
    # Обновляем OUROBOROS_MODEL для совместимости с существующим кодом
    os.environ["OUROBOROS_MODEL"] = profile.model
    log.info("[profiles] Switched to %s (model=%s, manual=%s)", name, profile.model, manual)
    return profile


def activate_codex_fallback(reason: str = "timeout", cooldown_sec: int = 3600) -> ModelProfile:
    """
    Activate automatic codex → haiku fallback with cooldown.
    reason: "timeout" | "rate_limit"
    """
    global _codex_cooldown_until, _fallback_reason
    _codex_cooldown_until = time.time() + cooldown_sec
    _fallback_reason = reason
    log.warning(
        "[profiles] Codex fallback activated: reason=%s, cooldown=%ds, until=%s",
        reason, cooldown_sec, time.strftime("%H:%M:%S", time.localtime(_codex_cooldown_until)),
    )
    return switch_profile("haiku", manual=False)


def get_codex_cooldown_remaining() -> int:
    """Seconds remaining on codex cooldown. 0 if not in cooldown."""
    if _codex_cooldown_until <= 0:
        return 0
    remaining = _codex_cooldown_until - time.time()
    return max(0, int(remaining))


def get_fallback_reason() -> str:
    """Why we're on fallback. Empty string if not on fallback."""
    return _fallback_reason


def is_manual_switch() -> bool:
    """Whether current profile was set manually."""
    return _manual_switch


def get_status_dict() -> Dict:
    """Full status for /model command."""
    profile = get_active_profile()
    cooldown = get_codex_cooldown_remaining()
    return {
        "profile": profile.name,
        "model": profile.model,
        "display_name": profile.display_name,
        "tools": profile.tools_enabled,
        "max_rounds": profile.max_rounds if profile.max_rounds > 0 else int(os.environ.get("OUROBOROS_MAX_ROUNDS", "200")),
        "fallback_to": profile.fallback_to,
        "auto_return_to": profile.auto_return_to,
        "manual": _manual_switch,
        "codex_cooldown_sec": cooldown,
        "codex_cooldown_human": f"{cooldown // 60}m {cooldown % 60}s" if cooldown > 0 else "",
        "fallback_reason": _fallback_reason,
    }
