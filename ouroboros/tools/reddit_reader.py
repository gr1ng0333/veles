"""reddit_reader — Reddit subreddit monitor via public JSON API (no auth required).

Provides structured access to any public subreddit's posts — new, hot, top, rising —
plus a lightweight subreddit watchlist for monitoring topics across multiple subs.

No API key required — uses Reddit's anonymous JSON endpoint.

Storage: /opt/veles-data/memory/reddit_watchlist.json
Each subreddit entry stores:
  - subreddit: monitored sub name (e.g. "MachineLearning")
  - flair_filter: optional flair text filter (case-insensitive)
  - added_at: ISO timestamp
  - last_checked: ISO timestamp
  - last_seen_ids: set of post IDs already delivered

Tools:
    reddit_posts(subreddit, sort?, limit?, flair?)  — fetch posts from a subreddit
    reddit_search(query, subreddit?, limit?)         — search Reddit posts
    reddit_watchlist_add(subreddit, flair?)          — add sub to watchlist
    reddit_watchlist_remove(subreddit)               — remove sub from watchlist
    reddit_watchlist_status()                        — list active subs
    reddit_watchlist_check(limit?)                   — fetch new posts since last check

Reddit JSON API:
    https://www.reddit.com/r/{sub}/{sort}.json?limit=N&after=...
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
_WATCHLIST_FILE = "memory/reddit_watchlist.json"
_API_BASE = "https://www.reddit.com"
_TIMEOUT = 20
_MAX_ITEMS = 50
_MAX_SEEN_IDS = 2000  # cap stored IDs per subreddit

# Rate limiting — Reddit asks for 1 req/sec for bots
_rate_lock = threading.Lock()
_last_request_time: float = 0.0
_RATE_LIMIT_SEC = 1.1  # slightly above 1 req/sec

_USER_AGENT = "VelesBot/1.0 (monitoring bot; +https://github.com/gr1ng0333/veles)"


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _reddit_get(path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """GET from Reddit JSON API. Returns parsed JSON or None on error."""
    global _last_request_time  # noqa: PLW0603

    with _rate_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < _RATE_LIMIT_SEC:
            time.sleep(_RATE_LIMIT_SEC - elapsed)
        _last_request_time = time.monotonic()

    query = ""
    if params:
        query = "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{_API_BASE}/{path.lstrip('/')}{query}"

    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        log.warning("Reddit API HTTP %d: %s", exc.code, url)
        return None
    except Exception as exc:
        log.warning("Reddit API error (%s): %s", exc, url)
        return None


# ---------------------------------------------------------------------------
# Post parsing
# ---------------------------------------------------------------------------

def _parse_post(child: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a Reddit post child to a standard post dict."""
    d = child.get("data") or {}
    post_id = str(d.get("id") or "")
    fullname = str(d.get("name") or f"t3_{post_id}")
    title = (d.get("title") or "").strip()
    author = str(d.get("author") or "")
    subreddit = str(d.get("subreddit") or "")
    score = int(d.get("score") or 0)
    num_comments = int(d.get("num_comments") or 0)
    flair = str(d.get("link_flair_text") or "").strip()
    is_self = bool(d.get("is_self"))
    selftext = (d.get("selftext") or "").strip()

    # Prefer external link, fallback to Reddit permalink
    url = (d.get("url") or "").strip()
    permalink = f"https://www.reddit.com{d.get('permalink', '')}"
    if is_self or not url or "reddit.com" in url:
        url = permalink

    created_utc = d.get("created_utc")
    if created_utc:
        try:
            date_str = datetime.fromtimestamp(float(created_utc), tz=timezone.utc).isoformat()
        except (ValueError, TypeError):
            date_str = ""
    else:
        date_str = ""

    text = selftext[:500] if selftext else ""

    return {
        "id": fullname,
        "post_id": post_id,
        "title": title,
        "author": author,
        "subreddit": subreddit,
        "flair": flair,
        "score": score,
        "comments": num_comments,
        "url": url,
        "reddit_url": permalink,
        "text": text,
        "date": date_str,
        "source_type": "reddit",
        "source_name": subreddit,
    }


