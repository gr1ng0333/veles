"""evolution_report — one-call audit of recent evolution cycles.

For each of the last N evolution tasks shows:
  - task timing (start, end, duration)
  - LLM rounds, model, shadow cost
  - tool errors / timeouts
  - git commits produced during the task window (by timestamp correlation)
  - file-level diff stat for each commit

Why: task_stats profiles metrics; activity_timeline shows events.
Neither answers "what code did evolution #159 actually change?" at a glance.
evolution_report bridges that gap: one call, full audit per cycle.

Usage:
    evolution_report()                  # last 5 evolution cycles
    evolution_report(limit=10)          # last 10
    evolution_report(cycle=161)         # specific cycle number
    evolution_report(format="json")     # machine-readable
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections import Counter
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")
_REPO_DIR = os.environ.get("OUROBOROS_REPO_DIR", "/opt/veles")


# ── Helpers ───────────────────────────────────────────────────────────────────

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


def _fmt_dur(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m{seconds % 60:.0f}s"
    return f"{seconds / 3600:.1f}h"


def _load_jsonl_tail(path: Path, tail_bytes: int = 4_000_000) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    file_size = path.stat().st_size
    try:
        with path.open("rb") as f:
            if file_size > tail_bytes:
                f.seek(-tail_bytes, 2)
                f.readline()
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


# ── Git helpers ───────────────────────────────────────────────────────────────

def _git(args: List[str], timeout: int = 15) -> str:
    """Run git command in repo dir, return stdout or '' on error."""
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=_REPO_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.stdout if r.returncode == 0 else ""
    except Exception as exc:
        log.warning("git %s failed: %s", args[:2], exc)
        return ""


def _git_commits_in_window(
    start_dt: datetime,
    end_dt: datetime,
    branch: str = "veles",
) -> List[Dict[str, str]]:
    """Return commits on the veles branch whose author timestamp falls in [start_dt, end_dt]."""
    # git log with ISO-strict timestamps
    since_str = start_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    until_str = end_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    out = _git([
        "log", branch,
        f"--after={since_str}",
        f"--before={until_str}",
        "--no-merges",
        "--pretty=format:%H|%aI|%s",
    ])
    commits = []
    for line in out.strip().splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append({"hash": parts[0], "ts": parts[1], "msg": parts[2]})
    return list(reversed(commits))  # chronological order


def _git_diff_stat(commit_hash: str) -> str:
    """Return --stat for a commit (files changed, insertions, deletions)."""
    out = _git(["show", "--stat", "--no-patch", "--format=", commit_hash])
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    # Last line is summary like "3 files changed, 45 insertions(+), 12 deletions(-)"
    if not lines:
        return "(no stat)"
    # Return up to 12 lines (file list + summary)
    return "\n".join(lines[:12])


# ── Evolution task builder ────────────────────────────────────────────────────

def _build_evolution_record(
    task_id: str,
    cycle: Optional[int],
    start_dt: datetime,
    end_dt: Optional[datetime],
    events: List[Dict[str, Any]],
    tools_log: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build a full report record for one evolution task."""

    # Filter events for this task
    task_events = [
        r for r in events
        if r.get("task_id") == task_id
    ]
    task_tools = [
        r for r in tools_log
        if r.get("task_id") == task_id
    ]

    # Status
    status = "running"
    for r in reversed(task_events):
        if r.get("type") == "task_done":
            status = "done"
            break
        if r.get("type") == "task_failed":
            status = "failed"
            break

    # Duration
    duration_sec: Optional[float] = None
    if end_dt and start_dt:
        duration_sec = round((end_dt - start_dt).total_seconds(), 1)

    # Rounds + cost
    round_events = [r for r in task_events if r.get("type") == "llm_round"]
    llm_usage = [r for r in task_events if r.get("type") == "llm_usage"]
    round_count = len(round_events)
    shadow_cost = sum(float(r.get("shadow_cost", 0.0)) for r in llm_usage)
    real_cost = sum(float(r.get("cost", 0.0)) for r in llm_usage)

    models: Counter = Counter()
    for r in llm_usage:
        m = r.get("model", r.get("requested_model", ""))
        if m:
            models[m] += 1

    # Tool errors/timeouts
    tool_errors: List[str] = []
    tool_timeouts: List[str] = []
    for r in task_events:
        if r.get("type") == "tool_timeout":
            tool_timeouts.append(r.get("tool", "?"))
        elif r.get("type") == "tool_error":
            tool_errors.append(r.get("tool", "?"))
    for r in task_tools:
        if r.get("error") or r.get("status") == "error":
            tool_errors.append(r.get("tool", r.get("name", "?")))
        if r.get("timeout") or r.get("status") == "timeout":
            tool_timeouts.append(r.get("tool", r.get("name", "?")))

    # Git commits during this task window
    commits: List[Dict[str, Any]] = []
    if end_dt:
        raw_commits = _git_commits_in_window(start_dt, end_dt)
        for c in raw_commits:
            stat = _git_diff_stat(c["hash"])
            commits.append({
                "hash": c["hash"][:8],
                "ts": c["ts"],
                "msg": c["msg"][:120],
                "stat": stat,
            })

    return {
        "task_id": task_id[:8],
        "cycle": cycle,
        "status": status,
        "start_ts": start_dt.isoformat() if start_dt else None,
        "end_ts": end_dt.isoformat() if end_dt else None,
        "duration": _fmt_dur(duration_sec) if duration_sec else "?",
        "rounds": round_count,
        "models": dict(models.most_common(3)),
        "shadow_cost_usd": round(shadow_cost, 4),
        "real_cost_usd": round(real_cost, 6),
        "tool_errors": list(dict.fromkeys(tool_errors))[:5],
        "tool_timeouts": list(dict.fromkeys(tool_timeouts))[:5],
        "commits": commits,
    }


