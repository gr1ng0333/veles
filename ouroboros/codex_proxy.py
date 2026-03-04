"""
Ouroboros — Codex OAuth proxy.

Routes LLM calls through ChatGPT's Codex endpoint using OAuth tokens.
Uses urllib only (no requests dependency).
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

CODEX_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
AUTH_ENDPOINT = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_FILE = Path("/opt/veles-data/state/codex_tokens.json")
TIMEOUT_SEC = 120
MAX_RETRIES = 2
REFRESH_THRESHOLD_SEC = 3600  # refresh if < 1 hour until expiry


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def _load_tokens() -> Dict[str, str]:
    """Load tokens from env vars, falling back to token file."""
    tokens = {
        "access_token": os.environ.get("CODEX_ACCESS_TOKEN", ""),
        "refresh_token": os.environ.get("CODEX_REFRESH_TOKEN", ""),
        "expires": os.environ.get("CODEX_TOKEN_EXPIRES", "0"),
        "account_id": os.environ.get("CODEX_ACCOUNT_ID", ""),
    }
    if not tokens["access_token"] and TOKEN_FILE.exists():
        try:
            stored = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            tokens["access_token"] = stored.get("access_token", "")
            tokens["refresh_token"] = stored.get("refresh_token", "")
            tokens["expires"] = str(stored.get("expires", "0"))
            tokens["account_id"] = stored.get("account_id", tokens["account_id"])
        except Exception as e:
            log.warning("Failed to load codex tokens from file: %s", e)
    return tokens


def _save_tokens(tokens: Dict[str, str]) -> None:
    """Persist tokens to env vars and to disk."""
    os.environ["CODEX_ACCESS_TOKEN"] = tokens["access_token"]
    os.environ["CODEX_REFRESH_TOKEN"] = tokens["refresh_token"]
    os.environ["CODEX_TOKEN_EXPIRES"] = str(tokens["expires"])
    if tokens.get("account_id"):
        os.environ["CODEX_ACCOUNT_ID"] = tokens["account_id"]
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Failed to save codex tokens to file: %s", e)


def refresh_token_if_needed() -> str:
    """Check token expiry and refresh if needed. Returns current access token."""
    tokens = _load_tokens()
    expires = float(tokens.get("expires") or 0)
    now = time.time()

    if tokens["access_token"] and (expires - now) > REFRESH_THRESHOLD_SEC:
        return tokens["access_token"]

    if not tokens["refresh_token"]:
        log.warning("Codex token expired and no refresh token available")
        return tokens["access_token"]

    log.info("Refreshing Codex OAuth token (expires in %.0fs)", max(0, expires - now))
    try:
        body = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": CLIENT_ID,
        }).encode()
        req = urllib.request.Request(
            AUTH_ENDPOINT,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        new_access = data.get("access_token", "")
        new_refresh = data.get("refresh_token", tokens["refresh_token"])
        new_expires = str(int(now + int(data.get("expires_in", 864000))))

        tokens["access_token"] = new_access
        tokens["refresh_token"] = new_refresh
        tokens["expires"] = new_expires
        _save_tokens(tokens)
        log.info("Codex OAuth token refreshed successfully")
        return new_access
    except Exception as e:
        log.error("Failed to refresh Codex token: %s", e)
        return tokens["access_token"]


# ---------------------------------------------------------------------------
# Format converters: Chat Completions <-> Responses API
# ---------------------------------------------------------------------------

def _messages_to_input(
    messages: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Convert OpenAI Chat Completions messages -> Responses API input items.

    Returns (input_items, system_instructions).
    """
    items: List[Dict[str, Any]] = []
    system_parts: List[str] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            if content:
                if isinstance(content, str):
                    system_parts.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("text"):
                            system_parts.append(part["text"])
                        elif isinstance(part, str):
                            system_parts.append(part)
                else:
                    system_parts.append(str(content))
            continue

        if role == "user":
            if isinstance(content, list):
                converted = []
                for part in content:
                    if isinstance(part, dict):
                        p = dict(part)
                        if p.get("type") == "text":
                            p["type"] = "input_text"
                        elif p.get("type") == "image_url":
                            # Flatten nested {"image_url": {"url": "..."}} → {"image_url": "..."}
                            img = p.get("image_url", {})
                            if isinstance(img, dict):
                                p = {"type": "input_image", "image_url": img.get("url", "")}
                            else:
                                p = {"type": "input_image", "image_url": str(img)}
                        converted.append(p)
                    else:
                        converted.append(part)
                items.append({"role": "user", "content": converted})
            elif content:
                items.append({
                    "role": "user",
                    "content": [{"type": "input_text", "text": str(content)}],
                })
            continue

        if role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                if content:
                    text = content if isinstance(content, str) else json.dumps(content)
                    items.append({
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    })
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    items.append({
                        "type": "function_call",
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "{}"),
                        "call_id": tc.get("id", ""),
                    })
            elif content:
                text = content if isinstance(content, str) else json.dumps(content)
                items.append({
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                })
            continue

        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id", ""),
                "output": content if isinstance(content, str) else json.dumps(content),
            })
            continue

    return items, "\n\n".join(system_parts)


