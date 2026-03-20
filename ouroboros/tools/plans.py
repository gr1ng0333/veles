"""Plan management tools — create, approve, execute, and complete structured plans."""

from __future__ import annotations

import json
from typing import List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros import plans


def _plan_create(ctx: ToolContext, title: str, steps: list, notes: str = "") -> str:
    plan = plans.create_plan(ctx.drive_root, title, steps, notes=notes)
    return plans.format_plan_summary(plan)


def _plan_approve(ctx: ToolContext, plan_id: str) -> str:
    plan = plans.approve_plan(ctx.drive_root, plan_id)
    return plans.format_plan_summary(plan)


def _plan_reject(ctx: ToolContext, plan_id: str, reason: str = "") -> str:
    plan = plans.reject_plan(ctx.drive_root, plan_id, reason=reason)
    return f"Plan '{plan['title']}' rejected."


def _plan_step_done(ctx: ToolContext, plan_id: str, result: str, commit: str = "") -> str:
    plan = plans.step_done(ctx.drive_root, plan_id, result=result, commit=commit)
    return plans.format_plan_summary(plan)


def _plan_update(ctx: ToolContext, plan_id: str, notes: str = None,
                 add_steps: list = None, remove_step_indices: list = None) -> str:
    plan = plans.update_plan(
        ctx.drive_root, plan_id,
        notes=notes, add_steps=add_steps, remove_step_indices=remove_step_indices,
    )
    return plans.format_plan_summary(plan)


def _plan_complete(ctx: ToolContext, plan_id: str, summary: str = "") -> str:
    plan = plans.complete_plan(ctx.drive_root, plan_id, summary=summary)
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
