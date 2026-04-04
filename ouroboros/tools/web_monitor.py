"""web_monitor — track URL pages for content changes.

Saves a text snapshot of a URL on first check. On subsequent checks,
computes a diff against the previous snapshot and generates an LLM summary
of what changed. Useful for tracking OpenRouter pricing, model changelogs,
documentation pages, or any web resource that updates over time.

Storage: /opt/veles-data/memory/web_monitor.json (index) +
         /opt/veles-data/memory/web_snapshots/<name>.txt (snapshots)

Tools:
    web_monitor_add(url, name, selector?)      — register a URL for monitoring
    web_monitor_check(name?)                   — check for changes (all or one)
    web_monitor_status()                       — list monitored URLs + last check info
    web_monitor_remove(name)                   — unregister a URL

Usage:
    web_monitor_add(url="https://openrouter.ai/models", name="openrouter_models")
    web_monitor_check()                        # check all → returns diff summaries
    web_monitor_check(name="openrouter_models")
    web_monitor_status()
"""

from __future__ import annotations

import difflib
import hashlib
import html as html_module
import json
import logging
import os
import pathlib
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")
_MONITOR_FILE = "memory/web_monitor.json"
_SNAPSHOTS_DIR = "memory/web_snapshots"

_DEFAULT_TIMEOUT = 25
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
_SUMMARY_MODEL_DEFAULT = "codex/gpt-4.1-mini"
_SUMMARY_MODEL_FALLBACK = "copilot/claude-haiku-4.5"  # was anthropic/ (OpenRouter)

# Max bytes to store in snapshot (enough to detect meaningful changes)
_MAX_SNAPSHOT_CHARS = 60_000
# Max diff lines to pass to LLM
_MAX_DIFF_LINES_LLM = 200

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _monitor_path() -> pathlib.Path:
    return pathlib.Path(_DRIVE_ROOT) / _MONITOR_FILE


def _snapshots_dir() -> pathlib.Path:
    d = pathlib.Path(_DRIVE_ROOT) / _SNAPSHOTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_index() -> Dict[str, Any]:
    path = _monitor_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_index(index: Dict[str, Any]) -> None:
    path = _monitor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


def _snapshot_path(name: str) -> pathlib.Path:
    safe_name = re.sub(r"[^\w\-]", "_", name)[:80]
    return _snapshots_dir() / f"{safe_name}.txt"


def _load_snapshot(name: str) -> Optional[str]:
    p = _snapshot_path(name)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _save_snapshot(name: str, text: str) -> None:
    _snapshot_path(name).write_text(text[:_MAX_SNAPSHOT_CHARS], encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Fetching + text extraction
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_MULTI_NL = re.compile(r"\n{3,}")
_MULTI_SPACE = re.compile(r"[ \t]+")


def _html_to_text(raw_html: str) -> str:
    """Strip HTML to readable plain text for diffing."""
    # Remove script/style blocks first
    text = _SCRIPT_RE.sub("", raw_html)
    # Strip tags
    text = _TAG_RE.sub(" ", text)
    # Unescape HTML entities
    text = html_module.unescape(text)
    # Normalize whitespace
    text = _MULTI_SPACE.sub(" ", text)
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l]  # drop empty
    text = "\n".join(lines)
    text = _MULTI_NL.sub("\n\n", text)
    return text.strip()


def _extract_selector(html_body: str, selector: str) -> str:
    """Naive CSS selector extraction (id/class only).

    Supports: #id-name, .class-name, tag-name
    Falls back to full body on complex selectors.
    """
    selector = selector.strip()
    if not selector:
        return html_body

    # id selector: #some-id
    if selector.startswith("#"):
        id_val = re.escape(selector[1:])
        m = re.search(rf'id="{id_val}"[^>]*>(.*?)</[a-zA-Z]+>', html_body, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1)

    # class selector: .class-name
    if selector.startswith("."):
        cls_val = re.escape(selector[1:])
        m = re.search(rf'class="[^"]*{cls_val}[^"]*"[^>]*>(.*?)</[a-zA-Z]+>', html_body, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1)

    # Tag selector: tagname
    tag = re.escape(selector)
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", html_body, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1)

    return html_body


