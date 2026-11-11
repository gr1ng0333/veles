"""Measure the startup blocks assembled by the normal task builder."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from ouroboros.context import STARTUP_TARGET_TOKENS, build_startup_sections
from ouroboros.memory import Memory
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import estimate_tokens

_BLOCKS = ("Block0_Static_cached_1h", "Block1_SemiStable_cached", "Block2_Dynamic_uncached")


def _build_context_inspect(
    ctx: ToolContext,
    include_preview: bool = False,
    task_type: str = "user",
) -> str:
    task = {
        "id": ctx.task_id, "type": task_type, "chat_id": ctx.current_chat_id,
        "conversation_mode": ctx.conversation_mode,
    }
    blocks = {}
    for name, sections in zip(_BLOCKS, build_startup_sections(ctx, Memory(ctx.drive_root), task)):
        entries: List[Dict[str, Any]] = []
        texts = []
        for section, text in sections:
            if not text:
                continue
            texts.append(text)
            entry = {"section": section, "chars": len(text), "tokens_est": estimate_tokens(text)}
            if include_preview:
                entry["preview"] = text[:200] + ("..." if len(text) > 200 else "")
            entries.append(entry)
        block_text = "\n\n".join(texts)
        top = sorted(entries, key=lambda entry: -entry["tokens_est"])[:3]
        blocks[name] = {
            "tokens_est": estimate_tokens(block_text),
            "chars": len(block_text),
            "sections": entries,
            "top_sections": [{"section": entry["section"], "tokens_est": entry["tokens_est"]} for entry in top],
        }
    total = sum(block["tokens_est"] for block in blocks.values())
    return json.dumps({
        "task_type": task_type,
        "startup_target_tokens": STARTUP_TARGET_TOKENS,
        "grand_total_est": total,
        "dominant_block": max(blocks, key=lambda name: blocks[name]["tokens_est"]),
        "blocks": blocks,
        "loaded_skills": {name: estimate_tokens(body) for name, body in ctx.active_skills.items()},
        "note": "Heuristic for base prompt text. Current request, tools and later turns are excluded. "
                "The startup target is informational.",
    }, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="context_inspect",
            schema={
                "name": "context_inspect",
                "description": "Estimate the shared startup context by block and section.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "include_preview": {
                            "type": "boolean",
                            "description": "Include 200-char text preview for each section. Default: false.",
                        },
                        "task_type": {
                            "type": "string",
                            "description": "Task type to display in the runtime block.",
                            "enum": ["user", "evolution", "review", "scheduled"],
                        },
                    },
                    "required": [],
                },
            },
            handler=_build_context_inspect,
            timeout_sec=30,
        )
    ]
