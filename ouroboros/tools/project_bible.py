"""project_bible — PROJECT_BIBLE management for autonomous external project evolution.

Each external project has a PROJECT_BIBLE.md file stored at:
    {drive_root}/projects/{alias}/PROJECT_BIBLE.md

This file is the anchor for autonomous evolution cycles on external repos.
Without it, there is no "what is good here", no goals, no constraints — just
random code changes. With it, Veles can work 12–20 hours on a project autonomously.

Tools:
    project_bible_read(alias)
        — read the full PROJECT_BIBLE for a project

    project_bible_init(alias, repo_url, goal, stack, touch, no_touch, good_commit)
        — create a new PROJECT_BIBLE (overwrites if exists)

    project_bible_update(alias, section, content)
        — append or replace a named section in the PROJECT_BIBLE

    project_bible_status(alias)
        — get a compact status snapshot: current state + next steps

    project_bible_list()
        — list all known project bibles on disk

Usage in autonomous evolution:
    1. Start cycle: project_bible_read("copilot-tgbot") → load context
    2. Do work, commit to external repo
    3. End cycle: project_bible_update("copilot-tgbot", "Current state", updated_state)
    4. Next cycle starts with fresh context from PROJECT_BIBLE
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_PROJECTS_DIR = "projects"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _projects_root(ctx: ToolContext) -> Path:
    root = ctx.drive_path(_PROJECTS_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _bible_path(ctx: ToolContext, alias: str) -> Path:
    alias = _validate_alias(alias)
    return _projects_root(ctx) / alias / "PROJECT_BIBLE.md"


def _validate_alias(alias: str) -> str:
    alias = str(alias or "").strip()
    if not alias:
        raise ValueError("alias must be non-empty")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    if any(ch not in allowed for ch in alias):
        raise ValueError("alias may contain only letters, digits, '-' and '_'")
    return alias


def _parse_sections(text: str) -> Dict[str, str]:
    """Parse markdown sections (## heading) into a dict."""
    sections: Dict[str, str] = {}
    current_key: Optional[str] = None
    current_lines: List[str] = []

    for line in text.splitlines():
        if line.startswith("## "):
            if current_key is not None:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = line[3:].strip()
            current_lines = []
        else:
            if current_key is not None:
                current_lines.append(line)

    if current_key is not None:
        sections[current_key] = "\n".join(current_lines).strip()

    return sections


def _serialize_sections(title_block: str, sections: Dict[str, str]) -> str:
    """Reconstruct markdown from title block + sections dict."""
    parts = [title_block.rstrip()]
    for heading, body in sections.items():
        parts.append(f"\n## {heading}")
        if body:
            parts.append(body)
    return "\n".join(parts) + "\n"


def _read_bible(ctx: ToolContext, alias: str) -> str:
    """Read raw PROJECT_BIBLE content. Raises FileNotFoundError if missing."""
    path = _bible_path(ctx, alias)
    if not path.exists():
        raise FileNotFoundError(f"PROJECT_BIBLE not found for alias '{alias}'. Run project_bible_init first.")
    return path.read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# Tool implementations
# ──────────────────────────────────────────────────────────────────────────────


def _execute_project_bible_read(ctx: ToolContext, alias: str) -> Dict[str, Any]:
    """Read the full PROJECT_BIBLE for a project."""
    try:
        content = _read_bible(ctx, alias)
        path = _bible_path(ctx, alias)
        stat = path.stat()
        size = stat.st_size
        lines = content.count("\n")
        sections = list(_parse_sections(content).keys())
        return {
            "ok": True,
            "alias": alias,
            "path": str(path),
            "size_bytes": size,
            "lines": lines,
            "sections": sections,
            "content": content,
        }
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"Unexpected error: {e}"}


