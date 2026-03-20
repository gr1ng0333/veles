"""
Plan management for structured multi-step task execution.

Plans are stored as JSON files in <drive_root>/plans/<plan_id>.json.
Only one plan can be active at a time.
"""

import json
import time
import uuid
import pathlib
import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# Plan statuses
STATUS_DRAFT = "draft"
STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"
STATUS_REJECTED = "rejected"

# Step statuses
STEP_PENDING = "pending"
STEP_IN_PROGRESS = "in_progress"
STEP_DONE = "done"
STEP_SKIPPED = "skipped"


def _plans_dir(drive_root: pathlib.Path) -> pathlib.Path:
    d = drive_root / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _plan_path(drive_root: pathlib.Path, plan_id: str) -> pathlib.Path:
    return _plans_dir(drive_root) / f"{plan_id}.json"


def _save_plan(drive_root: pathlib.Path, plan: Dict[str, Any]) -> None:
    path = _plan_path(drive_root, plan["id"])
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_plan(drive_root: pathlib.Path, plan_id: str) -> Optional[Dict[str, Any]]:
    path = _plan_path(drive_root, plan_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def get_active_plan(drive_root: pathlib.Path) -> Optional[Dict[str, Any]]:
    """Return the currently active plan, or None."""
    plans_dir = _plans_dir(drive_root)
    for f in plans_dir.glob("*.json"):
        try:
            plan = json.loads(f.read_text(encoding="utf-8"))
            if plan.get("status") == STATUS_ACTIVE:
                return plan
        except Exception:
            continue
    return None


def get_plan(drive_root: pathlib.Path, plan_id: str) -> Optional[Dict[str, Any]]:
    """Load a specific plan by ID."""
    return _load_plan(drive_root, plan_id)


def create_plan(
    drive_root: pathlib.Path,
    title: str,
    steps: List[Dict[str, str]],
    notes: str = "",
) -> Dict[str, Any]:
    """
    Create a new plan in draft status.

    steps: list of {"title": "...", "description": "..."}
    Returns the created plan dict.
    Raises ValueError if there is already an active plan.
    """
    active = get_active_plan(drive_root)
    if active:
        raise ValueError(
            f"Cannot create new plan: plan '{active['title']}' (id={active['id']}) is already active. "
            f"Complete or reject it first."
        )

    plan_id = f"plan_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    plan_steps = []
    for i, s in enumerate(steps, 1):
        plan_steps.append({
            "index": i,
            "title": s.get("title", f"Step {i}"),
            "description": s.get("description", ""),
            "status": STEP_PENDING,
            "commit": None,
            "completed_at": None,
            "result": None,
        })

    plan = {
        "id": plan_id,
        "title": title,
        "status": STATUS_DRAFT,
        "created_at": _utcnow_iso(),
        "approved_at": None,
        "completed_at": None,
        "rejected_at": None,
        "notes": notes,
        "steps": plan_steps,
    }

    _save_plan(drive_root, plan)
    log.info("plan_created id=%s title=%s steps=%d", plan_id, title, len(plan_steps))
    return plan


def approve_plan(drive_root: pathlib.Path, plan_id: str) -> Dict[str, Any]:
    """Approve a draft plan, making it active. First step becomes in_progress."""
    plan = _load_plan(drive_root, plan_id)
    if not plan:
        raise ValueError(f"Plan {plan_id} not found")
    if plan["status"] != STATUS_DRAFT:
        raise ValueError(f"Plan {plan_id} is '{plan['status']}', expected 'draft'")

    # Check no other active plan
    active = get_active_plan(drive_root)
    if active and active["id"] != plan_id:
        raise ValueError(f"Cannot approve: plan '{active['title']}' is already active")

    plan["status"] = STATUS_ACTIVE
    plan["approved_at"] = _utcnow_iso()

    # Set first pending step to in_progress
    for step in plan["steps"]:
        if step["status"] == STEP_PENDING:
            step["status"] = STEP_IN_PROGRESS
            break

    _save_plan(drive_root, plan)
    log.info("plan_approved id=%s title=%s", plan_id, plan["title"])
    return plan


def reject_plan(drive_root: pathlib.Path, plan_id: str, reason: str = "") -> Dict[str, Any]:
    """Reject a draft or active plan."""
    plan = _load_plan(drive_root, plan_id)
    if not plan:
        raise ValueError(f"Plan {plan_id} not found")
    if plan["status"] not in (STATUS_DRAFT, STATUS_ACTIVE):
        raise ValueError(f"Plan {plan_id} is '{plan['status']}', can only reject draft or active")

    plan["status"] = STATUS_REJECTED
    plan["rejected_at"] = _utcnow_iso()
    if reason:
        plan["notes"] = (plan.get("notes") or "") + f"\n\nRejected: {reason}"

    _save_plan(drive_root, plan)
    log.info("plan_rejected id=%s reason=%s", plan_id, reason[:100])
    return plan


def step_done(
    drive_root: pathlib.Path,
    plan_id: str,
    result: str,
    commit: str = "",
) -> Dict[str, Any]:
    """
    Mark the current in_progress step as done.
    Automatically advances the next pending step to in_progress.
    Returns updated plan.
    """
    plan = _load_plan(drive_root, plan_id)
    if not plan:
        raise ValueError(f"Plan {plan_id} not found")
    if plan["status"] != STATUS_ACTIVE:
        raise ValueError(f"Plan {plan_id} is '{plan['status']}', expected 'active'")

    # Find current in_progress step
    current = None
    for step in plan["steps"]:
        if step["status"] == STEP_IN_PROGRESS:
            current = step
            break

    if not current:
        raise ValueError(f"No in_progress step found in plan {plan_id}")

    current["status"] = STEP_DONE
    current["completed_at"] = _utcnow_iso()
    current["result"] = result
    if commit:
        current["commit"] = commit

    # Advance next pending step
    for step in plan["steps"]:
        if step["status"] == STEP_PENDING:
            step["status"] = STEP_IN_PROGRESS
            break

    _save_plan(drive_root, plan)
    done_count = sum(1 for s in plan["steps"] if s["status"] == STEP_DONE)
    log.info(
        "plan_step_done id=%s step=%d/%d title=%s",
        plan_id, done_count, len(plan["steps"]), current["title"],
    )
    return plan


def update_plan(
    drive_root: pathlib.Path,
    plan_id: str,
    notes: Optional[str] = None,
    add_steps: Optional[List[Dict[str, str]]] = None,
    remove_step_indices: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """
    Update an active or draft plan: change notes, add steps, remove pending steps.
    Cannot remove done or in_progress steps.
    """
    plan = _load_plan(drive_root, plan_id)
    if not plan:
        raise ValueError(f"Plan {plan_id} not found")
    if plan["status"] not in (STATUS_DRAFT, STATUS_ACTIVE):
        raise ValueError(f"Plan {plan_id} is '{plan['status']}', cannot update")

    if notes is not None:
        plan["notes"] = notes

    if remove_step_indices:
        for idx in sorted(remove_step_indices, reverse=True):
            for step in plan["steps"]:
                if step["index"] == idx:
                    if step["status"] in (STEP_DONE, STEP_IN_PROGRESS):
                        raise ValueError(f"Cannot remove step {idx}: status is '{step['status']}'")
                    plan["steps"].remove(step)
                    break

    if add_steps:
        max_idx = max((s["index"] for s in plan["steps"]), default=0)
        for i, s in enumerate(add_steps, max_idx + 1):
            plan["steps"].append({
                "index": i,
                "title": s.get("title", f"Step {i}"),
                "description": s.get("description", ""),
                "status": STEP_PENDING,
                "commit": None,
                "completed_at": None,
                "result": None,
            })

    # Re-index steps sequentially
    for i, step in enumerate(plan["steps"], 1):
        step["index"] = i

    _save_plan(drive_root, plan)
    log.info("plan_updated id=%s", plan_id)
    return plan


def complete_plan(drive_root: pathlib.Path, plan_id: str, summary: str = "") -> Dict[str, Any]:
    """Mark plan as completed. All steps should be done."""
    plan = _load_plan(drive_root, plan_id)
    if not plan:
        raise ValueError(f"Plan {plan_id} not found")
    if plan["status"] != STATUS_ACTIVE:
        raise ValueError(f"Plan {plan_id} is '{plan['status']}', expected 'active'")

    pending = [s for s in plan["steps"] if s["status"] in (STEP_PENDING, STEP_IN_PROGRESS)]
    if pending:
        log.warning(
            "plan_complete_with_pending id=%s pending_steps=%d",
            plan_id, len(pending),
        )
        # Mark remaining as skipped
        for s in pending:
            s["status"] = STEP_SKIPPED

    plan["status"] = STATUS_COMPLETED
    plan["completed_at"] = _utcnow_iso()
    if summary:
        plan["notes"] = (plan.get("notes") or "") + f"\n\nCompletion summary: {summary}"

    _save_plan(drive_root, plan)
    log.info("plan_completed id=%s title=%s", plan_id, plan["title"])
    return plan


def format_plan_for_context(plan: Dict[str, Any]) -> str:
    """Format active plan as compact text for LLM system prompt context."""
    lines = [f"## Active Plan: {plan['title']}"]
    if plan.get("notes"):
        # Only first 500 chars of notes in context
        notes_short = plan["notes"][:500]
        lines.append(f"Notes: {notes_short}")
    lines.append("")

    for step in plan["steps"]:
        status_icon = {
            STEP_DONE: "DONE",
            STEP_IN_PROGRESS: "IN PROGRESS",
            STEP_PENDING: "PENDING",
            STEP_SKIPPED: "SKIPPED",
        }.get(step["status"], step["status"])

        line = f"Step {step['index']} [{status_icon}] {step['title']}"
        if step.get("commit"):
            line += f" → {step['commit']}"
        lines.append(line)

        # Show result for done steps (compact)
        if step["status"] == STEP_DONE and step.get("result"):
            result_short = step["result"][:200]
            lines.append(f"  Result: {result_short}")

    lines.append("")

    # Current step details
    current = None
    for step in plan["steps"]:
        if step["status"] == STEP_IN_PROGRESS:
            current = step
            break

    if current:
        lines.append(f">>> Current step: #{current['index']} — {current['title']}")
        if current.get("description"):
            lines.append(f"Description: {current['description']}")
    else:
        done_count = sum(1 for s in plan["steps"] if s["status"] == STEP_DONE)
        lines.append(f"All {done_count} steps completed. Call plan_complete to finalize.")

    return "\n".join(lines)


def format_plan_summary(plan: Dict[str, Any]) -> str:
    """Format plan as summary for Telegram / tool response."""
    done = sum(1 for s in plan["steps"] if s["status"] == STEP_DONE)
    total = len(plan["steps"])
    lines = [
        f"📋 Plan: {plan['title']}",
        f"Status: {plan['status']} | Progress: {done}/{total}",
        "",
    ]
    for step in plan["steps"]:
        icon = {"done": "✅", "in_progress": "🔄", "pending": "⬚", "skipped": "⏭"}.get(step["status"], "?")
        line = f"{icon} {step['index']}. {step['title']}"
        if step.get("commit"):
            line += f" ({step['commit']})"
        lines.append(line)

    if plan.get("notes"):
        lines.append(f"\nNotes: {plan['notes'][:300]}")

    return "\n".join(lines)