def _fetch_url(url: str) -> Tuple[str, int]:
    """Fetch URL and return (text_content, status_code).

    Raises on network errors.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return raw, resp.status


# ---------------------------------------------------------------------------
# Diff + LLM summary
# ---------------------------------------------------------------------------

def _unified_diff(old: str, new: str, name: str) -> str:
    """Return unified diff between old and new snapshots (text lines)."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"{name} (previous)",
        tofile=f"{name} (current)",
        lineterm="",
    ))
    return "".join(diff_lines)


def _compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _call_llm_summary(
    url: str,
    diff_text: str,
    old_text: str,
    new_text: str,
) -> str:
    """Call LLM to produce a human-readable summary of what changed."""
    from ouroboros.llm import LLMClient

    # Truncate diff to LLM budget
    diff_lines = diff_text.splitlines()
    if len(diff_lines) > _MAX_DIFF_LINES_LLM:
        diff_truncated = "\n".join(diff_lines[:_MAX_DIFF_LINES_LLM])
        diff_truncated += f"\n... (+{len(diff_lines) - _MAX_DIFF_LINES_LLM} more diff lines)"
    else:
        diff_truncated = diff_text

    prompt = f"""A web page at {url} has changed.

Below is the unified diff between the previous snapshot and the current one:

{diff_truncated}

Summarize the changes in 3–7 bullet points. Be specific and concrete:
- What content was added, removed, or modified?
- Are there new models, prices, features, or deprecations?
- Are there any version numbers, dates, or other significant values that changed?

Write plain text bullets. No headers. No markdown beyond bullets.
"""

    llm = LLMClient()
    models = [_SUMMARY_MODEL_DEFAULT, _SUMMARY_MODEL_FALLBACK]
    last_exc: Optional[Exception] = None
    for model in models:
        try:
            msg, usage = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                tools=None,
                reasoning_effort="low",
                max_tokens=600,
            )
            # Emit budget update
            try:
                from supervisor.state import update_budget_from_usage
                update_budget_from_usage({
                    "cost": float(usage.get("cost") or 0),
                    "rounds": 1,
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "cached_tokens": usage.get("cached_tokens", 0),
                })
            except Exception:
                pass
            return (msg.get("content") or "").strip()
        except Exception as exc:
            last_exc = exc
            msg_lower = str(exc).lower()
            if any(w in msg_lower for w in ("401", "403", "unauthorized", "user not found", "authentication")):
                log.warning("web_monitor summary: auth error on %s, trying fallback: %s", model, exc)
                continue
            raise

    raise last_exc or RuntimeError("All summary models failed")


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _web_monitor_add(
    ctx: ToolContext,
    url: str,
    name: str,
    selector: str = "",
) -> str:
    """Register a URL for monitoring."""
    url = url.strip()
    name = name.strip().lower()

    if not url:
        return json.dumps({"error": "url must not be empty"})
    if not name:
        return json.dumps({"error": "name must not be empty"})
    if not re.match(r"^[\w\-]{1,80}$", name):
        return json.dumps({"error": "name must be alphanumeric/hyphens/underscores, max 80 chars"})

    index = _load_index()
    if name in index:
        return json.dumps({
            "status": "already_registered",
            "name": name,
            "url": index[name].get("url"),
            "message": "Already monitoring this name. Use web_monitor_check() to check for changes.",
        })

    index[name] = {
        "url": url,
        "selector": selector,
        "added_at": _utc_now(),
        "last_checked": None,
        "last_hash": None,
        "last_changed": None,
        "check_count": 0,
    }
    _save_index(index)

    return json.dumps({
        "status": "registered",
        "name": name,
        "url": url,
        "selector": selector or None,
        "message": "Registered. Call web_monitor_check() to take first snapshot.",
    })


