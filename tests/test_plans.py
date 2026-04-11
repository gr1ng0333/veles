"""Tests for plan management system."""

import json
import pathlib
import pytest

from ouroboros.tools.registry import ToolContext
from ouroboros.tools.plans import (
    _plan_approve,
    _plan_complete,
    _plan_create,
    _plan_reject,
    _plan_step_done,
    _plan_update,
)
from ouroboros.plans import (
    create_plan, approve_plan, reject_plan, step_done,
    update_plan, complete_plan, get_active_plan, get_plan,
    format_plan_for_context, format_plan_summary,
    STATUS_DRAFT, STATUS_ACTIVE, STATUS_COMPLETED, STATUS_REJECTED,
    STEP_PENDING, STEP_IN_PROGRESS, STEP_DONE, STEP_SKIPPED,
)


@pytest.fixture
def drive_root(tmp_path):
    return tmp_path


@pytest.fixture
def sample_steps():
    return [
        {"title": "Step 1", "description": "Do thing A"},
        {"title": "Step 2", "description": "Do thing B"},
        {"title": "Step 3", "description": "Do thing C"},
    ]


def test_create_plan(drive_root, sample_steps):
    plan = create_plan(drive_root, "Test Plan", sample_steps, notes="Test notes")
    assert plan["status"] == STATUS_DRAFT
    assert len(plan["steps"]) == 3
    assert plan["steps"][0]["status"] == STEP_PENDING
    assert plan["title"] == "Test Plan"
    assert plan["notes"] == "Test notes"
    # Verify file saved
    saved = get_plan(drive_root, plan["id"])
    assert saved is not None
    assert saved["title"] == "Test Plan"


def test_create_plan_blocks_when_active_exists(drive_root, sample_steps):
    plan = create_plan(drive_root, "Plan 1", sample_steps)
    approve_plan(drive_root, plan["id"])
    with pytest.raises(ValueError, match="already active"):
        create_plan(drive_root, "Plan 2", sample_steps)


def test_approve_plan(drive_root, sample_steps):
    plan = create_plan(drive_root, "Test", sample_steps)
    approved = approve_plan(drive_root, plan["id"])
    assert approved["status"] == STATUS_ACTIVE
    assert approved["approved_at"] is not None
    assert approved["steps"][0]["status"] == STEP_IN_PROGRESS
    assert approved["steps"][1]["status"] == STEP_PENDING


def test_reject_plan(drive_root, sample_steps):
    plan = create_plan(drive_root, "Test", sample_steps)
    rejected = reject_plan(drive_root, plan["id"], reason="Bad plan")
    assert rejected["status"] == STATUS_REJECTED
    assert "Bad plan" in rejected["notes"]


def test_step_done_advances(drive_root, sample_steps):
    plan = create_plan(drive_root, "Test", sample_steps)
    approve_plan(drive_root, plan["id"])

    updated = step_done(drive_root, plan["id"], result="Done A", commit="v1.0")
    assert updated["steps"][0]["status"] == STEP_DONE
    assert updated["steps"][0]["result"] == "Done A"
    assert updated["steps"][0]["commit"] == "v1.0"
    assert updated["steps"][1]["status"] == STEP_IN_PROGRESS
    assert updated["steps"][2]["status"] == STEP_PENDING


def test_step_done_last_step(drive_root, sample_steps):
    plan = create_plan(drive_root, "Test", sample_steps)
    approve_plan(drive_root, plan["id"])
    step_done(drive_root, plan["id"], result="A")
    step_done(drive_root, plan["id"], result="B")
    updated = step_done(drive_root, plan["id"], result="C")
    # All done, no more in_progress
    assert all(s["status"] == STEP_DONE for s in updated["steps"])


def test_complete_plan(drive_root, sample_steps):
    plan = create_plan(drive_root, "Test", sample_steps)
    approve_plan(drive_root, plan["id"])
    step_done(drive_root, plan["id"], result="A")
    step_done(drive_root, plan["id"], result="B")
    step_done(drive_root, plan["id"], result="C")
    completed = complete_plan(drive_root, plan["id"], summary="All good")
    assert completed["status"] == STATUS_COMPLETED
    assert "All good" in completed["notes"]


