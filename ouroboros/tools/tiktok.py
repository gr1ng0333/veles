"""
TikTok tools — search, metadata, profile, history.

All tools operate through yt-dlp (no video download).
tiktok_search    — search videos by hashtag/query; returns structured list
tiktok_metadata  — full metadata for a single video URL
tiktok_profile   — list videos from a @username profile
tiktok_history   — dedup registry of already-sent video URLs
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import shutil
import importlib.util
import time
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

_HISTORY_REL = "memory/tiktok_sent_videos.json"


def _resolve_yt_dlp() -> List[str]:
    python_exe = shutil.which("python") or shutil.which("python3")
    if python_exe and importlib.util.find_spec("yt_dlp") is not None:
        return [python_exe, "-m", "yt_dlp"]
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    raise RuntimeError("yt-dlp is not installed in the runtime.")


def _run_ytdlp(cmd: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _parse_dump_single(raw: str) -> Optional[Dict[str, Any]]:
    """Parse yt-dlp --dump-single-json output (may contain multiple JSON lines)."""
    for line in reversed(raw.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def _extract_video_meta(info: Dict[str, Any]) -> Dict[str, Any]:
    """Extract a clean, concise metadata dict from a yt-dlp info object."""
    return {
        "id": str(info.get("id") or ""),
        "url": str(info.get("webpage_url") or info.get("url") or ""),
        "title": str(info.get("title") or ""),
        "description": str(info.get("description") or ""),
        "uploader": str(info.get("uploader") or info.get("channel") or ""),
        "uploader_id": str(info.get("uploader_id") or info.get("channel_id") or ""),
        "duration": info.get("duration"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "timestamp": info.get("timestamp"),
        "upload_date": str(info.get("upload_date") or ""),
        "tags": list(info.get("tags") or [])[:20],
        "categories": list(info.get("categories") or [])[:10],
    }


# --------------------------------------------------------------------------- #
# tiktok_search
# --------------------------------------------------------------------------- #

def _tiktok_search(ctx: ToolContext, query: str, max_results: int = 10) -> str:
    """
    Search TikTok for videos matching a hashtag or keyword query.
    Returns a JSON list of results with title, url, uploader, duration, views, likes.

    Uses yt-dlp ytsearch/tiktok extractor — no video files are downloaded.
    query examples: "python tutorial", "#programming", "@username"
    """
    query = str(query or "").strip()
    if not query:
        return json.dumps({"status": "failed", "error": "query must not be empty"})

    max_results = max(1, min(int(max_results or 10), 50))

    try:
        ytdlp = _resolve_yt_dlp()
    except RuntimeError as exc:
        return json.dumps({"status": "failed", "error": str(exc)})

    # Build search URL for TikTok
    # yt-dlp supports "tiktoksearch:<query>" extractor and direct hashtag URLs
    if query.startswith("#"):
        tag = query.lstrip("#").strip()
        search_target = f"https://www.tiktok.com/tag/{tag}"
    elif query.startswith("@"):
        # Profile — delegate to tiktok_profile semantics
        uname = query.lstrip("@").strip()
        search_target = f"https://www.tiktok.com/@{uname}"
    else:
        search_target = f"tiktoksearch{max_results}:{query}"

    cmd = [
        *ytdlp,
        "--no-download",
        "--flat-playlist",
        "--dump-single-json",
        "--no-warnings",
        "--extractor-args", "tiktok:webpage_download=0",
        search_target,
    ]
    try:
        proc = _run_ytdlp(cmd, timeout=45)
    except subprocess.TimeoutExpired:
        return json.dumps({"status": "failed", "error": "yt-dlp timed out after 45s"})

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "yt-dlp error").strip()[:500]
        # Fallback: try dumping without flat-playlist for single video
        return json.dumps({"status": "failed", "error": err, "query": query})

    info = _parse_dump_single(proc.stdout)
    if info is None:
        return json.dumps({"status": "failed", "error": "yt-dlp returned no JSON", "query": query})

    entries = info.get("entries") or []
    if not entries and info.get("id"):
        # Single video returned
        entries = [info]

    results = []
    for entry in entries[:max_results]:
        results.append({
            "id": str(entry.get("id") or ""),
            "url": str(entry.get("url") or entry.get("webpage_url") or ""),
            "title": str(entry.get("title") or ""),
            "uploader": str(entry.get("uploader") or entry.get("channel") or ""),
            "duration": entry.get("duration"),
            "view_count": entry.get("view_count"),
            "like_count": entry.get("like_count"),
            "upload_date": str(entry.get("upload_date") or ""),
        })

    return json.dumps({
        "status": "ok",
        "query": query,
        "count": len(results),
        "results": results,
    }, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# tiktok_metadata
# --------------------------------------------------------------------------- #

def _tiktok_metadata(ctx: ToolContext, url: str) -> str:
    """
    Fetch full metadata for a single TikTok video URL.
    Returns structured JSON: title, description, uploader, duration, view/like/comment counts,
    upload date, tags, thumbnail URL.
    No video download.
    """
    url = str(url or "").strip()
    if not url:
        return json.dumps({"status": "failed", "error": "url must not be empty"})

    try:
        ytdlp = _resolve_yt_dlp()
    except RuntimeError as exc:
        return json.dumps({"status": "failed", "error": str(exc)})

    cmd = [
        *ytdlp,
        "--no-download",
        "--dump-single-json",
        "--no-warnings",
        "--extractor-args", "tiktok:webpage_download=0",
        url,
    ]
    try:
        proc = _run_ytdlp(cmd, timeout=30)
    except subprocess.TimeoutExpired:
        return json.dumps({"status": "failed", "error": "yt-dlp timed out after 30s", "url": url})

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "yt-dlp error").strip()[:500]
        return json.dumps({"status": "failed", "error": err, "url": url})

    info = _parse_dump_single(proc.stdout)
    if info is None:
        return json.dumps({"status": "failed", "error": "no JSON in yt-dlp output", "url": url})

    meta = _extract_video_meta(info)
    meta["thumbnail"] = str(info.get("thumbnail") or "")
    meta["formats_count"] = len(info.get("formats") or [])
    meta["status"] = "ok"
    return json.dumps(meta, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# tiktok_profile
# --------------------------------------------------------------------------- #

def _tiktok_profile(ctx: ToolContext, username: str, max_results: int = 20) -> str:
    """
    List recent videos from a TikTok profile by username (with or without @).
    Returns JSON list with video title, url, duration, view/like counts, upload_date.
    max_results: 1–100 (default 20). No video download.
    """
    username = str(username or "").strip().lstrip("@")
    if not username:
        return json.dumps({"status": "failed", "error": "username must not be empty"})

    max_results = max(1, min(int(max_results or 20), 100))

    try:
        ytdlp = _resolve_yt_dlp()
    except RuntimeError as exc:
        return json.dumps({"status": "failed", "error": str(exc)})

    profile_url = f"https://www.tiktok.com/@{username}"
    cmd = [
        *ytdlp,
        "--no-download",
        "--flat-playlist",
        "--dump-single-json",
        "--no-warnings",
        "--playlist-end", str(max_results),
        "--extractor-args", "tiktok:webpage_download=0",
        profile_url,
    ]
    try:
        proc = _run_ytdlp(cmd, timeout=45)
    except subprocess.TimeoutExpired:
        return json.dumps({"status": "failed", "error": "yt-dlp timed out after 45s", "username": username})

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "yt-dlp error").strip()[:500]
        return json.dumps({"status": "failed", "error": err, "username": username})

    info = _parse_dump_single(proc.stdout)
    if info is None:
        return json.dumps({"status": "failed", "error": "no JSON in yt-dlp output", "username": username})

    entries = info.get("entries") or []
    if not entries and info.get("id"):
        entries = [info]

    results = []
    for entry in entries[:max_results]:
        results.append({
            "id": str(entry.get("id") or ""),
            "url": str(entry.get("url") or entry.get("webpage_url") or ""),
            "title": str(entry.get("title") or ""),
            "duration": entry.get("duration"),
            "view_count": entry.get("view_count"),
            "like_count": entry.get("like_count"),
            "upload_date": str(entry.get("upload_date") or ""),
        })

    return json.dumps({
        "status": "ok",
        "username": username,
        "profile_url": profile_url,
        "count": len(results),
        "videos": results,
    }, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# tiktok_history
# --------------------------------------------------------------------------- #

def _history_path(ctx: ToolContext) -> pathlib.Path:
    return ctx.drive_root / _HISTORY_REL


def _load_history(ctx: ToolContext) -> Dict[str, Any]:
    path = _history_path(ctx)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"urls": [], "entries": []}
    return {"urls": [], "entries": []}


def _save_history(ctx: ToolContext, data: Dict[str, Any]) -> None:
    path = _history_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _tiktok_history(
    ctx: ToolContext,
    action: str = "list",
    url: str = "",
    title: str = "",
    note: str = "",
) -> str:
    """
    Manage the TikTok sent-video history registry.

    action="list"  — return all recorded URLs (to avoid resending)
    action="add"   — add a URL to the history (url required; title/note optional)
    action="check" — check if a URL is already in history; returns {in_history: bool}
    action="clear" — wipe history entirely

    History is stored in drive_root/memory/tiktok_sent_videos.json.
    """
    action = str(action or "list").strip().lower()
    url = str(url or "").strip()

    if action not in ("list", "add", "check", "clear"):
        return json.dumps({"status": "failed", "error": f"unknown action '{action}'; use: list, add, check, clear"})

    data = _load_history(ctx)
    urls_set = set(data.get("urls") or [])

    if action == "list":
        return json.dumps({
            "status": "ok",
            "count": len(urls_set),
            "urls": sorted(urls_set),
            "entries": data.get("entries") or [],
        }, ensure_ascii=False, indent=2)

    if action == "clear":
        _save_history(ctx, {"urls": [], "entries": []})
        return json.dumps({"status": "ok", "cleared": len(urls_set)})

    if action == "check":
        if not url:
            return json.dumps({"status": "failed", "error": "url required for check"})
        return json.dumps({"status": "ok", "url": url, "in_history": url in urls_set})

    # action == "add"
    if not url:
        return json.dumps({"status": "failed", "error": "url required for add"})

    already_present = url in urls_set
    if not already_present:
        urls_set.add(url)
        entry = {
            "url": url,
            "title": str(title or "").strip(),
            "note": str(note or "").strip(),
            "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        entries = list(data.get("entries") or [])
        entries.append(entry)
        _save_history(ctx, {"urls": sorted(urls_set), "entries": entries})

    return json.dumps({
        "status": "ok",
        "url": url,
        "already_present": already_present,
        "total": len(urls_set),
    }, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# Tool registration
# --------------------------------------------------------------------------- #

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="tiktok_search",
            schema={
                "name": "tiktok_search",
                "description": (
                    "Search TikTok for videos matching a query, hashtag (#tag), or profile (@username). "
                    "Returns structured JSON list: title, url, uploader, duration, view_count, like_count. "
                    "No video download — metadata only. "
                    "Use this instead of web_search for structured TikTok discovery."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query. Examples: 'python tutorial', '#programming', '@mkbhd'",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum results to return (1–50, default 10)",
                        },
                    },
                    "required": ["query"],
                },
            },
            handler=lambda ctx, **kw: _tiktok_search(ctx, **kw),
            timeout_sec=60,
        ),
        ToolEntry(
            name="tiktok_metadata",
            schema={
                "name": "tiktok_metadata",
                "description": (
                    "Fetch full metadata for a single TikTok video URL. "
                    "Returns: title, description, uploader, duration, view_count, like_count, comment_count, "
                    "upload_date, tags, thumbnail URL. No video download."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "Full TikTok video URL, e.g. https://www.tiktok.com/@user/video/123",
                        },
                    },
                    "required": ["url"],
                },
            },
            handler=lambda ctx, **kw: _tiktok_metadata(ctx, **kw),
            timeout_sec=45,
        ),
        ToolEntry(
            name="tiktok_profile",
            schema={
                "name": "tiktok_profile",
                "description": (
                    "List recent videos from a TikTok profile by username (with or without @). "
                    "Returns structured list: title, url, duration, view_count, like_count, upload_date. "
                    "max_results: 1–100 (default 20). No video download."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "username": {
                            "type": "string",
                            "description": "TikTok username with or without @, e.g. 'mkbhd' or '@mkbhd'",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum videos to return (1–100, default 20)",
                        },
                    },
                    "required": ["username"],
                },
            },
            handler=lambda ctx, **kw: _tiktok_profile(ctx, **kw),
            timeout_sec=60,
        ),
        ToolEntry(
            name="tiktok_history",
            schema={
                "name": "tiktok_history",
                "description": (
                    "Manage the TikTok sent-video dedup registry stored in drive memory. "
                    "action='list'  — return all recorded URLs. "
                    "action='add'   — add url to history (url required; title/note optional). "
                    "action='check' — check if url is already in history. "
                    "action='clear' — wipe history entirely."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "add", "check", "clear"],
                            "description": "Operation to perform (default: list)",
                        },
                        "url": {
                            "type": "string",
                            "description": "TikTok video URL (required for add/check)",
                        },
                        "title": {
                            "type": "string",
                            "description": "Optional video title to store with the URL",
                        },
                        "note": {
                            "type": "string",
                            "description": "Optional free-form note",
                        },
                    },
                    "required": [],
                },
            },
            handler=lambda ctx, **kw: _tiktok_history(ctx, **kw),
            timeout_sec=15,
        ),
    ]
