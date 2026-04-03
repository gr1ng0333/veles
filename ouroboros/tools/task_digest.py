"""task_digest — detailed per-task report from logs.

Given a task_id (or "last" / "last:N"), assembles a complete picture:
  - task type, goal text, start/end timestamps, duration
  - model used, rounds, total tokens, shadow cost
  - tool calls: name, args preview, result preview, errors
  - LLM errors (api_error, tool_timeout events)
  - task reflection (if present)

Why: investigating any completed task currently requires manual grep across
3 log files. This closes that gap and makes task retrospectives instant.

Usage:
    task_digest(task_id="adf32351")         # specific task
    task_digest(task_id="last")             # most recent task
    task_digest(task_id="last:3")           # 3rd-most-recent task
    task_digest(task_id="last", format="json")
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")

# ── Log loading helpers ───────────────────────────────────────────────────────

def _iter_jsonl(path: pathlib.Path, max_lines: int = 50_000):
    """Yield parsed JSON objects from a jsonl file (tail max_lines)."""
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as exc:
        log.warning("task_digest: cannot read %s: %s", path, exc)
        return
    for raw in lines[-max_lines:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


# ── Task ID resolution ────────────────────────────────────────────────────────

def _resolve_task_id(task_id: str, drive_root: pathlib.Path) -> Optional[str]:
    """Resolve 'last' / 'last:N' to a concrete task_id, or pass through."""
    if not task_id.startswith("last"):
        return task_id.strip()

    # Collect all task_ids in order from events.jsonl
    seen: List[str] = []
    for ev in _iter_jsonl(drive_root / "logs" / "events.jsonl"):
        tid = ev.get("task_id") or (ev.get("task", {}) or {}).get("id")
        if tid and (not seen or seen[-1] != tid):
            seen.append(tid)

    if not seen:
        return None

    m = re.match(r"^last(?::(\d+))?$", task_id.strip())
    if not m:
        return task_id.strip()
    offset = int(m.group(1) or 1)
    idx = len(seen) - offset
    return seen[idx] if 0 <= idx < len(seen) else None


# ── Data collection ───────────────────────────────────────────────────────────

def _collect_events(task_id: str, drive_root: pathlib.Path) -> List[Dict[str, Any]]:
    return [
        ev for ev in _iter_jsonl(drive_root / "logs" / "events.jsonl")
        if (ev.get("task_id") == task_id or
            (ev.get("task", {}) or {}).get("id") == task_id)
    ]


def _collect_tool_calls(task_id: str, drive_root: pathlib.Path) -> List[Dict[str, Any]]:
    return [
        ev for ev in _iter_jsonl(drive_root / "logs" / "tools.jsonl")
        if ev.get("task_id") == task_id
    ]


def _collect_reflection(task_id: str, drive_root: pathlib.Path) -> Optional[Dict[str, Any]]:
    for ev in _iter_jsonl(drive_root / "logs" / "task_reflections.jsonl"):
        if ev.get("task_id") == task_id:
            return ev
    return None


# ── Digest builder ─────────────────────────────────────────────────────────────

def _build_digest(
    task_id: str,
    events: List[Dict[str, Any]],
    tool_calls: List[Dict[str, Any]],
    reflection: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Assemble structured digest from raw log records."""
    # --- Header from task_received ---
    task_type = "unknown"
    goal = ""
    queued_at = ""
    for ev in events:
        if ev.get("type") == "task_received":
            task_obj = ev.get("task", {}) or {}
            task_type = task_obj.get("type", "unknown")
            goal = task_obj.get("text", "")
            queued_at = task_obj.get("queued_at", ev.get("ts", ""))
            break

    # --- LLM rounds ---
    rounds: List[Dict[str, Any]] = [
        ev for ev in events if ev.get("type") == "llm_round"
    ]
    total_rounds = len(rounds)
    total_prompt_tokens = sum(r.get("prompt_tokens", 0) for r in rounds)
    total_completion_tokens = sum(r.get("completion_tokens", 0) for r in rounds)
    total_cost_usd = sum(r.get("cost_usd", 0) for r in rounds)
    total_shadow_cost = sum(r.get("shadow_cost", 0) for r in rounds)
    model = rounds[0].get("model", "unknown") if rounds else "unknown"

    # --- Timing ---
    ts_list = [ev.get("ts", "") for ev in events if ev.get("ts")]
    start_ts = min(ts_list) if ts_list else ""
    end_ts = max(ts_list) if ts_list else ""
    duration_s: Optional[float] = None
    if start_ts and end_ts:
        try:
            from datetime import datetime, timezone
            fmt = "%Y-%m-%dT%H:%M:%S.%f+00:00"
            t0 = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
            duration_s = (t1 - t0).total_seconds()
        except Exception:
            pass

    # --- Errors ---
    api_errors = [
        ev for ev in events
        if ev.get("type") in ("llm_api_error", "tool_timeout", "tool_error")
    ]

    # --- Tool call summary ---
    tool_summary: List[Dict[str, Any]] = []
    for tc in tool_calls:
        tool_summary.append({
            "ts": tc.get("ts", ""),
            "tool": tc.get("tool", "?"),
            "args": tc.get("args", {}),
            "result_preview": (tc.get("result_preview") or "")[:200],
        })

    return {
        "task_id": task_id,
        "task_type": task_type,
        "goal": goal[:500],
        "queued_at": queued_at,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "duration_s": duration_s,
        "model": model,
        "rounds": total_rounds,
        "prompt_tokens": total_prompt_tokens,
        "completion_tokens": total_completion_tokens,
        "cost_usd": total_cost_usd,
        "shadow_cost_usd": total_shadow_cost,
        "tool_calls": tool_summary,
        "errors": api_errors,
        "reflection": reflection,
    }


