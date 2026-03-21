from __future__ import annotations

import os

from ouroboros.model_modes import (
    MODEL_MODES,
    bootstrap_mode_env,
    execution_style_for_active_mode,
    get_background_model,
    get_background_reasoning_effort,
    get_runtime_diagnostics,
    get_runtime_policy,
    mode_summary_text,
    sync_mode_env_from_state,
)
from supervisor.state import load_state, save_state


def test_bootstrap_mode_env_uses_persisted_active_mode() -> None:
    st = load_state()
    old_mode = st.get("active_model_mode")
    old_model = os.environ.get("OUROBOROS_MODEL")
    old_rounds = os.environ.get("OUROBOROS_MAX_ROUNDS")
    old_tools = os.environ.get("OUROBOROS_MODEL_TOOLS_ENABLED")
    old_light = os.environ.get("OUROBOROS_MODEL_LIGHT")
    try:
        st["active_model_mode"] = "sonnet"
        save_state(st)
        mode = bootstrap_mode_env()
        assert mode.key == "sonnet"
        assert mode.model == "copilot/claude-sonnet-4.6"
        assert os.environ["OUROBOROS_MODEL"] == MODEL_MODES["sonnet"].model
        assert os.environ["OUROBOROS_MAX_ROUNDS"] == str(MODEL_MODES["sonnet"].max_rounds)
        assert os.environ["OUROBOROS_MODEL_TOOLS_ENABLED"] == "1"
        assert os.environ.get("OUROBOROS_MODEL_LIGHT")
    finally:
        st2 = load_state()
        if old_mode is None:
            st2.pop("active_model_mode", None)
        else:
            st2["active_model_mode"] = old_mode
        save_state(st2)
        if old_model is None:
            os.environ.pop("OUROBOROS_MODEL", None)
        else:
            os.environ["OUROBOROS_MODEL"] = old_model
        if old_rounds is None:
            os.environ.pop("OUROBOROS_MAX_ROUNDS", None)
        else:
            os.environ["OUROBOROS_MAX_ROUNDS"] = old_rounds
        if old_tools is None:
            os.environ.pop("OUROBOROS_MODEL_TOOLS_ENABLED", None)
        else:
            os.environ["OUROBOROS_MODEL_TOOLS_ENABLED"] = old_tools
        if old_light is None:
            os.environ.pop("OUROBOROS_MODEL_LIGHT", None)
        else:
            os.environ["OUROBOROS_MODEL_LIGHT"] = old_light


def test_runtime_policy_uses_active_mode_registry(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "qwen/qwen3-coder:free")
    monkeypatch.delenv("OUROBOROS_MODEL_BACKGROUND", raising=False)
    monkeypatch.delenv("CODEX_CONSCIOUSNESS_ACCESS", raising=False)
    monkeypatch.delenv("CODEX_CONSCIOUSNESS_REFRESH", raising=False)
    policy = get_runtime_policy({"active_model_mode": "haiku"})
    assert policy.mode_key == "haiku"
    assert policy.main_model == MODEL_MODES["haiku"].model
    assert policy.max_rounds == MODEL_MODES["haiku"].max_rounds
    assert policy.tools_enabled is True
    assert policy.main_model == "copilot/claude-haiku-4.5"
    assert policy.aux_light_model == "qwen/qwen3-coder:free"
    assert policy.background_model == "qwen/qwen3-coder:free"
    assert policy.background_reasoning_effort == "medium"


def test_mode_summary_text_for_codex_includes_mode_details(monkeypatch) -> None:
    from ouroboros import model_modes as mm

    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "qwen/qwen3-coder:free")
    monkeypatch.setenv("OUROBOROS_MODEL_BACKGROUND", "google/gemini-2.5-pro-preview")
    monkeypatch.setenv("OUROBOROS_BG_REASONING_EFFORT", "minimal")
    monkeypatch.delenv("CODEX_CONSCIOUSNESS_ACCESS", raising=False)
    monkeypatch.delenv("CODEX_CONSCIOUSNESS_REFRESH", raising=False)
    monkeypatch.setattr(mm, "get_active_mode", lambda st=None: MODEL_MODES["codex"])

    import ouroboros.codex_proxy as cp
    monkeypatch.setattr(
        cp,
        "get_accounts_status",
        lambda: [{"index": 2, "is_active": True, "usage_5h": 11, "usage_7d": 222}],
    )

    summary = mode_summary_text()
    assert "Mode: codex" in summary
    assert "Main: codex/gpt-5.4 → codex → gpt-5.4" in summary
    assert "Rounds limit: 200" in summary
    assert "Tools: on" in summary
    assert "Execution: loop" in summary
    assert "Aux light: qwen/qwen3-coder:free → openrouter → qwen/qwen3-coder:free" in summary
    assert "Background: google/gemini-2.5-pro-preview → openrouter → google/gemini-2.5-pro-preview" in summary
    assert "Background reasoning: minimal" in summary
    assert "Account: acc2" in summary
    assert "Limits: 5h=11 7d=222" in summary


def test_runtime_policy_exposes_execution_style(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "qwen/qwen3-coder:free")
    policy = get_runtime_policy({"active_model_mode": "opus"})
    assert policy.mode_key == "opus"
    assert policy.execution_style == "loop"


def test_execution_style_for_active_mode_reads_persisted_mode() -> None:
    st = load_state()
    old_mode = st.get("active_model_mode")
    try:
        st["active_model_mode"] = "sonnet"
        save_state(st)
        assert execution_style_for_active_mode() == "loop"
    finally:
        st2 = load_state()
        if old_mode is None:
            st2.pop("active_model_mode", None)
        else:
            st2["active_model_mode"] = old_mode
        save_state(st2)


