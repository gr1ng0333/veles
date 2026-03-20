"""Tests for plan management system."""

import json
import pathlib
import pytest
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
