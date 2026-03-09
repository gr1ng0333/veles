"""
Ouroboros — Copilot Pro OAuth proxy.

Routes LLM calls through GitHub Copilot API using GitHub PAT tokens.
Standard Chat Completions format — no format conversion, no streaming.
Uses urllib only (no requests dependency).
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

COPILOT_DEFAULT_API_BASE = "https://api.individual.githubcopilot.com"
TIMEOUT_SEC = 180
MAX_RETRIES = 2

from ouroboros import copilot_proxy_accounts as _accounts_impl


# ---------------------------------------------------------------------------
# HTTP request
# ---------------------------------------------------------------------------

def _do_request(copilot_token: str, payload: Dict[str, Any], endpoint: str = "") -> Dict[str, Any]:
    """Send POST to Copilot Chat Completions endpoint and return parsed JSON."""
    url = endpoint or (COPILOT_DEFAULT_API_BASE + "/chat/completions")
    body = json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {copilot_token}",
        "Content-Type": "application/json",
        "User-Agent": "GitHubCopilotChat/0.29.1",
        "Editor-Version": "vscode/1.96.0",
        "Editor-Plugin-Version": "copilot-chat/0.24.0",
        "Copilot-Integration-Id": "vscode-chat",
        "Openai-Organization": "github-copilot",
        "Openai-Intent": "conversation-panel",
    }
    req = urllib.request.Request(
        url, data=body, headers=headers, method="POST",
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC, context=ctx) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Multi-account request with rotation
# ---------------------------------------------------------------------------

def _call_with_rotation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute request with multi-account rotation on errors."""
    tried: set = set()
    last_error: Optional[Exception] = None

    while True:
        result = _accounts_impl._get_active_account()
        if result is None:
            raise RuntimeError(
                f"All Copilot accounts exhausted. Last error: {last_error}"
            )
        acc, idx = result

        if idx in tried:
            raise RuntimeError(f"All Copilot accounts tried. Last error: {last_error}")
        tried.add(idx)

        copilot_token = _accounts_impl._ensure_copilot_token(
            acc, idx, urllib.request.urlopen,
        )
        if not copilot_token:
            _accounts_impl._on_dead_account(idx)
            continue

        api_base = acc.get("copilot_api_base", COPILOT_DEFAULT_API_BASE)
        endpoint = api_base.rstrip("/") + "/chat/completions"

        for attempt in range(MAX_RETRIES + 1):
            try:
                data = _do_request(copilot_token, payload, endpoint=endpoint)
                log.info("Copilot request succeeded via account #%d", idx)
                _accounts_impl._record_successful_request(idx)
                return data
            except urllib.error.HTTPError as e:
                last_error = e
                if e.code == 429:
                    retry_after = 0
                    try:
                        ra = e.headers.get("Retry-After", "")
                        if ra and ra.isdigit():
                            retry_after = int(ra)
                    except Exception:
                        pass
                    _accounts_impl._on_rate_limit(idx, retry_after=retry_after)
                    break  # outer while picks next account
                if e.code in (401, 403):
                    if attempt < MAX_RETRIES:
                        acc["expires_at"] = 0
                        copilot_token = _accounts_impl._ensure_copilot_token(
                            acc, idx, urllib.request.urlopen,
                        )
                        if not copilot_token:
                            _accounts_impl._on_dead_account(idx)
                            break
                        api_base = acc.get("copilot_api_base", COPILOT_DEFAULT_API_BASE)
                        endpoint = api_base.rstrip("/") + "/chat/completions"
                        continue
                    _accounts_impl._on_dead_account(idx)
                    break
                if e.code in (500, 502, 503):
                    if attempt < MAX_RETRIES:
                        log.warning(
                            "[copilot_api_error] Server error %d (account #%d, attempt %d)",
                            e.code, idx, attempt + 1,
                        )
                        time.sleep(2 ** attempt)
                        continue
                    break
                body_preview = ""
                try:
                    body_preview = e.read().decode(errors="replace")[:500]
                except Exception:
                    pass
                log.error("[copilot_api_error] HTTP %d: %s", e.code, body_preview)
                raise
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    log.warning(
                        "[copilot_api_error] Network error (account #%d, attempt %d): %s",
                        idx, attempt + 1, e,
                    )
                    time.sleep(2 ** attempt)
                    continue
                break


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def call_copilot(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    model: str = "claude-sonnet-4-5",
    max_tokens: int = 16384,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Call LLM via GitHub Copilot API.

    Standard Chat Completions format — messages and tools passed as-is.
    Returns (message_dict, usage_dict) — same contract as LLMClient.chat().
    """
    _accounts_impl._init_accounts()

    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    response_data: Dict[str, Any] = {}

    if _accounts_impl._is_multi_account():
        response_data = _call_with_rotation(payload)
    else:
        # Single account path
        result = _accounts_impl._get_active_account()
        if result is None:
            raise RuntimeError("No Copilot account configured")
        acc, idx = result
        last_error: Optional[Exception] = None

        for attempt in range(MAX_RETRIES + 1):
            copilot_token = _accounts_impl._ensure_copilot_token(
                acc, idx, urllib.request.urlopen,
            )
            if not copilot_token:
                raise RuntimeError("No Copilot API token available")

            api_base = acc.get("copilot_api_base", COPILOT_DEFAULT_API_BASE)
            endpoint = api_base.rstrip("/") + "/chat/completions"

            try:
                response_data = _do_request(copilot_token, payload, endpoint=endpoint)
                _accounts_impl._record_successful_request(idx)
                break
            except urllib.error.HTTPError as e:
                last_error = e
                if e.code in (401, 403) and attempt < MAX_RETRIES:
                    log.warning(
                        "[copilot_api_error] HTTP %d, forcing token re-exchange (attempt %d)",
                        e.code, attempt + 1,
                    )
                    acc["expires_at"] = 0
                    continue
                body_preview = ""
                try:
                    body_preview = e.read().decode(errors="replace")[:500]
                except Exception:
                    pass
                log.error("[copilot_api_error] HTTP %d: %s", e.code, body_preview)
                raise
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    log.warning(
                        "[copilot_api_error] Network error (attempt %d): %s",
                        attempt + 1, e,
                    )
                    time.sleep(2 ** attempt)
                    continue
                raise
        else:
            raise RuntimeError(
                f"Copilot request failed after {MAX_RETRIES + 1} attempts: {last_error}"
            )

    # Extract message from standard Chat Completions response
    choices = response_data.get("choices", [{}])
    msg = (choices[0] if choices else {}).get("message", {})
    if not msg.get("role"):
        msg["role"] = "assistant"

    usage_raw = response_data.get("usage", {})
    prompt_tokens = int(usage_raw.get("prompt_tokens", 0))
    completion_tokens = int(usage_raw.get("completion_tokens", 0))

    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cost": 0.0,  # Free via Copilot Pro subscription
    }

    return msg, usage