def _check_one(name: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    """Check one monitored URL. Returns result dict."""
    url = meta.get("url", "")
    selector = meta.get("selector", "")
    prev_hash = meta.get("last_hash")
    now = _utc_now()

    # Fetch
    try:
        raw_html, status_code = _fetch_url(url)
    except Exception as exc:
        return {
            "name": name,
            "url": url,
            "status": "fetch_error",
            "error": str(exc)[:300],
            "changed": False,
        }

    # Extract selector if specified
    if selector:
        content_html = _extract_selector(raw_html, selector)
    else:
        content_html = raw_html

    # Convert to text
    text = _html_to_text(content_html)
    current_hash = _compute_hash(text)

    result: Dict[str, Any] = {
        "name": name,
        "url": url,
        "checked_at": now,
        "status_code": status_code,
    }

    if prev_hash is None:
        # First check — just save snapshot
        _save_snapshot(name, text)
        result["status"] = "first_snapshot"
        result["changed"] = False
        result["message"] = f"First snapshot saved ({len(text)} chars). Check again later for diffs."
        result["snapshot_chars"] = len(text)
        return result

    if current_hash == prev_hash:
        result["status"] = "unchanged"
        result["changed"] = False
        result["message"] = "No changes since last check."
        return result

    # Changed — compute diff
    old_text = _load_snapshot(name) or ""
    diff = _unified_diff(old_text, text, name)

    # Count added/removed lines
    added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))

    # LLM summary of changes
    summary = ""
    try:
        summary = _call_llm_summary(url, diff, old_text, text)
    except Exception as exc:
        log.warning("web_monitor: LLM summary failed for %s: %s", name, exc)
        summary = f"(LLM summary unavailable: {exc})"

    # Save new snapshot
    _save_snapshot(name, text)

    result["status"] = "changed"
    result["changed"] = True
    result["lines_added"] = added
    result["lines_removed"] = removed
    result["diff_preview"] = "\n".join(diff.splitlines()[:30]) + ("..." if len(diff.splitlines()) > 30 else "")
    result["change_summary"] = summary

    return result


def _web_monitor_check(
    ctx: ToolContext,
    name: str = "",
) -> str:
    """Check one or all monitored URLs for changes."""
    index = _load_index()
    if not index:
        return json.dumps({
            "monitored_count": 0,
            "message": "No URLs registered. Use web_monitor_add() first.",
            "results": [],
        })

    if name:
        name = name.strip().lower()
        if name not in index:
            return json.dumps({"error": f"Name '{name}' not found in monitor index."})
        targets = {name: index[name]}
    else:
        targets = dict(index)

    results: List[Dict[str, Any]] = []
    changed_count = 0
    now = _utc_now()

    for n, meta in sorted(targets.items()):
        res = _check_one(n, meta)
        results.append(res)

        if res.get("changed"):
            changed_count += 1
            index[n]["last_changed"] = now
            index[n]["last_hash"] = _compute_hash(
                _load_snapshot(n) or ""
            )
        elif res.get("status") == "first_snapshot":
            index[n]["last_hash"] = _compute_hash(_load_snapshot(n) or "")
        elif res.get("status") == "unchanged" and index[n].get("last_hash") is None:
            # Snapshot exists but hash wasn't recorded
            snap = _load_snapshot(n)
            if snap:
                index[n]["last_hash"] = _compute_hash(snap)

        if "error" not in res:
            index[n]["last_checked"] = now
            index[n]["check_count"] = int(index[n].get("check_count", 0)) + 1

    _save_index(index)

    return json.dumps({
        "checked": len(results),
        "changed": changed_count,
        "unchanged": sum(1 for r in results if r.get("status") == "unchanged"),
        "first_snapshot": sum(1 for r in results if r.get("status") == "first_snapshot"),
        "errors": sum(1 for r in results if r.get("status") == "fetch_error"),
        "results": results,
    }, ensure_ascii=False)


