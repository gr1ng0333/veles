import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def test_arm_manual_terminal_restart_handoff_sets_pending_for_known_owner():
    from supervisor.restart_observability import arm_manual_terminal_restart_handoff

    state = {"owner_chat_id": 123, "restart_notify_pending": False}
    updated, armed = arm_manual_terminal_restart_handoff(
        state, previous_pid=4242, requested_at="2026-03-11T16:30:00+00:00"
    )

    assert armed is True
    assert updated["restart_notify_pending"] is True
    assert updated["restart_notify_reason"] == "manual_terminal_restart"
    assert updated["restart_notify_source"] == "manual_terminal_restart"
    assert updated["restart_notify_requested_at"] == "2026-03-11T16:30:00+00:00"


def test_arm_manual_terminal_restart_handoff_does_not_override_explicit_pending_restart():
    from supervisor.restart_observability import arm_manual_terminal_restart_handoff

    state = {
        "owner_chat_id": 123,
        "restart_notify_pending": True,
        "restart_notify_reason": "owner_restart",
        "restart_notify_source": "owner_restart_command",
    }
    updated, armed = arm_manual_terminal_restart_handoff(state, previous_pid=4242)

    assert armed is False
    assert updated["restart_notify_reason"] == "owner_restart"
    assert updated["restart_notify_source"] == "owner_restart_command"


def test_arm_manual_terminal_restart_handoff_requires_known_owner():
    from supervisor.restart_observability import arm_manual_terminal_restart_handoff

    state = {"restart_notify_pending": False}
    updated, armed = arm_manual_terminal_restart_handoff(state, previous_pid=4242)

    assert armed is False
    assert updated.get("restart_notify_pending") is False
    assert "restart_notify_source" not in updated
