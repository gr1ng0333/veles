"""
Ouroboros — Codex tool-call recovery.

Detects tool calls embedded as text in Codex responses and extracts them.
Disabled by default (CODEX_TOOL_RECOVERY_ENABLED=false).
Split from codex_proxy.py for maintainability.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


def _extract_balanced_braces(text: str, start: int) -> Optional[str]:
    """Extract a balanced {...} substring starting at position *start*."""
    if start >= len(text) or text[start] != '{':
        return None
    depth = 0
    in_str = False
    esc = False
    limit = min(start + 10000, len(text))
    for i in range(start, limit):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == '\\' and in_str:
            esc = True
        elif ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def _make_tool_call(name: str, args: Any) -> Dict[str, Any]:
    """Create a Chat Completions tool_call dict with a unique id."""
    if isinstance(args, dict):
        args_str = json.dumps(args)
    elif isinstance(args, str):
        args_str = args
    else:
        args_str = json.dumps(args)
    return {
        "id": f"call_x_{os.urandom(6).hex()}",
        "type": "function",
        "function": {
            "name": str(name),
            "arguments": args_str,
        },
    }


def _try_parse_tool_json(json_str: str, out: List[Dict[str, Any]]) -> None:
    """Try to parse *json_str* as tool call(s) and append to *out*."""
    try:
        obj = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(obj, dict):
        return

    # {"name": "tool", "arguments": {...}}
    if "name" in obj and ("arguments" in obj or "args" in obj):
        args = obj.get("arguments", obj.get("args", {}))
        out.append(_make_tool_call(obj["name"], args))
        return

    # {"cmd": "tool", "args": {...}}  (ChatGPT internal format)
    if "cmd" in obj:
        args = obj.get("args", obj.get("arguments", {}))
        out.append(_make_tool_call(obj["cmd"], args))
        return

    # {"tool_uses": [{"name": ..., "arguments": ...}, ...]}
    # {"tool_uses": [{"recipient_name": "functions.tool", "parameters": {...}}, ...]}
    if "tool_uses" in obj and isinstance(obj["tool_uses"], list):
        for item in obj["tool_uses"]:
            if not isinstance(item, dict):
                continue
            if "name" in item:
                args = item.get("arguments", item.get("args", {}))
                out.append(_make_tool_call(item["name"], args))
                continue
            if "recipient_name" in item:
                rn = str(item["recipient_name"])
                name = rn[len("functions."):] if rn.startswith("functions.") else rn
                args = item.get("parameters", item.get("arguments", {}))
                out.append(_make_tool_call(name, args))
        return

    # {"recipient_name": "functions.tool_name", "parameters": {...}}
    if "recipient_name" in obj:
        rn = str(obj["recipient_name"])
        name = rn[len("functions."):] if rn.startswith("functions.") else rn
        args = obj.get("parameters", obj.get("arguments", {}))
        out.append(_make_tool_call(name, args))
        return


# Quick-check regex: text that might contain an embedded tool call
_TOOL_JSON_HINT = re.compile(
    r'"(?:name|cmd|tool_uses|recipient_name)"\s*:', re.IGNORECASE,
)


def _try_extract_tool_calls_from_text(
    text: str,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Detect tool-call JSON embedded in assistant text and extract them.

    When Codex returns tool calls as plain text (e.g. in final_answer phase)
    instead of proper ``function_call`` items, this function recovers them.

    Returns ``(tool_calls_list, cleaned_text)``.
    """
    if not text or len(text) < 5:
        return [], text
    if not _TOOL_JSON_HINT.search(text):
        return [], text

    extracted: List[Dict[str, Any]] = []
    regions: List[Tuple[int, int]] = []

    # Pass 1 — JSON inside markdown code blocks
    for m in re.finditer(r'```(?:json)?\s*(\{.+?\})\s*```', text, re.DOTALL):
        n = len(extracted)
        _try_parse_tool_json(m.group(1), extracted)
        if len(extracted) > n:
            regions.append((m.start(), m.end()))

    # Pass 2 — raw JSON objects (scan all '{' characters)
    i = 0
    attempts = 0
    while i < len(text) and attempts < 50:
        idx = text.find('{', i)
        if idx == -1:
            break
        i = idx + 1
        # Skip if inside an already-captured region
        if any(s <= idx < e for s, e in regions):
            continue
        attempts += 1
        obj_str = _extract_balanced_braces(text, idx)
        if not obj_str:
            continue
        n = len(extracted)
        _try_parse_tool_json(obj_str, extracted)
        if len(extracted) > n:
            end = idx + len(obj_str)
            regions.append((idx, end))
            i = end

    if not extracted:
        return [], text

    # Remove extracted JSON from the text
    cleaned = text
    for s, e in sorted(regions, reverse=True):
        cleaned = cleaned[:s] + cleaned[e:]
    # If recovery fired, strip leftover pseudo-tool-call preambles like
    # "to=multi_tool_use.parallel" that should never leak into chat.
    cleaned = re.sub(r'(?im)^\s*to=[^\n]+\n?', '', cleaned)
    cleaned = re.sub(r'(?im)^\s*```(?:json)?\s*$', '', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

    log.info(
        "Recovered %d tool call(s) from Codex text content",
        len(extracted),
    )
    return extracted, cleaned