def _tools_to_responses_format(
    tools: Optional[List[Dict[str, Any]]],
) -> Optional[List[Dict[str, Any]]]:
    """Convert Chat Completions tools -> Responses API format."""
    if not tools:
        return None
    converted = []
    for t in tools:
        if t.get("type") == "function":
            fn = t.get("function", {})
            converted.append({
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            })
        else:
            converted.append(t)
    return converted


# ---------------------------------------------------------------------------
# Tool-call recovery: detect tool calls embedded as text
# ---------------------------------------------------------------------------

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
    if "tool_uses" in obj and isinstance(obj["tool_uses"], list):
        for item in obj["tool_uses"]:
            if isinstance(item, dict) and "name" in item:
                args = item.get("arguments", item.get("args", {}))
                out.append(_make_tool_call(item["name"], args))
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
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

    log.info(
        "Recovered %d tool call(s) from Codex text content",
        len(extracted),
    )
    return extracted, cleaned


# ---------------------------------------------------------------------------
# Output converter
# ---------------------------------------------------------------------------

def _output_to_chat_message(output: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Convert Responses API output items -> Chat Completions message dict."""
    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []

    for item in output:
        item_type = item.get("type", "")

        if item_type == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    text_parts.append(c.get("text", ""))

        elif item_type == "function_call":
            tool_calls.append({
                "id": item.get("call_id", ""),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "{}"),
                },
            })

    msg: Dict[str, Any] = {"role": "assistant"}
    content_text = "\n".join(str(p) for p in text_parts) if text_parts else None

    # Recovery: if Codex returned tool calls as text instead of function_call items
    if not tool_calls and content_text:
        recovered, cleaned = _try_extract_tool_calls_from_text(content_text)
        if recovered:
            tool_calls = recovered
            content_text = cleaned or None

    msg["content"] = content_text
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------

def _parse_sse_response(raw: str) -> Dict[str, Any]:
    """Parse SSE stream text and extract the response.completed event payload."""
    current_event = ""
    current_data_lines: List[str] = []

    for line in raw.split("\n"):
        stripped = line.strip()

        if stripped == "":
            # Blank line -> dispatch buffered event
            if current_event == "response.completed" and current_data_lines:
                data_str = "\n".join(current_data_lines)
                try:
                    return json.loads(data_str)
                except json.JSONDecodeError:
                    log.warning("Failed to parse response.completed data: %s", data_str[:200])
            current_event = ""
            current_data_lines = []
            continue

        if stripped.startswith("event:"):
            current_event = stripped[6:].strip()
        elif stripped.startswith("data:"):
            current_data_lines.append(stripped[5:].strip())

    # Handle case where stream ends without trailing blank line
    if current_event == "response.completed" and current_data_lines:
        data_str = "\n".join(current_data_lines)
        return json.loads(data_str)

    raise ValueError("No response.completed event found in SSE stream")


# ---------------------------------------------------------------------------
# HTTP request
# ---------------------------------------------------------------------------

def _do_request(access_token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Send POST to Codex endpoint and return parsed response.completed data."""
    body = json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    req = urllib.request.Request(
        CODEX_ENDPOINT, data=body, headers=headers, method="POST",
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC, context=ctx) as resp:
        raw = resp.read().decode("utf-8")

    # Debug: dump raw SSE response
    try:
        Path("/tmp/codex_sse_raw.txt").write_text(raw[:50000], encoding="utf-8")
    except Exception:
        pass

    parsed = _parse_sse_response(raw)

    # Debug: dump parsed response.completed
    try:
        _resp = parsed.get("response", {})
        _output = _resp.get("output", [])
        Path("/tmp/codex_response_debug.json").write_text(json.dumps({
            "output_count": len(_output),
            "output_types": [item.get("type") for item in _output],
            "output_items": _output[:5],
            "usage": _resp.get("usage"),
        }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        pass

    return parsed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def call_codex(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    system_prompt: Optional[str] = None,
    model: str = "gpt-5.3-codex",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Call Codex via ChatGPT OAuth endpoint.

    Args:
        messages: OpenAI Chat Completions format messages.
        tools: OpenAI Chat Completions format tools (optional).
        system_prompt: Override system prompt (if None, extracted from messages).
        model: Codex model name.

    Returns:
        (message_dict, usage_dict) — same contract as LLMClient.chat().
    """
    try:
        input_items, extracted_instructions = _messages_to_input(messages)
    except Exception as e:
        log.warning("Failed to convert messages, filtering images: %s", e)
        # Retry without image content
        filtered = []
        for msg in messages:
            c = msg.get("content", "")
            if isinstance(c, list):
                text_only = [
                    p for p in c
                    if isinstance(p, dict) and p.get("type") in ("text", "input_text")
                ]
                if not text_only:
                    text_only = [
                        {"type": "input_text", "text": p.get("text", "")}
                        for p in c
                        if isinstance(p, dict) and p.get("text")
                    ]
                if text_only:
                    filtered.append({**msg, "content": text_only})
            elif isinstance(c, str):
                filtered.append(msg)
            else:
                filtered.append(msg)
        input_items, extracted_instructions = _messages_to_input(filtered)
    instructions = system_prompt or extracted_instructions

    payload: Dict[str, Any] = {
        "model": model,
        "input": input_items,
        "store": False,
        "stream": True,
    }
    if instructions:
        payload["instructions"] = instructions
    else:
        payload["instructions"] = "You are a helpful assistant."

    # Codex needs explicit encouragement to use tools with large system prompts
    if tools:
        codex_tool_hint = (
            "\n\nIMPORTANT: You have tools available. When the user asks to search, "
            "look up information, read/write files, or perform any action — you MUST "
            "use the appropriate tool. Do NOT answer from memory when a tool call is "
            "more appropriate. Always prefer tool calls over text responses for "
            "actionable requests."
        )
        payload["instructions"] += codex_tool_hint

    payload["reasoning"] = {"effort": "high" if tools else "medium"}

    converted_tools = _tools_to_responses_format(tools)
    if converted_tools:
        payload["tools"] = converted_tools
        payload["tool_choice"] = "auto"

    # Debug: dump full payload before sending
    try:
        _debug_path = Path("/tmp/codex_debug.json")
        _debug_path.write_text(json.dumps({
            "payload": payload,
            "raw_tools_count": len(tools) if tools else 0,
            "converted_tools_count": len(converted_tools) if converted_tools else 0,
            "raw_tools_sample": tools[:2] if tools else [],
            "converted_tools_sample": converted_tools[:2] if converted_tools else [],
        }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        pass

    last_error: Optional[Exception] = None
    event_data: Dict[str, Any] = {}

    for attempt in range(MAX_RETRIES + 1):
        access_token = refresh_token_if_needed()
        if not access_token:
            raise RuntimeError("No Codex access token available")

        try:
            event_data = _do_request(access_token, payload)
            break
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code in (401, 403) and attempt < MAX_RETRIES:
                log.warning(
                    "Codex returned %d, forcing token refresh (attempt %d)",
                    e.code, attempt + 1,
                )
                os.environ["CODEX_TOKEN_EXPIRES"] = "0"
                continue
            body_preview = ""
            try:
                body_preview = e.read().decode(errors="replace")[:500]
            except Exception:
                pass
            log.error("Codex HTTP error %d: %s", e.code, body_preview)
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_error = e
            if attempt < MAX_RETRIES:
                log.warning(
                    "Codex network error, retrying (attempt %d): %s",
                    attempt + 1, e,
                )
                time.sleep(2 ** attempt)
                continue
            raise
        except ValueError as e:
            # SSE parse error
            last_error = e
            if attempt < MAX_RETRIES:
                log.warning(
                    "Codex SSE parse error, retrying (attempt %d): %s",
                    attempt + 1, e,
                )
                continue
            raise
    else:
        raise RuntimeError(
            f"Codex request failed after {MAX_RETRIES + 1} attempts: {last_error}"
        )

    # Extract response from event data
    response = event_data.get("response", {})
    output = response.get("output", [])
    usage_raw = response.get("usage", {})

    msg = _output_to_chat_message(output)

    usage = {
        "prompt_tokens": int(usage_raw.get("input_tokens", 0)),
        "completion_tokens": int(usage_raw.get("output_tokens", 0)),
        "total_tokens": int(usage_raw.get("total_tokens", 0)),
        "cost": 0.0,  # Free via OAuth
    }

    return msg, usage
