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
TIMEOUT_SEC = 120
MAX_RETRIES = 2
REFRESH_THRESHOLD_SEC = 3600  # refresh if < 1 hour until expiry
RATE_LIMIT_COOLDOWN_SEC = 600  # 10 minutes cooldown on 429


# ---------------------------------------------------------------------------
# Token management (single-account, backward-compatible)
# ---------------------------------------------------------------------------

def _load_tokens(prefix: str = "CODEX") -> Dict[str, str]:
    """Load tokens from env vars, falling back to token file.

    Args:
        prefix: Environment variable prefix (e.g. "CODEX" or "CODEX_CONSCIOUSNESS").
    """
    # Map env var names based on prefix
    if prefix == "CODEX":
        # Backward-compatible: original env var names
        access_key, refresh_key, expires_key, account_key = (
            "CODEX_ACCESS_TOKEN", "CODEX_REFRESH_TOKEN",
            "CODEX_TOKEN_EXPIRES", "CODEX_ACCOUNT_ID",
        )
    else:
        access_key = f"{prefix}_ACCESS"
        refresh_key = f"{prefix}_REFRESH"
        expires_key = f"{prefix}_EXPIRES"
        account_key = f"{prefix}_ACCOUNT_ID"

    tokens = {
        "access_token": os.environ.get(access_key, ""),
        "refresh_token": os.environ.get(refresh_key, ""),
        "expires": os.environ.get(expires_key, "0"),
        "account_id": os.environ.get(account_key, ""),
    }
    if prefix == "CODEX" and not tokens["access_token"] and TOKEN_FILE.exists():
        try:
            stored = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            tokens["access_token"] = stored.get("access_token", "")
            tokens["refresh_token"] = stored.get("refresh_token", "")
            tokens["expires"] = str(stored.get("expires", "0"))
            tokens["account_id"] = stored.get("account_id", tokens["account_id"])
        except Exception as e:
            log.warning("Failed to load codex tokens from file: %s", e)
    return tokens


def _save_tokens(tokens: Dict[str, str], prefix: str = "CODEX") -> None:
    """Persist tokens to env vars and to disk."""
    if prefix == "CODEX":
        access_key, refresh_key, expires_key, account_key = (
            "CODEX_ACCESS_TOKEN", "CODEX_REFRESH_TOKEN",
            "CODEX_TOKEN_EXPIRES", "CODEX_ACCOUNT_ID",
        )
    else:
        access_key = f"{prefix}_ACCESS"
        refresh_key = f"{prefix}_REFRESH"
        expires_key = f"{prefix}_EXPIRES"
        account_key = f"{prefix}_ACCOUNT_ID"

    os.environ[access_key] = tokens["access_token"]
    os.environ[refresh_key] = tokens["refresh_token"]
    os.environ[expires_key] = str(tokens["expires"])
    if tokens.get("account_id"):
        os.environ[account_key] = tokens["account_id"]
    # Only persist to disk for the default CODEX prefix
    if prefix == "CODEX":
        try:
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_FILE.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
        except Exception as e:
            log.warning("Failed to save codex tokens to file: %s", e)


def _do_refresh(refresh_token: str) -> Optional[Dict[str, str]]:
    """Execute OAuth refresh and return new tokens dict, or None on failure."""
    now = time.time()
    try:
        body = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
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
        return {
            "access_token": data.get("access_token", ""),
            "refresh_token": data.get("refresh_token", refresh_token),
            "expires": str(int(now + int(data.get("expires_in", 864000)))),
        }
    except Exception as e:
        log.error("OAuth refresh failed: %s", e)
        return None


def refresh_token_if_needed(prefix: str = "CODEX") -> str:
    """Check token expiry and refresh if needed. Returns current access token."""
    tokens = _load_tokens(prefix)
    expires = float(tokens.get("expires") or 0)
    now = time.time()

    if tokens["access_token"] and (expires - now) > REFRESH_THRESHOLD_SEC:
        return tokens["access_token"]

    if not tokens["refresh_token"]:
        log.warning("Codex token expired and no refresh token available (prefix=%s)", prefix)
        return tokens["access_token"]

    log.info("Refreshing Codex OAuth token (prefix=%s, expires in %.0fs)", prefix, max(0, expires - now))
    result = _do_refresh(tokens["refresh_token"])
    if result:
        tokens.update(result)
        _save_tokens(tokens, prefix)
        log.info("Codex OAuth token refreshed successfully (prefix=%s)", prefix)
        return tokens["access_token"]
    return tokens["access_token"]