def test_complete_plan_skips_pending(drive_root, sample_steps):
    plan = create_plan(drive_root, "Test", sample_steps)
    approve_plan(drive_root, plan["id"])
    step_done(drive_root, plan["id"], result="A")
    # Steps 2 and 3 still pending/in_progress
    completed = complete_plan(drive_root, plan["id"], summary="Partial")
    skipped = [s for s in completed["steps"] if s["status"] == STEP_SKIPPED]
    assert len(skipped) == 2


def test_update_plan_add_steps(drive_root, sample_steps):
    plan = create_plan(drive_root, "Test", sample_steps)
    updated = update_plan(drive_root, plan["id"], add_steps=[{"title": "Step 4"}])
    assert len(updated["steps"]) == 4
    assert updated["steps"][3]["title"] == "Step 4"


def test_update_plan_remove_pending(drive_root, sample_steps):
    plan = create_plan(drive_root, "Test", sample_steps)
    updated = update_plan(drive_root, plan["id"], remove_step_indices=[3])
    assert len(updated["steps"]) == 2


def test_update_plan_cannot_remove_done(drive_root, sample_steps):
    plan = create_plan(drive_root, "Test", sample_steps)
    approve_plan(drive_root, plan["id"])
    step_done(drive_root, plan["id"], result="A")
    with pytest.raises(ValueError, match="Cannot remove"):
        update_plan(drive_root, plan["id"], remove_step_indices=[1])


def test_get_active_plan(drive_root, sample_steps):
    assert get_active_plan(drive_root) is None
    plan = create_plan(drive_root, "Test", sample_steps)
    assert get_active_plan(drive_root) is None  # Still draft
    approve_plan(drive_root, plan["id"])
    active = get_active_plan(drive_root)
    assert active is not None
    assert active["id"] == plan["id"]


def test_format_plan_for_context(drive_root, sample_steps):
    plan = create_plan(drive_root, "Test Plan", sample_steps)
    approve_plan(drive_root, plan["id"])
    step_done(drive_root, plan["id"], result="Done A", commit="v1.0")

    plan = get_plan(drive_root, plan["id"])
    text = format_plan_for_context(plan)
    assert "Active Plan: Test Plan" in text
    assert "[DONE]" in text
    assert "[IN PROGRESS]" in text
    assert "[PENDING]" in text
    assert "v1.0" in text
    assert "Current step: #2" in text


def test_format_plan_summary(drive_root, sample_steps):
    plan = create_plan(drive_root, "Test Plan", sample_steps)
    text = format_plan_summary(plan)
    assert "Test Plan" in text
    assert "0/3" in text



def _make_tool_ctx(drive_root, progress_messages):
    return ToolContext(
        repo_dir=drive_root,
        drive_root=drive_root,
        emit_progress_fn=progress_messages.append,
    )


def test_plan_tool_create_emits_progress(drive_root, sample_steps):
    progress = []
    ctx = _make_tool_ctx(drive_root, progress)

    result = _plan_create(ctx, "Test Plan", sample_steps, notes="Test notes")

    assert "Test Plan" in result
    assert progress
    assert "План собран" in progress[-1]
    assert "Test Plan" in progress[-1]


def test_plan_tool_approve_emits_progress(drive_root, sample_steps):
    progress = []
    ctx = _make_tool_ctx(drive_root, progress)
    plan = create_plan(drive_root, "Test", sample_steps)

    result = _plan_approve(ctx, plan["id"])

    assert "Status: active" in result
    assert progress
    assert "Перевожу план" in progress[-1]
    assert "шаг 1/3" in progress[-1]


