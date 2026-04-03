from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple



def _message_text_content(msg: Dict[str, Any]) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: List[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and part.get("text"):
            parts.append(str(part["text"]))
    return "\n".join(parts)


def _messages_to_input(messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
    """Convert Chat Completions style messages into Codex Responses API input."""
    input_items: List[Dict[str, Any]] = []
    system_parts: List[str] = []

    for msg in messages:
        role = msg.get("role")

        if role == "system":
            text = _message_text_content(msg)
            if text:
                system_parts.append(text)
            continue

        if role == "user":
            content = msg.get("content")
            if isinstance(content, str):
                input_items.append({
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": content}],
                })
                continue

            if isinstance(content, list):
                parts: List[Dict[str, Any]] = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype == "text" and part.get("text"):
                        parts.append({"type": "input_text", "text": str(part["text"])})
                    elif ptype == "image_url":
                        image_url = part.get("image_url") or {}
                        url = image_url.get("url") if isinstance(image_url, dict) else None
                        if url:
                            parts.append({"type": "input_image", "image_url": str(url)})
                if parts:
                    input_items.append({"type": "message", "role": "user", "content": parts})
                continue

        if role == "assistant":
            content = _message_text_content(msg)
            tool_calls = msg.get("tool_calls") or []
            if content:
                input_items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content}],
                })
            for tc in tool_calls:
                fn = tc.get("function") or {}
                input_items.append({
                    "type": "function_call",
                    "call_id": tc.get("id") or f"call_{os.urandom(6).hex()}",
                    "name": fn.get("name") or "unknown_tool",
                    "arguments": fn.get("arguments") or "{}",
                })
            continue

        if role == "tool":
            input_items.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id") or "call_unknown",
                "output": msg.get("content") if isinstance(msg.get("content"), str) else json.dumps(msg.get("content")),
            })

    return input_items, "\n\n".join(system_parts)



def _tools_to_responses_format(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if not tools:
        return []

    converted: List[Dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        fn = tool.get("function") or {}
        converted.append({
            "type": "function",
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return converted



def _output_to_chat_message(output_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Convert Codex Responses API output items back to Chat Completions message shape."""
    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []

    for item in output_items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")

        if item_type == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"} and part.get("text"):
                    text_parts.append(str(part["text"]))
            continue

        if item_type in {"output_text", "text"} and item.get("text"):
            text_parts.append(str(item["text"]))
            continue

        if item_type == "function_call":
            tool_calls.append({
                "id": item.get("call_id") or f"call_{os.urandom(6).hex()}",
                "type": "function",
                "function": {
                    "name": item.get("name") or "unknown_tool",
                    "arguments": item.get("arguments") or "{}",
                },
            })

    content = "\n".join(part for part in text_parts if part).strip()
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls or None,
    }