def _execute_project_bible_init(
    ctx: ToolContext,
    alias: str,
    repo_url: str,
    goal: str,
    stack: str,
    touch: Optional[str] = None,
    no_touch: Optional[str] = None,
    good_commit: Optional[str] = None,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Create a new PROJECT_BIBLE file for a project."""
    try:
        alias = _validate_alias(alias)
        path = _bible_path(ctx, alias)

        if path.exists() and not overwrite:
            return {
                "ok": False,
                "error": f"PROJECT_BIBLE already exists for '{alias}'. Pass overwrite=true to replace.",
                "path": str(path),
            }

        path.parent.mkdir(parents=True, exist_ok=True)

        # Build the name from repo_url
        repo_name = repo_url.rstrip("/").split("/")[-1]

        touch_block = touch or "- (fill in: which files to modify)"
        no_touch_block = no_touch or "- (fill in: which files to leave alone)"
        good_commit_block = good_commit or "- One working feature\n- Tests pass\n- Clean commit message"

        content = f"""# Project: {repo_name}

**Alias:** {alias}
**Repo:** {repo_url}
**Goal:** {goal}
**Stack:** {stack}
**Created:** {_utc_now()}

## What to touch
{touch_block}

## What NOT to touch
{no_touch_block}

## Good commit =
{good_commit_block}

## Current state ({_utc_now()})
- (fill in: what's working, what's missing)

## Next steps
1. (fill in: first thing to do)
"""

        path.write_text(content, encoding="utf-8")

        return {
            "ok": True,
            "alias": alias,
            "path": str(path),
            "repo_name": repo_name,
            "message": f"PROJECT_BIBLE created for '{alias}' at {path}",
        }

    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"Unexpected error: {e}"}


def _execute_project_bible_update(
    ctx: ToolContext,
    alias: str,
    section: str,
    content: str,
    mode: str = "replace",
) -> Dict[str, Any]:
    """Update a named section in the PROJECT_BIBLE.

    mode='replace' — replaces the section content entirely.
    mode='append'  — appends content to the existing section.
    If the section doesn't exist, it is created.
    """
    try:
        raw = _read_bible(ctx, alias)
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"Read error: {e}"}

    try:
        # Split title block (everything before first ## section) from sections
        first_section_match = re.search(r"\n## ", raw)
        if first_section_match:
            title_block = raw[: first_section_match.start()]
            sections_text = raw[first_section_match.start():]
        else:
            title_block = raw
            sections_text = ""

        # Parse existing sections
        sections: Dict[str, str] = {}
        if sections_text:
            sections = _parse_sections(sections_text)

        # Apply update
        section = section.strip()
        if mode == "append" and section in sections:
            existing = sections[section]
            sections[section] = (existing + "\n" + content.strip()).strip()
        else:
            sections[section] = content.strip()

        # Reconstruct and write
        new_content = _serialize_sections(title_block, sections)
        path = _bible_path(ctx, alias)
        path.write_text(new_content, encoding="utf-8")

        return {
            "ok": True,
            "alias": alias,
            "section": section,
            "mode": mode,
            "message": f"Section '{section}' updated in PROJECT_BIBLE for '{alias}'",
        }

    except Exception as e:
        return {"ok": False, "error": f"Update error: {e}"}


def _execute_project_bible_status(ctx: ToolContext, alias: str) -> Dict[str, Any]:
    """Get a compact status snapshot: current state + next steps.

    This is designed for the START of an evolution cycle — loads just enough
    context to know what to do next without reading the full file.
    """
    try:
        raw = _read_bible(ctx, alias)
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"Read error: {e}"}

    try:
        sections = _parse_sections(raw)

        # Extract header fields
        repo_url = ""
        goal = ""
        stack = ""
        for line in raw.splitlines()[:15]:
            if line.startswith("**Repo:**"):
                repo_url = line.split("**Repo:**", 1)[1].strip()
            elif line.startswith("**Goal:**"):
                goal = line.split("**Goal:**", 1)[1].strip()
            elif line.startswith("**Stack:**"):
                stack = line.split("**Stack:**", 1)[1].strip()

        # Find "Current state" section (may have a date suffix)
        current_state = ""
        next_steps = ""
        for key, val in sections.items():
            k_lower = key.lower()
            if "current state" in k_lower:
                current_state = val
            elif "next steps" in k_lower or "next step" in k_lower:
                next_steps = val

        return {
            "ok": True,
            "alias": alias,
            "repo_url": repo_url,
            "goal": goal,
            "stack": stack,
            "current_state": current_state or "(no current state section found)",
            "next_steps": next_steps or "(no next steps section found)",
            "all_sections": list(sections.keys()),
        }

    except Exception as e:
        return {"ok": False, "error": f"Parse error: {e}"}


def _execute_project_bible_list(ctx: ToolContext) -> Dict[str, Any]:
    """List all project bibles on disk."""
    try:
        root = _projects_root(ctx)
        projects = []
        for alias_dir in sorted(root.iterdir()):
            if not alias_dir.is_dir():
                continue
            bible_file = alias_dir / "PROJECT_BIBLE.md"
            if bible_file.exists():
                stat = bible_file.stat()
                # Extract goal line quickly
                goal = "(unknown)"
                try:
                    for line in bible_file.read_text(encoding="utf-8").splitlines()[:10]:
                        if line.startswith("**Goal:**"):
                            goal = line.split("**Goal:**", 1)[1].strip()
                            break
                except Exception:
                    pass
                projects.append({
                    "alias": alias_dir.name,
                    "path": str(bible_file),
                    "size_bytes": stat.st_size,
                    "goal": goal,
                })
        return {
            "ok": True,
            "count": len(projects),
            "projects": projects,
        }
    except Exception as e:
        return {"ok": False, "error": f"List error: {e}"}


# ──────────────────────────────────────────────────────────────────────────────
# Tool registry
# ──────────────────────────────────────────────────────────────────────────────


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="project_bible_read",
            description=(
                "Read the full PROJECT_BIBLE for an external project. "
                "Returns goal, stack, constraints, current state, next steps. "
                "Call at the start of every autonomous project evolution cycle."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "alias": {
                        "type": "string",
                        "description": "Project alias (e.g. 'copilot-tgbot')",
                    },
                },
                "required": ["alias"],
            },
            execute=lambda ctx, alias: _execute_project_bible_read(ctx, alias),
        ),
        ToolEntry(
            name="project_bible_init",
            description=(
                "Create a new PROJECT_BIBLE for an external project. "
                "Sets up the anchor document that enables autonomous evolution cycles. "
                "Pass overwrite=true to replace an existing file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "alias": {
                        "type": "string",
                        "description": "Short alias for the project (letters/digits/-/_)",
                    },
                    "repo_url": {
                        "type": "string",
                        "description": "GitHub URL of the project (e.g. https://github.com/user/repo)",
                    },
                    "goal": {
                        "type": "string",
                        "description": "One-sentence project goal",
                    },
                    "stack": {
                        "type": "string",
                        "description": "Tech stack (e.g. 'Python, FastAPI, SQLite')",
                    },
                    "touch": {
                        "type": "string",
                        "description": "Which files/modules to modify (markdown bullet list)",
                    },
                    "no_touch": {
                        "type": "string",
                        "description": "Which files/modules to leave alone (markdown bullet list)",
                    },
                    "good_commit": {
                        "type": "string",
                        "description": "Criteria for a good commit (markdown bullet list)",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "If true, replace existing PROJECT_BIBLE",
                    },
                },
                "required": ["alias", "repo_url", "goal", "stack"],
            },
            execute=lambda ctx, alias, repo_url, goal, stack, touch=None, no_touch=None, good_commit=None, overwrite=False: _execute_project_bible_init(
                ctx, alias, repo_url, goal, stack, touch, no_touch, good_commit, overwrite
            ),
        ),
        ToolEntry(
            name="project_bible_update",
            description=(
                "Update a named section in the PROJECT_BIBLE. "
                "Use after each evolution cycle to record what was done and what's next. "
                "mode='replace' (default) replaces section; mode='append' adds to it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "alias": {
                        "type": "string",
                        "description": "Project alias",
                    },
                    "section": {
                        "type": "string",
                        "description": "Section heading to update (e.g. 'Current state', 'Next steps')",
                    },
                    "content": {
                        "type": "string",
                        "description": "New section content (markdown)",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["replace", "append"],
                        "description": "replace (default) or append",
                    },
                },
                "required": ["alias", "section", "content"],
            },
            execute=lambda ctx, alias, section, content, mode="replace": _execute_project_bible_update(
                ctx, alias, section, content, mode
            ),
        ),
        ToolEntry(
            name="project_bible_status",
            description=(
                "Get a compact status snapshot of an external project: "
                "goal, current state, and next steps. "
                "Faster than project_bible_read when you only need orientation, not full file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "alias": {
                        "type": "string",
                        "description": "Project alias",
                    },
                },
                "required": ["alias"],
            },
            execute=lambda ctx, alias: _execute_project_bible_status(ctx, alias),
        ),
        ToolEntry(
            name="project_bible_list",
            description=(
                "List all known project bibles on disk. "
                "Shows alias, goal, and file size for each project."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            execute=lambda ctx: _execute_project_bible_list(ctx),
        ),
    ]