def test_plan_tool_step_done_emits_progress_and_next_step(drive_root, sample_steps):
    progress = []
    ctx = _make_tool_ctx(drive_root, progress)
    plan = create_plan(drive_root, "Test", sample_steps)
    approve_plan(drive_root, plan["id"])

    result = _plan_step_done(ctx, plan["id"], result="Implemented thing A", commit="abc123")

    assert "Status: active" in result
    assert progress
    message = progress[-1]
    assert "Шаг 1/3 завершён" in message
    assert "Commit: `abc123`" in message
    assert "Результат: Implemented thing A" in message
    assert "Перехожу к следующему шагу 2/3" in message


def test_plan_tool_update_emits_progress(drive_root, sample_steps):
    progress = []
    ctx = _make_tool_ctx(drive_root, progress)
    plan = create_plan(drive_root, "Test", sample_steps)
    approve_plan(drive_root, plan["id"])

    result = _plan_update(ctx, plan["id"], notes="New notes", add_steps=[{"title": "Step 4"}])

    assert "Status: active" in result
    assert progress
    assert "Подправил план" in progress[-1]
    assert "добавил 1 шаг(а/ов)" in progress[-1]


def test_plan_tool_complete_emits_progress(drive_root, sample_steps):
    progress = []
    ctx = _make_tool_ctx(drive_root, progress)
    plan = create_plan(drive_root, "Test", sample_steps)
    approve_plan(drive_root, plan["id"])
    step_done(drive_root, plan["id"], result="A")
    step_done(drive_root, plan["id"], result="B")
    step_done(drive_root, plan["id"], result="C")

    result = _plan_complete(ctx, plan["id"], summary="Everything done")

    assert "Status: completed" in result
    assert progress
    assert "План **Test** завершён" in progress[-1]
    assert "Итог: Everything done" in progress[-1]


def test_plan_tool_reject_emits_progress(drive_root, sample_steps):
    progress = []
    ctx = _make_tool_ctx(drive_root, progress)
    plan = create_plan(drive_root, "Test", sample_steps)

    result = _plan_reject(ctx, plan["id"], reason="Owner stopped it")

    assert "rejected" in result
    assert progress
    assert "Останавливаю план" in progress[-1]
    assert "Owner stopped it" in progress[-1]


def test_get_active_plan_auto_completes_stale_active_plan(drive_root, sample_steps):
    plan = create_plan(drive_root, "Test", sample_steps)
    approved = approve_plan(drive_root, plan["id"])
    approved["steps"][0]["status"] = STEP_DONE
    approved["steps"][1]["status"] = STEP_SKIPPED
    approved["steps"][2]["status"] = STEP_DONE
    approved["status"] = STATUS_ACTIVE
    (drive_root / "plans" / f"{plan['id']}.json").write_text(json.dumps(approved, ensure_ascii=False, indent=2), encoding="utf-8")

    active = get_active_plan(drive_root)
    healed = get_plan(drive_root, plan["id"])

    assert active is None
    assert healed["status"] == STATUS_COMPLETED
    assert healed["completed_at"] is not None
    assert "Auto-finalized stale active plan" in healed["notes"]


def test_complete_plan_requires_exact_plan_id(drive_root, sample_steps):
    plan = create_plan(drive_root, "Test", sample_steps)
    approve_plan(drive_root, plan["id"])

    with pytest.raises(ValueError, match="exact plan_id"):
        complete_plan(drive_root, "fitness-bot-extraction", summary="done")


# ===== NEW TESTS FOR FIXED BEHAVIOURS =====

def test_get_draft_plan(drive_root, sample_steps):
    """Draft plan should be discoverable via get_draft_plan."""
    from ouroboros.plans import get_draft_plan
    assert get_draft_plan(drive_root) is None
    plan = create_plan(drive_root, "Draft Test", sample_steps)
    draft = get_draft_plan(drive_root)
    assert draft is not None
    assert draft["id"] == plan["id"]
    assert draft["status"] == STATUS_DRAFT


