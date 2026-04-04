"""veles_channel — Veles's own voice in @veles_agi Telegram channel.

This module gives Veles first-person tools to publish to its own channel:

    veles_say(text, topics?, format?)         — publish a thought/insight to @veles_agi
    veles_channel_history(limit?)             — read recent posts from own channel
    veles_channel_stats()                     — subscriber count, recent post stats

Design principles:
- Posts are deduplicated (stores SHA of last N posted texts).
- Channel is read from VELES_CHANNEL env var (default: @veles_agi).
- Uses TELEGRAM_BOT_TOKEN for posting (same as tg_channel_post).
- Maintains a local post log at {drive_root}/memory/channel_posts.jsonl.
- Formats posts as clean Telegram HTML (no raw markdown).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import append_jsonl

log = logging.getLogger(__name__)

_DEFAULT_CHANNEL = "@veles_agi"
_DEFAULT_TIMEOUT = 20
_TG_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_MAX_DEDUP_HISTORY = 200  # SHA hashes kept in memory (posts.jsonl)


# ── Internal helpers ────────────────────────────────────────────────────────

def _get_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN env var is not set")
    return token


def _get_channel() -> str:
    return os.environ.get("VELES_CHANNEL", _DEFAULT_CHANNEL).strip()


def _get_drive_root(ctx: ToolContext) -> Path:
    dr = getattr(ctx, "drive_root", None) or os.environ.get("OUROBOROS_DRIVE_ROOT", "/opt/veles-data")
    return Path(str(dr))


def _posts_log(ctx: ToolContext) -> Path:
    p = _get_drive_root(ctx) / "memory" / "channel_posts.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def _load_recent_hashes(log_path: Path, limit: int = _MAX_DEDUP_HISTORY) -> set:
    if not log_path.exists():
        return set()
    hashes = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
                if "text_hash" in obj:
                    hashes.append(obj["text_hash"])
            except Exception:
                pass
    return set(hashes[-limit:])


def _tg_api_call(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    token = _get_token()
    url = _TG_API_BASE.format(token=token, method=method)
    data = json.dumps({k: v for k, v in payload.items() if v is not None}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Telegram API {method} HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Telegram API {method} network error: {e.reason}") from e


def _format_html(text: str, topics: Optional[List[str]] = None) -> str:
    """Light HTML formatting: escape special chars, add hashtags footer."""
    import html
    safe = html.escape(text)
    if topics:
        tags = " ".join(f"#{t.strip().lstrip('#').replace(' ', '_')}" for t in topics if t.strip())
        if tags:
            safe = safe + "\n\n" + tags
    return safe


# ── Tool handlers ────────────────────────────────────────────────────────────

def _veles_say(
    ctx: ToolContext,
    text: str,
    topics: Optional[List[str]] = None,
    format: str = "auto",
    channel: Optional[str] = None,
) -> str:
    """Publish a thought/insight to @veles_agi.

    Args:
        text:    The message text (1–4096 chars).
        topics:  Optional list of hashtag topics to append (e.g. ["evolution", "ai"]).
        format:  "html" | "markdown" | "auto" (default — auto-selects HTML).
        channel: Override target channel (default: VELES_CHANNEL env or @veles_agi).
    """
    if not text or not text.strip():
        return json.dumps({"ok": False, "error": "text is required"})
    if len(text) > 4096:
        return json.dumps({"ok": False, "error": f"text too long ({len(text)} chars, max 4096)"})

    log_path = _posts_log(ctx)
    h = _text_hash(text)
    existing_hashes = _load_recent_hashes(log_path)
    if h in existing_hashes:
        return json.dumps({"ok": False, "error": "duplicate: identical post already in channel history"})

    target = channel or _get_channel()
    parse_mode: Optional[str]
    if format == "markdown":
        formatted = text
        parse_mode = "Markdown"
    else:
        formatted = _format_html(text, topics)
        parse_mode = "HTML"

    resp = _tg_api_call("sendMessage", {
        "chat_id": target,
        "text": formatted,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False,
    })

    if not resp.get("ok"):
        err = resp.get("description", "unknown error")
        return json.dumps({"ok": False, "error": f"Telegram error: {err}"})

    message_id = resp["result"]["message_id"]
    ts = datetime.now(tz=timezone.utc).isoformat()
    
    # Log post
    log_entry = {
        "ts": ts,
        "message_id": message_id,
        "channel": target,
        "text_hash": h,
        "text_preview": text[:120],
        "topics": topics or [],
    }
    append_jsonl(_posts_log(ctx), log_entry)

    return json.dumps({
        "ok": True,
        "message_id": message_id,
        "channel": target,
        "ts": ts,
        "preview": text[:80] + ("..." if len(text) > 80 else ""),
    })


def _veles_channel_history(ctx: ToolContext, limit: int = 20) -> str:
    """Read recent posts from @veles_agi channel (via local log)."""
    limit = max(1, min(limit, 200))
    log_path = _posts_log(ctx)
    if not log_path.exists():
        return json.dumps({"ok": True, "posts": [], "message": "No posts yet"})

    posts = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            try:
                posts.append(json.loads(line))
            except Exception:
                pass
    recent = posts[-limit:]
    recent.reverse()  # newest first
    return json.dumps({"ok": True, "count": len(recent), "posts": recent})


def _veles_channel_stats(ctx: ToolContext) -> str:
    """Get @veles_agi channel subscriber count and recent post statistics."""
    target = _get_channel()
    try:
        resp = _tg_api_call("getChatMemberCount", {"chat_id": target})
        member_count = resp.get("result", 0) if resp.get("ok") else None
    except Exception as e:
        member_count = None
        log.warning("veles_channel_stats: getChatMemberCount failed: %s", e)

    # Count posts from log
    log_path = _posts_log(ctx)
    total_posts = 0
    recent_topics: List[str] = []
    if log_path.exists():
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                    total_posts += 1
                    recent_topics.extend(obj.get("topics", []))
                except Exception:
                    pass
    
    # Top topics
    from collections import Counter
    topic_counts = Counter(recent_topics)
    top_topics = [t for t, _ in topic_counts.most_common(5)]

    return json.dumps({
        "ok": True,
        "channel": target,
        "subscribers": member_count,
        "total_posts": total_posts,
        "top_topics": top_topics,
    })


# ── Tool registry ────────────────────────────────────────────────────────────


_SAY_SCHEMA: Dict[str, Any] = {
    "name": "veles_say",
    "description": (
        "Publish a thought, insight, or update to @veles_agi — Veles's own Telegram channel. "
        "Use this to share ideas, observations, evolution milestones, or anything worth saying publicly. "
        "Posts are deduplicated — identical text is never sent twice. "
        "Hashtag topics are added as a footer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Message text (1–4096 chars). Plain text or HTML. First-person voice.",
            },
            "topics": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional hashtag topics (e.g. ['evolution', 'memory', 'ai']). Added as footer.",
            },
            "format": {
                "type": "string",
                "enum": ["auto", "html", "markdown"],
                "description": "Message format. Default 'auto' uses HTML.",
            },
            "channel": {
                "type": "string",
                "description": "Override target channel (default: @veles_agi). Usually leave empty.",
            },
        },
        "required": ["text"],
    },
}

_HISTORY_SCHEMA: Dict[str, Any] = {
    "name": "veles_channel_history",
    "description": (
        "Read recent posts from @veles_agi (Veles's own channel) via the local post log. "
        "Returns newest-first list with message IDs, timestamps, topics, and text previews."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max posts to return (1–200, default 20).",
            }
        },
    },
}

_STATS_SCHEMA: Dict[str, Any] = {
    "name": "veles_channel_stats",
    "description": (
        "Get statistics for @veles_agi: subscriber count (via Telegram API), "
        "total posts published, top hashtag topics."
    ),
    "parameters": {"type": "object", "properties": {}},
}


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="veles_say",
            schema=_SAY_SCHEMA,
            handler=lambda ctx, **kw: _veles_say(ctx, **kw),
        ),
        ToolEntry(
            name="veles_channel_history",
            schema=_HISTORY_SCHEMA,
            handler=lambda ctx, **kw: _veles_channel_history(ctx, **kw),
        ),
        ToolEntry(
            name="veles_channel_stats",
            schema=_STATS_SCHEMA,
            handler=lambda ctx, **kw: _veles_channel_stats(ctx, **kw),
        ),
    ]
