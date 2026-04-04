"""digest_schedule — schedule automatic inbox digest delivery.

Allows configuring when to auto-run inbox_digest and deliver it to the owner.
The schedule is stored in /opt/veles-data/memory/digest_schedule.json.
The DigestScheduler daemon thread in colab_launcher.py reads it and fires inbox_digest.

Tools:
    digest_schedule_set(interval_hours, sources?, notify_owner?, model?)  — set/update schedule
    digest_schedule_status()                                               — show current schedule
    digest_schedule_disable()                                              — turn off auto-digest
    digest_run_now(sources?, notify_owner?, model?)                        — immediate run (alias)

Usage:
    digest_schedule_set(interval_hours=6)                   # digest every 6h, notify owner
    digest_schedule_set(interval_hours=24, sources=["hn", "reddit"])
    digest_schedule_status()                                # when is next run?
    digest_schedule_disable()                               # stop auto-digest
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")
_SCHEDULE_FILE = "memory/digest_schedule.json"

# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _schedule_path() -> pathlib.Path:
    return pathlib.Path(_DRIVE_ROOT) / _SCHEDULE_FILE


def _load_schedule() -> Dict[str, Any]:
    path = _schedule_path()
    if not path.exists():
        return {"enabled": False}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"enabled": False}


def _save_schedule(data: Dict[str, Any]) -> None:
    path = _schedule_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _digest_schedule_set(
    ctx: ToolContext,
    interval_hours: float = 6.0,
    sources: Optional[List[str]] = None,
    notify_owner: bool = True,
    model: str = "codex/gpt-4.1-mini",
) -> str:
    """Set or update the automatic digest schedule."""
    if interval_hours < 0.5:
        return "❌ Minimum interval is 0.5 hours (30 minutes)"
    if interval_hours > 168:
        return "❌ Maximum interval is 168 hours (1 week)"

    now = datetime.now(tz=timezone.utc)
    next_run = (now + timedelta(hours=interval_hours)).isoformat()

    data: Dict[str, Any] = {
        "enabled": True,
        "interval_hours": interval_hours,
        "sources": sources or [],  # empty = all
        "notify_owner": notify_owner,
        "model": model,
        "created_at": now.isoformat(),
        "next_run_at": next_run,
        "last_run_at": None,
        "run_count": 0,
    }
    _save_schedule(data)

    src_str = ", ".join(sources) if sources else "all sources"
    return (
        f"✅ Digest schedule set: every {interval_hours:.1f}h from {src_str}\n"
        f"Next run: {next_run[:16]} UTC\n"
        f"Notify owner: {notify_owner}"
    )


def _digest_schedule_status(ctx: ToolContext) -> str:
    """Show current digest schedule status."""
    data = _load_schedule()
    if not data.get("enabled"):
        return "📭 Auto-digest is disabled. Use digest_schedule_set() to enable."

    interval = data.get("interval_hours", 0)
    sources = data.get("sources") or []
    src_str = ", ".join(sources) if sources else "all sources"
    next_run = data.get("next_run_at", "?")
    last_run = data.get("last_run_at") or "never"
    run_count = data.get("run_count", 0)
    notify = data.get("notify_owner", True)

    # Calculate time until next run
    time_until = ""
    try:
        next_dt = datetime.fromisoformat(next_run)
        now = datetime.now(tz=timezone.utc)
        if next_dt.tzinfo is None:
            next_dt = next_dt.replace(tzinfo=timezone.utc)
        delta = next_dt - now
        if delta.total_seconds() > 0:
            mins = int(delta.total_seconds() / 60)
            if mins >= 60:
                time_until = f" (in {mins // 60}h {mins % 60}m)"
            else:
                time_until = f" (in {mins}m)"
        else:
            time_until = " (overdue, will run soon)"
    except Exception:
        pass

    return (
        f"📅 Auto-digest schedule:\n"
        f"  Interval: every {interval:.1f}h\n"
        f"  Sources: {src_str}\n"
        f"  Notify owner: {notify}\n"
        f"  Next run: {str(next_run)[:16]} UTC{time_until}\n"
        f"  Last run: {str(last_run)[:16]}\n"
        f"  Total runs: {run_count}"
    )


def _digest_schedule_disable(ctx: ToolContext) -> str:
    """Disable automatic digest."""
    data = _load_schedule()
    data["enabled"] = False
    _save_schedule(data)
    return "📭 Auto-digest disabled."


def _digest_run_now(
    ctx: ToolContext,
    sources: Optional[List[str]] = None,
    notify_owner: bool = True,
    model: str = "codex/gpt-4.1-mini",
) -> str:
    """Run inbox_digest immediately (alias for manual trigger)."""
    try:
        from ouroboros.tools.inbox_digest import _inbox_digest_impl
        return _inbox_digest_impl(
            ctx=ctx,
            sources=sources or [],
            notify_owner=notify_owner,
            model=model,
        )
    except ImportError:
        return "❌ inbox_digest module not available"


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_SCHEMA_SET = {
    "name": "digest_schedule_set",
    "description": (
        "Configure automatic inbox digest delivery. The DigestScheduler daemon runs inbox_digest "
        "at the specified interval and optionally sends the result to the owner via Telegram.\n\n"
        "After setting, use digest_schedule_status() to verify and check next run time."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "interval_hours": {
                "type": "number",
                "description": "How often to run the digest, in hours (0.5–168, default 6)",
                "default": 6,
            },
            "sources": {
                "type": "array",
                "items": {"type": "string", "enum": ["telegram", "rss", "web", "hn", "reddit", "arxiv"]},
                "description": "Sources to include (default: all)",
            },
            "notify_owner": {
                "type": "boolean",
                "description": "Send digest to owner via Telegram (default true)",
                "default": True,
            },
            "model": {
                "type": "string",
                "description": "LLM model for digest generation (default: codex/gpt-4.1-mini)",
                "default": "codex/gpt-4.1-mini",
            },
        },
        "required": [],
    },
}

_SCHEMA_STATUS = {
    "name": "digest_schedule_status",
    "description": "Show current auto-digest schedule status: interval, sources, next run time, run count.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

_SCHEMA_DISABLE = {
    "name": "digest_schedule_disable",
    "description": "Disable automatic inbox digest. Use digest_schedule_set() to re-enable.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

_SCHEMA_RUN_NOW = {
    "name": "digest_run_now",
    "description": (
        "Run inbox_digest immediately (manual trigger). Same as inbox_digest() but simpler API. "
        "Collects new items from all monitoring sources and generates a unified intelligence briefing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sources": {
                "type": "array",
                "items": {"type": "string", "enum": ["telegram", "rss", "web", "hn", "reddit", "arxiv"]},
                "description": "Sources to include (default: all)",
            },
            "notify_owner": {
                "type": "boolean",
                "description": "Send to owner via Telegram (default true)",
                "default": True,
            },
            "model": {
                "type": "string",
                "description": "LLM model for summarization",
                "default": "codex/gpt-4.1-mini",
            },
        },
        "required": [],
    },
}


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(name="digest_schedule_set", schema=_SCHEMA_SET, execute=_digest_schedule_set),
        ToolEntry(name="digest_schedule_status", schema=_SCHEMA_STATUS, execute=_digest_schedule_status),
        ToolEntry(name="digest_schedule_disable", schema=_SCHEMA_DISABLE, execute=_digest_schedule_disable),
        ToolEntry(name="digest_run_now", schema=_SCHEMA_RUN_NOW, execute=_digest_run_now),
    ]