def test_get_draft_plan_not_visible_after_approve(drive_root, sample_steps):
    """After approval, get_draft_plan returns None."""
    from ouroboros.plans import get_draft_plan
    plan = create_plan(drive_root, "Test", sample_steps)
    approve_plan(drive_root, plan["id"])
    assert get_draft_plan(drive_root) is None


def test_format_plan_for_context_shows_draft(drive_root, sample_steps):
    """Draft plan shows its ID in context so model can approve without guessing."""
    plan = create_plan(drive_root, "My Draft", sample_steps)
    text = format_plan_for_context(plan)
    assert "Draft Plan" in text
    assert "awaiting approval" in text
    assert plan["id"] in text


def test_plan_approve_auto_detects_draft(drive_root, sample_steps):
    """plan_approve with empty plan_id auto-detects the latest draft."""
    from ouroboros.tools.plans import _plan_approve
    progress = []
    ctx = _make_tool_ctx(drive_root, progress)
    plan = create_plan(drive_root, "AutoDetect", sample_steps)

    # Call approve WITHOUT specifying plan_id
    result = _plan_approve(ctx, plan_id="")

    assert "Status: active" in result
    assert progress
    assert "AutoDetect" in progress[-1]


def test_plan_approve_no_draft_returns_message(drive_root, sample_steps):
    """plan_approve with no draft gives helpful message instead of crashing."""
    from ouroboros.tools.plans import _plan_approve
    progress = []
    ctx = _make_tool_ctx(drive_root, progress)

    result = _plan_approve(ctx, plan_id="")
    assert "No draft plan" in result


def test_step_done_auto_completes_last_step(drive_root, sample_steps):
    """When the last step is marked done, plan auto-completes — no separate plan_complete needed."""
    plan = create_plan(drive_root, "Test", sample_steps)
    approve_plan(drive_root, plan["id"])
    step_done(drive_root, plan["id"], result="A")
    step_done(drive_root, plan["id"], result="B")
    updated = step_done(drive_root, plan["id"], result="C")

    assert updated["status"] == STATUS_COMPLETED
    assert updated["completed_at"] is not None


def test_complete_plan_idempotent(drive_root, sample_steps):
    """plan_complete is safe to call even after auto-complete by step_done."""
    plan = create_plan(drive_root, "Test", sample_steps)
    approve_plan(drive_root, plan["id"])
    step_done(drive_root, plan["id"], result="A")
    step_done(drive_root, plan["id"], result="B")
    step_done(drive_root, plan["id"], result="C")
    # Now call complete again (idempotent)
    result = complete_plan(drive_root, plan["id"], summary="Extra summary")
    assert result["status"] == STATUS_COMPLETED
    assert "Extra summary" in result["notes"]


def test_plan_complete_tool_idempotent(drive_root, sample_steps):
    """plan_complete tool works even if plan was auto-completed by step_done."""
    from ouroboros.tools.plans import _plan_complete
    progress = []
    ctx = _make_tool_ctx(drive_root, progress)
    plan = create_plan(drive_root, "Test", sample_steps)
    approve_plan(drive_root, plan["id"])
    step_done(drive_root, plan["id"], result="A")
    step_done(drive_root, plan["id"], result="B")
    step_done(drive_root, plan["id"], result="C")
    # plan is now auto-completed; calling _plan_complete should not crash
    result = _plan_complete(ctx, plan["id"], summary="All done")
    assert "completed" in result
    assert progress  # should still emit something


def test_step_done_tool_reports_auto_complete(drive_root, sample_steps):
    """When last step done, progress message reflects plan completion."""
    from ouroboros.tools.plans import _plan_step_done
    progress = []
    ctx = _make_tool_ctx(drive_root, progress)
    plan = create_plan(drive_root, "Test", sample_steps)
    approve_plan(drive_root, plan["id"])
    step_done(drive_root, plan["id"], result="A")
    step_done(drive_root, plan["id"], result="B")

    result = _plan_step_done(ctx, plan["id"], result="C final step", commit="v9.0.0")
    assert "completed" in result
    message = progress[-1]
    assert "больше нет активных шагов" in message
    assert "3/3" in message
