"""
Ouroboros — Copilot Pro OAuth proxy.

Routes LLM calls through GitHub Copilot API using GitHub PAT tokens.
Standard Chat Completions format — no format conversion, no streaming.
Uses urllib only (no requests dependency).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

COPILOT_DEFAULT_API_BASE = "https://api.individual.githubcopilot.com"
TIMEOUT_SEC = 180
MAX_RETRIES = 2

from ouroboros import copilot_proxy_accounts as _accounts_impl


# ---------------------------------------------------------------------------
# Copilot session tracking — per interaction_id stats
# ---------------------------------------------------------------------------

_session_lock = threading.Lock()
_active_sessions: Dict[str, Dict[str, Any]] = {}  # interaction_id → stats

# Diagnostic: track first payload per interaction for billing investigation
_diag_lock = threading.Lock()
_diag_seen_interactions: set = set()


_SERVER_ERROR_COOLDOWN_SEC = 60


class CopilotServerCooldownError(RuntimeError):
    def __init__(
        self,
        *,
        account_idx: int,
        status_code: int,
        cooldown_sec: int,
        interaction_id: Optional[str],
        body_preview: str = "",
    ) -> None:
        self.account_idx = account_idx
        self.status_code = status_code
        self.cooldown_sec = cooldown_sec
        self.interaction_id = interaction_id
        self.body_preview = body_preview
        super().__init__(
            f"Copilot HTTP {status_code} on account #{account_idx}; cooldown {cooldown_sec}s; "
            f"interaction={(interaction_id or '?')[:8]}"
        )


def _track_session(interaction_id: Optional[str], usage: Dict[str, Any], initiator: str) -> Dict[str, Any]:
    """Track cumulative stats for a Copilot agentic session. Returns session stats."""
    if not interaction_id:
        return {}
    with _session_lock:
        if interaction_id not in _active_sessions:
            _active_sessions[interaction_id] = {
                "started": time.time(),
                "rounds": 0,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "premium_requests": 0,  # should be 1 for entire session
            }
        s = _active_sessions[interaction_id]
        s["rounds"] += 1
        s["total_prompt_tokens"] += usage.get("prompt_tokens", 0)
        s["total_completion_tokens"] += usage.get("completion_tokens", 0)
        if initiator == "user":
            s["premium_requests"] += 1
        s["last_activity"] = time.time()
        return dict(s)


def get_session_stats(interaction_id: str) -> Optional[Dict[str, Any]]:
    """Get stats for a Copilot session. Returns None if not tracked."""
    with _session_lock:
        return dict(_active_sessions[interaction_id]) if interaction_id in _active_sessions else None


def cleanup_stale_sessions(max_age_seconds: int = 3600) -> int:
    """Remove sessions older than max_age_seconds. Returns count of removed sessions."""
    now = time.time()
    removed = 0
    with _session_lock:
        stale = [k for k, v in _active_sessions.items() if now - v.get("last_activity", 0) > max_age_seconds]
        for k in stale:
            del _active_sessions[k]
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# HTTP request
# ---------------------------------------------------------------------------

def _do_request(
    copilot_token: str,
    payload: Dict[str, Any],
    endpoint: str = "",
    initiator: str = "user",
    interaction_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Send POST to Copilot Chat Completions endpoint and return parsed JSON."""
    url = endpoint or (COPILOT_DEFAULT_API_BASE + "/chat/completions")
    body = json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {copilot_token}",
        "Content-Type": "application/json",
        "User-Agent": "GitHubCopilotChat/0.43.0",
        "Editor-Version": "vscode/1.115.0",
        "Editor-Plugin-Version": "copilot-chat/0.43.0",
        "Copilot-Integration-Id": "vscode-chat",
        "Openai-Organization": "github-copilot",
        "Openai-Intent": "conversation-agent",
        "X-Initiator": initiator,
        "X-Interaction-Type": "conversation-agent",
        "X-Request-Id": str(uuid.uuid4()),
    }
    if interaction_id:
        headers["X-Interaction-Id"] = interaction_id
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

