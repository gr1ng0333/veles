"""hn_reader — Hacker News reader via Algolia HN API (no API key required).

Provides structured access to HN front page, Ask HN, Show HN, and full-text
search — plus a lightweight keyword watchlist for monitoring topics.

No external dependencies — pure Python stdlib (urllib + json).

Storage: /opt/veles-data/memory/hn_watchlist.json
Each keyword entry stores:
  - keyword: monitored term
  - added_at: ISO timestamp
  - last_checked: ISO timestamp
  - last_seen_ids: set of story objectIDs already delivered

Tools:
    hn_top(limit?, type?)          — fetch front page / best / new stories
    hn_search(query, limit?)       — full-text search across HN
    hn_watchlist_add(keyword)      — add keyword to watchlist
    hn_watchlist_remove(keyword)   — remove keyword from watchlist
    hn_watchlist_status()          — list active keywords
    hn_watchlist_check(limit?)     — fetch new stories matching any keyword

Algolia HN API reference: https://hn.algolia.com/api
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")
_WATCHLIST_FILE = "memory/hn_watchlist.json"
_API_BASE = "https://hn.algolia.com/api/v1"
_TIMEOUT = 20
_MAX_ITEMS = 50
_MAX_SEEN_IDS = 1000  # cap stored IDs per keyword

# Rate limiting — HN Algolia is polite, but don't hammer it
_rate_lock = threading.Lock()
_last_request_time: float = 0.0
_RATE_LIMIT_SEC = 0.5

_USER_AGENT = "Mozilla/5.0 (compatible; VelesBot/1.0; +https://github.com/gr1ng0333/veles)"


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _hn_get(path: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """GET from Algolia HN API. Returns parsed JSON or None on error."""
    global _last_request_time  # noqa: PLW0603

    with _rate_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < _RATE_LIMIT_SEC:
            time.sleep(_RATE_LIMIT_SEC - elapsed)
        _last_request_time = time.monotonic()

    url = f"{_API_BASE}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        log.warning("HN API HTTP %d: %s", exc.code, url)
        return None
    except Exception as exc:
        log.warning("HN API error (%s): %s", exc, url)
        return None


# ---------------------------------------------------------------------------
# Story parsing
# ---------------------------------------------------------------------------

def _parse_hit(hit: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize an Algolia hit to a standard story dict."""
    oid = str(hit.get("objectID") or hit.get("story_id") or "")
    title = (hit.get("title") or "").strip()
    url = (hit.get("url") or f"https://news.ycombinator.com/item?id={oid}").strip()
    author = (hit.get("author") or "").strip()
    points = int(hit.get("points") or 0)
    comments = int(hit.get("num_comments") or 0)
    created_at = (hit.get("created_at") or "").strip()
    story_id = oid

    return {
        "id": story_id,
        "title": title,
        "url": url,
        "author": author,
        "points": points,
        "comments": comments,
        "date": created_at,
        "hn_url": f"https://news.ycombinator.com/item?id={story_id}",
        "source_type": "hn",
        "source_name": "hacker_news",
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _watchlist_path() -> pathlib.Path:
    return pathlib.Path(_DRIVE_ROOT) / _WATCHLIST_FILE


def _load_watchlist() -> Dict[str, Any]:
    path = _watchlist_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_watchlist(data: Dict[str, Any]) -> None:
    path = _watchlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _slug_keyword(kw: str) -> str:
    return re.sub(r"\s+", " ", kw.strip().lower())[:100]


# ---------------------------------------------------------------------------
# Tool: hn_top
# ---------------------------------------------------------------------------

def _hn_top(
    ctx: ToolContext,
    limit: int = 20,
    story_type: str = "front_page",
) -> str:
    """Fetch top/front-page stories from Hacker News.

    Args:
        limit: number of stories to return (1–50, default 20)
        story_type: 'front_page' | 'story' | 'ask_hn' | 'show_hn' | 'job' (default: front_page)
    """
    limit = max(1, min(limit, _MAX_ITEMS))
    valid_types = {"front_page", "story", "ask_hn", "show_hn", "job"}
    tag = story_type if story_type in valid_types else "front_page"

    data = _hn_get("search_by_date", {
        "tags": tag,
        "hitsPerPage": limit,
    })

    if data is None:
        return json.dumps({"error": "HN API unavailable", "stories": []})

    hits = data.get("hits") or []
    stories = [_parse_hit(h) for h in hits]

    return json.dumps({
        "type": tag,
        "count": len(stories),
        "stories": stories,
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tool: hn_search
# ---------------------------------------------------------------------------

def _hn_search(
    ctx: ToolContext,
    query: str,
    limit: int = 10,
    sort_by: str = "relevance",
) -> str:
    """Search Hacker News stories by keyword.

    Args:
        query: search query string
        limit: number of results (1–50, default 10)
        sort_by: 'relevance' | 'date' (default: relevance)
    """
    if not query or not query.strip():
        return json.dumps({"error": "query must not be empty", "results": []})

    limit = max(1, min(limit, _MAX_ITEMS))
    endpoint = "search" if sort_by == "relevance" else "search_by_date"

    data = _hn_get(endpoint, {
        "query": query.strip(),
        "tags": "story",
        "hitsPerPage": limit,
    })

    if data is None:
        return json.dumps({"error": "HN API unavailable", "query": query, "results": []})

    hits = data.get("hits") or []
    stories = [_parse_hit(h) for h in hits]

    return json.dumps({
        "query": query,
        "sort_by": sort_by,
        "count": len(stories),
        "results": stories,
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tool: hn_watchlist_add
# ---------------------------------------------------------------------------

def _hn_watchlist_add(ctx: ToolContext, keyword: str) -> str:
    """Add a keyword to the HN monitoring watchlist.

    Future calls to hn_watchlist_check() will return new stories
    matching this keyword since last check.
    """
    keyword = _slug_keyword(keyword)
    if not keyword:
        return json.dumps({"error": "keyword must not be empty"})

    watchlist = _load_watchlist()

    if keyword in watchlist:
        return json.dumps({
            "status": "already_exists",
            "keyword": keyword,
            "message": f"'{keyword}' is already in the watchlist.",
        })

    # Seed with current stories (mark as seen so we only get new ones next time)
    data = _hn_get("search_by_date", {
        "query": keyword,
        "tags": "story",
        "hitsPerPage": 20,
    })
    initial_ids = []
    if data:
        initial_ids = [str(h.get("objectID") or "") for h in data.get("hits") or [] if h.get("objectID")]

    watchlist[keyword] = {
        "keyword": keyword,
        "added_at": _utc_now(),
        "last_checked": _utc_now(),
        "last_seen_ids": initial_ids,
    }
    _save_watchlist(watchlist)

    return json.dumps({
        "status": "added",
        "keyword": keyword,
        "seeded_count": len(initial_ids),
        "message": (
            f"Added '{keyword}' to HN watchlist "
            f"({len(initial_ids)} current stories marked as seen). "
            "Call hn_watchlist_check() to get new stories as they appear."
        ),
    })


# ---------------------------------------------------------------------------
# Tool: hn_watchlist_remove
# ---------------------------------------------------------------------------

def _hn_watchlist_remove(ctx: ToolContext, keyword: str) -> str:
    """Remove a keyword from the HN watchlist."""
    keyword = _slug_keyword(keyword)
    watchlist = _load_watchlist()

    if keyword not in watchlist:
        return json.dumps({"status": "not_found", "keyword": keyword})

    del watchlist[keyword]
    _save_watchlist(watchlist)

    return json.dumps({"status": "removed", "keyword": keyword})


# ---------------------------------------------------------------------------
# Tool: hn_watchlist_status
# ---------------------------------------------------------------------------

def _hn_watchlist_status(ctx: ToolContext) -> str:
    """List active HN watchlist keywords with last-check info."""
    watchlist = _load_watchlist()

    if not watchlist:
        return json.dumps({
            "keywords": [],
            "count": 0,
            "message": "No HN keywords being monitored. Use hn_watchlist_add(keyword=...) to add one.",
        })

    entries = []
    for kw, meta in sorted(watchlist.items()):
        entries.append({
            "keyword": kw,
            "added_at": meta.get("added_at"),
            "last_checked": meta.get("last_checked"),
            "seen_count": len(meta.get("last_seen_ids") or []),
        })

    return json.dumps({"keywords": entries, "count": len(entries)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool: hn_watchlist_check
# ---------------------------------------------------------------------------

def _hn_watchlist_check(ctx: ToolContext, limit_per_keyword: int = 20) -> str:
    """Fetch new HN stories matching any monitored keyword since last check.

    Returns only stories not seen since the keyword was added or last checked.
    Updates watermarks after fetching.

    Args:
        limit_per_keyword: max new stories per keyword (1–50, default 20)
    """
    watchlist = _load_watchlist()

    if not watchlist:
        return json.dumps({
            "total_new": 0,
            "keywords_checked": 0,
            "message": "No HN keywords being monitored.",
            "stories": [],
        })

    limit_per_keyword = max(1, min(limit_per_keyword, _MAX_ITEMS))
    all_new: List[Dict[str, Any]] = []
    keyword_summary: Dict[str, int] = {}

    for keyword, meta in sorted(watchlist.items()):
        seen_ids = set(meta.get("last_seen_ids") or [])

        data = _hn_get("search_by_date", {
            "query": keyword,
            "tags": "story",
            "hitsPerPage": limit_per_keyword + 10,  # slight overfetch to account for seen
        })

        if data is None:
            keyword_summary[keyword] = 0
            continue

        hits = data.get("hits") or []
        new_hits = [h for h in hits if str(h.get("objectID") or "") not in seen_ids]
        new_stories = [_parse_hit(h) for h in new_hits[:limit_per_keyword]]

        for story in new_stories:
            story["matched_keyword"] = keyword

        all_new.extend(new_stories)
        keyword_summary[keyword] = len(new_stories)

        # Update watermark
        all_ids = [str(h.get("objectID") or "") for h in hits if h.get("objectID")]
        updated_seen = list(set(seen_ids) | set(all_ids))
        if len(updated_seen) > _MAX_SEEN_IDS:
            updated_seen = updated_seen[-_MAX_SEEN_IDS:]

        watchlist[keyword]["last_checked"] = _utc_now()
        watchlist[keyword]["last_seen_ids"] = updated_seen

    _save_watchlist(watchlist)

    # Sort by date ascending (oldest new first)
    def _sort_key(s: Dict[str, Any]) -> str:
        return s.get("date") or ""

    all_new.sort(key=_sort_key)

    return json.dumps({
        "total_new": len(all_new),
        "keywords_checked": len(watchlist),
        "keyword_summary": keyword_summary,
        "stories": all_new,
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_TOP_SCHEMA = {
    "name": "hn_top",
    "description": (
        "Fetch top/front-page stories from Hacker News via Algolia API (no key required).\n\n"
        "Parameters:\n"
        "- limit: number of stories (1–50, default 20)\n"
        "- story_type: 'front_page' | 'story' | 'ask_hn' | 'show_hn' | 'job' (default: front_page)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Number of stories (1–50, default 20)", "default": 20},
            "story_type": {
                "type": "string",
                "description": "Story type: front_page | story | ask_hn | show_hn | job",
                "enum": ["front_page", "story", "ask_hn", "show_hn", "job"],
            },
        },
        "required": [],
    },
}

_SEARCH_SCHEMA = {
    "name": "hn_search",
    "description": (
        "Search Hacker News stories by keyword via Algolia full-text API (no key required).\n\n"
        "Parameters:\n"
        "- query: search term (required)\n"
        "- limit: number of results (1–50, default 10)\n"
        "- sort_by: 'relevance' | 'date' (default: relevance)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "limit": {"type": "integer", "description": "Number of results (1–50, default 10)", "default": 10},
            "sort_by": {
                "type": "string",
                "description": "Sort order: relevance | date",
                "enum": ["relevance", "date"],
            },
        },
        "required": ["query"],
    },
}

_WATCHLIST_ADD_SCHEMA = {
    "name": "hn_watchlist_add",
    "description": (
        "Add a keyword to the HN monitoring watchlist. "
        "Future hn_watchlist_check() calls will return new stories matching this keyword.\n"
        "Current stories are marked as seen (no flood of historical items)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "Keyword to monitor (e.g. 'LLM agents', 'rust async')"},
        },
        "required": ["keyword"],
    },
}

_WATCHLIST_REMOVE_SCHEMA = {
    "name": "hn_watchlist_remove",
    "description": "Remove a keyword from the HN watchlist.",
    "parameters": {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "Keyword to remove"},
        },
        "required": ["keyword"],
    },
}

_WATCHLIST_STATUS_SCHEMA = {
    "name": "hn_watchlist_status",
    "description": "List active HN watchlist keywords with last-check timestamps.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

_WATCHLIST_CHECK_SCHEMA = {
    "name": "hn_watchlist_check",
    "description": (
        "Fetch new HN stories matching any monitored keyword since last check. "
        "Updates watermarks so repeated calls return only truly new stories.\n\n"
        "Parameters:\n"
        "- limit_per_keyword: max new stories per keyword (1–50, default 20)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit_per_keyword": {
                "type": "integer",
                "description": "Max new stories per keyword (1–50, default 20)",
                "default": 20,
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="hn_top",
            schema=_TOP_SCHEMA,
            handler=lambda ctx, **kw: _hn_top(ctx, **kw),
        ),
        ToolEntry(
            name="hn_search",
            schema=_SEARCH_SCHEMA,
            handler=lambda ctx, **kw: _hn_search(ctx, **kw),
        ),
        ToolEntry(
            name="hn_watchlist_add",
            schema=_WATCHLIST_ADD_SCHEMA,
            handler=lambda ctx, **kw: _hn_watchlist_add(ctx, **kw),
        ),
        ToolEntry(
            name="hn_watchlist_remove",
            schema=_WATCHLIST_REMOVE_SCHEMA,
            handler=lambda ctx, **kw: _hn_watchlist_remove(ctx, **kw),
        ),
        ToolEntry(
            name="hn_watchlist_status",
            schema=_WATCHLIST_STATUS_SCHEMA,
            handler=lambda ctx, **kw: _hn_watchlist_status(ctx),
        ),
        ToolEntry(
            name="hn_watchlist_check",
            schema=_WATCHLIST_CHECK_SCHEMA,
            handler=lambda ctx, **kw: _hn_watchlist_check(ctx, **kw),
        ),
    ]
