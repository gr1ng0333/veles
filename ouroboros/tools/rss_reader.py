"""rss_reader — subscribe to RSS/Atom feeds and fetch new items since last check.

No external dependencies — pure Python stdlib (urllib + xml.etree).

Persistent storage: /opt/veles-data/memory/rss_feeds.json
Each feed stores last_seen_guids (set of item GUIDs) as watermark.

Supports:
    RSS 2.0 / 0.9x — <item> tags with <title>, <link>, <pubDate>, <description>, <guid>
    Atom 1.0       — <entry> tags with <title>, <link>, <published/updated>, <summary/content>, <id>

Tools:
    rss_subscribe(url, name?)          — subscribe to a feed
    rss_unsubscribe(name)              — unsubscribe from a feed
    rss_status()                       — list subscribed feeds with last-check info
    rss_check(name?, limit_per_feed?)  — fetch new items since last check (all or one feed)

Usage:
    rss_subscribe(url="https://arxiv.org/rss/cs.AI", name="arxiv_ai")
    rss_check()                        # returns only new items across all feeds
    rss_check(name="arxiv_ai", limit_per_feed=30)
    rss_status()
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")
_FEEDS_FILE = "memory/rss_feeds.json"

_DEFAULT_TIMEOUT = 20
_MAX_ITEMS_PER_FEED = 200
_USER_AGENT = (
    "Mozilla/5.0 (compatible; VelesBot/1.0; +https://github.com/gr1ng0333/veles)"
)

# XML namespaces
_ATOM_NS = "http://www.w3.org/2005/Atom"
_CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
_DC_NS = "http://purl.org/dc/elements/1.1/"

# ── Persistence ────────────────────────────────────────────────────────────────


def _feeds_path() -> pathlib.Path:
    return pathlib.Path(_DRIVE_ROOT) / _FEEDS_FILE


def _load_feeds() -> Dict[str, Any]:
    path = _feeds_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_feeds(feeds: Dict[str, Any]) -> None:
    path = _feeds_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(feeds, indent=2, ensure_ascii=False), encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _slug(name: str) -> str:
    return re.sub(r"[^\w\-]", "_", name.strip().lower())[:80]


def _auto_name(url: str) -> str:
    """Derive a feed name from URL."""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        host = p.hostname or ""
        path = p.path.strip("/").replace("/", "_")
        base = f"{host}_{path}" if path else host
        return _slug(base)[:60] or "feed"
    except Exception:
        return "feed"


# ── Fetch ─────────────────────────────────────────────────────────────────────


def _fetch_feed_xml(url: str) -> str:
    """Fetch RSS/Atom XML from URL. Returns raw XML string."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
        raw = resp.read()
    # Detect encoding
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


# ── Parsing ───────────────────────────────────────────────────────────────────


def _text(el: Optional[ET.Element]) -> str:
    if el is None:
        return ""
    return (el.text or "").strip()


def _parse_date(date_str: str) -> str:
    """Normalize date string to ISO 8601 UTC. Returns original string on failure."""
    if not date_str:
        return ""
    # Try RFC 2822 (RSS)
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    # Try ISO 8601 (Atom)
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    return date_str


def _parse_rss(root: ET.Element) -> Tuple[str, List[Dict[str, Any]]]:
    """Parse RSS 2.0 / 0.9x. Returns (feed_title, items)."""
    channel = root.find("channel")
    if channel is None:
        channel = root

    feed_title = _text(channel.find("title")) or ""

    items: List[Dict[str, Any]] = []
    for item in channel.findall("item"):
        title = _text(item.find("title"))
        link = _text(item.find("link"))
        pub_date = _text(item.find("pubDate"))
        description = _text(item.find("description"))
        guid_el = item.find("guid")
        guid = _text(guid_el) if guid_el is not None else (link or title)

        # content:encoded fallback
        content_el = item.find(f"{{{_CONTENT_NS}}}encoded")
        if content_el is not None and content_el.text:
            description = content_el.text.strip()

        # dc:date fallback
        if not pub_date:
            dc_date = item.find(f"{{{_DC_NS}}}date")
            if dc_date is not None:
                pub_date = _text(dc_date)

        items.append({
            "guid": guid or link,
            "title": title,
            "link": link,
            "date": _parse_date(pub_date),
            "summary": _strip_html(description)[:1000],
        })

    return feed_title, items


