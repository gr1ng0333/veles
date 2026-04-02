import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def test_apply_soft_cooldown_sets_live_accounts(monkeypatch):
    import ouroboros.copilot_proxy_accounts as cpa

    monkeypatch.setattr(cpa, "_init_accounts", lambda force=False: None)
    monkeypatch.setattr(cpa, "_save_accounts_state", lambda accounts: None)
    monkeypatch.setattr(cpa.time, "time", lambda: 1000.0)
    cpa._accounts = [
        {"dead": False, "cooldown_until": 0},
        {"dead": False, "cooldown_until": 1005.0},
        {"dead": True, "cooldown_until": 0},
    ]

    applied = cpa._apply_soft_cooldown(30)

    assert applied == 30.0
    assert cpa._accounts[0]["cooldown_until"] == 1030.0
    assert cpa._accounts[1]["cooldown_until"] == 1030.0
    assert cpa._accounts[2]["cooldown_until"] == 0


def test_call_with_rotation_waits_soft_cooldown_before_terminal_error(monkeypatch):
    import pytest
    from ouroboros import copilot_proxy

    waits = []
    monkeypatch.setattr(copilot_proxy._accounts_impl, "_get_active_account", lambda: None)
    monkeypatch.setattr(copilot_proxy._accounts_impl, "_shortest_cooldown_remaining", lambda: 0.0)
    monkeypatch.setattr(copilot_proxy._accounts_impl, "_apply_soft_cooldown", lambda seconds: float(seconds))
    monkeypatch.setattr(copilot_proxy.time, "sleep", lambda seconds: waits.append(seconds))

    with pytest.raises(RuntimeError, match="All Copilot accounts exhausted after retry"):
        copilot_proxy._call_with_rotation({"messages": []}, interaction_id="abc12345")

    assert waits == [30.0]


def test_call_llm_with_fallback_handoffs_same_round_to_codex(monkeypatch):
    import ouroboros.loop_fallback as loop_fallback_mod
    from ouroboros.loop_fallback import call_llm_with_fallback

    calls = []
    sleeps = []
    progress = []

    def fake_call(
        llm, messages, model, tools, effort, max_retries, drive_logs,
        task_id, round_idx, event_queue, accumulated_usage, task_type="", interaction_id=None, force_user_initiator=False,
    ):
        calls.append(model)
        if model.startswith("copilot/"):
            accumulated_usage["_last_llm_error"] = "All Copilot accounts exhausted (no cooldown to wait for). Last error: boom"
            accumulated_usage["_last_llm_error_model"] = model
            return None, 0.0
        accumulated_usage["_last_llm_error"] = None
        accumulated_usage["_last_llm_error_model"] = model
        return {"content": "codex rescue"}, 0.0

    monkeypatch.setattr(
        loop_fallback_mod,
        "maybe_sleep_before_evolution_copilot_request",
        lambda **kwargs: sleeps.append((kwargs["active_model"], kwargs["phase"])),
    )

    msg = call_llm_with_fallback(
        llm=None,
        messages=[{"role": "user", "content": "continue"}],
        active_model="copilot/claude-sonnet-4.6",
        tool_schemas=[],
        active_effort="high",
        max_retries=3,
        drive_logs=pathlib.Path('/tmp'),
        task_id="task-1",
        round_idx=33,
        event_queue=None,
        accumulated_usage={},
        task_type="evolution",
        emit_progress=progress.append,
        interaction_id="ix-1",
        _call_llm_with_retry_fn=fake_call,
    )

    assert msg["content"] == "codex rescue"
    assert calls == [
        "copilot/claude-sonnet-4.6",
        "copilot/claude-haiku-4.5",
        "codex/gpt-5.4",
    ]
    assert ("copilot/claude-sonnet-4.6", "primary") in sleeps
    assert ("copilot/claude-haiku-4.5", "fallback") in sleeps
    assert any("Codex" in item for item in progress)