def test_runtime_policy_uses_copilot_tags_for_sonnet_and_opus(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "qwen/qwen3-coder:free")
    sonnet = get_runtime_policy({"active_model_mode": "sonnet"})
    opus = get_runtime_policy({"active_model_mode": "opus"})
    assert sonnet.main_model == "copilot/claude-sonnet-4.6"
    assert opus.main_model == "copilot/claude-opus-4.6"
    assert sonnet.execution_style == "loop"
    assert opus.execution_style == "loop"


def test_background_model_prefers_explicit_background_override(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "qwen/qwen3-coder:free")
    monkeypatch.setenv("OUROBOROS_MODEL_BACKGROUND", "google/gemini-2.5-pro-preview")
    monkeypatch.delenv("CODEX_CONSCIOUSNESS_ACCESS", raising=False)
    monkeypatch.delenv("CODEX_CONSCIOUSNESS_REFRESH", raising=False)
    assert get_background_model() == "google/gemini-2.5-pro-preview"


def test_background_model_prefers_consciousness_codex_tokens(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "qwen/qwen3-coder:free")
    monkeypatch.setenv("OUROBOROS_MODEL_BACKGROUND", "google/gemini-2.5-pro-preview")
    monkeypatch.setenv("CODEX_CONSCIOUSNESS_ACCESS", "token")
    monkeypatch.setenv("CODEX_CONSCIOUSNESS_MODEL", "gpt-5.1-codex-mini")
    assert get_background_model() == "codex-consciousness/gpt-5.1-codex-mini"


def test_background_reasoning_effort_reads_explicit_env(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_BG_REASONING_EFFORT", "minimal")
    assert get_background_reasoning_effort() == "minimal"


def test_runtime_diagnostics_exposes_requested_transport_and_actual_models(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "qwen/qwen3-coder:free")
    monkeypatch.setenv("OUROBOROS_MODEL_BACKGROUND", "google/gemini-2.5-pro-preview")
    monkeypatch.setenv("CODEX_CONSCIOUSNESS_ACCESS", "token")
    diagnostics = get_runtime_diagnostics({"active_model_mode": "sonnet"})
    assert diagnostics["mode_key"] == "sonnet"
    assert diagnostics["main"]["requested_model"] == "copilot/claude-sonnet-4.6"
    assert diagnostics["main"]["transport"] == "copilot"
    assert diagnostics["main"]["actual_model"] == "claude-sonnet-4.6"
    assert diagnostics["aux_light"]["transport"] == "openrouter"
    assert diagnostics["background"]["transport"] == "codex-consciousness"



def test_sync_mode_env_from_state_overrides_stale_env() -> None:
    st = load_state()
    old_mode = st.get("active_model_mode")
    old_model = os.environ.get("OUROBOROS_MODEL")
    old_rounds = os.environ.get("OUROBOROS_MAX_ROUNDS")
    old_tools = os.environ.get("OUROBOROS_MODEL_TOOLS_ENABLED")
    try:
        st["active_model_mode"] = "codex"
        save_state(st)
        os.environ["OUROBOROS_MODEL"] = "copilot/claude-haiku-4.5"
        os.environ["OUROBOROS_MAX_ROUNDS"] = "10"
        os.environ["OUROBOROS_MODEL_TOOLS_ENABLED"] = "1"

        mode = sync_mode_env_from_state()

        assert mode.key == "codex"
        assert os.environ["OUROBOROS_MODEL"] == MODEL_MODES["codex"].model
        assert os.environ["OUROBOROS_MAX_ROUNDS"] == str(MODEL_MODES["codex"].max_rounds)
        assert os.environ["OUROBOROS_MODEL_TOOLS_ENABLED"] == "1"
    finally:
        st2 = load_state()
        if old_mode is None:
            st2.pop("active_model_mode", None)
        else:
            st2["active_model_mode"] = old_mode
        save_state(st2)
        if old_model is None:
            os.environ.pop("OUROBOROS_MODEL", None)
        else:
            os.environ["OUROBOROS_MODEL"] = old_model
        if old_rounds is None:
            os.environ.pop("OUROBOROS_MAX_ROUNDS", None)
        else:
            os.environ["OUROBOROS_MAX_ROUNDS"] = old_rounds
        if old_tools is None:
            os.environ.pop("OUROBOROS_MODEL_TOOLS_ENABLED", None)
        else:
            os.environ["OUROBOROS_MODEL_TOOLS_ENABLED"] = old_tools


def test_opus_agentic_loop_mode():
    """Opus must run in full agentic loop with tools enabled."""
    mode = MODEL_MODES["opus"]
    assert mode.execution_style == "loop"
    assert mode.tools_enabled is True
    assert mode.max_rounds == 100


def test_sonnet_agentic_loop_mode():
    """Sonnet must run in full agentic loop with tools enabled."""
    mode = MODEL_MODES["sonnet"]
    assert mode.execution_style == "loop"
    assert mode.tools_enabled is True
    assert mode.max_rounds == 50


def test_haiku_extended_rounds():
    """Haiku must support extended round limit for medium tasks."""
    mode = MODEL_MODES["haiku"]
    assert mode.execution_style == "loop"
    assert mode.tools_enabled is True
    assert mode.max_rounds == 30


def test_copilot_modes_all_loop():
    """All Copilot-backed modes must use loop execution style."""
    for key in ("haiku", "sonnet", "opus"):
        mode = MODEL_MODES[key]
        assert mode.execution_style == "loop", f"{key} must be loop, got {mode.execution_style}"
        assert mode.tools_enabled is True, f"{key} must have tools enabled"