def _parse_atom(root: ET.Element) -> Tuple[str, List[Dict[str, Any]]]:
    """Parse Atom 1.0. Returns (feed_title, items)."""
    ns = _ATOM_NS

    title_el = root.find(f"{{{ns}}}title")
    feed_title = _text(title_el) or ""

    items: List[Dict[str, Any]] = []
    for entry in root.findall(f"{{{ns}}}entry"):
        title = _text(entry.find(f"{{{ns}}}title"))

        link_el = entry.find(f"{{{ns}}}link[@rel='alternate']")
        if link_el is None:
            link_el = entry.find(f"{{{ns}}}link")
        link = (link_el.get("href") or "") if link_el is not None else ""

        id_el = entry.find(f"{{{ns}}}id")
        guid = _text(id_el) or link or title

        pub_el = entry.find(f"{{{ns}}}published")
        upd_el = entry.find(f"{{{ns}}}updated")
        date_raw = _text(pub_el) or _text(upd_el)

        summary_el = entry.find(f"{{{ns}}}summary")
        content_el = entry.find(f"{{{ns}}}content")
        raw_summary = _text(content_el) or _text(summary_el)

        items.append({
            "guid": guid,
            "title": title,
            "link": link,
            "date": _parse_date(date_raw),
            "summary": _strip_html(raw_summary)[:1000],
        })

    return feed_title, items


