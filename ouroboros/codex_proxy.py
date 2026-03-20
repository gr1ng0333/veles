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
import threading
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
ACCOUNTS_STATE_FILE = Path("/opt/veles-data/state/codex_accounts_state.json")
TIMEOUT_SEC = 180
MAX_RETRIES = 2
REFRESH_THRESHOLD_SEC = 3600  # refresh if < 1 hour until expiry
RATE_LIMIT_COOLDOWN_SEC = 600  # 10 minutes default cooldown on 429
RATE_LIMIT_REPEAT_WINDOW = 1800  # 30 min window for repeat 429 detection
RATE_LIMIT_ESCALATED_SEC = 3600  # 1 hour cooldown on repeated 429


# ---------------------------------------------------------------------------
# Helper modules (accounts + format conversion)
# ---------------------------------------------------------------------------

from ouroboros import codex_proxy_accounts as _accounts_impl
from ouroboros.codex_proxy_format import (
    _messages_to_input,
    _output_to_chat_message,
    _tools_to_responses_format,
)


def _load_tokens(prefix: str = "CODEX") -> Dict[str, str]:
    return _accounts_impl._load_tokens(prefix)


def _save_tokens(tokens: Dict[str, str], prefix: str = "CODEX") -> None:
    _accounts_impl._save_tokens(tokens, prefix)


def _do_refresh(refresh_token: str) -> Optional[Dict[str, str]]:
    return _accounts_impl._do_refresh(refresh_token, AUTH_ENDPOINT, urllib.request.urlopen)


def refresh_token_if_needed(prefix: str = "CODEX") -> str:
    return _accounts_impl.refresh_token_if_needed(AUTH_ENDPOINT, urllib.request.urlopen, prefix)


def _tolerant_json_loads(raw: str) -> Any:
    return _accounts_impl._tolerant_json_loads(raw)


def _load_accounts() -> List[Dict[str, Any]]:
    return _accounts_impl._load_accounts()


def _save_accounts_state(accounts: List[Dict[str, Any]]) -> None:
    _accounts_impl._save_accounts_state(accounts)


def _init_accounts(force: bool = False) -> None:
    _accounts_impl._init_accounts(force)


def _get_active_account() -> Optional[Tuple[Dict[str, Any], int]]:
    return _accounts_impl._get_active_account()


def _on_rate_limit(account_idx: int, retry_after: int = 0) -> None:
    _accounts_impl._on_rate_limit(account_idx, retry_after)


def _on_dead_account(account_idx: int) -> None:
    _accounts_impl._on_dead_account(account_idx)


def _refresh_account(acc: Dict[str, Any], account_idx: int) -> str:
    return _accounts_impl._refresh_account(acc, account_idx, AUTH_ENDPOINT, urllib.request.urlopen)


def _is_multi_account() -> bool:
    return _accounts_impl._is_multi_account()


def get_account_usage(acc: Dict[str, Any]) -> Dict[str, int]:
    return _accounts_impl.get_account_usage(acc)


def _record_successful_request(account_idx: int) -> None:
    _accounts_impl._record_successful_request(account_idx)


def _clear_last_error(account_idx: int) -> None:
    _accounts_impl._clear_last_error(account_idx)


def _update_account_quota(account_idx: int, quota: Dict[str, Any]) -> None:
    _accounts_impl._update_account_quota(account_idx, quota)


def force_switch_account(target_idx: int = -1) -> Dict[str, Any]:
    return _accounts_impl.force_switch_account(target_idx)


def bootstrap_refresh_missing_access_tokens() -> Dict[str, Any]:
    return _accounts_impl.bootstrap_refresh_missing_access_tokens(AUTH_ENDPOINT, urllib.request.urlopen)


def get_accounts_status() -> List[Dict[str, Any]]:
    return _accounts_impl.get_accounts_status()


def refresh_all_quotas() -> Dict[int, Optional[Dict[str, Any]]]:
    return _accounts_impl.refresh_all_quotas()


# Tool-call recovery moved to ouroboros/codex_recovery.py
from ouroboros.codex_recovery import _try_extract_tool_calls_from_text  # noqa: F401


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
    _recovery_enabled = os.environ.get("CODEX_TOOL_RECOVERY_ENABLED", "false").lower() in ("1", "true", "yes")
    if _recovery_enabled and not tool_calls and content_text:
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

