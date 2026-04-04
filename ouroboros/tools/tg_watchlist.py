"""tg_watchlist — persistent Telegram channel subscription tracker.

Stores subscribed channels with last-seen post IDs in
/opt/veles-data/memory/tg_watchlist.json so background consciousness
can call tg_watchlist_check() and receive ONLY new posts since last check.

Tools:
    tg_watchlist_add(channel)          — subscribe to a channel
    tg_watchlist_remove(channel)       — unsubscribe
    tg_watchlist_status()              — list subscriptions with last-seen info
    tg_watchlist_check(limit_per_channel=20) — fetch new posts, update watermarks

Usage:
    tg_watchlist_add(channel="abstractDL")
    tg_watchlist_check()               # returns only posts never seen before
    tg_watchlist_check(limit_per_channel=50)
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")
_WATCHLIST_FILE = "memory/tg_watchlist.json"


# ── Persistence ────────────────────────────────────────────────────────────────

def _watchlist_path() -> pathlib.Path:
    return pathlib.Path(_DRIVE_ROOT) / _WATCHLIST_FILE


def _load_watchlist() -> Dict[str, Any]:
    """Load watchlist from disk. Returns dict {channel: {last_id, added_at, last_checked}}."""
    path = _watchlist_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_watchlist(watchlist: Dict[str, Any]) -> None:
    """Persist watchlist to disk."""
    path = _watchlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(watchlist, indent=2, ensure_ascii=False), encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalize_channel(channel: str) -> str:
    return channel.lstrip("@").strip().lower()


# ── Tool implementations ───────────────────────────────────────────────────────

def _tg_watchlist_add(ctx: ToolContext, channel: str) -> str:
    """Subscribe to a Telegram channel."""
    ch = _normalize_channel(channel)
    if not ch:
        return json.dumps({"error": "channel must not be empty"})

    watchlist = _load_watchlist()
    if ch in watchlist:
        return json.dumps({
            "status": "already_subscribed",
            "channel": ch,
            "last_id": watchlist[ch].get("last_id", 0),
        })

    watchlist[ch] = {
        "last_id": 0,          # 0 = "haven't read anything yet"
        "added_at": _utc_now(),
        "last_checked": None,
    }
    _save_watchlist(watchlist)
    return json.dumps({
        "status": "subscribed",
        "channel": ch,
        "message": f"Subscribed. Call tg_watchlist_check() to fetch posts.",
    })


def _tg_watchlist_remove(ctx: ToolContext, channel: str) -> str:
    """Unsubscribe from a Telegram channel."""
    ch = _normalize_channel(channel)
    watchlist = _load_watchlist()
    if ch not in watchlist:
        return json.dumps({"status": "not_found", "channel": ch})

    del watchlist[ch]
    _save_watchlist(watchlist)
    return json.dumps({"status": "unsubscribed", "channel": ch})


def _tg_watchlist_status(ctx: ToolContext) -> str:
    """Show all subscribed channels and their last-seen post IDs."""
    watchlist = _load_watchlist()
    if not watchlist:
        return json.dumps({
            "subscriptions": [],
            "count": 0,
            "message": "Watchlist is empty. Use tg_watchlist_add() to subscribe.",
        })

    subs = []
    for ch, meta in sorted(watchlist.items()):
        subs.append({
            "channel": ch,
            "last_id": meta.get("last_id", 0),
            "added_at": meta.get("added_at"),
            "last_checked": meta.get("last_checked"),
        })

    return json.dumps({
        "subscriptions": subs,
        "count": len(subs),
    }, ensure_ascii=False)


def _tg_watchlist_check(
    ctx: ToolContext,
    limit_per_channel: int = 20,
    channels: Optional[List[str]] = None,
) -> str:
    """Fetch new posts from subscribed channels since last check.

    Returns only posts with id > last_seen for each channel.
    Updates last_seen watermarks after fetching.

    Args:
        limit_per_channel: max new posts to return per channel (default 20)
        channels: optional subset of channels to check (default: all subscribed)
    """
    from ouroboros.tools.tg_channel_read import _fetch_channel_posts  # noqa: PLC0415

    watchlist = _load_watchlist()
    if not watchlist:
        return json.dumps({
            "total_new_posts": 0,
            "channels_checked": 0,
            "message": "Watchlist is empty. Use tg_watchlist_add() to subscribe.",
            "posts": [],
        })

    # Determine which channels to check
    if channels:
        targets = {_normalize_channel(c) for c in channels}
        check_list = {k: v for k, v in watchlist.items() if k in targets}
        missing = targets - set(check_list.keys())
        if missing:
            log.warning("Channels not in watchlist: %s", missing)
    else:
        check_list = dict(watchlist)

    limit_per_channel = max(1, min(limit_per_channel, 100))
    all_new_posts: List[Dict[str, Any]] = []
    per_channel_summary: Dict[str, Any] = {}
    now = _utc_now()

    for ch, meta in check_list.items():
        last_id = meta.get("last_id", 0)

        # Fetch posts newer than last_id
        result = _fetch_channel_posts(
            channel=ch,
            limit=limit_per_channel,
            since_post_id=last_id + 1 if last_id > 0 else 0,
        )

        if result.get("error"):
            per_channel_summary[ch] = {
                "error": result["error"],
                "new_posts": 0,
                "last_id": last_id,
            }
            continue

        posts = result.get("posts", [])

        # Extra guard: only posts strictly newer than last_id
        if last_id > 0:
            posts = [p for p in posts if p["id"] > last_id]

        new_count = len(posts)
        max_id = max((p["id"] for p in posts), default=last_id)

        # Update watermark
        watchlist[ch]["last_id"] = max_id
        watchlist[ch]["last_checked"] = now

        per_channel_summary[ch] = {
            "new_posts": new_count,
            "last_id": max_id,
            "error": None,
        }

        for p in posts:
            all_new_posts.append({**p, "channel": ch})

    # Persist updated watermarks
    _save_watchlist(watchlist)

    # Sort merged posts by id ascending
    all_new_posts.sort(key=lambda p: (p.get("date", ""), p.get("id", 0)))

    return json.dumps({
        "total_new_posts": len(all_new_posts),
        "channels_checked": len(check_list),
        "checked_at": now,
        "per_channel": per_channel_summary,
        "posts": all_new_posts,
    }, ensure_ascii=False, default=str)


# ── Tool registration ──────────────────────────────────────────────────────────

_ADD_SCHEMA = {
    "name": "tg_watchlist_add",
    "description": (
        "Subscribe to a public Telegram channel. "
        "The channel is added to the persistent watchlist with last_seen_id=0. "
        "Call tg_watchlist_check() to fetch posts. "
        "Example: tg_watchlist_add(channel='abstractDL')"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "channel": {
                "type": "string",
                "description": "Channel username without @, e.g. 'abstractDL'",
            }
        },
        "required": ["channel"],
    },
}

_REMOVE_SCHEMA = {
    "name": "tg_watchlist_remove",
    "description": (
        "Unsubscribe from a Telegram channel. "
        "Removes it from the persistent watchlist. "
        "Example: tg_watchlist_remove(channel='abstractDL')"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "channel": {
                "type": "string",
                "description": "Channel username without @",
            }
        },
        "required": ["channel"],
    },
}

_STATUS_SCHEMA = {
    "name": "tg_watchlist_status",
    "description": (
        "List all subscribed Telegram channels with their last-seen post IDs and "
        "last check timestamps. Shows what's being monitored."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

_CHECK_SCHEMA = {
    "name": "tg_watchlist_check",
    "description": (
        "Fetch new posts from all (or specified) subscribed Telegram channels. "
        "Returns ONLY posts not seen before (id > last_seen_id). "
        "Automatically updates the last-seen watermark for each channel after fetching. "
        "Perfect for background monitoring — call periodically to get fresh content.\n\n"
        "Parameters:\n"
        "- limit_per_channel: max new posts per channel (1–100, default 20)\n"
        "- channels: optional list of channel names to check (default: all subscribed)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit_per_channel": {
                "type": "integer",
                "description": "Max new posts to return per channel (1–100, default 20)",
                "default": 20,
            },
            "channels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Subset of channels to check (default: all subscribed)",
            },
        },
        "required": [],
    },
}


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="tg_watchlist_add",
            schema=_ADD_SCHEMA,
            handler=lambda ctx, **kw: _tg_watchlist_add(ctx, **kw),
        ),
        ToolEntry(
            name="tg_watchlist_remove",
            schema=_REMOVE_SCHEMA,
            handler=lambda ctx, **kw: _tg_watchlist_remove(ctx, **kw),
        ),
        ToolEntry(
            name="tg_watchlist_status",
            schema=_STATUS_SCHEMA,
            handler=lambda ctx, **kw: _tg_watchlist_status(ctx, **kw),
        ),
        ToolEntry(
            name="tg_watchlist_check",
            schema=_CHECK_SCHEMA,
            handler=lambda ctx, **kw: _tg_watchlist_check(ctx, **kw),
        ),
    ]
