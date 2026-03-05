from ouroboros.antistagnation import (
    AntiStagnationConfig,
    compute_round_limit,
    should_force_round_finalize,
    stagnation_action,
)


def test_round_limit_no_recent_progress_uses_base_cap():
    limit = compute_round_limit([False, False, False, False, False], cap=30, extension_cap=50, progress_window=5)
    assert limit == 30


def test_round_limit_recent_progress_extends_cap():
    limit = compute_round_limit([False, False, True, False, False], cap=30, extension_cap=50, progress_window=5)
    assert limit == 50


def test_should_force_finalize_before_cap_false():
    cfg = AntiStagnationConfig(task_round_cap=30, extension_cap=50, extension_progress_window=5)
    assert should_force_round_finalize(29, [False] * 5, cfg) is False


def test_should_force_finalize_at_cap_when_stagnating():
    cfg = AntiStagnationConfig(task_round_cap=30, extension_cap=50, extension_progress_window=5)
    assert should_force_round_finalize(30, [False] * 5, cfg) is True


def test_should_not_force_finalize_at_cap_when_recent_progress_exists():
    cfg = AntiStagnationConfig(task_round_cap=30, extension_cap=50, extension_progress_window=5)
    assert should_force_round_finalize(30, [False, False, True, False, False], cfg) is False


def test_should_force_finalize_at_extension_cap_when_progress_exists():
    cfg = AntiStagnationConfig(task_round_cap=30, extension_cap=50, extension_progress_window=5)
    assert should_force_round_finalize(50, [False, False, True, False, False], cfg) is True


def teststagnation_action_none_before_threshold():
    cfg = AntiStagnationConfig(stagnation_rounds=8, stagnation_grace=4)
    assert stagnation_action(7, cfg, already_injected=False) == "none"


def teststagnation_action_inject_on_threshold():
    cfg = AntiStagnationConfig(stagnation_rounds=8, stagnation_grace=4)
    assert stagnation_action(8, cfg, already_injected=False) == "inject_self_check"


def teststagnation_action_none_when_already_injected():
    cfg = AntiStagnationConfig(stagnation_rounds=8, stagnation_grace=4)
    assert stagnation_action(9, cfg, already_injected=True) == "none"


def teststagnation_action_force_after_grace():
    cfg = AntiStagnationConfig(stagnation_rounds=8, stagnation_grace=4)
    assert stagnation_action(12, cfg, already_injected=True) == "force_finalize"
