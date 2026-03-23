
"""Plan management tools — create, approve, execute, and complete structured plans."""

from __future__ import annotations

import json
from typing import List

from ouroboros import plans
from ouroboros.tools.registry import ToolContext, ToolEntry


_PROGRESS_PREVIEW_LIMIT = 220


def _truncate_preview(text: str, limit: int = _PROGRESS_PREVIEW_LIMIT) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _current_step(plan: dict) -> dict | None:
    for step in plan.get("steps", []):
        if step.get("status") == plans.STEP_IN_PROGRESS:
            return step
    return None


def _done_step(plan: dict) -> dict | None:
    done_steps = [step for step in plan.get("steps", []) if step.get("status") == plans.STEP_DONE]
    if not done_steps:
        return None
    return max(done_steps, key=lambda step: step.get("index", 0))


def _emit_plan_progress(ctx: ToolContext, text: str) -> None:
    message = text.strip()
    if message:
        ctx.emit_progress_fn(message)


def _plan_create(ctx: ToolContext, title: str, steps: list, notes: str = "") -> str:
    plan = plans.create_plan(ctx.drive_root, title, steps, notes=notes)
    step_count = len(plan.get("steps", []))
    _emit_plan_progress(
        ctx,
        f"План собран: **{plan['title']}**. Внутри {step_count} шаг(а/ов); жду одобрения перед исполнением.",
    )
    return plans.format_plan_summary(plan)


def _plan_approve(ctx: ToolContext, plan_id: str) -> str:
    plan = plans.approve_plan(ctx.drive_root, plan_id)
    current = _current_step(plan)
    step_line = ""
    if current:
        step_line = (
            f" Сейчас начат шаг {current['index']}/{len(plan['steps'])}: "
            f"**{current['title']}**."
        )
    _emit_plan_progress(ctx, f"Перевожу план **{plan['title']}** в execution.{step_line}")
    return plans.format_plan_summary(plan)


def _plan_reject(ctx: ToolContext, plan_id: str, reason: str = "") -> str:
    plan = plans.reject_plan(ctx.drive_root, plan_id, reason=reason)
    reason_preview = _truncate_preview(reason)
    reason_line = f" Причина: {reason_preview}" if reason_preview else ""
    _emit_plan_progress(ctx, f"Останавливаю план **{plan['title']}**.{reason_line}")
    return f"Plan '{plan['title']}' rejected."


def _plan_step_done(ctx: ToolContext, plan_id: str, result: str, commit: str = "") -> str:
    plan = plans.step_done(ctx.drive_root, plan_id, result=result, commit=commit)
    finished = _done_step(plan)
    current = _current_step(plan)
    done_count = sum(1 for step in plan.get("steps", []) if step.get("status") == plans.STEP_DONE)

    lines = []
    if finished:
        commit_line = f" Commit: `{commit}`." if commit else ""
        lines.append(
            f"Шаг {finished['index']}/{len(plan['steps'])} завершён: **{finished['title']}**.{commit_line}"
        )
    result_preview = _truncate_preview(result)
    if result_preview:
        lines.append(f"Результат: {result_preview}")
    if current:
        lines.append(
            f"Перехожу к следующему шагу {current['index']}/{len(plan['steps'])}: **{current['title']}**."
        )
    else:
        lines.append(f"Внутри плана больше нет активных шагов; готово {done_count}/{len(plan['steps'])}.")

    _emit_plan_progress(ctx, "\n".join(lines))
    return plans.format_plan_summary(plan)


def _plan_update(ctx: ToolContext, plan_id: str, notes: str = None,
                 add_steps: list = None, remove_step_indices: list = None) -> str:
    plan = plans.update_plan(
        ctx.drive_root, plan_id,
        notes=notes, add_steps=add_steps, remove_step_indices=remove_step_indices,
    )
    changes = []
    if notes is not None:
        changes.append("обновил notes")
    if add_steps:
        changes.append(f"добавил {len(add_steps)} шаг(а/ов)")
    if remove_step_indices:
        changes.append(f"убрал шаги {', '.join(str(i) for i in remove_step_indices)}")
    if not changes:
        changes.append("обновил состояние плана")

    current = _current_step(plan)
    current_line = ""
    if current:
        current_line = (
            f" Активный шаг сейчас: {current['index']}/{len(plan['steps'])} — "
            f"**{current['title']}**."
        )
    _emit_plan_progress(
        ctx,
        f"Подправил план **{plan['title']}**: {', '.join(changes)}.{current_line}",
    )
    return plans.format_plan_summary(plan)


