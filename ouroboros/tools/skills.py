"""Skills system — dynamic context loading by domain.

skill_load(name) writes the skill name to active_skills in state.json.
context.py Block 1 then picks up active_skills and injects the
corresponding prompts/skills/{name}.md file into the next LLM round.

Skills are task-scoped: active_skills is reset to [] on every task_done.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import List

from ouroboros.tools.registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

# Relative to repo_dir
_SKILLS_DIR = "prompts/skills"


def _skill_load(ctx: ToolContext, name: str) -> str:
    """Load a skill into active context for this task."""
    if not name or not isinstance(name, str):
        return "⚠️ skill_load: name must be a non-empty string."

    # Sanitize: only alphanumeric, dash, underscore
    import re
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$', name.strip()):
        return f"⚠️ skill_load: invalid name '{name}'. Use alphanumeric, dash, underscore only."

    name = name.strip()

    # Check that the skill file exists
    skill_path = ctx.repo_dir / _SKILLS_DIR / f"{name}.md"
    if not skill_path.exists():
        # List available skills so the agent can pick the right one
        available = _list_skills(ctx)
        return (
            f"⚠️ skill_load: skill '{name}' not found at {skill_path}.\n"
            f"Available skills:\n{available}"
        )

    # Update state.json: add name to active_skills (dedup)
    state_path = ctx.drive_root / "state" / "state.json"
    try:
        from supervisor.state import load_state, save_state
        st = load_state()
        current: List[str] = list(st.get("active_skills") or [])
        if name not in current:
            current.append(name)
        st["active_skills"] = current
        save_state(st)
    except Exception as e:
        log.warning("skill_load: failed to update state: %s", e, exc_info=True)
        return f"⚠️ skill_load: failed to update state: {e}"

    # Return preview of the skill header (first 3 non-empty lines)
    try:
        text = skill_path.read_text(encoding="utf-8")
        preview_lines = [l for l in text.splitlines() if l.strip()][:3]
        preview = "\n".join(preview_lines)
    except Exception:
        preview = "(could not read preview)"

    return (
        f"✅ Skill '{name}' loaded. It will appear in context from the next round.\n\n"
        f"Preview:\n{preview}"
    )


def _skill_list(ctx: ToolContext) -> str:
    """List available skills from prompts/skills/."""
    return _list_skills(ctx)


def _list_skills(ctx: ToolContext) -> str:
    skills_dir = ctx.repo_dir / _SKILLS_DIR
    if not skills_dir.exists():
        return "(no skills directory found at prompts/skills/)"

    entries = []
    for f in sorted(skills_dir.glob("*.md")):
        if f.name.startswith("_"):
            continue  # skip _map.md and other meta-files
        topic = f.stem
        try:
            text = f.read_text(encoding="utf-8")
            # First heading line as description
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    desc = line[:100]
                    break
                if line.startswith("#"):
                    desc = line.lstrip("#").strip()[:100]
                    break
            else:
                desc = "(no description)"
        except Exception:
            desc = "(unreadable)"
        entries.append(f"- **{topic}**: {desc}")

    if not entries:
        return "(no skills defined yet)"

    return "\n".join(entries)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("skill_load", {
            "name": "skill_load",
            "description": (
                "Load a skill (domain-specific context block) for the current task. "
                "The skill file from prompts/skills/{name}.md will be injected into "
                "the LLM context starting from the NEXT round. "
                "Skills are task-scoped: automatically cleared on task completion. "
                "Call this in the first round when the task touches a known domain "
                "(e.g. '3xui', 'ssh-servers', 'vpn'). "
                "Check the skills map in the system prompt to know what's available."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name (alphanumeric, dash, underscore). E.g. '3xui', 'ssh-servers', 'vpn'",
                    },
                },
                "required": ["name"],
            },
        }, _skill_load),

        ToolEntry("skill_list", {
            "name": "skill_list",
            "description": (
                "List all available skills in prompts/skills/ with descriptions. "
                "Use when unsure what skills exist."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        }, _skill_list),
    ]
