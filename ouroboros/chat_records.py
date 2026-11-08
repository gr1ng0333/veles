"""Authorship and stable references for deployed and legacy chat rows."""

import hashlib
import json
import pathlib
import re
from collections import deque
from contextlib import closing
from itertools import islice


def in_chat(entry: dict, chat_id: int | str | None) -> bool:
    """Keep old owner-chat rows that predate chat IDs."""
    recorded = entry.get("chat_id")
    return chat_id is None or recorded is None or str(recorded) == str(chat_id)


def chat_message_id(entry: dict) -> str:
    canonical = json.dumps(entry, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return "chat:" + hashlib.sha256(canonical.encode("ascii")).hexdigest()[:24]


def chat_message_label(entry: dict) -> str:
    author = {"in": "Owner", "out": "Veles", "system": "System", "unknown": "Unknown speaker"}
    return f"{author[chat_direction(entry)]} | {str(entry.get('ts') or 'unknown date')[:25]}"


def _chat_rows(path: pathlib.Path, chat_id: int):
    with path.open("rb") as file:
        for line in file:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and in_chat(row, chat_id):
                yield row


def read_chat_message(path: pathlib.Path, chat_id: int, message_id: str, offset: int = 0) -> str:
    if not isinstance(message_id, str) or not re.fullmatch(r"chat:[0-9a-f]{24}", message_id):
        return "⚠️ Use the message_id returned by semantic chat search."
    if type(offset) is not int or offset < 0:
        return "⚠️ text_offset must be a nonnegative integer."
    before = deque(maxlen=2)
    try:
        with closing(_chat_rows(path, chat_id)) as rows:
            for row in rows:
                if chat_message_id(row) == message_id:
                    neighbors = [*before, *islice(rows, 2)] if offset == 0 else []
                    return _message_page(row, message_id, offset, neighbors)
                before.append(row)
    except FileNotFoundError:
        pass
    return "⚠️ That message is not present in this chat log."


def _message_page(row: dict, message_id: str, offset: int, neighbors: list[dict]) -> str:
    text = str(row.get("text") or "")
    if offset > len(text):
        return f"⚠️ text_offset exceeds the message length ({len(text)} characters)."
    end = min(offset + 1400, len(text))
    lines = [f"{message_id} | {chat_message_label(row)}"]
    for neighbor in neighbors:
        preview = " ".join(str(neighbor.get("text") or "").split())[:95]
        lines.append(f"Nearby {chat_message_id(neighbor)} | {chat_message_label(neighbor)} | {preview}")
    next_offset = end if end < len(text) else "end"
    lines.append(f"Original text | offset={offset} next_text_offset={next_offset}\n\n{text[offset:end]}")
    return "\n".join(lines)


def owner_chat_id(drive_root: pathlib.Path) -> int | None:
    try:
        state = json.loads((drive_root / "state/state.json").read_text(encoding="utf-8"))
        return int(state.get("owner_chat_id") or 0) or None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def chat_direction(entry: dict) -> str:
    aliases = {"in": "in", "incoming": "in", "user": "in", "creator": "in",
               "out": "out", "outgoing": "out", "assistant": "out", "veles": "out",
               "system": "system"}
    for key in ("role", "direction"):
        value = aliases.get(str(entry.get(key) or "").strip().lower())
        if value:
            return value
    return "unknown"