_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_WS = re.compile(r"[ \t]+")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    import html as html_mod
    text = _TAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    text = _MULTI_WS.sub(" ", text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n".join(lines)


def _parse_feed_xml(xml_str: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Detect RSS vs Atom and parse. Returns (feed_title, items)."""
    # Strip BOM / declaration quirks
    xml_str = xml_str.strip()
    if xml_str.startswith("\ufeff"):
        xml_str = xml_str[1:]

    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as e:
        raise ValueError(f"XML parse error: {e}") from e

    tag = root.tag.lower()
    if "atom" in tag or root.tag == f"{{{_ATOM_NS}}}feed":
        return _parse_atom(root)
    else:
        # RSS
        return _parse_rss(root)


# ── Core logic ────────────────────────────────────────────────────────────────


def _fetch_and_parse(url: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Fetch URL and parse RSS/Atom. Returns (feed_title, items)."""
    xml_str = _fetch_feed_xml(url)
    return _parse_feed_xml(xml_str)


# ── Tool implementations ───────────────────────────────────────────────────────


def _rss_subscribe(ctx: ToolContext, url: str, name: str = "") -> str:
    """Subscribe to an RSS/Atom feed.

    Fetches the feed once to validate it and extract the feed title.
    If name is not provided, derives one from the URL.
    """
    url = url.strip()
    if not url:
        return json.dumps({"error": "url must not be empty"})

    feeds = _load_feeds()

    # Auto-derive name if not provided
    if not name:
        name = _auto_name(url)
    else:
        name = _slug(name)

    if not name:
        return json.dumps({"error": "could not derive a valid feed name"})

    # Check for duplicate URL
    for existing_name, meta in feeds.items():
        if meta.get("url") == url:
            return json.dumps({
                "status": "already_subscribed",
                "name": existing_name,
                "url": url,
                "message": f"Already subscribed as '{existing_name}'. Use rss_check() to fetch new items.",
            })

    if name in feeds:
        return json.dumps({
            "status": "name_conflict",
            "name": name,
            "message": f"Name '{name}' already in use. Provide a unique name.",
        })

    # Validate feed by fetching
    try:
        feed_title, items = _fetch_and_parse(url)
    except urllib.error.HTTPError as e:
        return json.dumps({"error": f"HTTP {e.code}: {e.reason}", "url": url})
    except urllib.error.URLError as e:
        return json.dumps({"error": f"Network error: {e.reason}", "url": url})
    except ValueError as e:
        return json.dumps({"error": str(e), "url": url})
    except Exception as e:
        return json.dumps({"error": f"Feed fetch failed: {e}", "url": url})

    # Store with all current GUIDs as seen (don't flood with historical items)
    seen_guids = [item["guid"] for item in items if item.get("guid")]

    feeds[name] = {
        "url": url,
        "title": feed_title or name,
        "added_at": _utc_now(),
        "last_checked": _utc_now(),
        "last_seen_guids": seen_guids,
        "total_fetched": 0,
    }
    _save_feeds(feeds)

    return json.dumps({
        "status": "subscribed",
        "name": name,
        "title": feed_title or name,
        "url": url,
        "items_found": len(items),
        "message": (
            f"Subscribed to '{feed_title or name}' ({len(items)} current items marked as seen). "
            "Call rss_check() to get new items as they appear."
        ),
    })


def _rss_unsubscribe(ctx: ToolContext, name: str) -> str:
    """Unsubscribe from an RSS/Atom feed."""
    name = _slug(name)
    feeds = _load_feeds()
    if name not in feeds:
        return json.dumps({"status": "not_found", "name": name})
    title = feeds[name].get("title", name)
    del feeds[name]
    _save_feeds(feeds)
    return json.dumps({"status": "unsubscribed", "name": name, "title": title})


def _rss_status(ctx: ToolContext) -> str:
    """List subscribed RSS/Atom feeds with last-check info."""
    feeds = _load_feeds()
    if not feeds:
        return json.dumps({
            "subscriptions": [],
            "count": 0,
            "message": "No feeds subscribed. Use rss_subscribe(url=...) to add one.",
        })

    subs = []
    for name, meta in sorted(feeds.items()):
        subs.append({
            "name": name,
            "title": meta.get("title", name),
            "url": meta.get("url", ""),
            "added_at": meta.get("added_at"),
            "last_checked": meta.get("last_checked"),
            "seen_guids_count": len(meta.get("last_seen_guids", [])),
            "total_fetched": meta.get("total_fetched", 0),
        })

    return json.dumps({"subscriptions": subs, "count": len(subs)}, ensure_ascii=False)


def _check_one_feed(name: str, meta: Dict[str, Any], limit: int) -> Dict[str, Any]:
    """Fetch one feed and return only new items (not in last_seen_guids)."""
    url = meta.get("url", "")
    seen_guids = set(meta.get("last_seen_guids", []))

    try:
        feed_title, items = _fetch_and_parse(url)
    except urllib.error.HTTPError as e:
        return {"name": name, "error": f"HTTP {e.code}: {e.reason}", "new_items": []}
    except urllib.error.URLError as e:
        return {"name": name, "error": f"Network error: {e.reason}", "new_items": []}
    except Exception as e:
        return {"name": name, "error": str(e), "new_items": []}

    # New items = those whose guid is not in the seen set
    new_items = [
        item for item in items
        if item.get("guid") and item["guid"] not in seen_guids
    ]

    # Sort by date ascending (oldest new first)
    def _sort_key(item: Dict[str, Any]) -> str:
        return item.get("date") or ""

    new_items.sort(key=_sort_key)
    new_items = new_items[:limit]

    # Update seen set: old seen + all fetched GUIDs
    all_guids = [item["guid"] for item in items if item.get("guid")]
    # Keep only the most recent MAX_ITEMS_PER_FEED guids to prevent unbounded growth
    updated_seen = list(set(seen_guids) | set(all_guids))
    if len(updated_seen) > _MAX_ITEMS_PER_FEED:
        updated_seen = updated_seen[-_MAX_ITEMS_PER_FEED:]

    return {
        "name": name,
        "title": feed_title or meta.get("title", name),
        "url": url,
        "new_items": new_items,
        "new_count": len(new_items),
        "updated_guids": updated_seen,
        "error": None,
    }


def _rss_check(
    ctx: ToolContext,
    name: str = "",
    limit_per_feed: int = 20,
) -> str:
    """Fetch new items from subscribed RSS/Atom feeds.

    Returns only items not seen since last check.
    Updates last-seen watermarks after fetching.

    Args:
        name: check only this feed (default: all subscribed feeds)
        limit_per_feed: max new items per feed (1–100, default 20)
    """
    feeds = _load_feeds()
    if not feeds:
        return json.dumps({
            "total_new": 0,
            "feeds_checked": 0,
            "message": "No feeds subscribed. Use rss_subscribe(url=...) to add one.",
            "items": [],
        })

    if name:
        name = _slug(name)
        if name not in feeds:
            return json.dumps({"error": f"Feed '{name}' not found."})
        targets = {name: feeds[name]}
    else:
        targets = dict(feeds)

    limit_per_feed = max(1, min(limit_per_feed, 100))

    all_new_items: List[Dict[str, Any]] = []
    per_feed_summary: Dict[str, Any] = {}
    now = _utc_now()

    for feed_name, meta in sorted(targets.items()):
        result = _check_one_feed(feed_name, meta, limit_per_feed)

        new_items = result.get("new_items", [])
        per_feed_summary[feed_name] = {
            "title": result.get("title", feed_name),
            "new_count": result.get("new_count", 0),
            "error": result.get("error"),
        }

        if result.get("error") is None:
            # Update watermarks
            feeds[feed_name]["last_checked"] = now
            feeds[feed_name]["last_seen_guids"] = result.get("updated_guids", [])
            feeds[feed_name]["total_fetched"] = (
                feeds[feed_name].get("total_fetched", 0) + len(new_items)
            )
            if result.get("title"):
                feeds[feed_name]["title"] = result["title"]

        for item in new_items:
            all_new_items.append({**item, "feed_name": feed_name})

    _save_feeds(feeds)

    # Sort all new items by date ascending
    all_new_items.sort(key=lambda i: i.get("date") or "")

    return json.dumps({
        "total_new": len(all_new_items),
        "feeds_checked": len(targets),
        "checked_at": now,
        "per_feed": per_feed_summary,
        "items": all_new_items,
    }, ensure_ascii=False, default=str)


# ── Tool schemas ───────────────────────────────────────────────────────────────

_SUBSCRIBE_SCHEMA = {
    "name": "rss_subscribe",
    "description": (
        "Subscribe to an RSS or Atom feed. Fetches the feed once to validate it. "
        "All current items are marked as seen — only NEW items after subscription "
        "will be returned by rss_check(). Useful for: arxiv AI papers, OpenAI/Anthropic "
        "blogs, Hacker News, model changelogs, GitHub releases.\n\n"
        "Parameters:\n"
        "- url: full feed URL, e.g. 'https://arxiv.org/rss/cs.AI'\n"
        "- name: optional short name for the feed (auto-derived if omitted)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Full URL of the RSS or Atom feed",
            },
            "name": {
                "type": "string",
                "description": "Short name for the feed (alphanumeric/hyphens, auto-derived if omitted)",
                "default": "",
            },
        },
        "required": ["url"],
    },
}

