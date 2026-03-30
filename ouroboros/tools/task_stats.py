"""task_stats — one-call profiler for any task_id.

Growth tool: replaces 5-6 log_query calls with a single tool invocation.
Returns a structured profile of a task:
  - timing (start, end, duration_sec)
  - LLM rounds breakdown (count, models used, token totals, cache rate)
  - cost (real USD, shadow USD)
  - tool calls (total, per-tool breakdown, timeouts/errors)
  - errors (count, types, first occurrence)
  - final status (done/failed/still_running + reason)

Useful for debugging, cost auditing, and post-mortem analysis.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")

_LOG_FILES = {
    "events": "logs/events.jsonl",
    "tools": "logs/tools.jsonl",
    "progress": "logs/progress.jsonl",
}


def _load_jsonl_tail(path: Path, tail_bytes: int = 2_000_000) -> List[Dict[str, Any]]:
    """Load records from a JSONL file, reading up to tail_bytes from the end."""
    if not path.exists():
        return []
    file_size = path.stat().st_size
    try:
        with open(path, "rb") as f:
            if file_size > tail_bytes:
                f.seek(-tail_bytes, 2)
                f.readline()  # skip partial line
            else:
                f.seek(0)
            raw = f.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    records: List[Dict[str, Any]] = []
    for line in raw.splitlines():
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
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _filter_by_task(records: List[Dict[str, Any]], task_id: str) -> List[Dict[str, Any]]:
    """Return records where task_id matches (substring)."""
    result = []
    for r in records:
        tid = r.get("task_id", r.get("task", {}).get("id", "") if isinstance(r.get("task"), dict) else "")
        if task_id in str(tid):
            result.append(r)
    return result


def _task_stats(
    ctx: ToolContext,
    task_id: str = "",
    recent: bool = False,
    limit: int = 5,
) -> str:
    """Compute a full stats profile for a task_id (or N most recent tasks)."""

    drive = Path(_DRIVE_ROOT)

    # --- Load recent logs ---
    events = _load_jsonl_tail(drive / _LOG_FILES["events"])
    tools_log = _load_jsonl_tail(drive / _LOG_FILES["tools"])
    progress_log = _load_jsonl_tail(drive / _LOG_FILES["progress"])

    # --- Recent tasks mode: list N most recent task_done/task_failed ---
    if recent or not task_id:
        task_events: List[Dict[str, Any]] = []
        for r in events:
            if r.get("type") in ("task_done", "task_failed"):
                task_events.append(r)
        task_events = task_events[-limit:]
        results = []
        for te in reversed(task_events):
            tid = te.get("task_id", "?")
            profile = _build_profile(tid, events, tools_log)
            results.append(profile)
        return json.dumps({
            "mode": "recent",
            "limit": limit,
            "tasks": results,
        }, ensure_ascii=False, indent=2)

    # --- Single task mode ---
    profile = _build_profile(task_id, events, tools_log)
    return json.dumps(profile, ensure_ascii=False, indent=2)


def _build_profile(
    task_id: str,
    events: List[Dict[str, Any]],
    tools_log: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build a full stats profile for a single task_id."""

    task_events = _filter_by_task(events, task_id)
    task_tools = _filter_by_task(tools_log, task_id)

    # --- Timing ---
    timestamps = []
    for r in task_events:
        dt = _parse_ts(r.get("ts", ""))
        if dt:
            timestamps.append(dt)
    start_ts = min(timestamps).isoformat() if timestamps else None
    end_ts = max(timestamps).isoformat() if timestamps else None
    duration_sec = None
    if timestamps and len(timestamps) >= 2:
        duration_sec = round((max(timestamps) - min(timestamps)).total_seconds(), 1)

    # --- Final status ---
    status = "unknown"
    status_reason = None
    for r in reversed(task_events):
        if r.get("type") == "task_done":
            status = "done"
            break
        elif r.get("type") == "task_failed":
            status = "failed"
            status_reason = r.get("reason", r.get("error", ""))
            break
    if status == "unknown" and task_events:
        status = "still_running"

    # --- LLM rounds ---
    round_events = [r for r in task_events if r.get("type") == "llm_round"]
    round_count = len(round_events)

    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cached_tokens = 0
    total_cache_write_tokens = 0
    total_cost_usd = 0.0
    total_shadow_cost = 0.0
    models_used: Counter = Counter()
    per_round_tokens: List[Dict[str, Any]] = []

    for r in round_events:
        pt = int(r.get("prompt_tokens", 0))
        ct = int(r.get("completion_tokens", 0))
        cached = int(r.get("cached_tokens", 0))
        cache_write = int(r.get("cache_write_tokens", 0))
        cost = float(r.get("cost_usd", 0.0))
        shadow = float(r.get("shadow_cost", 0.0))
        model = r.get("model", "unknown")

        total_prompt_tokens += pt
        total_completion_tokens += ct
        total_cached_tokens += cached
        total_cache_write_tokens += cache_write
        total_cost_usd += cost
        total_shadow_cost += shadow
        models_used[model] += 1

        per_round_tokens.append({
            "round": r.get("round", "?"),
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "cached_tokens": cached,
            "cost_usd": round(cost, 6),
            "shadow_cost": round(shadow, 6),
        })

    cache_hit_rate = None
    if total_prompt_tokens > 0:
        cache_hit_rate = round(total_cached_tokens / total_prompt_tokens, 3)

    # --- Tool calls from tools.jsonl ---
    tool_counts: Counter = Counter()
    tool_durations: Dict[str, List[float]] = defaultdict(list)
    tool_errors: List[Dict[str, Any]] = []
    tool_timeouts: List[str] = []

    for r in task_tools:
        tool_name = r.get("tool", r.get("name", "unknown"))
        tool_counts[tool_name] += 1
        dur = r.get("duration_sec")
        if dur is not None:
            try:
                tool_durations[tool_name].append(float(dur))
            except (TypeError, ValueError):
                pass
        if r.get("error") or r.get("status") == "error":
            tool_errors.append({
                "tool": tool_name,
                "error": str(r.get("error", r.get("result", "")))[:200],
                "ts": r.get("ts", ""),
            })
        if r.get("timeout") or r.get("status") == "timeout":
            tool_timeouts.append(tool_name)

    # Also pick up tool errors/timeouts from events.jsonl
    for r in task_events:
        if r.get("type") in ("tool_timeout", "tool_error"):
            tool_name = r.get("tool", "unknown")
            if r.get("type") == "tool_timeout":
                tool_timeouts.append(tool_name)
            else:
                tool_errors.append({
                    "tool": tool_name,
                    "error": str(r.get("error", ""))[:200],
                    "ts": r.get("ts", ""),
                })

    # --- Error events ---
    error_types: Counter = Counter()
    first_error_ts = None
    for r in task_events:
        etype = r.get("type", "")
        if "error" in etype.lower() or "fail" in etype.lower() or "timeout" in etype.lower():
            error_types[etype] += 1
            ts = _parse_ts(r.get("ts", ""))
            if ts and (first_error_ts is None or ts < first_error_ts):
                first_error_ts = ts
    # Copilot-specific server errors
    copilot_server_cooldowns = sum(1 for r in task_events if r.get("type") == "copilot_server_cooldown")

    # Top tools by call count
    top_tools = [
        {
            "tool": name,
            "calls": count,
            "avg_duration_sec": round(sum(tool_durations[name]) / len(tool_durations[name]), 2)
            if tool_durations[name] else None,
        }
        for name, count in tool_counts.most_common(15)
    ]

    # --- Task type ---
    task_type = None
    for r in task_events:
        if r.get("task_type"):
            task_type = r["task_type"]
            break
        if r.get("type") in ("task_done", "task_failed"):
            task_type = r.get("task_type")
            break

    return {
        "task_id": task_id,
        "task_type": task_type,
        "status": status,
        "status_reason": status_reason or None,
        # Timing
        "start_ts": start_ts,
        "end_ts": end_ts,
        "duration_sec": duration_sec,
        # LLM
        "rounds": round_count,
        "models_used": dict(models_used),
        "prompt_tokens_total": total_prompt_tokens,
        "completion_tokens_total": total_completion_tokens,
        "cached_tokens_total": total_cached_tokens,
        "cache_write_tokens_total": total_cache_write_tokens,
        "cache_hit_rate": cache_hit_rate,
        "cost_usd": round(total_cost_usd, 6),
        "shadow_cost_usd": round(total_shadow_cost, 4),
        # Tools
        "tool_calls_total": sum(tool_counts.values()),
        "tool_calls_unique": len(tool_counts),
        "tool_timeouts": list(dict.fromkeys(tool_timeouts)),  # deduplicated, order preserved
        "tool_errors_count": len(tool_errors),
        "tool_errors": tool_errors[:10],
        "top_tools": top_tools,
        # Errors
        "error_event_types": dict(error_types),
        "first_error_ts": first_error_ts.isoformat() if first_error_ts else None,
        "copilot_server_cooldowns": copilot_server_cooldowns,
        # Per-round detail (last 5 rounds to avoid bloat)
        "last_rounds": per_round_tokens[-5:] if per_round_tokens else [],
    }


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="task_stats",
            schema={
                "name": "task_stats",
                "description": (
                    "Profile any task_id with a single call: timing, LLM rounds, token/cost breakdown, "
                    "cache hit rate, tool call breakdown, timeouts, errors, Copilot server cooldowns, "
                    "and final status. "
                    "Replaces multiple log_query calls for post-mortem analysis and cost auditing. "
                    "Parameters:\n"
                    "- task_id: full or partial task ID to profile (required unless recent=true)\n"
                    "- recent: if true, list the N most recently completed tasks with their profiles\n"
                    "- limit: how many recent tasks to return (default 5, used with recent=true)\n"
                    "Returns: structured JSON with all key task metrics."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "Full or partial task_id to profile.",
                        },
                        "recent": {
                            "type": "boolean",
                            "description": "If true, return N most recently completed tasks. Default: false.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max tasks to return in recent mode. Default: 5.",
                        },
                    },
                    "required": [],
                },
            },
            handler=_task_stats,
        )
    ]
