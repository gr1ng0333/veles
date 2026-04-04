"""yt_reader — YouTube channel watchlist for new video tracking.

Uses YouTube's public RSS feeds — no API key or auth required.
Supports channel IDs (UCxxx...), channel URLs, and @handles.

RSS endpoint: https://www.youtube.com/feeds/videos.xml?channel_id=<channel_id>

Storage: /opt/veles-data/memory/yt_watchlist.json
Each entry stores: channel_id, label, last_seen_ids, added_at, last_checked.

Tools:
    yt_subscribe(channel, label?)      — subscribe to a YouTube channel
    yt_unsubscribe(channel_or_label)   — unsubscribe
    yt_status()                        — list subscriptions with last-check info
    yt_check(limit?)                   — fetch new videos since last check
    yt_latest(channel, limit?)         — get latest N videos without subscribing

Channel formats accepted:
    UCxxxxxx...           — channel ID directly
    @handle               — YouTube @-handle (page scraped to resolve ID)
    https://youtube.com/@handle
    https://youtube.com/channel/UCxxxxxx
    https://www.youtube.com/c/name
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")
_WATCHLIST_FILE = "memory/yt_watchlist.json"

_RSS_BASE = "https://www.youtube.com/feeds/videos.xml?channel_id="
_YT_PAGE_TIMEOUT = 20
_RSS_TIMEOUT = 15
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

# Atom namespace
_ATOM_NS = "http://www.w3.org/2005/Atom"
_YT_NS = "http://www.youtube.com/xml/schemas/2015"
_MEDIA_NS = "http://search.yahoo.com/mrss/"

# ── Persistence ────────────────────────────────────────────────────────────────


def _watchlist_path() -> pathlib.Path:
    return pathlib.Path(_DRIVE_ROOT) / _WATCHLIST_FILE


def _load_watchlist() -> Dict[str, Any]:
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
    path = _watchlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(watchlist, indent=2, ensure_ascii=False), encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ── Channel ID resolution ──────────────────────────────────────────────────────


def _is_channel_id(s: str) -> bool:
    """Return True if s looks like a raw UCxxxxxx channel ID."""
    return bool(re.match(r"^UC[A-Za-z0-9_\-]{20,30}$", s))


def _extract_channel_id_from_url(url: str) -> Optional[str]:
    """Try to extract UCxxxxxx from a youtube.com/channel/UCxxx URL."""
    m = re.search(r"/channel/(UC[A-Za-z0-9_\-]{20,30})", url)
    if m:
        return m.group(1)
    return None


def _resolve_channel_id(channel: str) -> str:
    """Resolve any channel reference to a channel_id string.

    Raises ValueError if resolution fails.
    """
    # Strip whitespace and leading @
    channel = channel.strip()

    # Direct channel_id
    if _is_channel_id(channel):
        return channel

    # URL containing /channel/UCxxx
    if "/channel/" in channel:
        cid = _extract_channel_id_from_url(channel)
        if cid:
            return cid

    # Build URL to fetch if needed
    if channel.startswith("http"):
        url_to_fetch = channel
    elif channel.startswith("@"):
        url_to_fetch = f"https://www.youtube.com/{channel}"
    else:
        # Bare handle without @
        url_to_fetch = f"https://www.youtube.com/@{channel}"

    # Fetch the page and look for channel_id
    try:
        req = urllib.request.Request(
            url_to_fetch,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        )
        with urllib.request.urlopen(req, timeout=_YT_PAGE_TIMEOUT) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise ValueError(f"Failed to fetch YouTube page {url_to_fetch}: {exc}") from exc

    # Look for channel_id in page HTML (various patterns YouTube uses)
    patterns = [
        r'"channelId"\s*:\s*"(UC[A-Za-z0-9_\-]{20,30})"',
        r'"externalId"\s*:\s*"(UC[A-Za-z0-9_\-]{20,30})"',
        r'channel_id=(UC[A-Za-z0-9_\-]{20,30})',
        r'/channel/(UC[A-Za-z0-9_\-]{20,30})',
        # RSS link in page <head>
        r'feeds/videos\.xml\?channel_id=(UC[A-Za-z0-9_\-]{20,30})',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return m.group(1)

    raise ValueError(
        f"Could not resolve channel_id from: {channel!r}. "
        "Try providing the channel_id (UCxxx...) directly from the channel's 'About' page."
    )


# ── RSS fetching ───────────────────────────────────────────────────────────────


def _fetch_channel_videos(channel_id: str, limit: int = 15) -> List[Dict[str, Any]]:
    """Fetch latest videos for a channel via RSS. Returns list of video dicts."""
    rss_url = f"{_RSS_BASE}{urllib.parse.quote(channel_id)}"
    req = urllib.request.Request(
        rss_url,
        headers={"User-Agent": _USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=_RSS_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning("yt_reader: RSS fetch failed for %s: %s", channel_id, exc)
        raise

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"XML parse error for channel {channel_id}: {exc}") from exc

    # Parse Atom feed
    ns = {"atom": _ATOM_NS, "yt": _YT_NS, "media": _MEDIA_NS}

    channel_title = ""
    title_el = root.find("atom:title", ns)
    if title_el is not None and title_el.text:
        channel_title = title_el.text.strip()

    videos: List[Dict[str, Any]] = []
    for entry in root.findall("atom:entry", ns):
        vid_id_el = entry.find("yt:videoId", ns)
        vid_id = vid_id_el.text.strip() if vid_id_el is not None and vid_id_el.text else ""

        title_el2 = entry.find("atom:title", ns)
        title = title_el2.text.strip() if title_el2 is not None and title_el2.text else ""

        link_el = entry.find("atom:link", ns)
        link = link_el.get("href", "") if link_el is not None else ""

        published_el = entry.find("atom:published", ns)
        published = published_el.text.strip() if published_el is not None and published_el.text else ""

        # media:group/media:description
        description = ""
        media_group = entry.find("media:group", ns)
        if media_group is not None:
            desc_el = media_group.find("media:description", ns)
            if desc_el is not None and desc_el.text:
                description = desc_el.text.strip()[:500]

        if vid_id or title:
            videos.append({
                "video_id": vid_id,
                "title": title,
                "url": link or (f"https://www.youtube.com/watch?v={vid_id}" if vid_id else ""),
                "published": published,
                "description": description,
                "channel_id": channel_id,
                "channel_title": channel_title,
            })

        if len(videos) >= limit:
            break

    return videos


# ── Tool implementations ───────────────────────────────────────────────────────


def _yt_subscribe(ctx: ToolContext, channel: str, label: str = "") -> str:
    """Subscribe to a YouTube channel."""
    try:
        channel_id = _resolve_channel_id(channel)
    except ValueError as exc:
        return f"❌ {exc}"

    watchlist = _load_watchlist()

    if channel_id in watchlist:
        existing_label = watchlist[channel_id].get("label", channel_id)
        return f"Already subscribed to {existing_label} ({channel_id})"

    # Fetch once to get channel title and seed seen IDs
    try:
        videos = _fetch_channel_videos(channel_id, limit=15)
        channel_title = videos[0].get("channel_title", channel_id) if videos else channel_id
        seen_ids = [v["video_id"] for v in videos if v.get("video_id")]
    except Exception as exc:
        log.warning("yt_subscribe: initial fetch failed for %s: %s", channel_id, exc)
        channel_title = channel_id
        seen_ids = []

    effective_label = label or channel_title or channel_id
    watchlist[channel_id] = {
        "channel_id": channel_id,
        "label": effective_label,
        "last_seen_ids": seen_ids,
        "added_at": _utc_now(),
        "last_checked": _utc_now(),
        "video_count_seen": len(seen_ids),
    }
    _save_watchlist(watchlist)
    return (
        f"✅ Subscribed to **{effective_label}** ({channel_id})\n"
        f"Seeded with {len(seen_ids)} existing videos — future checks will return only new ones."
    )


def _yt_unsubscribe(ctx: ToolContext, channel_or_label: str) -> str:
    """Unsubscribe from a YouTube channel."""
    watchlist = _load_watchlist()
    key = channel_or_label.strip()

    # Try direct channel_id match
    if key in watchlist:
        label = watchlist[key].get("label", key)
        del watchlist[key]
        _save_watchlist(watchlist)
        return f"✅ Unsubscribed from {label} ({key})"

    # Try label match (case-insensitive)
    for cid, entry in list(watchlist.items()):
        if entry.get("label", "").lower() == key.lower():
            del watchlist[cid]
            _save_watchlist(watchlist)
            return f"✅ Unsubscribed from {entry.get('label', cid)} ({cid})"

    return f"❌ Not subscribed to: {key!r}"


def _yt_status(ctx: ToolContext) -> str:
    """List subscribed YouTube channels with last-check info."""
    watchlist = _load_watchlist()
    if not watchlist:
        return "No YouTube channels subscribed. Use yt_subscribe(channel) to add one."

    lines = [f"**YouTube watchlist** — {len(watchlist)} channel(s)\n"]
    for cid, entry in watchlist.items():
        label = entry.get("label", cid)
        last_checked = entry.get("last_checked", "never")[:16]
        seen_count = entry.get("video_count_seen", len(entry.get("last_seen_ids", [])))
        lines.append(f"• **{label}** (`{cid}`) — last checked {last_checked} UTC, {seen_count} seen")

    return "\n".join(lines)


def _yt_watchlist_check(ctx: ToolContext, limit: int = 5) -> List[Dict[str, Any]]:
    """Fetch new videos from all subscribed channels since last check.

    Updates watermarks so each video is returned only once.
    Returns a list of new video dicts, newest-first.
    """
    watchlist = _load_watchlist()
    if not watchlist:
        return []

    all_new: List[Dict[str, Any]] = []

    for cid, entry in watchlist.items():
        seen_ids: List[str] = entry.get("last_seen_ids", [])
        seen_set = set(seen_ids)
        try:
            videos = _fetch_channel_videos(cid, limit=max(limit * 3, 15))
        except Exception as exc:
            log.warning("yt_check: fetch failed for %s: %s", cid, exc)
            continue

        new_videos = [v for v in videos if v.get("video_id") and v["video_id"] not in seen_set]

        if new_videos:
            # Update watermark
            new_ids = [v["video_id"] for v in new_videos if v.get("video_id")]
            updated_seen = new_ids + seen_ids
            # Keep last 100 IDs to bound memory
            entry["last_seen_ids"] = updated_seen[:100]
            entry["last_checked"] = _utc_now()
            entry["video_count_seen"] = entry.get("video_count_seen", len(seen_ids)) + len(new_ids)
            all_new.extend(new_videos[:limit])
        else:
            entry["last_checked"] = _utc_now()

    _save_watchlist(watchlist)

    # Sort by published date descending
    def _pub_key(v: Dict[str, Any]) -> str:
        return v.get("published", "") or ""

    all_new.sort(key=_pub_key, reverse=True)
    return all_new


def _yt_latest(ctx: ToolContext, channel: str, limit: int = 10) -> str:
    """Get latest N videos from a channel without subscribing."""
    try:
        channel_id = _resolve_channel_id(channel)
    except ValueError as exc:
        return f"❌ {exc}"

    try:
        videos = _fetch_channel_videos(channel_id, limit=min(limit, 15))
    except Exception as exc:
        return f"❌ Failed to fetch videos: {exc}"

    if not videos:
        return f"No videos found for channel {channel_id}"

    channel_title = videos[0].get("channel_title", channel_id) if videos else channel_id
    lines = [f"**{channel_title}** — latest {len(videos)} video(s)\n"]
    for v in videos:
        pub = (v.get("published") or "")[:10]
        lines.append(f"• [{v.get('title', 'no title')}]({v.get('url', '')}) — {pub}")

    return "\n".join(lines)


# ── Public check helper (for inbox.py) ────────────────────────────────────────


def _yt_check_for_inbox(ctx: ToolContext, limit: int = 10) -> List[Dict[str, Any]]:
    """Lightweight entry point for inbox.py to collect new YouTube videos."""
    return _yt_watchlist_check(ctx, limit=limit)


# ── Tool registry ──────────────────────────────────────────────────────────────


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="yt_subscribe",
            description=(
                "Subscribe to a YouTube channel for new video tracking. "
                "Accepts: channel_id (UCxxx), @handle, channel URL. "
                "Seeds existing videos as seen — future yt_check calls return only NEW videos."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Channel ID (UCxxx), @handle, or full YouTube channel URL.",
                    },
                    "label": {
                        "type": "string",
                        "description": "Optional human-readable label (defaults to channel title).",
                    },
                },
                "required": ["channel"],
            },
            execute=lambda ctx, **kw: _yt_subscribe(ctx, **kw),
        ),
        ToolEntry(
            name="yt_unsubscribe",
            description="Unsubscribe from a YouTube channel.",
            parameters={
                "type": "object",
                "properties": {
                    "channel_or_label": {
                        "type": "string",
                        "description": "Channel ID or label to unsubscribe.",
                    },
                },
                "required": ["channel_or_label"],
            },
            execute=lambda ctx, **kw: _yt_unsubscribe(ctx, **kw),
        ),
        ToolEntry(
            name="yt_status",
            description="List subscribed YouTube channels with last-check timestamps and video counts.",
            parameters={"type": "object", "properties": {}},
            execute=lambda ctx, **kw: _yt_status(ctx, **kw),
        ),
        ToolEntry(
            name="yt_check",
            description=(
                "Fetch new videos from all subscribed YouTube channels since last check. "
                "Updates watermarks so each video is returned only once. "
                "Returns list of new videos with title, URL, published date."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max new videos per channel to return (default 5).",
                    },
                },
            },
            execute=lambda ctx, **kw: json.dumps(
                _yt_watchlist_check(ctx, **kw), ensure_ascii=False, indent=2
            ),
        ),
        ToolEntry(
            name="yt_latest",
            description=(
                "Get the latest N videos from a YouTube channel WITHOUT subscribing. "
                "Good for one-off checks. Accepts channel_id, @handle, or URL."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Channel ID (UCxxx), @handle, or full YouTube channel URL.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of latest videos to return (default 10, max 15).",
                    },
                },
                "required": ["channel"],
            },
            execute=lambda ctx, **kw: _yt_latest(ctx, **kw),
        ),
    ]
