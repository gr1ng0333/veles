"""Load local instruction snapshots into the current task's context."""
from __future__ import annotations

import logging
import re
from typing import List

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)
_SKILLS_DIR = "prompts/skills"
_SKILL_HEADER = "## Task skill: "


def _skill_blocks(messages: list) -> dict[str, str]:
    system = next((message for message in messages if message.get("role") == "system"), {})
    blocks = system.get("content")
    if not isinstance(blocks, list):
        return {}
    skills = {}
    for block in blocks:
        text = block.get("text", "") if isinstance(block, dict) else ""
        if isinstance(text, str) and text.startswith(_SKILL_HEADER):
            heading, separator, body = text.partition("\n\n")
            if separator:
                skills[heading[len(_SKILL_HEADER):]] = body
    return skills


def restore_task_skills(ctx: ToolContext, messages: list) -> None:
    # The checkpoint has the original bytes even if the file has since changed.
    ctx.active_skills = _skill_blocks(messages)


def attach_task_skills(ctx: ToolContext, messages: list) -> None:
    if not ctx.active_skills:
        return
    attached = _skill_blocks(messages)
    extra = [
        {"type": "text", "text": f"{_SKILL_HEADER}{name}\n\n{body}"}
        for name, body in ctx.active_skills.items() if name not in attached
    ]
    if not extra:
        return
    system = next((message for message in messages if message.get("role") == "system"), None)
    if system is None:
        messages.insert(0, {"role": "system", "content": extra})
        return
    content = system.get("content", "")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
    system["content"] = [*blocks, *extra]


def _skill_load(ctx: ToolContext, name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        return "⚠️ skill_load: name must be a non-empty string."
    name = name.strip()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
        return "⚠️ skill_load: invalid skill name."
    if name in ctx.active_skills:
        return f"✅ Skill '{name}' is already attached to this task."
    root = (ctx.repo_dir / _SKILLS_DIR).resolve()
    skill_path = (root / f"{name}.md").resolve()
    if skill_path.parent != root:
        return "⚠️ skill_load: skill path is outside the local skill directory."
    try:
        body = skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"⚠️ skill_load: skill '{name}' not found. Use skill_list or the skill map."
    except (OSError, UnicodeError):
        log.warning("Could not read skill %s", name, exc_info=True)
        return f"⚠️ skill_load: skill '{name}' could not be read."
    if not body.strip():
        return f"⚠️ skill_load: skill '{name}' is empty."
    ctx.active_skills[name] = body
    return f"✅ Skill '{name}' loaded. Its full instructions join the next request for this task."


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
                "Select a skill from the map based on the work at hand. Its full text is attached "
                "from the next request through the end of this task. Call when it becomes relevant."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact skill name from the map or skill_list.",
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
