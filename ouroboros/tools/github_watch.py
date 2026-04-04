"""github_watch — watch GitHub repositories for new releases and commits.

No external dependencies — pure Python stdlib (urllib + json).
Uses GitHub REST API v3. Authenticates via GITHUB_TOKEN env var if available
(5000 req/h with auth vs 60 req/h anonymous).

Persistent storage: /opt/veles-data/memory/gh_watch.json

Each watched repo stores:
    last_release_id   — ID of the last seen release (for releases tracking)
    last_commit_sha   — SHA of the last seen commit (for commits tracking)
    track             — "releases" | "commits" | "all"
    added_at          — ISO timestamp
    last_checked      — ISO timestamp

Tools:
    gh_watch_add(repo, track?)    — add a repo to watch (e.g. "anthropics/anthropic-sdk-python")
    gh_watch_remove(repo)         — stop watching a repo
    gh_watch_status()             — list all watched repos with watermark info
    gh_watch_check(repo?)         — fetch new releases/commits since last check

Usage:
    gh_watch_add(repo="openai/openai-python")
    gh_watch_add(repo="anthropics/anthropic-sdk-python", track="all")
    gh_watch_check()              # returns only events not seen before
    gh_watch_check(repo="openai/openai-python")
    gh_watch_status()

Integration:
    inbox_check() calls gh_watch_check() as source_type="github".
    inbox_status() shows watched repos.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")
_GH_WATCH_FILE = "memory/gh_watch.json"

_DEFAULT_TIMEOUT = 20
_GH_API_BASE = "https://api.github.com"
_USER_AGENT = "VelesBot/1.0 (+https://github.com/gr1ng0333/veles)"
_MAX_ITEMS_PER_REPO = 30

TRACK_MODES = ("releases", "commits", "all")


# ── Persistence ────────────────────────────────────────────────────────────────

def _watch_path() -> pathlib.Path:
    return pathlib.Path(_DRIVE_ROOT) / _GH_WATCH_FILE


def _load_watchlist() -> Dict[str, Any]:
    path = _watch_path()
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
    path = _watch_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(watchlist, indent=2, ensure_ascii=False), encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _normalize_repo(repo: str) -> str:
    """Normalize 'owner/repo' — strip leading @ or https://github.com/"""
    repo = repo.strip()
    if repo.startswith("https://github.com/"):
        repo = repo[len("https://github.com/"):]
    if repo.startswith("github.com/"):
        repo = repo[len("github.com/"):]
    return repo.strip("/")


# ── GitHub API helpers ─────────────────────────────────────────────────────────

def _make_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/vnd.github.v3+json",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _api_get(path: str, params: Optional[Dict[str, str]] = None) -> Any:
    """GET a GitHub API endpoint. Returns parsed JSON or raises RuntimeError."""
    url = f"{_GH_API_BASE}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(url, headers=_make_headers())
    try:
        with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(body).get("message", body[:200])
        except Exception:
            msg = body[:200]
        raise RuntimeError(f"GitHub API HTTP {exc.code}: {msg}") from exc
    except Exception as exc:
        raise RuntimeError(f"GitHub API request failed: {exc}") from exc


# ── Fetchers ───────────────────────────────────────────────────────────────────

def _fetch_new_releases(
    repo: str,
    last_release_id: Optional[int],
) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """
    Fetch releases newer than last_release_id.
    Returns (new_items, new_last_id).
    Items have: id, tag, name, url, date, body_snippet.
    """
    try:
        releases = _api_get(f"repos/{repo}/releases", {"per_page": str(_MAX_ITEMS_PER_REPO)})
    except RuntimeError as exc:
        log.warning("gh_watch: failed to fetch releases for %s: %s", repo, exc)
        return [], last_release_id

    if not isinstance(releases, list):
        return [], last_release_id

    new_items: List[Dict[str, Any]] = []
    new_last_id = last_release_id

    for rel in releases:
        rel_id = rel.get("id", 0)
        if last_release_id is None or rel_id > last_release_id:
            body = rel.get("body") or ""
            new_items.append({
                "event_type": "release",
                "id": rel_id,
                "tag": rel.get("tag_name", ""),
                "name": rel.get("name") or rel.get("tag_name", ""),
                "url": rel.get("html_url", ""),
                "date": rel.get("published_at") or rel.get("created_at") or "",
                "prerelease": rel.get("prerelease", False),
                "draft": rel.get("draft", False),
                "body_snippet": body[:300] + ("..." if len(body) > 300 else ""),
            })
            if new_last_id is None or rel_id > new_last_id:
                new_last_id = rel_id

    return new_items, new_last_id


