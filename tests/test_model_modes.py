from __future__ import annotations

import os

from ouroboros.model_modes import MODEL_MODES, bootstrap_mode_env, execution_style_for_active_mode, get_runtime_policy, mode_summary_text
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
        assert os.environ["OUROBOROS_MODEL"] == MODEL_MODES["sonnet"].model
        assert os.environ["OUROBOROS_MAX_ROUNDS"] == str(MODEL_MODES["sonnet"].max_rounds)
        assert os.environ["OUROBOROS_MODEL_TOOLS_ENABLED"] == "0"
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
    policy = get_runtime_policy({"active_model_mode": "haiku"})
    assert policy.mode_key == "haiku"
    assert policy.main_model == MODEL_MODES["haiku"].model
    assert policy.max_rounds == MODEL_MODES["haiku"].max_rounds
    assert policy.tools_enabled is True
    assert policy.aux_light_model == "qwen/qwen3-coder:free"


def test_mode_summary_text_for_codex_includes_mode_details(monkeypatch) -> None:
    from ouroboros import model_modes as mm

    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "qwen/qwen3-coder:free")
    monkeypatch.setattr(mm, "get_active_mode", lambda st=None: MODEL_MODES["codex"])

    def _fake_statuses():
        return [{"index": 2, "is_active": True, "usage_5h": 11, "usage_7d": 222}]

    import ouroboros.codex_proxy as cp
    monkeypatch.setattr(cp, "get_accounts_status", _fake_statuses)

    summary = mode_summary_text()
    assert "Mode: codex" in summary
    assert "Main: codex/gpt-5.4" in summary
    assert "Rounds limit: 200" in summary
    assert "Tools: on" in summary
    assert "Execution: loop" in summary
    assert "Aux light model: qwen/qwen3-coder:free" in summary
    assert "Account: acc2" in summary
    assert "Limits: 5h=11 7d=222" in summary



def test_runtime_policy_exposes_execution_style(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "qwen/qwen3-coder:free")
    policy = get_runtime_policy({"active_model_mode": "opus"})
    assert policy.mode_key == "opus"
    assert policy.execution_style == "one_shot"


def test_execution_style_for_active_mode_reads_persisted_mode() -> None:
    st = load_state()
    old_mode = st.get("active_model_mode")
    try:
        st["active_model_mode"] = "sonnet"
        save_state(st)
        assert execution_style_for_active_mode() == "one_shot"
    finally:
        st2 = load_state()
        if old_mode is None:
            st2.pop("active_model_mode", None)
        else:
            st2["active_model_mode"] = old_mode
        save_state(st2)
