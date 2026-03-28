"""log_query — structured JSONL log querying without shell.

Growth tool: native querying of agent log files (events, tools, chat,
progress, supervisor) with filtering, field extraction, and aggregation.
Replaces fragile shell oneliners and avoids run_shell timeouts.
"""

from __future__ import annotations

import json
import operator
import os
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")

_LOG_FILES: Dict[str, str] = {
    "events": "logs/events.jsonl",
    "tools": "logs/tools.jsonl",
    "chat": "logs/chat.jsonl",
    "progress": "logs/progress.jsonl",
    "supervisor": "logs/supervisor.jsonl",
    "reflections": "logs/task_reflections.jsonl",
}

_MAX_LINES_DEFAULT = 200
_MAX_LINES_CAP = 2000


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_jsonl(path: str, tail: Optional[int] = None) -> List[Dict[str, Any]]:
    """Load records from a JSONL file. Optionally read only the last `tail` lines."""
    p = Path(path)
    if not p.exists():
        return []

    if tail is not None and tail > 0:
        # Efficient tail: read last N lines via seek
        try:
            with open(p, "rb") as f:
                f.seek(0, 2)
                file_size = f.tell()
                if file_size == 0:
                    return []
                # Read up to 512 KB from the end — enough for most tails
                chunk = min(file_size, 512 * 1024)
                f.seek(-chunk, 2)
                raw = f.read().decode("utf-8", errors="replace")
            lines = raw.splitlines()
            # Drop first (possibly partial) line if we didn't start at offset 0
            if chunk < file_size and lines:
                lines = lines[1:]
            lines = lines[-tail:]
        except Exception:
            lines = []
    else:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception:
            return []

    records: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                records.append(obj)
        except json.JSONDecodeError:
            pass
    return records