def _web_monitor_status(ctx: ToolContext) -> str:
    """List all registered monitored URLs."""
    index = _load_index()
    if not index:
        return json.dumps({
            "count": 0,
            "message": "No URLs registered. Use web_monitor_add() to start monitoring.",
            "monitors": [],
        })

    monitors = []
    for name, meta in sorted(index.items()):
        monitors.append({
            "name": name,
            "url": meta.get("url"),
            "selector": meta.get("selector") or None,
            "added_at": meta.get("added_at"),
            "last_checked": meta.get("last_checked"),
            "last_changed": meta.get("last_changed"),
            "check_count": meta.get("check_count", 0),
        })

    return json.dumps({
        "count": len(monitors),
        "monitors": monitors,
    }, ensure_ascii=False)


def _web_monitor_remove(ctx: ToolContext, name: str) -> str:
    """Unregister a monitored URL."""
    name = name.strip().lower()
    index = _load_index()
    if name not in index:
        return json.dumps({"status": "not_found", "name": name})

    url = index[name].get("url", "")
    del index[name]
    _save_index(index)

    # Clean up snapshot
    snap_path = _snapshot_path(name)
    try:
        if snap_path.exists():
            snap_path.unlink()
    except OSError:
        pass

    return json.dumps({
        "status": "removed",
        "name": name,
        "url": url,
    })


# ---------------------------------------------------------------------------
# Schemas + registration
# ---------------------------------------------------------------------------

_ADD_SCHEMA = {
    "name": "web_monitor_add",
    "description": (
        "Register a URL for change monitoring. On first check, saves a text snapshot. "
        "On subsequent checks, detects changes and generates an LLM diff summary.\n"
        "Great for tracking: OpenRouter model/pricing pages, changelogs, documentation.\n\n"
        "Parameters:\n"
        "- url: full URL to monitor (e.g. 'https://openrouter.ai/models')\n"
        "- name: short identifier (alphanumeric/hyphens, e.g. 'openrouter_models')\n"
        "- selector: optional CSS-like selector to focus on a page section (e.g. '#pricing', '.changelog')"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Full URL to monitor",
            },
            "name": {
                "type": "string",
                "description": "Short identifier for this monitor (alphanumeric/hyphens, max 80 chars)",
            },
            "selector": {
                "type": "string",
                "description": "Optional CSS selector to focus on a page section (e.g. '#pricing', '.changelog'). Default: whole page.",
                "default": "",
            },
        },
        "required": ["url", "name"],
    },
}

_CHECK_SCHEMA = {
    "name": "web_monitor_check",
    "description": (
        "Check registered URL(s) for content changes. "
        "Fetches current content, compares to saved snapshot, and returns an LLM summary of what changed.\n"
        "- If no name given: checks ALL registered URLs.\n"
        "- If a name given: checks only that URL.\n"
        "First check always saves a baseline snapshot (no diff on first run).\n\n"
        "Parameters:\n"
        "- name: optional name of specific monitor to check (default: check all)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of specific monitor to check (default: check all registered)",
                "default": "",
            },
        },
        "required": [],
    },
}

_STATUS_SCHEMA = {
    "name": "web_monitor_status",
    "description": (
        "List all registered monitored URLs with their last check times and change history. "
        "Shows which URLs are being tracked and when they last changed."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

_REMOVE_SCHEMA = {
    "name": "web_monitor_remove",
    "description": (
        "Unregister a monitored URL. Removes it from the index and deletes its snapshot. "
        "Example: web_monitor_remove(name='openrouter_models')"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the monitor to remove",
            },
        },
        "required": ["name"],
    },
}


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="web_monitor_add",
            schema=_ADD_SCHEMA,
            handler=lambda ctx, **kw: _web_monitor_add(ctx, **kw),
        ),
        ToolEntry(
            name="web_monitor_check",
            schema=_CHECK_SCHEMA,
            handler=lambda ctx, **kw: _web_monitor_check(ctx, **kw),
        ),
        ToolEntry(
            name="web_monitor_status",
            schema=_STATUS_SCHEMA,
            handler=lambda ctx, **kw: _web_monitor_status(ctx, **kw),
        ),
        ToolEntry(
            name="web_monitor_remove",
            schema=_REMOVE_SCHEMA,
            handler=lambda ctx, **kw: _web_monitor_remove(ctx, **kw),
        ),
    ]