def _collect_evolution_tasks(
    events: List[Dict[str, Any]],
    limit: int,
    target_cycle: Optional[int],
) -> List[Tuple[str, Optional[int], datetime, Optional[datetime]]]:
    """
    Extract (task_id, cycle, start_dt, end_dt) for evolution tasks.
    Returns up to *limit* tasks, newest first → then reversed to chronological.
    """
    # Map task_id → (cycle, start_dt)
    task_starts: Dict[str, Tuple[Optional[int], datetime]] = {}
    task_ends: Dict[str, datetime] = {}

    for r in events:
        etype = r.get("type", "")
        if etype == "evolution_enqueued":
            # enqueued carries cycle number
            tid = r.get("task_id", "")
            cycle = r.get("cycle", r.get("evolution_cycle"))
            dt = _parse_ts(r.get("ts", ""))
            if tid and dt:
                task_starts[tid] = (cycle, dt)
        elif etype == "task_received":
            tid = r.get("task_id", "")
            task_type = r.get("task_type", r.get("type", ""))
            dt = _parse_ts(r.get("ts", ""))
            if tid and dt and ("evolution" in str(task_type).lower() or
                               "evolution" in str(r.get("task", {}).get("type", "")).lower()):
                if tid not in task_starts:
                    task_starts[tid] = (None, dt)
        elif etype in ("task_done", "task_failed"):
            tid = r.get("task_id", "")
            dt = _parse_ts(r.get("ts", ""))
            if tid and dt:
                task_ends[tid] = dt

    # Collect and sort newest-first
    results: List[Tuple[str, Optional[int], datetime, Optional[datetime]]] = []
    for tid, (cycle, start_dt) in task_starts.items():
        if target_cycle is not None and cycle != target_cycle:
            continue
        end_dt = task_ends.get(tid)
        results.append((tid, cycle, start_dt, end_dt))

    results.sort(key=lambda x: x[2], reverse=True)
    results = results[:limit]
    results.reverse()  # chronological order
    return results


# ── Formatter ─────────────────────────────────────────────────────────────────

def _format_text(records: List[Dict[str, Any]]) -> str:
    if not records:
        return "No evolution tasks found in the event log."
    lines: List[str] = []
    for rec in records:
        cycle_str = f"cycle #{rec['cycle']}" if rec.get("cycle") else "cycle ??"
        lines.append(f"## Evolution {cycle_str}  [id={rec['task_id']}]")
        lines.append(
            f"   Status: {rec['status']}  Duration: {rec['duration']}  "
            f"Rounds: {rec['rounds']}  Shadow: ${rec['shadow_cost_usd']:.3f}"
        )
        if rec["tool_errors"]:
            lines.append(f"   ⚠️ Tool errors: {', '.join(rec['tool_errors'])}")
        if rec["tool_timeouts"]:
            lines.append(f"   ⏱ Timeouts: {', '.join(rec['tool_timeouts'])}")
        if rec["models"]:
            top_model = next(iter(rec["models"]))
            lines.append(f"   Model: {top_model}")

        if rec["commits"]:
            lines.append(f"   📦 Commits ({len(rec['commits'])}):")
            for c in rec["commits"]:
                lines.append(f"      {c['hash']}  {c['msg']}")
                # Compact stat: last line (summary) only
                stat_lines = [ln for ln in c["stat"].splitlines() if ln.strip()]
                if stat_lines:
                    summary_line = stat_lines[-1].strip()
                    lines.append(f"              → {summary_line}")
        else:
            lines.append("   📦 No commits attributed to this task window")
        lines.append("")

    lines.append(f"─── {len(records)} evolution cycle(s) shown ───")
    return "\n".join(lines)


# ── Public tool ───────────────────────────────────────────────────────────────

def _evolution_report(
    ctx: ToolContext,
    limit: int = 5,
    cycle: Optional[int] = None,
    format: str = "text",
) -> str:
    """Generate an evolution audit report."""
    drive = Path(_DRIVE_ROOT)
    events = _load_jsonl_tail(drive / "logs" / "events.jsonl")
    tools_log = _load_jsonl_tail(drive / "logs" / "tools.jsonl")

    tasks = _collect_evolution_tasks(events, limit=max(1, min(50, limit)), target_cycle=cycle)
    if not tasks:
        return "evolution_report: no evolution tasks found in logs."

    records: List[Dict[str, Any]] = []
    for task_id, task_cycle, start_dt, end_dt in tasks:
        rec = _build_evolution_record(task_id, task_cycle, start_dt, end_dt, events, tools_log)
        records.append(rec)

    if format == "json":
        return json.dumps(records, ensure_ascii=False, indent=2)

    return _format_text(records)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="evolution_report",
            schema={
                "name": "evolution_report",
                "description": (
                    "Audit report for recent evolution cycles. For each cycle shows: "
                    "timing, LLM rounds, model, shadow cost, tool errors/timeouts, "
                    "and — most importantly — which git commits were produced during "
                    "the task window with file-level diff stats.\n\n"
                    "Answers 'what did evolution #159 actually change?' in one call. "
                    "Correlates task timing with git history to attribute commits to cycles.\n\n"
                    "Parameters:\n"
                    "- limit: how many recent evolution cycles to show (default 5, max 50)\n"
                    "- cycle: optional specific cycle number to inspect\n"
                    "- format: 'text' (default) or 'json'"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Number of recent evolution cycles to show (default 5).",
                        },
                        "cycle": {
                            "type": "integer",
                            "description": "Specific cycle number to inspect (optional).",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format: 'text' (default) or 'json'.",
                        },
                    },
                    "required": [],
                },
            },
            handler=lambda ctx, **kw: _evolution_report(ctx, **kw),
        )
    ]