# ---------------------------------------------------------------------------
# Multi-account rotation
# ---------------------------------------------------------------------------

_accounts_lock = threading.Lock()
_accounts: List[Dict[str, Any]] = []  # loaded account list
_active_idx: int = 0  # index of current account


def _tolerant_json_loads(raw: str) -> Any:
    """Parse JSON with tolerance for common .env / shell mangling.

    Handles: single quotes, outer quoting, trailing commas, BOM,
    backslash-escaped double quotes, bare (unquoted) keys.
    """
    s = raw.strip()
    # Strip BOM
    if s.startswith("\ufeff"):
        s = s[1:]
    # Strip outer single or double quotes added by shell / .env parsers
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        inner = s[1:-1]
        # Unescape backslash-escaped quotes: \" → "
        if s[0] == '"':
            inner = inner.replace('\\"', '"')
        # Only strip if the inner part looks like a JSON array/object
        if inner.lstrip().startswith(("[", "{")):
            s = inner
    # Try standard parse first
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Fix single quotes → double quotes (Python dict style)
    fixed = s.replace("'", '"')
    # Quote bare keys: {refresh: → {"refresh":
    fixed = re.sub(r'(?<=[{,])\s*([A-Za-z_]\w*)\s*:', r' "\1":', fixed)
    # Remove trailing commas before } or ]
    fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
    return json.loads(fixed)


