"""Evolution focus tool — cross-cycle goal memory.

Allows setting a persistent multi-cycle focus so evolution tasks
know what the current strategic goal is across restarts and cycles.

Tools:
  set_evolution_focus(goal, horizon_cycles)  — set/update the active focus
  get_evolution_focus()                      — read current focus (with progress)
  clear_evolution_focus()                    — clear when goal is achieved
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

_FOCUS_FILE = "state/evolution_focus.json"


def _focus_path(ctx: ToolContext) -> Path:
    return ctx.drive_root / _FOCUS_FILE


def _load_focus(ctx: ToolContext) -> Dict[str, Any]:
    p = _focus_path(ctx)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_focus(ctx: ToolContext, data: Dict[str, Any]) -> None:
    p = _focus_path(ctx)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Public helpers (used by context.py)
# ---------------------------------------------------------------------------

def load_evolution_focus(drive_root: Path) -> Dict[str, Any]:
    """Load the current evolution focus from drive_root. Returns {} if none."""
    p = drive_root / _FOCUS_FILE
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def format_focus_for_context(focus: Dict[str, Any]) -> str:
    """Format focus dict as a context section string."""
    if not focus:
        return ""
    goal = focus.get("goal", "(no goal set)")
    horizon = focus.get("horizon_cycles", "?")
    cycles_done = focus.get("cycles_completed", 0)
    set_at = focus.get("set_at", "")
    notes = focus.get("notes", [])

    lines = [
        "## Evolution Focus\n",
        f"**Goal:** {goal}",
        f"**Horizon:** {horizon} cycles | **Completed:** {cycles_done}",
    ]
    if set_at:
        lines.append(f"**Set at:** {set_at}")
    if notes:
        lines.append("\n**Progress notes:**")
        for n in notes[-5:]:  # last 5 notes
            ts = n.get("ts", "")
            text = n.get("text", "")
            lines.append(f"- [{ts[:16]}] {text}")
    lines.append(
        "\n*Use `add_focus_note(text)` to record progress, "
        "`clear_evolution_focus()` when the goal is achieved.*"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _set_evolution_focus(ctx: ToolContext, goal: str, horizon_cycles: int = 5, note: str = "") -> str:
    """Set or update the cross-cycle evolution focus."""
    if not goal.strip():
        return "⚠️ goal must be non-empty."
    if horizon_cycles < 1:
        return "⚠️ horizon_cycles must be >= 1."

    existing = _load_focus(ctx)
    data: Dict[str, Any] = {
        "goal": goal.strip(),
        "horizon_cycles": horizon_cycles,
        "set_at": utc_now_iso(),
        "cycles_completed": existing.get("cycles_completed", 0),
        "notes": existing.get("notes", []),
    }
    if note.strip():
        data["notes"].append({"ts": utc_now_iso(), "text": note.strip()})

    _save_focus(ctx, data)
    return (
        f"✅ Evolution focus set:\n"
        f"  Goal: {goal}\n"
        f"  Horizon: {horizon_cycles} cycles\n"
        f"  Completed so far: {data['cycles_completed']}"
    )


def _get_evolution_focus(ctx: ToolContext) -> str:
    """Read the current cross-cycle evolution focus."""
    focus = _load_focus(ctx)
    if not focus:
        return "No active evolution focus. Use set_evolution_focus(goal, horizon_cycles) to set one."
    return format_focus_for_context(focus)


def _add_focus_note(ctx: ToolContext, text: str) -> str:
    """Add a progress note to the current evolution focus."""
    if not text.strip():
        return "⚠️ text must be non-empty."
    focus = _load_focus(ctx)
    if not focus:
        return "⚠️ No active evolution focus. Set one first with set_evolution_focus()."
    notes: List[Dict[str, str]] = focus.get("notes", [])
    notes.append({"ts": utc_now_iso(), "text": text.strip()})
    focus["notes"] = notes
    _save_focus(ctx, focus)
    return f"✅ Note added to evolution focus ({len(notes)} total notes)."


def _complete_focus_cycle(ctx: ToolContext, note: str = "") -> str:
    """Increment the completed-cycles counter for the current focus."""
    focus = _load_focus(ctx)
    if not focus:
        return "⚠️ No active evolution focus."
    focus["cycles_completed"] = focus.get("cycles_completed", 0) + 1
    if note.strip():
        notes = focus.get("notes", [])
        notes.append({"ts": utc_now_iso(), "text": f"[cycle complete] {note.strip()}"})
        focus["notes"] = notes
    _save_focus(ctx, focus)
    done = focus["cycles_completed"]
    horizon = focus.get("horizon_cycles", "?")
    goal = focus.get("goal", "")
    remaining = (horizon - done) if isinstance(horizon, int) else "?"
    if isinstance(horizon, int) and done >= horizon:
        return (
            f"✅ Focus cycle {done}/{horizon} complete — **goal horizon reached!**\n"
            f"Goal: {goal}\n"
            f"Consider calling clear_evolution_focus() if the goal is achieved."
        )
    return (
        f"✅ Focus cycle {done}/{horizon} complete. {remaining} cycles remaining.\n"
        f"Goal: {goal}"
    )


def _clear_evolution_focus(ctx: ToolContext) -> str:
    """Clear the current evolution focus (goal achieved or abandoned)."""
    p = _focus_path(ctx)
    if not p.exists():
        return "No active evolution focus to clear."
    focus = _load_focus(ctx)
    goal = focus.get("goal", "(unknown)")
    done = focus.get("cycles_completed", 0)
    p.unlink()
    return f"✅ Evolution focus cleared. Goal was: '{goal}' ({done} cycles completed)."


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("set_evolution_focus", {
            "name": "set_evolution_focus",
            "description": (
                "Set a cross-cycle evolution focus — a strategic goal that persists "
                "across multiple evolution cycles and restarts. "
                "Visible in every subsequent evolution task as '## Evolution Focus'. "
                "Use this to maintain coherent multi-cycle work toward one goal."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "One-sentence description of the strategic goal for the next N cycles.",
                    },
                    "horizon_cycles": {
                        "type": "integer",
                        "description": "Expected number of evolution cycles to achieve the goal (default: 5).",
                        "default": 5,
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional initial progress note.",
                        "default": "",
                    },
                },
                "required": ["goal"],
            },
        }, _set_evolution_focus),
        ToolEntry("get_evolution_focus", {
            "name": "get_evolution_focus",
            "description": "Read the current cross-cycle evolution focus (goal, progress, notes).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }, _get_evolution_focus),
        ToolEntry("add_focus_note", {
            "name": "add_focus_note",
            "description": "Add a progress note to the current evolution focus. Use at the end of each cycle to record what was done toward the multi-cycle goal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Progress note (what was done this cycle)."},
                },
                "required": ["text"],
            },
        }, _add_focus_note),
        ToolEntry("complete_focus_cycle", {
            "name": "complete_focus_cycle",
            "description": "Mark one cycle as complete toward the evolution focus goal. Increments the cycle counter. Call at the end of each cycle that made progress toward the focus goal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "Optional summary of what was achieved this cycle.", "default": ""},
                },
                "required": [],
            },
        }, _complete_focus_cycle),
        ToolEntry("clear_evolution_focus", {
            "name": "clear_evolution_focus",
            "description": "Clear the current evolution focus (goal achieved or abandoned). Call when the multi-cycle goal is complete.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }, _clear_evolution_focus),
    ]