def _parse_ts(ts_str: str) -> Optional[datetime]:
    """Parse ISO 8601 timestamp string to aware datetime."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _matches(
    record: Dict[str, Any],
    event_type: Optional[str],
    task_id: Optional[str],
    search: Optional[str],
    field_filter: Optional[str],
    since: Optional[datetime],
    until: Optional[datetime],
) -> bool:
    """Return True if record passes all active filters."""

    # Timestamp filter
    if since or until:
        ts_val = record.get("ts", "")
        rec_dt = _parse_ts(ts_val)
        if rec_dt is None:
            return False
        if since and rec_dt < since:
            return False
        if until and rec_dt > until:
            return False

    # Event type filter (substring match for convenience)
    if event_type:
        rec_type = record.get("type", "")
        if event_type.lower() not in rec_type.lower():
            return False

    # Task ID filter
    if task_id:
        rec_tid = record.get("task_id", record.get("task", {}).get("id", "") if isinstance(record.get("task"), dict) else "")
        if task_id not in str(rec_tid):
            return False

    # Full-text search (serialized JSON)
    if search:
        serialized = json.dumps(record, ensure_ascii=False)
        if search.lower() not in serialized.lower():
            return False

    # Field equality filter: "field=value" or "field>value" or "field<value"
    if field_filter:
        try:
            for part in field_filter.split(","):
                part = part.strip()
                for op_str, op_fn in [(">=", operator.ge), ("<=", operator.le),
                                       (">", operator.gt), ("<", operator.lt),
                                       ("=", operator.eq), ("!=", operator.ne)]:
                    if op_str in part:
                        left, right = part.split(op_str, 1)
                        left = left.strip()
                        right = right.strip()
                        rec_val = record
                        for key in left.split("."):
                            if isinstance(rec_val, dict):
                                rec_val = rec_val.get(key)
                            else:
                                rec_val = None
                                break
                        if rec_val is None:
                            return False
                        # Try numeric comparison
                        try:
                            if not op_fn(float(rec_val), float(right)):
                                return False
                        except (ValueError, TypeError):
                            if not op_fn(str(rec_val).lower(), right.lower()):
                                return False
                        break
        except Exception:
            pass  # Malformed filter — ignore silently

    return True


def _aggregate(
    records: List[Dict[str, Any]],
    group_by: Optional[str],
    agg_field: Optional[str],
    agg_func: str,
) -> List[Dict[str, Any]]:
    """Group records and compute aggregation."""
    groups: Dict[str, List[Any]] = {}
    for rec in records:
        key = str(rec.get(group_by, "?")) if group_by else "__all__"
        if key not in groups:
            groups[key] = []
        if agg_field:
            val = rec
            for k in agg_field.split("."):
                if isinstance(val, dict):
                    val = val.get(k)
                else:
                    val = None
                    break
            if val is not None:
                try:
                    groups[key].append(float(val))
                except (TypeError, ValueError):
                    groups[key].append(val)
        else:
            groups[key].append(1)  # count sentinel

    result: List[Dict[str, Any]] = []
    for key, vals in sorted(groups.items(), key=lambda x: -len(x[1])):
        numeric = [v for v in vals if isinstance(v, (int, float))]
        entry: Dict[str, Any] = {group_by or "group": key, "count": len(vals)}
        if numeric and agg_field:
            if agg_func in ("sum", "total"):
                entry[f"{agg_field}_{agg_func}"] = round(sum(numeric), 6)
            elif agg_func == "avg":
                entry[f"{agg_field}_avg"] = round(sum(numeric) / len(numeric), 6)
            elif agg_func == "max":
                entry[f"{agg_field}_max"] = max(numeric)
            elif agg_func == "min":
                entry[f"{agg_field}_min"] = min(numeric)
            else:
                entry[f"{agg_field}_sum"] = round(sum(numeric), 6)
        result.append(entry)
    return result


# ── main handler ──────────────────────────────────────────────────────────────

def _log_query(
    ctx: ToolContext,
    log: str = "events",
    event_type: str = "",
    task_id: str = "",
    search: str = "",
    field_filter: str = "",
    since: str = "",
    until: str = "",
    limit: int = 50,
    tail: int = 0,
    fields: str = "",
    group_by: str = "",
    agg_field: str = "",
    agg_func: str = "count",
) -> str:
    """Query JSONL log files with filtering and optional aggregation."""

    # Resolve log file
    log_key = log.lower().strip()
    if log_key in _LOG_FILES:
        log_path = str(Path(_DRIVE_ROOT) / _LOG_FILES[log_key])
    else:
        # Allow absolute path as fallback
        log_path = log
    if not Path(log_path).exists():
        return json.dumps({
            "error": f"Log file not found: {log_path}",
            "available_logs": list(_LOG_FILES.keys()),
        }, indent=2)

    # Clamp limit
    limit = max(1, min(limit, _MAX_LINES_CAP))

    # Resolve time filters
    since_dt = _parse_ts(since) if since else None
    until_dt = _parse_ts(until) if until else None

    # Load — if tail requested and no other filters, load only tail lines first
    load_tail = tail if (tail > 0 and not since and not until and not event_type
                         and not task_id and not search and not field_filter) else None
    if load_tail is not None:
        records = _load_jsonl(log_path, tail=min(load_tail, _MAX_LINES_CAP))
    else:
        records = _load_jsonl(log_path)

    # Filter
    filtered = [
        r for r in records
        if _matches(r, event_type or None, task_id or None,
                    search or None, field_filter or None,
                    since_dt, until_dt)
    ]

    total_matched = len(filtered)

    # Aggregation mode
    if group_by or (agg_field and agg_func != "count"):
        agg_result = _aggregate(filtered, group_by or None, agg_field or None, agg_func)
        return json.dumps({
            "log": log_key,
            "total_matched": total_matched,
            "aggregation": {
                "group_by": group_by or None,
                "agg_field": agg_field or None,
                "agg_func": agg_func,
            },
            "results": agg_result[:limit],
        }, ensure_ascii=False, indent=2)

    # Extract specific fields if requested
    field_list = [f.strip() for f in fields.split(",") if f.strip()] if fields else []

    # Return last `limit` matched records (most recent)
    sliced = filtered[-limit:]

    if field_list:
        output_records = []
        for rec in sliced:
            extracted: Dict[str, Any] = {}
            for fld in field_list:
                val = rec
                for k in fld.split("."):
                    if isinstance(val, dict):
                        val = val.get(k)
                    else:
                        val = None
                        break
                extracted[fld] = val
            output_records.append(extracted)
    else:
        output_records = sliced

    return json.dumps({
        "log": log_key,
        "total_matched": total_matched,
        "returned": len(sliced),
        "records": output_records,
    }, ensure_ascii=False, indent=2)


# ── registry ──────────────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="log_query",
            schema={
                "name": "log_query",
                "description": (
                    "Query agent JSONL log files (events, tools, chat, progress, supervisor, reflections) "
                    "with filtering, field extraction, and aggregation. "
                    "Use instead of shell grep/jq for log analysis and self-diagnostics. "
                    "Supports: filter by event type, task_id, text search, field comparisons, time range. "
                    "Aggregation: group_by + agg_field + agg_func (count/sum/avg/max/min). "
                    "Field extraction: comma-separated field names to return only specific fields. "
                    "Dotted paths supported for nested fields (e.g. 'task.id', 'cost_usd')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "log": {
                            "type": "string",
                            "description": "Log to query: events, tools, chat, progress, supervisor, reflections. Default: events.",
                            "enum": ["events", "tools", "chat", "progress", "supervisor", "reflections"],
                        },
                        "event_type": {
                            "type": "string",
                            "description": "Filter by event type (substring match). E.g. 'llm_usage', 'task_done', 'tool_timeout'.",
                        },
                        "task_id": {
                            "type": "string",
                            "description": "Filter by task_id (substring match).",
                        },
                        "search": {
                            "type": "string",
                            "description": "Full-text search in serialized JSON of each record (case-insensitive).",
                        },
                        "field_filter": {
                            "type": "string",
                            "description": (
                                "Field comparison filter(s), comma-separated. Operators: =, !=, >, <, >=, <=. "
                                "Examples: 'cost_usd>0.1', 'round>=30', 'type=llm_usage'. "
                                "Dotted paths for nested: 'task.type=evolution'."
                            ),
                        },
                        "since": {
                            "type": "string",
                            "description": "ISO 8601 timestamp lower bound (inclusive). E.g. '2026-03-28T10:00:00Z'.",
                        },
                        "until": {
                            "type": "string",
                            "description": "ISO 8601 timestamp upper bound (inclusive). E.g. '2026-03-28T12:00:00Z'.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max records to return (default 50, max 2000).",
                        },
                        "tail": {
                            "type": "integer",
                            "description": "When set, read only the last N lines from the file (efficient, no full scan). Use for 'latest events' queries.",
                        },
                        "fields": {
                            "type": "string",
                            "description": "Comma-separated field names to extract from each record. Dotted paths supported. If empty, returns full records.",
                        },
                        "group_by": {
                            "type": "string",
                            "description": "Field name to group results by. Enables aggregation mode. E.g. 'type', 'model', 'task_type'.",
                        },
                        "agg_field": {
                            "type": "string",
                            "description": "Numeric field to aggregate (requires group_by). E.g. 'cost_usd', 'prompt_tokens', 'duration_sec'.",
                        },
                        "agg_func": {
                            "type": "string",
                            "description": "Aggregation function: count, sum, avg, max, min. Default: count.",
                            "enum": ["count", "sum", "avg", "max", "min"],
                        },
                    },
                },
            },
            handler=_log_query,
            timeout_sec=30,
        ),
    ]