def _call_with_rotation(
    payload: Dict[str, Any],
    initiator: str = "user",
    interaction_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], int]:
    """Execute request with multi-account rotation on errors.
    Returns (response_data, account_idx) tuple.
    """
    tried: set = set()
    last_error: Optional[Exception] = None
    _exhaustion_retried = False

    while True:
        result = _accounts_impl._get_active_account()

        if result is None or (result is not None and result[1] in tried):
            # All accounts exhausted or already tried — wait for shortest cooldown
            if _exhaustion_retried:
                raise RuntimeError(
                    f"All Copilot accounts exhausted after retry. Last error: {last_error}"
                )
            wait_time = _accounts_impl._shortest_cooldown_remaining()
            if wait_time <= 0:
                wait_time = _accounts_impl._apply_soft_cooldown(30)
                if wait_time <= 0:
                    raise RuntimeError(
                        f"All Copilot accounts exhausted (no cooldown to wait for). Last error: {last_error}"
                    )
                log.warning(
                    "copilot_all_accounts_exhausted_soft_cooldown waiting=%ds interaction=%s",
                    int(wait_time), (interaction_id or "?")[:8],
                )
            wait_time = min(wait_time, 120)  # cap at 2 minutes
            log.warning(
                "copilot_all_accounts_exhausted waiting=%ds interaction=%s",
                wait_time, (interaction_id or "?")[:8],
            )
            time.sleep(wait_time)
            tried.clear()
            _exhaustion_retried = True
            continue

        acc, idx = result
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
                data = _do_request(
                    copilot_token, payload, endpoint=endpoint,
                    initiator=initiator, interaction_id=interaction_id,
                )
                log.info("Copilot request succeeded via account #%d", idx)
                _accounts_impl._record_successful_request(idx)
                return data, idx
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
                    body_preview = ""
                    try:
                        body_preview = e.read().decode(errors="replace")[:500]
                    except Exception:
                        pass
                    _accounts_impl._on_server_error_cooldown(idx, _SERVER_ERROR_COOLDOWN_SEC)
                    log.warning(
                        "[copilot_api_error] HTTP %d (account #%d): server cooldown %ds, interaction=%s, body=%s",
                        e.code, idx, _SERVER_ERROR_COOLDOWN_SEC, (interaction_id or "?")[:8], body_preview,
                    )
                    raise CopilotServerCooldownError(
                        account_idx=idx,
                        status_code=e.code,
                        cooldown_sec=_SERVER_ERROR_COOLDOWN_SEC,
                        interaction_id=interaction_id,
                        body_preview=body_preview,
                    )
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
# Helpers extracted from call_copilot
# ---------------------------------------------------------------------------

def _dump_diag_payload(
    payload: Dict[str, Any],
    interaction_id: str,
    initiator: str,
    model: str,
) -> None:
    """Write first-request diagnostic payload to copilot_diag.jsonl (once per interaction)."""
    if interaction_id in _diag_seen_interactions:
        return
    with _diag_lock:
        if interaction_id in _diag_seen_interactions:
            return
        _diag_seen_interactions.add(interaction_id)

    try:
        import pathlib

        diag_payload = copy.deepcopy(payload)
        diag_messages = diag_payload.get("messages", [])
        for i, m in enumerate(diag_messages):
            content = m.get("content", "")
            if isinstance(content, list):
                parts_info = [
                    f"part[{j}]: len={len(p.get('text',''))}, cache_control={'cache_control' in p}"
                    for j, p in enumerate(content)
                    if isinstance(p, dict)
                ]
                m["_diag_content"] = f"MULTIPART ({len(content)} parts): {parts_info}"
                m["content"] = f"<multipart {len(content)} parts>"
            elif isinstance(content, str):
                m["_diag_content"] = f"STRING len={len(content)}, preview={content[:200]}"
                m["content"] = f"<string len={len(content)}>"
            if m.get("tool_calls"):
                m["_diag_tool_calls"] = f"{len(m['tool_calls'])} tool_calls"

        diag_tools = diag_payload.get("tools", [])
        import datetime as _dt
        diag_data = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "type": "copilot_payload_diag",
            "interaction_id": interaction_id[:8],
            "initiator": initiator,
            "model": model,
            "message_count": len(diag_messages),
            "message_roles": [m.get("role", "?") for m in diag_messages],
            "stream": diag_payload.get("stream"),
            "max_tokens": diag_payload.get("max_tokens"),
            "tool_choice": diag_payload.get("tool_choice"),
            "reasoning_effort": diag_payload.get("reasoning_effort"),
            "tools_count": len(diag_tools),
            "tools_sample": [t.get("function", {}).get("name", "?") for t in diag_tools[:5]],
            "messages_detail": [
                {
                    "idx": i,
                    "role": m.get("role"),
                    "content_info": m.get("_diag_content", "?"),
                    "has_tool_calls": bool(m.get("tool_calls")),
                }
                for i, m in enumerate(diag_messages)
            ],
        }
        diag_path = pathlib.Path("/opt/veles-data/logs/copilot_diag.jsonl")
        diag_path.parent.mkdir(parents=True, exist_ok=True)
        with open(diag_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(diag_data, ensure_ascii=False) + "\n")
        log.info(
            "copilot_payload_diag interaction=%s roles=%s tools=%d initiator=%s",
            interaction_id[:8],
            [m.get("role") for m in diag_messages],
            len(diag_tools),
            initiator,
        )
    except Exception as e:
        log.warning("copilot_diag_error: %s", e)