# ── Text formatter ─────────────────────────────────────────────────────────────

def _format_text(digest: Dict[str, Any]) -> str:
    lines: List[str] = []
    a = lines.append

    a(f"## task_digest: {digest['task_id']}")
    a(f"Type:      {digest['task_type']}")

    goal = digest["goal"]
    if goal:
        goal_short = goal[:200].replace("\n", " ")
        if len(goal) > 200:
            goal_short += "…"
        a(f"Goal:      {goal_short}")

    a(f"Start:     {digest['start_ts']}")
    a(f"End:       {digest['end_ts']}")

    dur = digest["duration_s"]
    if dur is not None:
        a(f"Duration:  {dur:.1f}s")

    a(f"Model:     {digest['model']}")
    a(f"Rounds:    {digest['rounds']}")
    a(f"Tokens:    {digest['prompt_tokens']}p + {digest['completion_tokens']}c")
    a(f"Cost:      ${digest['cost_usd']:.4f} real / ${digest['shadow_cost_usd']:.4f} shadow")

    # Errors
    errors = digest["errors"]
    if errors:
        a("")
        a(f"⚠️  Errors ({len(errors)}):")
        for err in errors[:5]:
            etype = err.get("type", "?")
            detail = err.get("error") or err.get("tool") or ""
            a(f"   [{err.get('ts','')[:19]}] {etype}: {detail}")
        if len(errors) > 5:
            a(f"   … +{len(errors) - 5} more")
    else:
        a("✅  No errors")

    # Tool calls
    tool_calls = digest["tool_calls"]
    if tool_calls:
        a("")
        a(f"🔧 Tool calls ({len(tool_calls)}):")
        for tc in tool_calls[:20]:
            tool_name = tc["tool"]
            # Compact args
            args = tc.get("args", {})
            if isinstance(args, dict):
                arg_str = ", ".join(f"{k}={repr(v)[:40]}" for k, v in list(args.items())[:3])
            else:
                arg_str = str(args)[:80]
            a(f"   {tc['ts'][:19]}  {tool_name}({arg_str})")
        if len(tool_calls) > 20:
            a(f"   … +{len(tool_calls) - 20} more")
    else:
        a("   (no tool calls logged)")

    # Reflection
    ref = digest["reflection"]
    if ref:
        a("")
        a("📝 Reflection:")
        markers = ref.get("key_markers", [])
        if markers:
            a(f"   Markers:  {', '.join(markers)}")
        ref_text = (ref.get("reflection") or "").strip()
        if ref_text:
            excerpt = ref_text[:400].replace("\n", " ")
            if len(ref_text) > 400:
                excerpt += "…"
            a(f"   Text:     {excerpt}")

    return "\n".join(lines)


# ── Main entry ────────────────────────────────────────────────────────────────

def _task_digest(ctx: ToolContext, task_id: str, format: str = "text") -> str:
    drive = ctx.drive_root if (ctx and ctx.drive_root) else pathlib.Path(_DRIVE_ROOT)

    resolved = _resolve_task_id(task_id, drive)
    if not resolved:
        return f"task_digest: cannot resolve task_id '{task_id}'."

    events = _collect_events(resolved, drive)
    if not events:
        return f"task_digest: no events found for task_id '{resolved}'."

    tool_calls = _collect_tool_calls(resolved, drive)
    reflection = _collect_reflection(resolved, drive)

    digest = _build_digest(resolved, events, tool_calls, reflection)

    if format == "json":
        return json.dumps(digest, ensure_ascii=False, indent=2, default=str)

    return _format_text(digest)


# ── Tool registration ─────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="task_digest",
            schema={
                "name": "task_digest",
                "description": (
                    "Full per-task report assembled from events.jsonl, tools.jsonl, "
                    "and task_reflections.jsonl. Returns: task type, goal, duration, "
                    "model, rounds, tokens, cost, tool call list, errors, and reflection. "
                    "Use to investigate any completed task without manual log grepping. "
                    "task_id accepts a hex id, 'last' (most recent), or 'last:N' (Nth from end)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": (
                                "Task ID to inspect. Use hex id, 'last', or 'last:2' / 'last:3' etc."
                            ),
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format (default: text)",
                        },
                    },
                    "required": ["task_id"],
                },
            },
            handler=lambda ctx, **kw: _task_digest(ctx, **kw),
        )
    ]