def _extract_codex_quota(resp_headers: dict) -> Dict[str, Any]:
    """Extract x-codex-* quota headers into a dict."""
    quota: Dict[str, Any] = {}
    for k, v in resp_headers.items():
        kl = k.lower()
        if not kl.startswith("x-codex-"):
            continue
        key = kl[len("x-codex-"):].replace("-", "_")  # e.g. primary_used_percent
        # Coerce numeric values
        if v.isdigit():
            quota[key] = int(v)
        else:
            try:
                quota[key] = float(v)
            except (ValueError, TypeError):
                quota[key] = v
    return quota


def _do_request(access_token: str, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Send POST to Codex endpoint.

    Returns (parsed_response_completed_data, codex_quota_headers).
    """
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
        resp_headers = dict(resp.headers)
        raw = resp.read().decode("utf-8")

    # Extract quota from response headers
    quota = _extract_codex_quota(resp_headers)

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

    return parsed, quota


# ---------------------------------------------------------------------------
# Multi-account request with rotation
# ---------------------------------------------------------------------------

def _call_with_rotation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute Codex request with multi-account rotation on errors.

    Tries the active account first. On 429 → cooldown + rotate.
    On 401/403 after failed refresh → mark dead + rotate.
    """
    tried: set = set()
    last_error: Optional[Exception] = None

    while True:
        result = _get_active_account()
        if result is None:
            raise RuntimeError(
                "All Codex accounts exhausted (dead or on cooldown). "
                f"Last error: {last_error}"
            )
        acc, idx = result

        if idx in tried:
            # We've cycled through all accounts
            raise RuntimeError(
                f"All Codex accounts tried. Last error: {last_error}"
            )
        tried.add(idx)

        # Refresh token for this account
        access_token = _refresh_account(acc, idx)
        if not access_token:
            _on_dead_account(idx)
            continue

        for attempt in range(MAX_RETRIES + 1):
            try:
                result, quota = _do_request(access_token, payload)
                log.info("Codex request succeeded via account #%d", idx)
                _record_successful_request(idx)
                _clear_last_error(idx)
                _update_account_quota(idx, quota)
                return result
            except urllib.error.HTTPError as e:
                body_preview = ""
                try:
                    body_preview = e.read().decode(errors="replace")
                except Exception:
                    pass
                diagnostic = classify_codex_http_failure(
                    e.code,
                    dict(getattr(e, "headers", {}) or {}),
                    body_preview,
                )
                last_error = RuntimeError(f"codex_http_failure account=#{idx} diagnostic={diagnostic}")
                _set_last_error(idx, diagnostic)
                if diagnostic["category"] == "rate_limit":
                    _on_rate_limit(
                        idx,
                        retry_after=diagnostic.get("retry_after", 0),
                        reason=diagnostic.get("reason", "rate_limited"),
                    )
                    quota = {
                        k: diagnostic[k]
                        for k in ("primary_used_percent", "secondary_used_percent")
                        if diagnostic.get(k) is not None
                    }
                    if quota:
                        _update_account_quota(idx, quota)
                    break  # break retry loop → outer while picks next account
                if diagnostic["category"] == "auth":
                    if attempt < MAX_RETRIES:
                        # Force refresh and retry only for real auth failures
                        acc["expires"] = 0
                        access_token = _refresh_account(acc, idx)
                        if not access_token:
                            _on_dead_account(idx)
                            break
                        continue
                    # All retries exhausted for this account
                    _on_dead_account(idx)
                    break
                if e.code in (500, 502, 503):
                    if attempt < MAX_RETRIES:
                        log.warning(
                            "Codex server error %d (account #%d, attempt %d), retrying",
                            e.code, idx, attempt + 1,
                        )
                        time.sleep(2 ** attempt)
                        continue
                    # All retries exhausted — rotate to next account
                    break
                log.error("Codex HTTP error account #%d: %s", idx, diagnostic)
                raise
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    log.warning(
                        "Codex network error (account #%d, attempt %d): %s",
                        idx, attempt + 1, e,
                    )
                    time.sleep(2 ** attempt)
                    continue
                # Network error on all retries — try next account
                break
            except ValueError as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    log.warning(
                        "Codex SSE parse error (account #%d, attempt %d): %s",
                        idx, attempt + 1, e,
                    )
                    continue
                break
        else:
            # retry loop completed without break → success already returned
            pass  # pragma: no cover


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def call_codex(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    system_prompt: Optional[str] = None,
    model: str = "gpt-5.3-codex",
    token_prefix: str = "CODEX",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Call Codex via ChatGPT OAuth endpoint.

    Args:
        messages: OpenAI Chat Completions format messages.
        tools: OpenAI Chat Completions format tools (optional).
        system_prompt: Override system prompt (if None, extracted from messages).
        model: Codex model name.
        token_prefix: Env var prefix for tokens ("CODEX" or "CODEX_CONSCIOUSNESS").

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

    # Codex tool hint — disabled by default; was causing model to force tool calls
    # even when it already had the answer, contributing to infinite loops.
    # Enable with CODEX_TOOL_HINT_ENABLED=true if Codex stops using tools.
    if tools and os.environ.get("CODEX_TOOL_HINT_ENABLED", "false").lower() in ("1", "true", "yes"):
        codex_tool_hint = (
            "\n\nIMPORTANT: You have tools available. When the user asks to search, "
            "look up information, read/write files, or perform any action — you MUST "
            "use the appropriate tool. Do NOT answer from memory when a tool call is "
            "more appropriate. Always prefer tool calls over text responses for "
            "actionable requests."
        )
        payload["instructions"] += codex_tool_hint

    payload["reasoning"] = {"effort": "medium"}

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

    # Multi-account rotation only for main CODEX prefix;
    # consciousness and other prefixes always use single-account path.
    use_rotation = _is_multi_account() and token_prefix == "CODEX"

    if use_rotation:
        event_data = _call_with_rotation(payload)
    else:
        _last_single_quota: Dict[str, Any] = {}
        # Determine correct env var for expires based on prefix
        expires_env_key = (
            "CODEX_TOKEN_EXPIRES" if token_prefix == "CODEX"
            else f"{token_prefix}_EXPIRES"
        )
        is_consciousness = token_prefix != "CODEX"

        for attempt in range(MAX_RETRIES + 1):
            access_token = refresh_token_if_needed(token_prefix)
            if not access_token:
                raise RuntimeError(
                    f"No Codex access token available (prefix={token_prefix})"
                )

            try:
                event_data, _last_single_quota = _do_request(access_token, payload)
                break
            except urllib.error.HTTPError as e:
                last_error = e
                if e.code in (401, 403) and attempt < MAX_RETRIES:
                    log.warning(
                        "Codex returned %d, forcing token refresh (prefix=%s, attempt %d)",
                        e.code, token_prefix, attempt + 1,
                    )
                    os.environ[expires_env_key] = "0"
                    continue
                body_preview = ""
                try:
                    body_preview = e.read().decode(errors="replace")[:500]
                except Exception:
                    pass
                if is_consciousness:
                    log.error(
                        "consciousness_codex_http_error code=%d body=%s",
                        e.code, body_preview,
                    )
                else:
                    log.error("Codex HTTP error %d: %s", e.code, body_preview)
                raise
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    log.warning(
                        "Codex network error, retrying (prefix=%s, attempt %d): %s",
                        token_prefix, attempt + 1, e,
                    )
                    time.sleep(2 ** attempt)
                    continue
                raise
            except ValueError as e:
                # SSE parse error
                last_error = e
                if attempt < MAX_RETRIES:
                    log.warning(
                        "Codex SSE parse error, retrying (prefix=%s, attempt %d): %s",
                        token_prefix, attempt + 1, e,
                    )
                    continue
                raise
        else:
            raise RuntimeError(
                f"Codex request failed after {MAX_RETRIES + 1} attempts "
                f"(prefix={token_prefix}): {last_error}"
            )

    # Extract response from event data
    response = event_data.get("response", {})
    output = response.get("output", [])
    usage_raw = response.get("usage", {})

    msg = _output_to_chat_message(output)

    prompt_tokens = int(usage_raw.get("input_tokens", 0))
    completion_tokens = int(usage_raw.get("output_tokens", 0))
    cached_tokens = int(usage_raw.get("cached_tokens", 0))

    # Shadow cost — what this would cost at GPT-5.3 Codex API prices
    # Non-cached input tokens charged at full price, cached at discount
    non_cached_input = max(0, prompt_tokens - cached_tokens)
    shadow_cost = (
        (non_cached_input / 1_000_000) * 1.75
        + (cached_tokens / 1_000_000) * 0.175
        + (completion_tokens / 1_000_000) * 14.00
    )

    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "total_tokens": int(usage_raw.get("total_tokens", 0)),
        "cost": 0.0,  # Free via OAuth
        "shadow_cost": round(shadow_cost, 6),
    }

    return msg, usage
