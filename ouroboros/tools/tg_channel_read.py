"""tg_channel_read — read public Telegram channel posts via t.me/s/ web preview.

No API key needed. Works for any public channel.
Returns structured list of posts: id, date, text, views, reactions, links.

Usage:
    tg_channel_read(channel="abstractDL")                  # last ~20 posts
    tg_channel_read(channel="abstractDL", limit=50)        # up to 50 posts
    tg_channel_read(channel="abstractDL", since_post_id=358)  # posts >= 358
    tg_channel_read(channel="abstractDL", before_post_id=400) # posts < 400
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 20
_MAX_POSTS_PER_REQUEST = 200
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

# ── HTML parsing ──────────────────────────────────────────────────────────────

_POST_BLOCK_RE = re.compile(
    r'<div class="tgme_widget_message_wrap[^"]*"[^>]*>(.*?)'
    r'(?=<div class="tgme_widget_message_wrap|$)',
    re.DOTALL,
)
_POST_ID_RE = re.compile(r'data-post="[^/]+/(\d+)"')
_DATE_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
_VIEWS_RE = re.compile(r'<span class="tgme_widget_message_views[^"]*"[^>]*>([^<]+)</span>')
_TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL,
)
_LINK_RE = re.compile(r'href="(https?://[^"]+)"')
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_views(raw: str) -> int:
    raw = raw.strip().upper()
    if not raw:
        return 0
    try:
        if raw.endswith("K"):
            return int(float(raw[:-1]) * 1_000)
        if raw.endswith("M"):
            return int(float(raw[:-1]) * 1_000_000)
        return int(raw.replace(",", "").replace(" ", ""))
    except (ValueError, TypeError):
        return 0


def _parse_posts(html_body: str) -> List[Dict[str, Any]]:
    """Extract structured posts from t.me/s/ HTML."""
    posts = []
    for block_m in _POST_BLOCK_RE.finditer(html_body):
        block = block_m.group(0)

        id_m = _POST_ID_RE.search(block)
        if not id_m:
            continue
        post_id = int(id_m.group(1))

        date_m = _DATE_RE.search(block)
        date = date_m.group(1) if date_m else ""

        text_m = _TEXT_RE.search(block)
        text = _strip_tags(text_m.group(1)) if text_m else ""

        views_m = _VIEWS_RE.search(block)
        views = _parse_views(views_m.group(1)) if views_m else 0

        links = list({
            href for href in _LINK_RE.findall(block)
            if not href.startswith("https://t.me")
            and not href.startswith("https://telegram.org")
        })

        posts.append({
            "id": post_id,
            "date": date,
            "text": text,
            "views": views,
            "links": links,
        })
    return posts


# ── Fetch ─────────────────────────────────────────────────────────────────────

def _fetch_page(channel: str, params: str = "") -> str:
    base = f"https://t.me/s/{channel}"
    url = f"{base}{params}" if params else base
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ── Tool implementation ───────────────────────────────────────────────────────

def _tg_channel_read(
    ctx: ToolContext,
    channel: str,
    limit: int = 20,
    since_post_id: int = 0,
    before_post_id: int = 0,
) -> str:
    """Read posts from a public Telegram channel."""
    channel = channel.lstrip("@").strip()
    if not channel:
        return json.dumps({"error": "channel must not be empty"})

    limit = max(1, min(limit, _MAX_POSTS_PER_REQUEST))

    collected: List[Dict[str, Any]] = []
    seen_ids: set = set()

    params = f"?before={before_post_id}" if before_post_id else ""

    try:
        for _ in range(10):  # max 10 pages
            if len(collected) >= limit:
                break

            try:
                body = _fetch_page(channel, params)
            except urllib.error.HTTPError as e:
                if not collected:
                    return json.dumps({"error": f"HTTP {e.code}: {e.reason}"})
                break
            except urllib.error.URLError as e:
                if not collected:
                    return json.dumps({"error": f"Network error: {e.reason}"})
                break

            page_posts = _parse_posts(body)
            if not page_posts:
                break

            new_posts = [p for p in page_posts if p["id"] not in seen_ids]
            for p in new_posts:
                seen_ids.add(p["id"])

            if since_post_id > 0:
                new_posts = [p for p in new_posts if p["id"] >= since_post_id]

            collected.extend(new_posts)

            min_id_on_page = min(p["id"] for p in page_posts)
            if since_post_id > 0 and min_id_on_page < since_post_id:
                break

            oldest_id = min(p["id"] for p in page_posts)
            params = f"?before={oldest_id}"
            time.sleep(0.3)

    except Exception as exc:
        log.exception("tg_channel_read error")
        return json.dumps({"error": str(exc)})

    collected.sort(key=lambda p: p["id"])
    collected = collected[:limit]

    return json.dumps({
        "channel": channel,
        "posts_count": len(collected),
        "posts": collected,
    }, ensure_ascii=False)


# ── Registry ──────────────────────────────────────────────────────────────────

_SCHEMA = {
    "name": "tg_channel_read",
    "description": (
        "Read posts from a public Telegram channel via t.me/s/ web preview. "
        "No API key required. Returns post id, date, text, views, and external links. "
        "Supports filtering by post id and pagination.\n\n"
        "Parameters:\n"
        "- channel: username without @, e.g. 'abstractDL'\n"
        "- limit: max posts to return (1–200, default 20)\n"
        "- since_post_id: return only posts with id >= this value\n"
        "- before_post_id: start fetching from posts before this id"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "channel": {
                "type": "string",
                "description": "Channel username (without @), e.g. 'abstractDL'",
            },
            "limit": {
                "type": "integer",
                "description": "Max posts to return (1–200, default 20)",
                "default": 20,
            },
            "since_post_id": {
                "type": "integer",
                "description": "Return only posts with id >= this value (0 = no filter)",
                "default": 0,
            },
            "before_post_id": {
                "type": "integer",
                "description": "Start fetching from posts before this id (0 = latest)",
                "default": 0,
            },
        },
        "required": ["channel"],
    },
}


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="tg_channel_read",
            schema=_SCHEMA,
            handler=lambda ctx, **kw: _tg_channel_read(ctx, **kw),
        )
    ]