_UNSUBSCRIBE_SCHEMA = {
    "name": "rss_unsubscribe",
    "description": (
        "Unsubscribe from an RSS/Atom feed. Removes it from the persistent feed list. "
        "Example: rss_unsubscribe(name='arxiv_ai')"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Feed name to unsubscribe from",
            }
        },
        "required": ["name"],
    },
}

_STATUS_SCHEMA = {
    "name": "rss_status",
    "description": (
        "List all subscribed RSS/Atom feeds with their names, titles, URLs, "
        "last check time, and how many items have been seen. "
        "Shows what feeds are being monitored."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

_CHECK_SCHEMA = {
    "name": "rss_check",
    "description": (
        "Fetch new items from subscribed RSS/Atom feeds since last check. "
        "Returns ONLY items not seen before (new GUIDs). "
        "Automatically updates the seen-GUIDs watermark for each feed. "
        "Call periodically from background consciousness to stay updated on AI news.\n\n"
        "Parameters:\n"
        "- name: check only this feed (default: all subscribed feeds)\n"
        "- limit_per_feed: max new items per feed (1–100, default 20)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Feed name to check (default: all subscribed feeds)",
                "default": "",
            },
            "limit_per_feed": {
                "type": "integer",
                "description": "Max new items per feed (1–100, default 20)",
                "default": 20,
            },
        },
        "required": [],
    },
}


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="rss_subscribe",
            schema=_SUBSCRIBE_SCHEMA,
            handler=lambda ctx, **kw: _rss_subscribe(ctx, **kw),
        ),
        ToolEntry(
            name="rss_unsubscribe",
            schema=_UNSUBSCRIBE_SCHEMA,
            handler=lambda ctx, **kw: _rss_unsubscribe(ctx, **kw),
        ),
        ToolEntry(
            name="rss_status",
            schema=_STATUS_SCHEMA,
            handler=lambda ctx, **kw: _rss_status(ctx, **kw),
        ),
        ToolEntry(
            name="rss_check",
            schema=_CHECK_SCHEMA,
            handler=lambda ctx, **kw: _rss_check(ctx, **kw),
        ),
    ]
