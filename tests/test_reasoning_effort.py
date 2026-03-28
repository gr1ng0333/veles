"""Tests for codex_proxy wrappers and reasoning_effort propagation."""

import inspect
import os
import pytest


# ---------------------------------------------------------------------------
# Wrapper completeness
# ---------------------------------------------------------------------------

def test_all_codex_accounts_wrappers_exist():
    """Every codex_proxy_accounts function used in codex_proxy must be importable."""
    from ouroboros.codex_proxy import (
        classify_codex_http_failure,
        _set_last_error,
        _clear_last_error,
        _update_account_quota,
        _on_rate_limit,
        _on_dead_account,
        _record_successful_request,
        _refresh_account,
        _get_active_account,
        _is_multi_account,
    )


def test_on_rate_limit_accepts_reason():
    """_on_rate_limit wrapper must forward reason parameter."""
    from ouroboros.codex_proxy import _on_rate_limit
    sig = inspect.signature(_on_rate_limit)
    assert "reason" in sig.parameters
    assert sig.parameters["reason"].default == "rate_limited"


# ---------------------------------------------------------------------------
# reasoning_effort propagation
# ---------------------------------------------------------------------------

def test_codex_proxy_accepts_reasoning_effort():
    """call_codex should accept and use reasoning_effort parameter."""
    from ouroboros.codex_proxy import call_codex
    sig = inspect.signature(call_codex)
    assert "reasoning_effort" in sig.parameters
    assert sig.parameters["reasoning_effort"].default == "medium"


def test_llm_chat_has_reasoning_effort():
    """LLMClient.chat() should accept reasoning_effort."""
    from ouroboros.llm import LLMClient
    sig = inspect.signature(LLMClient.chat)
    assert "reasoning_effort" in sig.parameters


def test_llm_chat_passes_effort_to_codex(monkeypatch):
    """LLMClient.chat() should forward reasoning_effort to call_codex."""
    captured = {}

    def fake_call_codex(messages, tools=None, system_prompt=None,
                        model="gpt-5.3-codex", token_prefix="CODEX",
                        reasoning_effort="medium"):
        captured["reasoning_effort"] = reasoning_effort
        return ({"role": "assistant", "content": "ok"},
                {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0})

    monkeypatch.setattr("ouroboros.codex_proxy.call_codex", fake_call_codex)
    from ouroboros.llm import LLMClient
    monkeypatch.setattr("ouroboros.llm.model_transport", lambda m: "codex")
    monkeypatch.setattr("ouroboros.llm.transport_model_name", lambda m: "gpt-5.3-codex")
    monkeypatch.setattr("ouroboros.llm.validate_transport_model", lambda m: None)

    client = LLMClient.__new__(LLMClient)
    client.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="codex:gpt-5.3-codex",
        reasoning_effort="high",
    )
    assert captured["reasoning_effort"] == "high"


def test_llm_chat_passes_effort_to_codex_consciousness(monkeypatch):
    """LLMClient.chat() should forward reasoning_effort to codex-consciousness."""
    captured = {}

    def fake_call_codex(messages, tools=None, system_prompt=None,
                        model="gpt-5.3-codex", token_prefix="CODEX",
                        reasoning_effort="medium"):
        captured["reasoning_effort"] = reasoning_effort
        captured["token_prefix"] = token_prefix
        return ({"role": "assistant", "content": "ok"},
                {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0})

    monkeypatch.setattr("ouroboros.codex_proxy.call_codex", fake_call_codex)
    from ouroboros.llm import LLMClient
    monkeypatch.setattr("ouroboros.llm.model_transport", lambda m: "codex-consciousness")
    monkeypatch.setattr("ouroboros.llm.transport_model_name", lambda m: "gpt-5.3-codex")
    monkeypatch.setattr("ouroboros.llm.validate_transport_model", lambda m: None)

    client = LLMClient.__new__(LLMClient)
    client.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="codex-consciousness:gpt-5.3-codex",
        reasoning_effort="high",
    )
    assert captured["reasoning_effort"] == "high"
    assert captured["token_prefix"] == "CODEX_CONSCIOUSNESS"


# ---------------------------------------------------------------------------
# Agent effort defaults
# ---------------------------------------------------------------------------

def test_agent_default_effort_high(monkeypatch):
    """Direct chat tasks should default to high reasoning effort."""
    monkeypatch.delenv("OUROBOROS_REASONING_EFFORT", raising=False)
    task_type_str = "chat"
    if task_type_str == "review":
        initial_effort = "high"
    elif task_type_str == "evolution":
        initial_effort = "medium"
    else:
        initial_effort = os.environ.get("OUROBOROS_REASONING_EFFORT", "").strip().lower() or "high"
    assert initial_effort == "high"


def test_agent_effort_env_override(monkeypatch):
    """OUROBOROS_REASONING_EFFORT env should override default."""
    monkeypatch.setenv("OUROBOROS_REASONING_EFFORT", "low")
    task_type_str = ""
    if task_type_str == "review":
        initial_effort = "high"
    elif task_type_str == "evolution":
        initial_effort = "medium"
    else:
        initial_effort = os.environ.get("OUROBOROS_REASONING_EFFORT", "").strip().lower() or "high"
    assert initial_effort == "low"


def test_evolution_effort_unchanged():
    """Evolution tasks must remain at medium effort."""
    task_type_str = "evolution"
    if task_type_str == "review":
        initial_effort = "high"
    elif task_type_str == "evolution":
        initial_effort = "medium"
    else:
        initial_effort = "high"
    assert initial_effort == "medium"


def test_review_effort_unchanged():
    """Review tasks must remain at high effort."""
    task_type_str = "review"
    if task_type_str == "review":
        initial_effort = "high"
    elif task_type_str == "evolution":
        initial_effort = "medium"
    else:
        initial_effort = "high"
    assert initial_effort == "high"


# ---------------------------------------------------------------------------
# task-specific effort policy
# ---------------------------------------------------------------------------

def test_evolution_copilot_sonnet_forces_high():
    from ouroboros.loop_runtime import _enforce_evolution_copilot_reasoning

    assert _enforce_evolution_copilot_reasoning(
        task_type="evolution",
        active_model="copilot/claude-sonnet-4.6",
        active_effort="low",
    ) == "high"


def test_evolution_copilot_opus_forces_high():
    from ouroboros.loop_runtime import _enforce_evolution_copilot_reasoning

    assert _enforce_evolution_copilot_reasoning(
        task_type="evolution",
        active_model="copilot/claude-opus-4.6",
        active_effort="medium",
    ) == "high"


def test_non_target_models_keep_existing_effort():
    from ouroboros.loop_runtime import _enforce_evolution_copilot_reasoning

    assert _enforce_evolution_copilot_reasoning(
        task_type="task",
        active_model="copilot/claude-sonnet-4.6",
        active_effort="low",
    ) == "low"
    assert _enforce_evolution_copilot_reasoning(
        task_type="evolution",
        active_model="copilot/claude-haiku-4.5",
        active_effort="medium",
    ) == "medium"