def _fetch_new_commits(
    repo: str,
    last_commit_sha: Optional[str],
    since_iso: Optional[str],
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Fetch commits newer than last_commit_sha.
    Falls back to since_iso when no previous SHA.
    Returns (new_items, new_last_sha).
    """
    params: Dict[str, str] = {"per_page": str(_MAX_ITEMS_PER_REPO)}
    if since_iso:
        params["since"] = since_iso

    try:
        commits = _api_get(f"repos/{repo}/commits", params)
    except RuntimeError as exc:
        log.warning("gh_watch: failed to fetch commits for %s: %s", repo, exc)
        return [], last_commit_sha

    if not isinstance(commits, list):
        return [], last_commit_sha

    # GitHub returns newest first — collect those newer than our watermark
    new_items: List[Dict[str, Any]] = []
    new_last_sha = last_commit_sha

    for c in commits:
        sha = c.get("sha", "")
        if sha == last_commit_sha:
            break  # reached watermark
        commit_info = c.get("commit", {})
        author = commit_info.get("author", {}) or {}
        message = commit_info.get("message", "")
        first_line = message.split("\n")[0][:120]
        new_items.append({
            "event_type": "commit",
            "sha": sha[:12],
            "full_sha": sha,
            "message": first_line,
            "url": c.get("html_url", ""),
            "date": author.get("date", ""),
            "author": author.get("name", ""),
        })
        if not new_last_sha:
            new_last_sha = sha  # record the newest SHA

    if new_items and not new_last_sha:
        new_last_sha = commits[0].get("sha", "") if commits else last_commit_sha

    return new_items, new_last_sha if new_items else last_commit_sha


# ── Tool implementations ───────────────────────────────────────────────────────

def _gh_watch_add(ctx: ToolContext, repo: str, track: str = "releases") -> str:
    """Add a GitHub repo to the watch list."""
    repo = _normalize_repo(repo)
    if "/" not in repo:
        return json.dumps({"error": "repo must be 'owner/repo' format"})
    if track not in TRACK_MODES:
        return json.dumps({"error": f"track must be one of: {', '.join(TRACK_MODES)}"})

    # Validate the repo exists
    try:
        info = _api_get(f"repos/{repo}")
        description = info.get("description", "")
        stars = info.get("stargazers_count", 0)
    except RuntimeError as exc:
        return json.dumps({"error": f"Cannot access repo '{repo}': {exc}"})

    watchlist = _load_watchlist()
    key = repo.lower()

    if key in watchlist:
        existing = watchlist[key]
        existing["track"] = track
        _save_watchlist(watchlist)
        return json.dumps({
            "status": "updated",
            "repo": repo,
            "track": track,
            "description": description,
        }, ensure_ascii=False)

    watchlist[key] = {
        "repo": repo,
        "track": track,
        "added_at": _utc_now(),
        "last_checked": None,
        "last_release_id": None,
        "last_commit_sha": None,
        "last_commit_since": _utc_now(),  # start watermark from now
        "description": description[:200] if description else "",
        "stars": stars,
    }
    _save_watchlist(watchlist)

    return json.dumps({
        "status": "added",
        "repo": repo,
        "track": track,
        "description": description[:200] if description else "",
        "stars": stars,
    }, ensure_ascii=False)


def _gh_watch_remove(ctx: ToolContext, repo: str) -> str:
    """Remove a repo from the watch list."""
    repo = _normalize_repo(repo)
    key = repo.lower()
    watchlist = _load_watchlist()

    if key not in watchlist:
        # Try case-insensitive match
        matches = [k for k in watchlist if k.lower() == key]
        if not matches:
            return json.dumps({"error": f"Repo '{repo}' is not watched"})
        key = matches[0]

    removed_repo = watchlist.pop(key)["repo"]
    _save_watchlist(watchlist)
    return json.dumps({"status": "removed", "repo": removed_repo}, ensure_ascii=False)


def _gh_watch_status(ctx: ToolContext) -> str:
    """List all watched repos with their tracking settings and watermark info."""
    watchlist = _load_watchlist()

    if not watchlist:
        return json.dumps({"count": 0, "repos": [], "message": "No repos watched. Use gh_watch_add(repo='owner/repo')"})

    repos = []
    for key, entry in sorted(watchlist.items()):
        repos.append({
            "repo": entry.get("repo", key),
            "track": entry.get("track", "releases"),
            "description": entry.get("description", ""),
            "stars": entry.get("stars", 0),
            "added_at": entry.get("added_at", ""),
            "last_checked": entry.get("last_checked", "never"),
            "last_release_id": entry.get("last_release_id"),
            "last_commit_sha": entry.get("last_commit_sha", "")[:8] if entry.get("last_commit_sha") else None,
        })

    return json.dumps({
        "count": len(repos),
        "repos": repos,
    }, ensure_ascii=False)


def _gh_watch_check(ctx: ToolContext, repo: Optional[str] = None) -> str:
    """Fetch new releases/commits from watched repos since last check.

    Returns only events not seen before. Updates watermarks.
    """
    watchlist = _load_watchlist()

    if not watchlist:
        return json.dumps({"total_new": 0, "repos": {}, "items": []})

    # Filter to specific repo if requested
    if repo:
        normalized = _normalize_repo(repo).lower()
        keys_to_check = [k for k in watchlist if k == normalized or watchlist[k].get("repo", "").lower() == normalized]
        if not keys_to_check:
            return json.dumps({"error": f"Repo '{repo}' is not watched. Use gh_watch_add first."})
    else:
        keys_to_check = list(watchlist.keys())

    all_items: List[Dict[str, Any]] = []
    repo_summary: Dict[str, Any] = {}
    now = _utc_now()

    for key in keys_to_check:
        entry = watchlist[key]
        repo_name = entry.get("repo", key)
        track = entry.get("track", "releases")
        repo_items: List[Dict[str, Any]] = []

        # Fetch releases
        if track in ("releases", "all"):
            new_releases, new_last_release_id = _fetch_new_releases(
                repo_name,
                entry.get("last_release_id"),
            )
            if new_releases:
                for item in new_releases:
                    item["repo"] = repo_name
                repo_items.extend(new_releases)
            if new_last_release_id is not None:
                entry["last_release_id"] = new_last_release_id

        # Fetch commits
        if track in ("commits", "all"):
            new_commits, new_last_sha = _fetch_new_commits(
                repo_name,
                entry.get("last_commit_sha"),
                entry.get("last_commit_since"),
            )
            if new_commits:
                for item in new_commits:
                    item["repo"] = repo_name
                repo_items.extend(new_commits)
            if new_last_sha:
                entry["last_commit_sha"] = new_last_sha
                entry["last_commit_since"] = None  # use SHA watermark going forward

        entry["last_checked"] = now
        repo_summary[repo_name] = {"new_items": len(repo_items)}
        all_items.extend(repo_items)

    # Sort by date ascending
    def _sort_key(item: Dict[str, Any]) -> datetime:
        dt_str = item.get("date", "")
        try:
            return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    all_items.sort(key=_sort_key)

    _save_watchlist(watchlist)

    return json.dumps({
        "total_new": len(all_items),
        "repos": repo_summary,
        "items": all_items,
    }, ensure_ascii=False)


# ── Tool schemas ───────────────────────────────────────────────────────────────

_SCHEMA_ADD = {
    "name": "gh_watch_add",
    "description": (
        "Watch a GitHub repository for new releases and/or commits. "
        "Useful for tracking SDK updates, model API libraries, dependencies, and interesting projects. "
        "Uses GitHub REST API — no external dependencies. "
        "Authenticates via GITHUB_TOKEN env var if set (5000 req/h), else anonymous (60 req/h).\n\n"
        "Parameters:\n"
        "- repo: 'owner/repo' or full GitHub URL (e.g. 'openai/openai-python')\n"
        "- track: what to watch — 'releases' (default), 'commits', or 'all'"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {
                "type": "string",
                "description": "GitHub repo in 'owner/repo' format (e.g. 'openai/openai-python')",
            },
            "track": {
                "type": "string",
                "enum": ["releases", "commits", "all"],
                "description": "What to track: 'releases' (default), 'commits', or 'all'",
                "default": "releases",
            },
        },
        "required": ["repo"],
    },
}

_SCHEMA_REMOVE = {
    "name": "gh_watch_remove",
    "description": "Stop watching a GitHub repository. Removes its watermark and entry from the watch list.",
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {
                "type": "string",
                "description": "GitHub repo in 'owner/repo' format",
            },
        },
        "required": ["repo"],
    },
}

_SCHEMA_STATUS = {
    "name": "gh_watch_status",
    "description": (
        "List all watched GitHub repositories with their tracking mode, last-check time, "
        "and watermark info (last seen release ID / commit SHA). "
        "Use before gh_watch_check() to see what's being monitored."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

_SCHEMA_CHECK = {
    "name": "gh_watch_check",
    "description": (
        "Fetch new releases and/or commits from all watched GitHub repos since last check. "
        "Returns ONLY items not seen before (watermark-based). "
        "Automatically updates watermarks for next call.\n\n"
        "Use for:\n"
        "- Tracking new LLM SDK releases (openai, anthropic, langchain)\n"
        "- Watching model checkpoint repos on HuggingFace/GitHub\n"
        "- Monitoring your own project's activity\n\n"
        "Each item has: event_type ('release'|'commit'), repo, date, url, "
        "and type-specific fields (tag/name for releases; sha/message/author for commits).\n\n"
        "Parameter:\n"
        "- repo: check only this repo (default: all watched repos)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {
                "type": "string",
                "description": "Check only this repo (default: all watched repos)",
                "default": "",
            },
        },
        "required": [],
    },
}


# ── Registry ───────────────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(name="gh_watch_add", schema=_SCHEMA_ADD, handler=_gh_watch_add),
        ToolEntry(name="gh_watch_remove", schema=_SCHEMA_REMOVE, handler=_gh_watch_remove),
        ToolEntry(name="gh_watch_status", schema=_SCHEMA_STATUS, handler=_gh_watch_status),
        ToolEntry(name="gh_watch_check", schema=_SCHEMA_CHECK, handler=_gh_watch_check),
    ]