def _plan_complete(ctx: ToolContext, plan_id: str, summary: str = "") -> str:
    plan = plans.complete_plan(ctx.drive_root, plan_id, summary=summary)
    summary_preview = _truncate_preview(summary)
    summary_line = f" Итог: {summary_preview}" if summary_preview else ""
    done_count = sum(1 for step in plan.get("steps", []) if step.get("status") == plans.STEP_DONE)
    _emit_plan_progress(
        ctx,
        f"План **{plan['title']}** завершён. Закрыто {done_count}/{len(plan['steps'])} шагов.{summary_line}",
    )
    return plans.format_plan_summary(plan)


def _plan_status(ctx: ToolContext, plan_id: str = "") -> str:
    if plan_id:
        plan = plans.get_plan(ctx.drive_root, plan_id)
    else:
        plan = plans.get_active_plan(ctx.drive_root)
    if not plan:
        return "No active plan found." if not plan_id else f"Plan {plan_id} not found."
    return plans.format_plan_summary(plan)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("plan_create", {
            "name": "plan_create",
            "description": (
                "Create a structured multi-step execution plan. The plan starts in 'draft' status "
                "and must be approved by owner before execution. Send owner the plan summary for "
                "review after creation. Only one active plan allowed at a time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short descriptive title for the plan (e.g. 'Copilot agentic loop integration')",
                    },
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "Step title (1 line)"},
                                "description": {
                                    "type": "string",
                                    "description": "What to do in this step (details, files to change, expected result)",
                                },
                            },
                            "required": ["title"],
                        },
                        "description": "Ordered list of steps. Each step = roughly one commit.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Additional context, constraints, owner requirements",
                    },
                },
                "required": ["title", "steps"],
            },
        }, _plan_create),

        ToolEntry("plan_approve", {
            "name": "plan_approve",
            "description": (
                "Approve a draft plan to start execution. Only call after owner confirms the plan. "
                "First step automatically becomes 'in_progress'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plan_id": {"type": "string", "description": "Plan ID to approve"},
                },
                "required": ["plan_id"],
            },
        }, _plan_approve),

        ToolEntry("plan_reject", {
            "name": "plan_reject",
            "description": "Reject a plan (draft or active). Use when owner says to abandon the plan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan_id": {"type": "string", "description": "Plan ID to reject"},
                    "reason": {"type": "string", "description": "Why the plan was rejected"},
                },
                "required": ["plan_id"],
            },
        }, _plan_reject),

        ToolEntry("plan_step_done", {
            "name": "plan_step_done",
            "description": (
                "Mark the current in-progress step as completed. Automatically advances to next step. "
                "Call after committing and verifying the step's work."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plan_id": {"type": "string", "description": "Plan ID"},
                    "result": {
                        "type": "string",
                        "description": "Brief summary of what was done (files changed, tests result, commit message)",
                    },
                    "commit": {
                        "type": "string",
                        "description": "Commit SHA or version tag (e.g. 'v6.70.1' or 'abc1234')",
                    },
                },
                "required": ["plan_id", "result"],
            },
        }, _plan_step_done),

        ToolEntry("plan_update", {
            "name": "plan_update",
            "description": (
                "Update an active or draft plan: change notes, add new steps at the end, "
                "or remove pending steps. Cannot remove done or in-progress steps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plan_id": {"type": "string", "description": "Plan ID"},
                    "notes": {"type": "string", "description": "Replace plan notes (optional)"},
                    "add_steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["title"],
                        },
                        "description": "Steps to add at the end (optional)",
                    },
                    "remove_step_indices": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Indices of pending steps to remove (optional)",
                    },
                },
                "required": ["plan_id"],
            },
        }, _plan_update),

        ToolEntry("plan_complete", {
            "name": "plan_complete",
            "description": (
                "Mark the plan as completed. Call when all steps are done. "
                "Any remaining pending steps will be marked as skipped. Sends owner a final summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plan_id": {"type": "string", "description": "Plan ID"},
                    "summary": {
                        "type": "string",
                        "description": "Final summary of the entire plan execution",
                    },
                },
                "required": ["plan_id"],
            },
        }, _plan_complete),

        ToolEntry("plan_status", {
            "name": "plan_status",
            "description": "Show current plan status and progress. If no plan_id given, shows the active plan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan_id": {
                        "type": "string",
                        "description": "Plan ID (optional — defaults to active plan)",
                    },
                },
            },
        }, _plan_status),
    ]
