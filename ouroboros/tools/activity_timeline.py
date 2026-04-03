"""Activity timeline — merged chronological view of what Veles did.

Joins events.jsonl + supervisor.jsonl + chat.jsonl into a single readable
timeline covering the requested window (default: last 6h).

Why: task_stats profiles individual tasks; context_inspect shows token budget.
Neither answers "what happened in the last 4 hours?" at a glance.
activity_timeline fills that gap — one call, full picture:
  - restarts (with source and SHA)
  - owner messages (incoming + outgoing, clipped)
  - evolution tasks (started / done / failed, rounds, model, cost)
  - user tasks (started / done / failed)
  - tool timeouts / errors
  - Copilot capacity events

Output formats:
  - "text" (default): compact human-readable timeline, one line per event group
  - "json": full structured records
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from collections import defaultdict
from datetime import datetime, timezone as dt_timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")


# ── Timestamp helpers ────────────────────────────────────────────────────────

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


def _fmt_ts(dt: datetime) -> str:
    """Format as HH:MM:SS (UTC)."""
    return dt.strftime("%H:%M:%S")


def _fmt_dur(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.0f}m"
    return f"{seconds/3600:.1f}h"


# ── Loaders ──────────────────────────────────────────────────────────────────

def _load_jsonl_window(
    path: pathlib.Path,
    since: datetime,
    tail_bytes: int = 3_000_000,
) -> List[Dict[str, Any]]:
    """Load records from a JSONL file newer than *since*."""
    if not path.exists():
        return []
    file_size = path.stat().st_size
    try:
        with path.open("rb") as f:
            if file_size > tail_bytes:
                f.seek(-tail_bytes, 2)
                f.readline()  # skip partial first line
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
            if not isinstance(obj, dict):
                continue
            dt = _parse_ts(obj.get("ts", ""))
            if dt and dt >= since:
                records.append(obj)
        except json.JSONDecodeError:
            pass
    return records


# ── Normalizer ───────────────────────────────────────────────────────────────

def _normalise_event(rec: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
    """Convert a raw record to a normalised timeline event dict.

    Returns None if the record should be skipped.
    """
    etype = rec.get("type", "")
    ts_str = rec.get("ts", "")
    dt = _parse_ts(ts_str)
    if dt is None:
        return None

    # ── Restarts ─────────────────────────────────────────────────────────────
    if etype in ("launcher_start", "startup_verification", "restart_verify"):
        sha = rec.get("sha", rec.get("current_sha", ""))[:8]
        src = rec.get("source", rec.get("restart_source", ""))
        branch = rec.get("branch", "")
        label = "🔄 restart"
        detail = f"src={src or '?'} sha={sha or '?'}"
        if branch:
            detail += f" branch={branch}"
        return {"dt": dt, "kind": "restart", "label": label, "detail": detail, "raw": rec}

    # ── Task lifecycle ────────────────────────────────────────────────────────
    if etype == "task_received":
        task_id = rec.get("task_id", "?")[:8]
        task_type = rec.get("task_type", rec.get("type", "?"))
        text = str(rec.get("text", rec.get("task", {}).get("text", "")))[:80]
        return {
            "dt": dt, "kind": "task_start",
            "label": f"📥 task {task_type}",
            "detail": f"id={task_id} {text!r}",
            "task_id": rec.get("task_id", ""),
            "task_type": task_type,
            "raw": rec,
        }

    if etype == "task_done":
        task_id = rec.get("task_id", "?")[:8]
        task_type = rec.get("task_type", "?")
        rounds = rec.get("rounds", "?")
        cost = rec.get("cost_usd", rec.get("shadow_cost", None))
        cost_str = f" ${cost:.3f}" if cost is not None else ""
        return {
            "dt": dt, "kind": "task_done",
            "label": f"✅ done {task_type}",
            "detail": f"id={task_id} rounds={rounds}{cost_str}",
            "task_id": rec.get("task_id", ""),
            "raw": rec,
        }

    if etype == "task_failed":
        task_id = rec.get("task_id", "?")[:8]
        reason = str(rec.get("reason", rec.get("error", "")))[:60]
        return {
            "dt": dt, "kind": "task_failed",
            "label": "❌ failed",
            "detail": f"id={task_id} {reason}",
            "task_id": rec.get("task_id", ""),
            "raw": rec,
        }

    if etype == "evolution_enqueued":
        cycle = rec.get("evolution_cycle", "?")
        return {
            "dt": dt, "kind": "evolution_enqueued",
            "label": f"🧬 evolution #{cycle} enqueued",
            "detail": "",
            "raw": rec,
        }

    # ── Tool issues ───────────────────────────────────────────────────────────
    if etype in ("tool_timeout", "TOOL_TIMEOUT"):
        tool = rec.get("tool", "?")
        return {
            "dt": dt, "kind": "tool_timeout",
            "label": f"⏱ timeout {tool}",
            "detail": str(rec.get("error", ""))[:80],
            "raw": rec,
        }

    if etype in ("tool_error", "TOOL_ERROR"):
        tool = rec.get("tool", "?")
        return {
            "dt": dt, "kind": "tool_error",
            "label": f"⚠️ tool_error {tool}",
            "detail": str(rec.get("error", ""))[:80],
            "raw": rec,
        }

    # ── Copilot capacity / cooldown ───────────────────────────────────────────
    if etype in ("copilot_server_cooldown", "copilot_capacity_blocked",
                 "copilot_all_exhausted"):
        return {
            "dt": dt, "kind": "copilot_issue",
            "label": f"🌐 {etype}",
            "detail": str(rec.get("reason", rec.get("error", "")))[:60],
            "raw": rec,
        }

    # ── Supervisor heartbeats (summarised later) ──────────────────────────────
    if etype == "main_loop_heartbeat":
        return {
            "dt": dt, "kind": "heartbeat",
            "label": "💓 heartbeat",
            "detail": (
                f"workers={rec.get('workers_alive','?')}/{rec.get('workers_total','?')} "
                f"pending={rec.get('pending_count',0)} running={rec.get('running_count',0)}"
            ),
            "raw": rec,
        }

    # Skip noisy / low-value event types
    if etype in ("llm_round", "llm_usage", "task_eval", "task_runtime_mode",
                 "worker_boot", "consciousness_audit"):
        return None

    # Catch-all for unknown events
    return {
        "dt": dt, "kind": "misc",
        "label": f"• {etype}",
        "detail": "",
        "raw": rec,
    }


def _normalise_chat(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert a chat.jsonl record to a timeline event."""
    ts_str = rec.get("ts", "")
    dt = _parse_ts(ts_str)
    if dt is None:
        return None
    direction = rec.get("direction", "?")
    text = str(rec.get("text", ""))
    if not text:
        return None
    if direction == "in":
        label = "💬 owner →"
        clipped = text[:120].replace("\n", " ")
    else:
        label = "← veles 💬"
        clipped = text[:80].replace("\n", " ")
    return {
        "dt": dt, "kind": f"chat_{direction}",
        "label": label,
        "detail": clipped + ("…" if len(text) > (120 if direction == "in" else 80) else ""),
        "raw": rec,
    }


