"""Tool: send_file — send a text file to the owner's Telegram chat."""

from __future__ import annotations

import logging
from typing import List

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)


def _send_file(ctx: ToolContext, content: str, filename: str, caption: str = "") -> str:
    """Queue a file for delivery to the owner's Telegram chat."""
    if not ctx.current_chat_id:
        return "⚠️ No active chat — cannot send file."

    if not content:
        return "⚠️ File content is empty."

    if not filename or not filename.strip():
        return "⚠️ Filename is required."

    ctx.pending_events.append({
        "type": "send_document",
        "chat_id": ctx.current_chat_id,
        "content": content,
        "filename": filename.strip(),
        "caption": (caption or "")[:1024],
    })
    return f"OK: file «{filename}» queued for delivery to owner."


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="send_file",
            schema={
                "name": "send_file",
                "description": (
                    "Send a file to the user in Telegram chat. "
                    "Use this when user asks for code, documents, or any content "
                    "as a downloadable file. Content is sent as a text file with "
                    "the specified filename and extension."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "The text content of the file",
                        },
                        "filename": {
                            "type": "string",
                            "description": (
                                "Filename with extension, e.g. "
                                "'calculator.py', 'report.md', 'data.json'"
                            ),
                        },
                        "caption": {
                            "type": "string",
                            "description": "Optional caption/description for the file",
                        },
                    },
                    "required": ["content", "filename"],
                },
            },
            handler=_send_file,
        ),
    ]