def _extract_children(data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract post list from Reddit API response."""
    if not data:
        return []
    listing = data.get("data") or {}
    children = listing.get("children") or []
    return [_parse_post(c) for c in children if c.get("kind") == "t3"]


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


def _slug_subreddit(name: str) -> str:
    """Normalize subreddit name (strip r/ prefix, lowercase, alphanumeric+_)."""
    name = re.sub(r"^r/", "", name.strip().lower())
    name = re.sub(r"[^a-z0-9_]", "", name)
    return name[:100]


# ---------------------------------------------------------------------------
# Tool: reddit_posts
# ---------------------------------------------------------------------------

def _reddit_posts(
    ctx: ToolContext,
    subreddit: str,
    sort: str = "new",
    limit: int = 20,
    flair: Optional[str] = None,
) -> str:
    """Fetch posts from a subreddit.

    Args:
        subreddit: subreddit name (e.g. 'MachineLearning' or 'r/Python')
        sort: 'new' | 'hot' | 'top' | 'rising' | 'best' (default: new)
        limit: number of posts (1–50, default 20)
        flair: optional flair text filter (case-insensitive substring match)
    """
    sub = _slug_subreddit(subreddit)
    if not sub:
        return json.dumps({"error": "subreddit name is required", "posts": []})

    limit = max(1, min(limit, _MAX_ITEMS))
    valid_sorts = {"new", "hot", "top", "rising", "best"}
    sort = sort if sort in valid_sorts else "new"

    data = _reddit_get(f"r/{sub}/{sort}.json", {"limit": limit})

    if data is None:
        return json.dumps({"error": f"Reddit API unavailable for r/{sub}", "posts": []})

    posts = _extract_children(data)

    if flair:
        flair_lower = flair.lower()
        posts = [p for p in posts if flair_lower in p.get("flair", "").lower()]

    return json.dumps({
        "subreddit": sub,
        "sort": sort,
        "count": len(posts),
        "posts": posts,
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tool: reddit_search
# ---------------------------------------------------------------------------

def _reddit_search(
    ctx: ToolContext,
    query: str,
    subreddit: Optional[str] = None,
    limit: int = 10,
    sort: str = "relevance",
) -> str:
    """Search Reddit posts by keyword.

    Args:
        query: search query string (required)
        subreddit: optional subreddit to restrict search (default: all of Reddit)
        limit: number of results (1–50, default 10)
        sort: 'relevance' | 'new' | 'top' | 'hot' (default: relevance)
    """
    if not query or not query.strip():
        return json.dumps({"error": "query must not be empty", "results": []})

    limit = max(1, min(limit, _MAX_ITEMS))
    valid_sorts = {"relevance", "new", "top", "hot"}
    sort = sort if sort in valid_sorts else "relevance"

    if subreddit:
        sub = _slug_subreddit(subreddit)
        path = f"r/{sub}/search.json"
        params: Dict[str, Any] = {"q": query.strip(), "restrict_sr": "1", "limit": limit, "sort": sort}
    else:
        path = "search.json"
        params = {"q": query.strip(), "limit": limit, "sort": sort}

    data = _reddit_get(path, params)

    if data is None:
        return json.dumps({"error": "Reddit search API unavailable", "query": query, "results": []})

    posts = _extract_children(data)

    return json.dumps({
        "query": query,
        "subreddit": subreddit,
        "sort": sort,
        "count": len(posts),
        "results": posts,
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tool: reddit_watchlist_add
# ---------------------------------------------------------------------------

def _reddit_watchlist_add(
    ctx: ToolContext,
    subreddit: str,
    flair: Optional[str] = None,
) -> str:
    """Add a subreddit to the Reddit monitoring watchlist.

    Future calls to reddit_watchlist_check() will return new posts from this sub.

    Args:
        subreddit: subreddit name (e.g. 'MachineLearning')
        flair: optional flair filter — only track posts with this flair text
    """
    sub = _slug_subreddit(subreddit)
    if not sub:
        return json.dumps({"error": "subreddit name is required"})

    watchlist = _load_watchlist()

    if sub in watchlist:
        return json.dumps({
            "status": "already_exists",
            "subreddit": sub,
            "message": f"r/{sub} is already in the watchlist.",
        })

    # Validate subreddit exists and seed seen IDs
    data = _reddit_get(f"r/{sub}/new.json", {"limit": 25})
    if data is None:
        return json.dumps({
            "error": f"Could not reach r/{sub} — subreddit may not exist or Reddit is down",
        })

    initial_posts = _extract_children(data)
    initial_ids = [p["id"] for p in initial_posts if p.get("id")]

    watchlist[sub] = {
        "subreddit": sub,
        "flair_filter": flair or None,
        "added_at": _utc_now(),
        "last_checked": _utc_now(),
        "last_seen_ids": initial_ids,
    }
    _save_watchlist(watchlist)

    msg = f"Added r/{sub} to Reddit watchlist ({len(initial_ids)} current posts marked as seen)."
    if flair:
        msg += f" Flair filter: '{flair}'."
    msg += " Call reddit_watchlist_check() to get new posts."

    return json.dumps({
        "status": "added",
        "subreddit": sub,
        "flair_filter": flair,
        "seeded_count": len(initial_ids),
        "message": msg,
    })


# ---------------------------------------------------------------------------
# Tool: reddit_watchlist_remove
# ---------------------------------------------------------------------------

def _reddit_watchlist_remove(ctx: ToolContext, subreddit: str) -> str:
    """Remove a subreddit from the Reddit watchlist."""
    sub = _slug_subreddit(subreddit)
    watchlist = _load_watchlist()

    if sub not in watchlist:
        return json.dumps({"status": "not_found", "subreddit": sub})

    del watchlist[sub]
    _save_watchlist(watchlist)
    return json.dumps({"status": "removed", "subreddit": sub})


# ---------------------------------------------------------------------------
# Tool: reddit_watchlist_status
# ---------------------------------------------------------------------------

def _reddit_watchlist_status(ctx: ToolContext) -> str:
    """List active Reddit watchlist subreddits with last-check info."""
    watchlist = _load_watchlist()

    if not watchlist:
        return json.dumps({
            "subs": [],
            "count": 0,
            "message": "No subreddits being monitored. Use reddit_watchlist_add(subreddit=...) to add one.",
        })

    entries = []
    for sub, meta in sorted(watchlist.items()):
        entries.append({
            "subreddit": sub,
            "flair_filter": meta.get("flair_filter"),
            "added_at": meta.get("added_at"),
            "last_checked": meta.get("last_checked"),
            "seen_count": len(meta.get("last_seen_ids") or []),
        })

    return json.dumps({"subs": entries, "count": len(entries)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool: reddit_watchlist_check
# ---------------------------------------------------------------------------

def _reddit_watchlist_check(ctx: ToolContext, limit_per_sub: int = 20) -> str:
    """Fetch new Reddit posts from all monitored subreddits since last check.

    Returns only posts not seen since the sub was added or last checked.
    Updates watermarks after fetching.

    Args:
        limit_per_sub: max new posts per subreddit (1–50, default 20)
    """
    watchlist = _load_watchlist()

    if not watchlist:
        return json.dumps({
            "total_new": 0,
            "subs_checked": 0,
            "message": "No subreddits being monitored.",
            "posts": [],
        })

    limit_per_sub = max(1, min(limit_per_sub, _MAX_ITEMS))
    all_new: List[Dict[str, Any]] = []
    sub_summary: Dict[str, int] = {}

    for sub, meta in sorted(watchlist.items()):
        seen_ids = set(meta.get("last_seen_ids") or [])
        flair_filter = meta.get("flair_filter")

        # Fetch recent posts (slightly overfetch to account for seen)
        data = _reddit_get(f"r/{sub}/new.json", {"limit": limit_per_sub + 15})
        if data is None:
            sub_summary[sub] = 0
            log.warning("reddit_watchlist_check: could not fetch r/%s", sub)
            continue

        posts = _extract_children(data)

        # Apply flair filter if set
        if flair_filter:
            flair_lower = flair_filter.lower()
            posts = [p for p in posts if flair_lower in p.get("flair", "").lower()]

        # Filter unseen
        new_posts = [p for p in posts if p.get("id") and p["id"] not in seen_ids]
        new_posts = new_posts[:limit_per_sub]

        for post in new_posts:
            post["matched_subreddit"] = sub

        all_new.extend(new_posts)
        sub_summary[sub] = len(new_posts)

        # Update watermark
        all_ids = [p["id"] for p in posts if p.get("id")]
        updated_seen = list(set(seen_ids) | set(all_ids))
        if len(updated_seen) > _MAX_SEEN_IDS:
            updated_seen = updated_seen[-_MAX_SEEN_IDS:]

        watchlist[sub]["last_checked"] = _utc_now()
        watchlist[sub]["last_seen_ids"] = updated_seen

    _save_watchlist(watchlist)

    # Sort oldest-first
    all_new.sort(key=lambda p: p.get("date") or "")

    return json.dumps({
        "total_new": len(all_new),
        "subs_checked": len(watchlist),
        "sub_summary": sub_summary,
        "posts": all_new,
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_POSTS_SCHEMA = {
    "name": "reddit_posts",
    "description": (
        "Fetch posts from a public subreddit via Reddit JSON API (no auth required).\n\n"
        "Parameters:\n"
        "- subreddit: subreddit name (e.g. 'MachineLearning', 'r/Python') — required\n"
        "- sort: 'new' | 'hot' | 'top' | 'rising' | 'best' (default: new)\n"
        "- limit: number of posts (1–50, default 20)\n"
        "- flair: optional flair text filter (case-insensitive substring)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "subreddit": {"type": "string", "description": "Subreddit name (e.g. 'MachineLearning')"},
            "sort": {
                "type": "string",
                "description": "Sort order: new | hot | top | rising | best",
                "enum": ["new", "hot", "top", "rising", "best"],
            },
            "limit": {"type": "integer", "description": "Number of posts (1–50, default 20)", "default": 20},
            "flair": {"type": "string", "description": "Optional flair text filter (case-insensitive)"},
        },
        "required": ["subreddit"],
    },
}

_SEARCH_SCHEMA = {
    "name": "reddit_search",
    "description": (
        "Search Reddit posts by keyword via Reddit JSON API (no auth required).\n\n"
        "Parameters:\n"
        "- query: search query (required)\n"
        "- subreddit: optional subreddit to restrict search (default: all Reddit)\n"
        "- limit: number of results (1–50, default 10)\n"
        "- sort: 'relevance' | 'new' | 'top' | 'hot' (default: relevance)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query (required)"},
            "subreddit": {"type": "string", "description": "Optional subreddit to restrict search"},
            "limit": {"type": "integer", "description": "Number of results (1–50, default 10)", "default": 10},
            "sort": {
                "type": "string",
                "description": "Sort order: relevance | new | top | hot",
                "enum": ["relevance", "new", "top", "hot"],
            },
        },
        "required": ["query"],
    },
}

_WATCHLIST_ADD_SCHEMA = {
    "name": "reddit_watchlist_add",
    "description": (
        "Add a subreddit to the Reddit monitoring watchlist. "
        "Future reddit_watchlist_check() calls will return new posts from this sub.\n\n"
        "Parameters:\n"
        "- subreddit: subreddit name (required)\n"
        "- flair: optional flair text filter — only deliver posts matching this flair"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "subreddit": {"type": "string", "description": "Subreddit name (e.g. 'MachineLearning')"},
            "flair": {"type": "string", "description": "Optional flair filter (case-insensitive substring)"},
        },
        "required": ["subreddit"],
    },
}

_WATCHLIST_REMOVE_SCHEMA = {
    "name": "reddit_watchlist_remove",
    "description": "Remove a subreddit from the Reddit monitoring watchlist.",
    "parameters": {
        "type": "object",
        "properties": {
            "subreddit": {"type": "string", "description": "Subreddit name to remove"},
        },
        "required": ["subreddit"],
    },
}

_WATCHLIST_STATUS_SCHEMA = {
    "name": "reddit_watchlist_status",
    "description": "List active Reddit watchlist subreddits with last-check timestamps and seen counts.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

_WATCHLIST_CHECK_SCHEMA = {
    "name": "reddit_watchlist_check",
    "description": (
        "Fetch new posts from all monitored Reddit subreddits since last check.\n\n"
        "Returns only posts not seen since the sub was added or last checked. "
        "Updates watermarks after fetching.\n\n"
        "Parameters:\n"
        "- limit_per_sub: max new posts per subreddit (1–50, default 20)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit_per_sub": {
                "type": "integer",
                "description": "Max new posts per subreddit (1–50, default 20)",
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
            name="reddit_posts",
            schema=_POSTS_SCHEMA,
            handler=lambda ctx, **kw: _reddit_posts(ctx, **kw),
        ),
        ToolEntry(
            name="reddit_search",
            schema=_SEARCH_SCHEMA,
            handler=lambda ctx, **kw: _reddit_search(ctx, **kw),
        ),
        ToolEntry(
            name="reddit_watchlist_add",
            schema=_WATCHLIST_ADD_SCHEMA,
            handler=lambda ctx, **kw: _reddit_watchlist_add(ctx, **kw),
        ),
        ToolEntry(
            name="reddit_watchlist_remove",
            schema=_WATCHLIST_REMOVE_SCHEMA,
            handler=lambda ctx, **kw: _reddit_watchlist_remove(ctx, **kw),
        ),
        ToolEntry(
            name="reddit_watchlist_status",
            schema=_WATCHLIST_STATUS_SCHEMA,
            handler=lambda ctx, **kw: _reddit_watchlist_status(ctx, **kw),
        ),
        ToolEntry(
            name="reddit_watchlist_check",
            schema=_WATCHLIST_CHECK_SCHEMA,
            handler=lambda ctx, **kw: _reddit_watchlist_check(ctx, **kw),
        ),
    ]