# ── Summary / merging ─────────────────────────────────────────────────────────

def _build_timeline(
    drive_root: pathlib.Path,
    since: datetime,
    include_heartbeats: bool = False,
    include_chat: bool = True,
) -> List[Dict[str, Any]]:
    """Build a sorted, normalised timeline from all log sources."""
    events: List[Dict[str, Any]] = []

    # events.jsonl
    ev_records = _load_jsonl_window(drive_root / "logs" / "events.jsonl", since)
    for r in ev_records:
        ne = _normalise_event(r, "events")
        if ne:
            events.append(ne)

    # supervisor.jsonl
    sup_records = _load_jsonl_window(drive_root / "logs" / "supervisor.jsonl", since)
    for r in sup_records:
        ne = _normalise_event(r, "supervisor")
        if ne:
            events.append(ne)

    # chat.jsonl
    if include_chat:
        chat_records = _load_jsonl_window(drive_root / "logs" / "chat.jsonl", since)
        for r in chat_records:
            ne = _normalise_chat(r)
            if ne:
                events.append(ne)

    # Sort by timestamp
    events.sort(key=lambda e: e["dt"])

    # Filter heartbeats unless explicitly requested
    if not include_heartbeats:
        events = [e for e in events if e["kind"] != "heartbeat"]

    return events