def _merge_choices(response_data: Dict[str, Any]) -> Dict[str, Any]:
    """Merge multiple choices from Copilot response into one assistant message.

    Claude Sonnet may split content and each tool_call into separate choices.
    This merges them so callers see all tool_calls together plus any text content.
    """
    choices = response_data.get("choices", [{}])
    msg: Dict[str, Any] = {}
    merged_tool_calls: List[Dict[str, Any]] = []
    for ch in choices:
        part = ch.get("message", {})
        if part.get("tool_calls"):
            merged_tool_calls.extend(part["tool_calls"])
            msg.setdefault("role", part.get("role", "assistant"))
        if part.get("content"):
            msg.setdefault("role", part.get("role", "assistant"))
            msg.setdefault("content", part["content"])
    if merged_tool_calls:
        msg["tool_calls"] = merged_tool_calls
    if not msg:
        msg = (choices[0] if choices else {}).get("message", {})
    if not msg.get("role"):
        msg["role"] = "assistant"
    return msg


def _execute_single_account(
    payload: Dict[str, Any],
    initiator: str,
    interaction_id: Optional[str],
) -> Tuple[Dict[str, Any], int]:
    """Execute request via single-account path (no rotation). Returns (response_data, account_idx)."""
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
            response_data = _do_request(
                copilot_token, payload, endpoint=endpoint,
                initiator=initiator, interaction_id=interaction_id,
            )
            _accounts_impl._record_successful_request(idx)
            return response_data, idx
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
    raise RuntimeError(
        f"Copilot request failed after {MAX_RETRIES + 1} attempts: {last_error}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def call_copilot(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    model: str = "claude-sonnet-4-5",
    max_tokens: int = 16384,
    tool_choice: Optional[Any] = None,
    interaction_id: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    force_user_initiator: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Call LLM via GitHub Copilot API.

    Standard Chat Completions format — messages and tools passed as-is.
    Returns (message_dict, usage_dict) — same contract as LLMClient.chat().
    """
    _accounts_impl._init_accounts()

    # Opus: flatten multipart system messages to plain string.
    # Copilot API silently drops multipart content for Opus (prompt_tokens → 13).
    # Sonnet handles multipart correctly and benefits from prefix caching, so keep as-is.
    if "opus" in model.lower():
        for msg in messages:
            if msg.get("role") == "system" and isinstance(msg.get("content"), list):
                parts = [b["text"] for b in msg["content"] if isinstance(b, dict) and b.get("text")]
                msg["content"] = "\n\n".join(parts)

    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
        "reasoning_effort": reasoning_effort or "high",
    }
    if tools:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    elif tools:
        payload["tool_choice"] = "auto"

    # Determine initiator from last message role
    last_role = messages[-1].get("role", "user") if messages else "user"
    initiator = "user" if last_role == "user" else "agent"
    if force_user_initiator:
        initiator = "user"
    log.debug(
        "copilot_request model=%s initiator=%s interaction_id=%s round_tokens=%d",
        model, initiator, interaction_id or "none", sum(len(json.dumps(m)) for m in messages) // 4,
    )

    # Dump first-request diagnostic payload (once per interaction)
    if interaction_id:
        _dump_diag_payload(payload, interaction_id, initiator, model)

    # Warn if context is getting large for Copilot
    approx_context_chars = sum(len(json.dumps(m)) for m in messages)
    if approx_context_chars > 400_000:  # ~100k tokens
        log.warning(
            "copilot_large_context model=%s chars=%d interaction=%s — approaching Copilot context limit",
            model, approx_context_chars, (interaction_id or "?")[:8],
        )

    if _accounts_impl._is_multi_account():
        response_data, used_account_idx = _call_with_rotation(
            payload, initiator=initiator, interaction_id=interaction_id,
        )
    else:
        response_data, used_account_idx = _execute_single_account(
            payload, initiator=initiator, interaction_id=interaction_id,
        )

    msg = _merge_choices(response_data)

    usage_raw = response_data.get("usage", {})
    prompt_tokens = int(usage_raw.get("prompt_tokens", 0))
    completion_tokens = int(usage_raw.get("completion_tokens", 0))
    prompt_details = usage_raw.get("prompt_tokens_details", {})
    cached_tokens = int(prompt_details.get("cached_tokens", 0))

    from ouroboros.pricing import estimate_cost as _estimate_cost
    _shadow_cost = _estimate_cost(
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
    )

    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cached_tokens": cached_tokens,
        "cost": 0.0,  # Free via Copilot Pro subscription
        "shadow_cost": round(_shadow_cost, 6),
        "provider": "copilot",
    }

    try:
        _accounts_impl.track_copilot_usage(used_account_idx, model)
    except Exception as e:
        log.warning("copilot_quota_track_error: %s", e)

    session_stats = _track_session(interaction_id, usage, initiator)
    if session_stats:
        log.debug(
            "copilot_session id=%s rounds=%d prompt_tok=%d compl_tok=%d premium=%d",
            interaction_id[:8] if interaction_id else "?",
            session_stats["rounds"],
            session_stats["total_prompt_tokens"],
            session_stats["total_completion_tokens"],
            session_stats["premium_requests"],
        )

    return msg, usage


# ---------------------------------------------------------------------------
# Session reset — summarize context and continue in a fresh session
# ---------------------------------------------------------------------------

# Лимит раундов в одной Copilot сессии перед session reset
COPILOT_SESSION_ROUND_LIMIT = 28

SUMMARIZE_PROMPT = """You are summarizing an ongoing agentic task session that needs to continue in a fresh context.

Provide a COMPLETE handoff summary so the agent can seamlessly continue working. Include:

1. **TASK**: Original task/goal (1-2 sentences)  
2. **DONE**: What has been accomplished so far (specific files changed, commands run, results)
3. **IN PROGRESS**: Current step being worked on
4. **REMAINING**: What still needs to be done
5. **KEY CONTEXT**: Critical details needed to continue:
   - File paths and line numbers being worked on
   - Variable names, function signatures, error messages
   - Decisions made and why
   - Any pending tool calls or expected results
6. **WARNINGS**: Things to avoid, failed approaches, known issues

Be specific and factual. Include exact file paths, code snippets, error messages. This summary replaces the full conversation history."""


def summarize_session_for_reset(
    messages: List[Dict[str, Any]],
    model: str = "claude-sonnet-4-5",
    interaction_id: Optional[str] = None,
) -> Optional[str]:
    """Summarize current session context for handoff to a fresh session.

    Returns summary text, or None on failure.
    Uses X-Initiator: agent to avoid premium billing.
    """
    summary_messages: List[Dict[str, Any]] = []

    # Сохранить system message если есть
    for m in messages:
        if m.get("role") == "system":
            summary_messages.append(m)
            break

    # Взять последние сообщения (не более ~50), пропуская system
    non_system = [m for m in messages if m.get("role") != "system"]
    if len(non_system) > 50:
        non_system = non_system[-50:]
    summary_messages.extend(non_system)

    # Добавить запрос на суммаризацию
    summary_messages.append({
        "role": "user",
        "content": SUMMARIZE_PROMPT,
    })

    try:
        msg, usage = call_copilot(
            messages=summary_messages,
            tools=None,
            model=model,
            max_tokens=4096,
            interaction_id=interaction_id,
            force_user_initiator=False,  # пойдёт как agent
        )
        summary = msg.get("content", "")
        if summary:
            log.info(
                "copilot_session_summarized interaction=%s summary_tokens=%d",
                (interaction_id or "?")[:8],
                usage.get("completion_tokens", 0),
            )
        return summary or None
    except Exception as e:
        log.error("copilot_session_summarize_failed: %s", e)
        return None


def should_reset_session(interaction_id: Optional[str]) -> bool:
    """Check if current Copilot session should be reset (approaching round limit)."""
    if not interaction_id:
        return False
    stats = get_session_stats(interaction_id)
    if not stats:
        return False
    return stats["rounds"] >= COPILOT_SESSION_ROUND_LIMIT
