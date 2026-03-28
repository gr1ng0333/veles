from ouroboros.loop_runtime import (
    _get_evolution_round_limit,
    _should_finalize_evolution_for_prompt_tokens,
    _update_large_prompt_streak,
)

from ouroboros.antistagnation import (
    AntiStagnationConfig,
    compute_round_limit,
    should_force_round_finalize,
    stagnation_action,
)

import pytest


@pytest.mark.parametrize(
    ("recent_progress", "expected"),
    [([False, False, False, False, False], 30), ([False, False, True, False, False], 50)],
)
def test_compute_round_limit_progress_window(recent_progress, expected):
    limit = compute_round_limit(recent_progress, cap=30, extension_cap=50, progress_window=5)
    assert limit == expected


@pytest.mark.parametrize(
    ("round_no", "recent_progress", "expected"),
    [(29, [False] * 5, False), (30, [False] * 5, True), (30, [False, False, True, False, False], False), (50, [False, False, True, False, False], True)],
)
def test_should_force_finalize_thresholds(round_no, recent_progress, expected):
    cfg = AntiStagnationConfig(task_round_cap=30, extension_cap=50, extension_progress_window=5)
    assert should_force_round_finalize(round_no, recent_progress, cfg) is expected


@pytest.mark.parametrize(
    ("round_no", "already_injected", "expected"),
    [(7, False, "none"), (8, False, "inject_self_check"), (9, True, "none"), (12, True, "force_finalize")],
)
def test_stagnation_action_thresholds(round_no, already_injected, expected):
    cfg = AntiStagnationConfig(stagnation_rounds=8, stagnation_grace=4)
    assert stagnation_action(round_no, cfg, already_injected=already_injected) == expected


@pytest.mark.parametrize(
    ("task_type", "env_value", "base_limit", "expected"),
    [("evolution", "40", 15, 40), ("task", "5", 15, 15), ("evolution", "6", 15, 6)],
)
def test_get_evolution_round_limit_contract(monkeypatch, task_type, env_value, base_limit, expected):
    if env_value is None:
        monkeypatch.delenv("OUROBOROS_EVOLUTION_MAX_ROUNDS", raising=False)
    else:
        monkeypatch.setenv("OUROBOROS_EVOLUTION_MAX_ROUNDS", env_value)
    assert _get_evolution_round_limit(task_type, base_limit) == expected


def test_large_prompt_streak_resets_for_non_evolution():
    assert _update_large_prompt_streak("task", 1, 50001) == 0


@pytest.mark.parametrize(
    ("starting_streak", "prompt_tokens", "expected_streak", "expected_finalize"),
    [(0, 120001, 1, False), (1, 120002, 2, True), (1, 119999, 0, False)],
)
def test_large_prompt_streak_transitions(starting_streak, prompt_tokens, expected_streak, expected_finalize):
    streak = _update_large_prompt_streak("evolution", starting_streak, prompt_tokens)
    assert streak == expected_streak
    assert _should_finalize_evolution_for_prompt_tokens("evolution", streak) is expected_finalize