def _load_accounts() -> List[Dict[str, Any]]:
    """Load accounts from CODEX_ACCOUNTS env var or state file.

    Returns list of account dicts with keys:
      access, refresh, expires, cooldown_until, dead
    """
    raw = os.environ.get("CODEX_ACCOUNTS", "")
    accounts: List[Dict[str, Any]] = []
    if raw:
        try:
            parsed = _tolerant_json_loads(raw)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and item.get("refresh"):
                        accounts.append({
                            "access": item.get("access", ""),
                            "refresh": item["refresh"],
                            "expires": float(item.get("expires", 0)),
                            "cooldown_until": 0.0,
                            "dead": False,
                        })
            if accounts:
                log.info("Loaded %d Codex accounts from CODEX_ACCOUNTS", len(accounts))
            else:
                log.warning("CODEX_ACCOUNTS parsed but contained 0 valid accounts (need 'refresh' key)")
        except (json.JSONDecodeError, ValueError) as e:
            log.error("Failed to parse CODEX_ACCOUNTS: %s  |  raw[:200]=%s", e, raw[:200])

    # Merge persisted state (cooldowns, updated tokens)
    if ACCOUNTS_STATE_FILE.exists():
        try:
            state = json.loads(ACCOUNTS_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(state, list):
                for i, s in enumerate(state):
                    if i < len(accounts) and isinstance(s, dict):
                        # Restore runtime fields from state
                        if s.get("access"):
                            accounts[i]["access"] = s["access"]
                        if s.get("refresh"):
                            accounts[i]["refresh"] = s["refresh"]
                        if s.get("expires"):
                            accounts[i]["expires"] = float(s["expires"])
                        accounts[i]["cooldown_until"] = float(s.get("cooldown_until", 0))
                        accounts[i]["dead"] = bool(s.get("dead", False))
        except Exception as e:
            log.warning("Failed to load accounts state: %s", e)

    return accounts


def _save_accounts_state(accounts: List[Dict[str, Any]]) -> None:
    """Persist account state (tokens, cooldowns) to disk."""
    try:
        ACCOUNTS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        serializable = []
        for acc in accounts:
            serializable.append({
                "access": acc.get("access", ""),
                "refresh": acc.get("refresh", ""),
                "expires": acc.get("expires", 0),
                "cooldown_until": acc.get("cooldown_until", 0),
                "dead": acc.get("dead", False),
            })
        ACCOUNTS_STATE_FILE.write_text(
            json.dumps(serializable, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.warning("Failed to save accounts state: %s", e)


def _init_accounts() -> None:
    """Initialize account list once (idempotent)."""
    global _accounts, _active_idx
    if _accounts:
        return
    _accounts = _load_accounts()
    if _accounts:
        # Pick first non-dead, non-cooldown account
        now = time.time()
        for i, acc in enumerate(_accounts):
            if not acc["dead"] and acc["cooldown_until"] < now:
                _active_idx = i
                break
        log.info(
            "Codex multi-account: %d accounts loaded, active=#%d",
            len(_accounts), _active_idx,
        )


def _get_active_account() -> Optional[Dict[str, Any]]:
    """Return the active account dict, or None if all exhausted."""
    global _active_idx
    with _accounts_lock:
        _init_accounts()
        if not _accounts:
            return None
        now = time.time()
        # Try current first
        acc = _accounts[_active_idx]
        if not acc["dead"] and acc["cooldown_until"] < now:
            return acc
        # Scan for a usable one
        for i in range(len(_accounts)):
            idx = (_active_idx + 1 + i) % len(_accounts)
            acc = _accounts[idx]
            if not acc["dead"] and acc["cooldown_until"] < now:
                _active_idx = idx
                log.info("Codex account rotation: switched to #%d", idx)
                return acc
        log.error("All Codex accounts exhausted (dead or on cooldown)")
        return None


def _on_rate_limit(account_idx: int) -> None:
    """Put account on cooldown after HTTP 429."""
    with _accounts_lock:
        if account_idx < len(_accounts):
            _accounts[account_idx]["cooldown_until"] = time.time() + RATE_LIMIT_COOLDOWN_SEC
            log.warning(
                "Codex account #%d rate-limited, cooldown %ds",
                account_idx, RATE_LIMIT_COOLDOWN_SEC,
            )
            _save_accounts_state(_accounts)


def _on_dead_account(account_idx: int) -> None:
    """Mark account as dead after unrecoverable auth failure."""
    with _accounts_lock:
        if account_idx < len(_accounts):
            _accounts[account_idx]["dead"] = True
            log.error("Codex account #%d marked dead", account_idx)
            _save_accounts_state(_accounts)


def _refresh_account(acc: Dict[str, Any], account_idx: int) -> str:
    """Refresh a specific account's token. Returns access token."""
    now = time.time()
    if acc["access"] and (acc["expires"] - now) > REFRESH_THRESHOLD_SEC:
        return acc["access"]

    if not acc["refresh"]:
        log.warning("Account #%d: no refresh token", account_idx)
        return acc["access"]

    log.info("Refreshing account #%d token (expires in %.0fs)", account_idx, max(0, acc["expires"] - now))
    result = _do_refresh(acc["refresh"])
    if result:
        with _accounts_lock:
            acc["access"] = result["access_token"]
            acc["refresh"] = result["refresh_token"]
            acc["expires"] = float(result["expires"])
            _save_accounts_state(_accounts)
        log.info("Account #%d token refreshed", account_idx)
        return acc["access"]
    return acc["access"]


def _is_multi_account() -> bool:
    """Check if multi-account rotation is configured."""
    with _accounts_lock:
        _init_accounts()
        return len(_accounts) > 0


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
        acc = _get_active_account()
        if acc is None:
            raise RuntimeError(
                "All Codex accounts exhausted (dead or on cooldown). "
                f"Last error: {last_error}"
            )

        with _accounts_lock:
            idx = _active_idx

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
                result = _do_request(access_token, payload)
                log.info("Codex request succeeded via account #%d", idx)
                return result
            except urllib.error.HTTPError as e:
                last_error = e
                if e.code == 429:
                    _on_rate_limit(idx)
                    break  # break retry loop → outer while picks next account
                if e.code in (401, 403):
                    if attempt < MAX_RETRIES:
                        # Force refresh and retry
                        acc["expires"] = 0
                        access_token = _refresh_account(acc, idx)
                        if not access_token:
                            _on_dead_account(idx)
                            break
                        continue
                    # All retries exhausted for this account
                    _on_dead_account(idx)
                    break
                # Other HTTP errors — don't rotate, raise immediately
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

    # Multi-account rotation or single-account fallback
    # Multi-account rotation only used for default CODEX prefix
    if token_prefix == "CODEX" and _is_multi_account():
        event_data = _call_with_rotation(payload)
    else:
        for attempt in range(MAX_RETRIES + 1):
            access_token = refresh_token_if_needed(token_prefix)
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
