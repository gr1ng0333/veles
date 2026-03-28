import pathlib
import sys
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ouroboros.loop_runtime import _maybe_sleep_before_evolution_copilot_request


def test_sleep_for_copilot_evolution_after_first_round():
    with mock.patch('ouroboros.loop_runtime.time.sleep') as sleep_mock:
        _maybe_sleep_before_evolution_copilot_request(
            task_type='evolution',
            active_model='copilot/claude-sonnet-4.6',
            round_idx=2,
            phase='primary',
        )
    sleep_mock.assert_called_once()


def test_skip_sleep_for_first_primary_round():
    with mock.patch('ouroboros.loop_runtime.time.sleep') as sleep_mock:
        _maybe_sleep_before_evolution_copilot_request(
            task_type='evolution',
            active_model='copilot/claude-sonnet-4.6',
            round_idx=1,
            phase='primary',
        )
    sleep_mock.assert_not_called()


def test_skip_sleep_for_codex_or_non_evolution():
    with mock.patch('ouroboros.loop_runtime.time.sleep') as sleep_mock:
        _maybe_sleep_before_evolution_copilot_request(
            task_type='task',
            active_model='copilot/claude-sonnet-4.6',
            round_idx=3,
            phase='primary',
        )
        _maybe_sleep_before_evolution_copilot_request(
            task_type='evolution',
            active_model='codex/gpt-5.4',
            round_idx=3,
            phase='primary',
        )
    sleep_mock.assert_not_called()


def test_sleep_for_fallback_request_even_on_round_one():
    with mock.patch('ouroboros.loop_runtime.time.sleep') as sleep_mock:
        _maybe_sleep_before_evolution_copilot_request(
            task_type='evolution',
            active_model='copilot/claude-haiku-4.5',
            round_idx=1,
            phase='fallback',
        )
    sleep_mock.assert_called_once()