def _summarise_heartbeats(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse heartbeat sequences into a single summary event."""
    result: List[Dict[str, Any]] = []
    hb_run: List[Dict[str, Any]] = []

    def flush_hb() -> None:
        if not hb_run:
            return
        first_dt = hb_run[0]["dt"]
        last_dt = hb_run[-1]["dt"]
        dur = (last_dt - first_dt).total_seconds()
        result.append({
            "dt": first_dt,
            "kind": "heartbeat_summary",
            "label": f"💓 ×{len(hb_run)} heartbeats",
            "detail": f"{_fmt_ts(first_dt)}–{_fmt_ts(last_dt)} ({_fmt_dur(dur)})",
        })
        hb_run.clear()

    for e in events:
        if e["kind"] == "heartbeat":
            hb_run.append(e)
        else:
            flush_hb()
            result.append(e)
    flush_hb()
    return result


# ── Formatter ────────────────────────────────────────────────────────────────

def _format_text(
    events: List[Dict[str, Any]],
    since: datetime,
    now: datetime,
) -> str:
    """Format events as human-readable text timeline."""
    if not events:
        window_str = _fmt_dur((now - since).total_seconds())
        return f"No notable events in the last {window_str}."

    lines: List[str] = []
    window_str = _fmt_dur((now - since).total_seconds())
    lines.append(f"## Activity timeline (last {window_str})\n")

    # Compute task durations by pairing start↔done/failed
    task_start: Dict[str, datetime] = {}
    for e in events:
        if e["kind"] == "task_start":
            task_start[e.get("task_id", "")] = e["dt"]

    prev_day: Optional[str] = None
    for e in events:
        day = e["dt"].strftime("%Y-%m-%d")
        if day != prev_day:
            lines.append(f"\n--- {day} ---")
            prev_day = day

        time_str = _fmt_ts(e["dt"])
        label = e["label"]
        detail = e["detail"]

        # Annotate task_done with duration
        if e["kind"] == "task_done":
            tid = e.get("task_id", "")
            if tid and tid in task_start:
                dur = (e["dt"] - task_start[tid]).total_seconds()
                detail += f" dur={_fmt_dur(dur)}"

        if detail:
            lines.append(f"  {time_str}  {label}  {detail}")
        else:
            lines.append(f"  {time_str}  {label}")

    # Summary counts
    counts: Dict[str, int] = defaultdict(int)
    for e in events:
        counts[e["kind"]] += 1

    total_tasks = counts["task_done"] + counts["task_failed"]
    restarts = counts["restart"]
    owner_msgs = counts["chat_in"]
    timeouts = counts["tool_timeout"]

    lines.append("\n---")
    lines.append(
        f"Summary: {total_tasks} tasks, {restarts} restart(s), "
        f"{owner_msgs} owner msg(s), {timeouts} timeout(s)"
    )
    return "\n".join(lines)


# ── Tool entrypoint ───────────────────────────────────────────────────────────

def _activity_timeline(
    ctx: ToolContext,
    hours: float = 6.0,
    format: str = "text",
    include_heartbeats: bool = False,
    include_chat: bool = True,
) -> str:
    """Build and return the activity timeline."""
    drive_root = pathlib.Path(_DRIVE_ROOT)
    now = datetime.now(dt_timezone.utc)
    hours = max(0.1, min(168.0, float(hours)))  # cap at 7 days
    since = now - timedelta(hours=hours)

    events = _build_timeline(drive_root, since, include_heartbeats, include_chat)

    if include_heartbeats:
        events = _summarise_heartbeats(events)

    if format == "json":
        # Serialise — drop 'raw' to avoid bloat
        serialisable = [
            {k: v for k, v in e.items() if k != "raw" and k != "dt"}
            | {"ts": e["dt"].isoformat()}
            for e in events
        ]
        return json.dumps(
            {"since": since.isoformat(), "events": serialisable},
            ensure_ascii=False,
            indent=2,
        )

    return _format_text(events, since, now)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="activity_timeline",
            schema={
                "name": "activity_timeline",
                "description": (
                    "Merged chronological view of what Veles did over the last N hours. "
                    "Joins events.jsonl + supervisor.jsonl + chat.jsonl into a single readable timeline. "
                    "Shows: restarts, owner messages, task starts/completions/failures, "
                    "evolution enqueues, tool timeouts, Copilot capacity issues.\n\n"
                    "More useful than task_stats(recent=True) for answering 'what happened recently?' "
                    "because it shows the full context — owner interactions, restarts, gaps — not just task metrics.\n\n"
                    "Parameters:\n"
                    "- hours: how many hours to look back (default 6, max 168)\n"
                    "- format: 'text' (human-readable, default) or 'json' (structured)\n"
                    "- include_heartbeats: include supervisor heartbeats (verbose, default false)\n"
                    "- include_chat: include owner ↔ Veles messages (default true)"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "hours": {
                            "type": "number",
                            "description": "How many hours to look back (default 6, max 168).",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format. 'text' is human-readable (default). 'json' returns structured records.",
                        },
                        "include_heartbeats": {
                            "type": "boolean",
                            "description": "Include supervisor heartbeats in the output (verbose). Default: false.",
                        },
                        "include_chat": {
                            "type": "boolean",
                            "description": "Include owner ↔ Veles messages. Default: true.",
                        },
                    },
                    "required": [],
                },
            },
            handler=lambda ctx, **kw: _activity_timeline(ctx, **kw),
        )
    ]
