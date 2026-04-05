"""ops_timeline — unified chronological stream from all operational logs.

Merges events.jsonl, tools.jsonl, chat.jsonl, progress.jsonl, and
supervisor.jsonl into a single time-ordered stream. Each record is annotated
with its source. Supports filtering by time window, source, task_id,
keyword, and event type.

Without this tool, answering "what happened between 03:00 and 03:15?" or
"trace everything during task X" requires 5 separate log_query calls and
manual merging. ops_timeline closes that gap.

Usage:
    ops_timeline(minutes=30)                      # last 30 minutes, all sources
    ops_timeline(task_id="abc123")                # everything for a task
    ops_timeline(since="2026-04-05T03:00Z", until="2026-04-05T03:15Z")
    ops_timeline(sources="events,tools", search="timeout")
    ops_timeline(minutes=60, event_type="llm_round")
    ops_timeline(format="json", limit=200)
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")

_SOURCE_FILES: Dict[str, str] = {
    "events":     "logs/events.jsonl",
    "tools":      "logs/tools.jsonl",
    "chat":       "logs/chat.jsonl",
    "progress":   "logs/progress.jsonl",
    "supervisor": "logs/supervisor.jsonl",
}

_SOURCE_ORDER = list(_SOURCE_FILES.keys())

# Label icons per source for text rendering
_SOURCE_ICON: Dict[str, str] = {
    "events":     "⚡",
    "tools":      "🔧",
    "chat":       "💬",
    "progress":   "⚙️",
    "supervisor": "🖥",
}


# ── helpers ────────────────────────────────────────────────────────────────────

def _parse_ts(ts_str: str) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except (ValueError, TypeError):
        return None


def _load_source(
    path: pathlib.Path,
    since: Optional[datetime],
    until: Optional[datetime],
    task_id: Optional[str],
    search: Optional[str],
    event_type: Optional[str],
    source_name: str,
) -> List[Dict[str, Any]]:
    """Load and filter records from one JSONL source file."""
    if not path.exists():
        return []

    records: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue

                # Time filter
                ts_str = rec.get("ts", "")
                ts_dt = _parse_ts(ts_str) if ts_str else None
                if since and (ts_dt is None or ts_dt < since):
                    continue
                if until and (ts_dt is None or ts_dt > until):
                    continue

                # Task ID filter
                if task_id:
                    tid = rec.get("task_id") or ""
                    # Also check nested task.id
                    if not tid:
                        task_obj = rec.get("task")
                        if isinstance(task_obj, dict):
                            tid = task_obj.get("id", "")
                    if task_id not in str(tid):
                        continue

                # Event type filter
                if event_type:
                    rec_type = rec.get("type", "")
                    if event_type.lower() not in rec_type.lower():
                        continue

                # Full-text search
                if search:
                    if search.lower() not in json.dumps(rec, ensure_ascii=False).lower():
                        continue

                # Annotate with source
                rec["_source"] = source_name
                rec["_ts_dt"] = ts_dt  # for sorting (stripped before output)
                records.append(rec)
    except Exception as exc:
        log.warning("ops_timeline: failed to read %s: %s", path, exc)

    return records


def _build_summary_line(rec: Dict[str, Any], source: str) -> str:
    """Build a compact single-line description of a log record."""
    ts = rec.get("ts", "")[:19]
    rtype = rec.get("type", "")
    icon = _SOURCE_ICON.get(source, "•")

    if source == "tools":
        tool_name = rec.get("tool", "?")
        task_id_short = str(rec.get("task_id", ""))[:8]
        return f"{ts}  {icon} [{source}]  {tool_name}  task={task_id_short}"

    if source == "chat":
        role = rec.get("role", "?")
        text = (rec.get("text") or rec.get("content") or "")
        text_short = str(text)[:80].replace("\n", " ")
        return f"{ts}  {icon} [{source}/{role}]  {text_short}"

    if source == "progress":
        text = (rec.get("text") or rec.get("content") or "")
        text_short = str(text)[:80].replace("\n", " ")
        return f"{ts}  {icon} [{source}]  {text_short}"

    if source == "supervisor":
        msg = (rec.get("msg") or rec.get("message") or rec.get("event") or "")
        msg_short = str(msg)[:80].replace("\n", " ")
        return f"{ts}  {icon} [{source}]  {rtype or msg_short}"

    # events — be descriptive by type
    if rtype == "llm_round":
        model = rec.get("model", "?")
        cost = rec.get("cost_usd", 0)
        rounds = rec.get("round", "?")
        prompt = rec.get("prompt_tokens", 0)
        return f"{ts}  {icon} [{source}]  llm_round  model={model}  round={rounds}  tokens={prompt}  cost=${cost:.5f}"

    if rtype in ("tool_timeout", "tool_error"):
        tool = rec.get("tool", "?")
        err = (rec.get("error") or "")[:60]
        return f"{ts}  {icon} [{source}]  {rtype}  tool={tool}  {err}"

    if rtype == "llm_api_error":
        err = (rec.get("error") or "")[:60]
        return f"{ts}  {icon} [{source}]  llm_api_error  {err}"

    if rtype in ("task_received", "task_done", "task_failed"):
        task_obj = rec.get("task") or {}
        task_type = task_obj.get("type", rec.get("task_type", "?")) if isinstance(task_obj, dict) else "?"
        tid = (task_obj.get("id", "") if isinstance(task_obj, dict) else "") or rec.get("task_id", "")
        return f"{ts}  {icon} [{source}]  {rtype}  type={task_type}  id={str(tid)[:8]}"

    # Generic fallback
    parts = [rtype] if rtype else []
    for key in ("task_id", "model", "tool", "error", "msg", "event"):
        val = rec.get(key)
        if val:
            parts.append(f"{key}={str(val)[:40]}")
    return f"{ts}  {icon} [{source}]  {' '.join(parts)}"


# ── main handler ───────────────────────────────────────────────────────────────

def _ops_timeline(
    ctx: ToolContext,
    minutes: int = 0,
    since: str = "",
    until: str = "",
    sources: str = "",
    task_id: str = "",
    search: str = "",
    event_type: str = "",
    limit: int = 200,
    format: str = "text",
    verbose: bool = False,
    _drive_root: Optional[str] = None,
) -> str:
    """Merge and filter log records from all sources into a unified timeline."""
    drive_root = pathlib.Path(_drive_root if _drive_root else _DRIVE_ROOT)

    # Resolve time window
    since_dt: Optional[datetime] = None
    until_dt: Optional[datetime] = None

    if minutes > 0:
        since_dt = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes)
    elif since:
        since_dt = _parse_ts(since)

    if until:
        until_dt = _parse_ts(until)

    # Resolve sources
    if sources:
        source_list = [s.strip().lower() for s in sources.split(",") if s.strip()]
        # Validate
        invalid = [s for s in source_list if s not in _SOURCE_FILES]
        if invalid:
            return json.dumps({
                "error": f"Unknown sources: {invalid}",
                "available": list(_SOURCE_FILES.keys()),
            }, indent=2)
    else:
        source_list = _SOURCE_ORDER[:]

    limit = max(1, min(limit, 5000))

    # Load from all sources
    all_records: List[Dict[str, Any]] = []
    source_counts: Dict[str, int] = {}
    for src in source_list:
        file_path = drive_root / _SOURCE_FILES[src]
        recs = _load_source(
            path=file_path,
            since=since_dt,
            until=until_dt,
            task_id=task_id or None,
            search=search or None,
            event_type=event_type or None,
            source_name=src,
        )
        source_counts[src] = len(recs)
        all_records.extend(recs)

    # Sort by timestamp (None timestamps go last)
    all_records.sort(key=lambda r: r.get("_ts_dt") or datetime.max.replace(tzinfo=timezone.utc))

    total_matched = len(all_records)

    # Trim to limit (take most recent if too many)
    if len(all_records) > limit:
        all_records = all_records[-limit:]
        truncated = True
    else:
        truncated = False

    # Strip internal sort key before output
    for r in all_records:
        r.pop("_ts_dt", None)

    if format == "json":
        return json.dumps({
            "total_matched": total_matched,
            "returned": len(all_records),
            "truncated": truncated,
            "source_counts": source_counts,
            "records": all_records,
        }, ensure_ascii=False, indent=2, default=str)

    # Text output
    lines = []
    window_desc = ""
    if since_dt:
        window_desc = f"since {since_dt.strftime('%Y-%m-%dT%H:%M')}Z"
        if until_dt:
            window_desc += f" until {until_dt.strftime('%Y-%m-%dT%H:%M')}Z"
    elif minutes:
        window_desc = f"last {minutes}min"

    header_parts = [f"ops_timeline  total={total_matched}"]
    if window_desc:
        header_parts.append(window_desc)
    if task_id:
        header_parts.append(f"task={task_id}")
    if truncated:
        header_parts.append(f"showing last {limit}")
    lines.append("## " + "  ".join(header_parts))
    lines.append(
        "   Sources: " + "  ".join(
            f"{src}={source_counts.get(src, 0)}" for src in source_list
        )
    )
    lines.append("")

    if not all_records:
        lines.append("  (no records found in window)")
        return "\n".join(lines)

    for rec in all_records:
        src = rec.get("_source", "?")
        if verbose:
            # Full JSON per record
            rec_copy = {k: v for k, v in rec.items() if k != "_source"}
            lines.append(f"  {json.dumps(rec_copy, ensure_ascii=False)}")
        else:
            lines.append("  " + _build_summary_line(rec, src))

    return "\n".join(lines)


# ── Tool registration ──────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="ops_timeline",
            schema={
                "name": "ops_timeline",
                "description": (
                    "Unified chronological event stream from all operational logs "
                    "(events, tools, chat, progress, supervisor). Merges and sorts "
                    "records from multiple sources into a single time-ordered view. "
                    "Use to investigate 'what happened during task X', "
                    "'what happened between 03:00 and 03:15', "
                    "or 'show all tool timeouts in the last hour'. "
                    "Much faster than calling log_query 5 times and manually merging."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "minutes": {
                            "type": "integer",
                            "description": "Show records from the last N minutes. Mutually exclusive with 'since'.",
                        },
                        "since": {
                            "type": "string",
                            "description": "ISO 8601 lower bound. E.g. '2026-04-05T03:00:00Z'.",
                        },
                        "until": {
                            "type": "string",
                            "description": "ISO 8601 upper bound. E.g. '2026-04-05T03:15:00Z'.",
                        },
                        "sources": {
                            "type": "string",
                            "description": (
                                "Comma-separated source names to include. "
                                "Options: events, tools, chat, progress, supervisor. "
                                "Default: all sources."
                            ),
                        },
                        "task_id": {
                            "type": "string",
                            "description": "Filter to records belonging to this task_id (substring match).",
                        },
                        "search": {
                            "type": "string",
                            "description": "Case-insensitive keyword search across all fields.",
                        },
                        "event_type": {
                            "type": "string",
                            "description": (
                                "Filter by event type (substring match). E.g. 'llm_round', 'tool_timeout', "
                                "'task_done', 'llm_api_error'."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max records to return (default 200, max 5000). When exceeded, shows most recent.",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format (default: text).",
                        },
                        "verbose": {
                            "type": "boolean",
                            "description": "If true, output full JSON per record instead of compact summary line.",
                        },
                    },
                },
            },
            handler=lambda ctx, **kw: _ops_timeline(ctx, **kw),
        )
    ]
