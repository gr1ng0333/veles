"""inbox — unified feed aggregator across all monitoring sources.

Collects new items from all four monitoring contours in one call:
  - Telegram channels (tg_watchlist)
  - RSS/Atom feeds (rss_reader)
  - Monitored web URLs (web_monitor)
  - Hacker News keywords (hn_reader)

Tools:
    inbox_check(limit_per_source=20)   — fetch all new items from all sources
    inbox_status()                     — show subscription counts for all sources

Each item in the result is tagged with `source_type` ('telegram', 'rss', 'web', 'hn')

Each item in the result is tagged with `source_type` ('telegram', 'rss', 'web')
and `source_name` so the caller can distinguish channels.

Usage:
    inbox_check()                      # everything new since last check
    inbox_check(limit_per_source=50)   # more items per source
    inbox_status()                     # what's being monitored

Returns items sorted by date ascending (oldest first), or by source type
if dates are unavailable.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_iso(dt_str: str | None) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _sort_key(item: Dict[str, Any]) -> tuple:
    dt = _parse_iso(item.get("date") or item.get("published") or item.get("detected_at") or "")
    return (dt or datetime.min.replace(tzinfo=timezone.utc),)


# ---------------------------------------------------------------------------
# Source collectors
# ---------------------------------------------------------------------------

def _collect_telegram(ctx: ToolContext, limit: int) -> List[Dict[str, Any]]:
    """Collect new posts from tg_watchlist."""
    try:
        from ouroboros.tools.tg_watchlist import _tg_watchlist_check  # noqa: PLC0415
        raw = _tg_watchlist_check(ctx, limit_per_channel=limit)
        data = json.loads(raw)
        posts = data.get("posts", [])
        items = []
        for p in posts:
            items.append({
                "source_type": "telegram",
                "source_name": p.get("channel", ""),
                "id": p.get("id"),
                "date": p.get("date", ""),
                "title": "",  # Telegram posts have no title
                "text": p.get("text", ""),
                "url": f"https://t.me/{p.get('channel', '')}/{p.get('id', '')}",
                "views": p.get("views", 0),
                "links": p.get("links", []),
            })
        return items
    except Exception as exc:
        log.warning("inbox: telegram collect failed: %s", exc)
        return []


def _collect_rss(ctx: ToolContext, limit: int) -> List[Dict[str, Any]]:
    """Collect new items from rss_reader."""
    try:
        from ouroboros.tools.rss_reader import _rss_check  # noqa: PLC0415
        raw = _rss_check(ctx, limit_per_feed=limit)
        data = json.loads(raw)
        feed_items = data.get("items", [])
        items = []
        for fi in feed_items:
            items.append({
                "source_type": "rss",
                "source_name": fi.get("feed", ""),
                "id": None,
                "date": fi.get("published", ""),
                "title": fi.get("title", ""),
                "text": fi.get("summary", ""),
                "url": fi.get("link", ""),
                "views": 0,
                "links": [fi.get("link", "")] if fi.get("link") else [],
            })
        return items
    except Exception as exc:
        log.warning("inbox: rss collect failed: %s", exc)
        return []


def _collect_web(ctx: ToolContext) -> List[Dict[str, Any]]:
    """Collect detected changes from web_monitor."""
    try:
        from ouroboros.tools.web_monitor import _web_monitor_check  # noqa: PLC0415
        raw = _web_monitor_check(ctx)
        data = json.loads(raw)
        changes = data.get("changes", [])
        items = []
        for ch in changes:
            items.append({
                "source_type": "web",
                "source_name": ch.get("name", ""),
                "id": None,
                "date": ch.get("detected_at", ""),
                "title": f"Change detected: {ch.get('name', '')}",
                "text": ch.get("diff_summary", ch.get("summary", "")),
                "url": ch.get("url", ""),
                "views": 0,
                "links": [ch.get("url", "")] if ch.get("url") else [],
            })
        return items
    except Exception as exc:
        log.warning("inbox: web_monitor collect failed: %s", exc)
        return []



# ---------------------------------------------------------------------------
# Source collector: Hacker News
# ---------------------------------------------------------------------------

def _collect_hn(ctx: ToolContext, limit: int) -> List[Dict[str, Any]]:
    """Collect new stories from hn_watchlist."""
    try:
        from ouroboros.tools.hn_reader import _hn_watchlist_check  # noqa: PLC0415
        raw = _hn_watchlist_check(ctx, limit_per_keyword=limit)
        data = json.loads(raw)
        stories = data.get("stories", [])
        items = []
        for s in stories:
            items.append({
                "source_type": "hn",
                "source_name": s.get("matched_keyword", "hacker_news"),
                "id": s.get("id"),
                "date": s.get("date", ""),
                "title": s.get("title", ""),
                "text": "",
                "url": s.get("url", s.get("hn_url", "")),
                "views": s.get("points", 0),
                "links": [s.get("url", ""), s.get("hn_url", "")] if s.get("url") else [],
                "author": s.get("author", ""),
                "comments": s.get("comments", 0),
            })
        return items
    except Exception as exc:
        log.warning("inbox: hn collect failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Tool: inbox_check
# ---------------------------------------------------------------------------

def _inbox_check(
    ctx: ToolContext,
    limit_per_source: int = 20,
    sources: Optional[List[str]] = None,
) -> str:
    """Fetch all new items from all monitoring sources since last check.

    Args:
        limit_per_source: max new items per individual source (1–100, default 20)
        sources: optional list of source types to check: 'telegram', 'rss', 'web', 'hn'
                 (default: all enabled sources)
    """
    limit_per_source = max(1, min(limit_per_source, 100))
    enabled = set(sources) if sources else {"telegram", "rss", "web", "hn"}

    all_items: List[Dict[str, Any]] = []
    source_summary: Dict[str, Any] = {}

    if "telegram" in enabled:
        tg_items = _collect_telegram(ctx, limit_per_source)
        all_items.extend(tg_items)
        source_summary["telegram"] = {"new_items": len(tg_items)}

    if "rss" in enabled:
        rss_items = _collect_rss(ctx, limit_per_source)
        all_items.extend(rss_items)
        source_summary["rss"] = {"new_items": len(rss_items)}

    if "web" in enabled:
        web_items = _collect_web(ctx)
        all_items.extend(web_items)
        source_summary["web"] = {"new_items": len(web_items)}

    if "hn" in enabled:
        hn_items = _collect_hn(ctx, limit_per_source)
        all_items.extend(hn_items)
        source_summary["hn"] = {"new_items": len(hn_items)}

    all_items.sort(key=_sort_key)

    return json.dumps({
        "total_new": len(all_items),
        "sources": source_summary,
        "items": all_items,
    }, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Tool: inbox_status
# ---------------------------------------------------------------------------

def _inbox_status(ctx: ToolContext) -> str:
    """Show subscription counts for all monitoring sources."""
    summary: Dict[str, Any] = {}

    # Telegram watchlist
    try:
        from ouroboros.tools.tg_watchlist import _tg_watchlist_status  # noqa: PLC0415
        raw = _tg_watchlist_status(ctx)
        data = json.loads(raw)
        summary["telegram"] = {
            "subscriptions": data.get("count", 0),
            "channels": [s["channel"] for s in data.get("subscriptions", [])],
        }
    except Exception as exc:
        summary["telegram"] = {"error": str(exc)}

    # RSS feeds
    try:
        from ouroboros.tools.rss_reader import _rss_status  # noqa: PLC0415
        raw = _rss_status(ctx)
        data = json.loads(raw)
        summary["rss"] = {
            "subscriptions": data.get("count", 0),
            "feeds": [f["name"] for f in data.get("feeds", [])],
        }
    except Exception as exc:
        summary["rss"] = {"error": str(exc)}

    # Web monitor
    try:
        from ouroboros.tools.web_monitor import _web_monitor_status  # noqa: PLC0415
        raw = _web_monitor_status(ctx)
        data = json.loads(raw)
        summary["web"] = {
            "subscriptions": data.get("count", 0),
            "urls": [m["name"] for m in data.get("monitors", [])],
        }
    except Exception as exc:
        summary["web"] = {"error": str(exc)}

    # Hacker News watchlist
    try:
        from ouroboros.tools.hn_reader import _hn_watchlist_status  # noqa: PLC0415
        raw = _hn_watchlist_status(ctx)
        data = json.loads(raw)
        summary["hn"] = {
            "subscriptions": data.get("count", 0),
            "keywords": [kw["keyword"] for kw in data.get("keywords", [])],
        }
    except Exception as exc:
        summary["hn"] = {"error": str(exc)}

    total = sum(
        v.get("subscriptions", 0) for v in summary.values()
        if isinstance(v, dict) and "error" not in v
    )

    return json.dumps({
        "total_subscriptions": total,
        "sources": summary,
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_CHECK_SCHEMA = {
    "name": "inbox_check",
    "description": (
        "Fetch all new items from all monitoring sources since last check: "
        "Telegram channels (tg_watchlist), RSS/Atom feeds (rss_reader), "
        "monitored web URLs (web_monitor), and Hacker News keywords (hn_watchlist). "
        "Returns a unified sorted feed.\n\n"
        "Each item has: source_type ('telegram'|'rss'|'web'|'hn'), source_name, "
        "date, title, text, url, links.\n\n"
        "Parameters:\n"
        "- limit_per_source: max new items per individual source (1–100, default 20)\n"
        "- sources: optional list of source types to check: 'telegram', 'rss', 'web'"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit_per_source": {
                "type": "integer",
                "description": "Max new items per source (1–100, default 20)",
                "default": 20,
            },
            "sources": {
                "type": "array",
                "items": {"type": "string", "enum": ["telegram", "rss", "web", "hn"]},
                "description": "Source types to check (default: all). E.g. ['telegram', 'rss', 'hn']",
            },
        },
        "required": [],
    },
}

_STATUS_SCHEMA = {
    "name": "inbox_status",
    "description": (
        "Show subscription counts and names across all monitoring sources: "
        "Telegram watchlist channels, RSS/Atom feeds, and web monitor URLs. "
        "Use to see what's being tracked without triggering a fetch."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="inbox_check",
            schema=_CHECK_SCHEMA,
            handler=lambda ctx, **kw: _inbox_check(ctx, **kw),
        ),
        ToolEntry(
            name="inbox_status",
            schema=_STATUS_SCHEMA,
            handler=lambda ctx, **kw: _inbox_status(ctx, **kw),
        ),
    ]
