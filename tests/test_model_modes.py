from __future__ import annotations

import os

from ouroboros.model_modes import MODEL_MODES, bootstrap_mode_env
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
