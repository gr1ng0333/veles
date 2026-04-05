"""performance_profile — Runtime performance observability from logs.

Answers questions like:
  - Which tools are slowest / most error-prone?
  - Which task types consume the most tokens / cost?
  - What are the latency percentiles per tool?
  - Which models are most cost-efficient?
  - What does the recent error rate look like?

Data sources:
  - /opt/veles-data/logs/tools.jsonl    — tool calls (ts, tool, task_id, args)
  - /opt/veles-data/logs/events.jsonl   — llm_round (cost/tokens), task_eval (duration),
                                          tool_timeout, llm_api_error

Why this exists:
  All previous growth tools do static code analysis. This is the first tool that
  looks at RUNTIME data — what actually happens in production. Without this, we
  optimize code structure while ignoring the real bottlenecks.

Usage:
    performance_profile()                    # full report, last 7 days
    performance_profile(view="tools")        # per-tool stats only
    performance_profile(view="models")       # per-model cost efficiency
    performance_profile(view="tasks")        # per-task-type breakdown
    performance_profile(view="errors")       # error & timeout summary
    performance_profile(days=1, format="json")
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median, mean, stdev
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")

# ── Data loading ───────────────────────────────────────────────────────────────


def _parse_ts(ts_str: str) -> Optional[datetime]:
    """Parse ISO8601 timestamp string to datetime (UTC)."""
    try:
        # Python 3.10 fromisoformat handles '+00:00', but not 'Z'
        ts_str = ts_str.replace("Z", "+00:00")
        return datetime.fromisoformat(ts_str)
    except Exception:
        return None


def _load_jsonl(path: pathlib.Path, since: datetime) -> List[Dict[str, Any]]:
    """Load JSONL file, filtering records newer than `since`."""
    records: List[Dict[str, Any]] = []
    if not path.exists():
        return records
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(d.get("ts", ""))
                if ts and ts >= since:
                    records.append(d)
    except Exception as exc:
        log.warning("performance_profile: failed to read %s: %s", path, exc)
    return records


# ── Tool stats ─────────────────────────────────────────────────────────────────


def _compute_tool_stats(
    tool_records: List[Dict],
    timeout_records: List[Dict],
) -> List[Dict[str, Any]]:
    """Per-tool call counts and error rates.

    Since tools.jsonl doesn't store duration, we count:
      - total calls
      - unique tasks
      - timeout count (from events.jsonl tool_timeout)
    """
    call_counts: Dict[str, int] = defaultdict(int)
    task_sets: Dict[str, set] = defaultdict(set)

    for rec in tool_records:
        name = rec.get("tool", "unknown")
        task_id = rec.get("task_id", "")
        call_counts[name] += 1
        if task_id:
            task_sets[name].add(task_id)

    timeout_counts: Dict[str, int] = defaultdict(int)
    for rec in timeout_records:
        name = rec.get("tool", "unknown")
        timeout_counts[name] += 1

    result = []
    for name in sorted(call_counts, key=lambda n: -call_counts[n]):
        calls = call_counts[name]
        timeouts = timeout_counts.get(name, 0)
        timeout_rate = timeouts / calls if calls else 0.0
        result.append({
            "tool": name,
            "calls": calls,
            "unique_tasks": len(task_sets[name]),
            "timeouts": timeouts,
            "timeout_rate": round(timeout_rate, 3),
        })

    return result


# ── Model stats ────────────────────────────────────────────────────────────────


def _compute_model_stats(llm_rounds: List[Dict]) -> List[Dict[str, Any]]:
    """Per-model: calls, total cost, avg cost per round, tokens."""
    by_model: Dict[str, Dict] = defaultdict(lambda: {
        "rounds": 0,
        "cost_usd": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
    })

    for rec in llm_rounds:
        model = rec.get("model", "unknown")
        m = by_model[model]
        m["rounds"] += 1
        m["cost_usd"] += rec.get("cost_usd", 0.0)
        m["prompt_tokens"] += rec.get("prompt_tokens", 0)
        m["completion_tokens"] += rec.get("completion_tokens", 0)
        m["cached_tokens"] += rec.get("cached_tokens", 0)

    result = []
    for model, m in sorted(by_model.items(), key=lambda kv: -kv[1]["cost_usd"]):
        rounds = m["rounds"]
        cost = m["cost_usd"]
        avg_cost = cost / rounds if rounds else 0.0
        prompt = m["prompt_tokens"]
        completion = m["completion_tokens"]
        cached = m["cached_tokens"]
        cache_rate = cached / prompt if prompt else 0.0
        result.append({
            "model": model,
            "rounds": rounds,
            "cost_usd": round(cost, 6),
            "avg_cost_per_round": round(avg_cost, 6),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cached_tokens": cached,
            "cache_hit_rate": round(cache_rate, 3),
        })

    return result


# ── Task stats ─────────────────────────────────────────────────────────────────


def _compute_task_stats(task_evals: List[Dict]) -> List[Dict[str, Any]]:
    """Per-task-type: count, avg duration, avg tool calls, error rate."""
    by_type: Dict[str, Dict] = defaultdict(lambda: {
        "count": 0,
        "failed": 0,
        "durations": [],
        "tool_calls": [],
        "tool_errors": [],
    })

    for rec in task_evals:
        ttype = rec.get("task_type", "unknown")
        t = by_type[ttype]
        t["count"] += 1
        if not rec.get("ok", True):
            t["failed"] += 1
        dur = rec.get("duration_sec")
        if dur is not None:
            t["durations"].append(dur)
        tc = rec.get("tool_calls")
        if tc is not None:
            t["tool_calls"].append(tc)
        te = rec.get("tool_errors")
        if te is not None:
            t["tool_errors"].append(te)

    result = []
    for ttype, t in sorted(by_type.items(), key=lambda kv: -kv[1]["count"]):
        count = t["count"]
        failed = t["failed"]
        durations = t["durations"]
        tool_calls = t["tool_calls"]
        tool_errors = t["tool_errors"]

        avg_dur = mean(durations) if durations else None
        med_dur = median(durations) if durations else None
        p95_dur = sorted(durations)[int(len(durations) * 0.95)] if len(durations) >= 2 else None
        avg_tc = mean(tool_calls) if tool_calls else None
        avg_te = mean(tool_errors) if tool_errors else None

        result.append({
            "task_type": ttype,
            "count": count,
            "failed": failed,
            "error_rate": round(failed / count, 3) if count else 0.0,
            "avg_duration_sec": round(avg_dur, 1) if avg_dur is not None else None,
            "median_duration_sec": round(med_dur, 1) if med_dur is not None else None,
            "p95_duration_sec": round(p95_dur, 1) if p95_dur is not None else None,
            "avg_tool_calls": round(avg_tc, 1) if avg_tc is not None else None,
            "avg_tool_errors": round(avg_te, 2) if avg_te is not None else None,
        })

    return result


# ── Error stats ────────────────────────────────────────────────────────────────


def _compute_error_stats(
    api_errors: List[Dict],
    timeouts: List[Dict],
) -> Dict[str, Any]:
    """Summarize API errors and tool timeouts."""
    # API errors by type
    by_error: Dict[str, int] = defaultdict(int)
    for rec in api_errors:
        err = rec.get("error", "unknown")
        # Shorten to first 80 chars to group similar errors
        key = str(err)[:80]
        by_error[key] += 1

    # Timeouts by tool
    by_tool: Dict[str, int] = defaultdict(int)
    for rec in timeouts:
        tool = rec.get("tool", "unknown")
        by_tool[tool] += 1

    return {
        "api_errors_total": len(api_errors),
        "tool_timeouts_total": len(timeouts),
        "top_api_errors": sorted(by_error.items(), key=lambda kv: -kv[1])[:10],
        "top_timeout_tools": sorted(by_tool.items(), key=lambda kv: -kv[1])[:10],
    }


# ── Formatters ─────────────────────────────────────────────────────────────────


def _fmt_tool_section(tool_stats: List[Dict]) -> str:
    lines = ["## Tool Call Profile\n"]
    if not tool_stats:
        lines.append("  No tool calls found in window.")
        return "\n".join(lines)

    lines.append(f"  {'Tool':<38} {'Calls':>6} {'Tasks':>6} {'Timeouts':>8} {'TO Rate':>8}")
    lines.append("  " + "-" * 72)
    for t in tool_stats[:25]:
        to_flag = " ⚠" if t["timeout_rate"] > 0.05 else ""
        lines.append(
            f"  {t['tool']:<38} {t['calls']:>6} {t['unique_tasks']:>6} "
            f"{t['timeouts']:>8} {t['timeout_rate']:>7.1%}{to_flag}"
        )
    if len(tool_stats) > 25:
        lines.append(f"  ... and {len(tool_stats) - 25} more tools")
    return "\n".join(lines)


def _fmt_model_section(model_stats: List[Dict]) -> str:
    lines = ["## Model Cost Profile\n"]
    if not model_stats:
        lines.append("  No LLM rounds found in window.")
        return "\n".join(lines)

    lines.append(f"  {'Model':<40} {'Rounds':>6} {'Cost $':>8} {'$/round':>8} {'Cache%':>7}")
    lines.append("  " + "-" * 74)
    for m in model_stats:
        lines.append(
            f"  {m['model']:<40} {m['rounds']:>6} {m['cost_usd']:>8.4f} "
            f"{m['avg_cost_per_round']:>8.5f} {m['cache_hit_rate']:>6.0%}"
        )
    return "\n".join(lines)


def _fmt_task_section(task_stats: List[Dict]) -> str:
    lines = ["## Task Type Profile\n"]
    if not task_stats:
        lines.append("  No task evaluations found in window.")
        return "\n".join(lines)

    lines.append(
        f"  {'Type':<16} {'Count':>6} {'Failed':>6} {'Err%':>6} "
        f"{'AvgDur':>7} {'MedDur':>7} {'P95Dur':>7} {'AvgTools':>9}"
    )
    lines.append("  " + "-" * 76)
    for t in task_stats:
        avg_d = f"{t['avg_duration_sec']:.0f}s" if t["avg_duration_sec"] is not None else "  -"
        med_d = f"{t['median_duration_sec']:.0f}s" if t["median_duration_sec"] is not None else "  -"
        p95_d = f"{t['p95_duration_sec']:.0f}s" if t["p95_duration_sec"] is not None else "  -"
        avg_tc = f"{t['avg_tool_calls']:.1f}" if t["avg_tool_calls"] is not None else "  -"
        err_flag = " ⚠" if t["error_rate"] > 0.1 else ""
        lines.append(
            f"  {t['task_type']:<16} {t['count']:>6} {t['failed']:>6} "
            f"{t['error_rate']:>5.0%} "
            f"{avg_d:>7} {med_d:>7} {p95_d:>7} {avg_tc:>9}{err_flag}"
        )
    return "\n".join(lines)


def _fmt_error_section(error_stats: Dict) -> str:
    lines = ["## Error & Timeout Summary\n"]
    lines.append(f"  API errors total  : {error_stats['api_errors_total']}")
    lines.append(f"  Tool timeouts total: {error_stats['tool_timeouts_total']}")

    if error_stats["top_timeout_tools"]:
        lines.append("\n  Top timeout tools:")
        for tool, cnt in error_stats["top_timeout_tools"]:
            lines.append(f"    {cnt:4}x  {tool}")

    if error_stats["top_api_errors"]:
        lines.append("\n  Top API errors:")
        for err, cnt in error_stats["top_api_errors"][:5]:
            lines.append(f"    {cnt:4}x  {err[:70]}")

    return "\n".join(lines)


# ── Main handler ───────────────────────────────────────────────────────────────


def _performance_profile(
    ctx: ToolContext,
    days: int = 7,
    view: str = "all",
    format: str = "text",
    _drive_root: Optional[str] = None,  # override for tests
) -> str:
    """Analyze runtime performance from logs and return structured report."""
    drive_root = pathlib.Path(_drive_root if _drive_root else _DRIVE_ROOT)
    since = datetime.now(tz=timezone.utc) - timedelta(days=max(1, min(90, days)))

    tools_path = drive_root / "logs" / "tools.jsonl"
    events_path = drive_root / "logs" / "events.jsonl"

    tool_records = _load_jsonl(tools_path, since)
    event_records = _load_jsonl(events_path, since)

    # Partition events by type
    llm_rounds = [r for r in event_records if r.get("type") == "llm_round"]
    task_evals = [r for r in event_records if r.get("type") == "task_eval"]
    api_errors = [r for r in event_records if r.get("type") == "llm_api_error"]
    timeouts = [r for r in event_records if r.get("type") == "tool_timeout"]

    # Compute stats
    tool_stats = _compute_tool_stats(tool_records, timeouts)
    model_stats = _compute_model_stats(llm_rounds)
    task_stats = _compute_task_stats(task_evals)
    error_stats = _compute_error_stats(api_errors, timeouts)

    # Build summary header
    total_cost = sum(r.get("cost_usd", 0.0) for r in llm_rounds)
    summary = {
        "period_days": days,
        "since": since.isoformat(),
        "tool_calls": len(tool_records),
        "llm_rounds": len(llm_rounds),
        "tasks_completed": len(task_evals),
        "tasks_failed": sum(1 for t in task_evals if not t.get("ok", True)),
        "total_cost_usd": round(total_cost, 4),
        "api_errors": len(api_errors),
        "tool_timeouts": len(timeouts),
    }

    if format == "json":
        return json.dumps({
            "summary": summary,
            "tools": tool_stats[:50] if view in ("all", "tools") else [],
            "models": model_stats if view in ("all", "models") else [],
            "tasks": task_stats if view in ("all", "tasks") else [],
            "errors": error_stats if view in ("all", "errors") else {},
        }, ensure_ascii=False, indent=2)

    # Text format
    lines = [
        f"## Performance Profile  (last {days}d, since {since.strftime('%Y-%m-%d')})\n",
        f"  Tool calls     : {summary['tool_calls']:,}",
        f"  LLM rounds     : {summary['llm_rounds']:,}",
        f"  Tasks completed: {summary['tasks_completed']:,}",
        f"  Tasks failed   : {summary['tasks_failed']:,}  "
        f"({summary['tasks_failed']/summary['tasks_completed']*100:.1f}%)"
        if summary["tasks_completed"] else f"  Tasks failed   : {summary['tasks_failed']}",
        f"  Total LLM cost : ${summary['total_cost_usd']:.4f}",
        f"  API errors     : {summary['api_errors']:,}",
        f"  Tool timeouts  : {summary['tool_timeouts']:,}",
        "",
    ]

    if view in ("all", "tools"):
        lines.append(_fmt_tool_section(tool_stats))
        lines.append("")

    if view in ("all", "models"):
        lines.append(_fmt_model_section(model_stats))
        lines.append("")

    if view in ("all", "tasks"):
        lines.append(_fmt_task_section(task_stats))
        lines.append("")

    if view in ("all", "errors"):
        lines.append(_fmt_error_section(error_stats))

    return "\n".join(lines)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="performance_profile",
            schema={
                "name": "performance_profile",
                "description": (
                    "Runtime performance observability from production logs.\n\n"
                    "Analyzes tools.jsonl and events.jsonl to answer:\n"
                    "  - Which tools are called most / have highest timeout rates?\n"
                    "  - Which LLM models cost most and have best cache efficiency?\n"
                    "  - Which task types take longest / fail most often?\n"
                    "  - What are the top API errors and timeout tools?\n\n"
                    "Unlike static analysis tools, this reflects ACTUAL production behavior.\n\n"
                    "Parameters:\n"
                    "  - days: lookback window in days (default 7, max 90)\n"
                    "  - view: 'all' | 'tools' | 'models' | 'tasks' | 'errors' (default 'all')\n"
                    "  - format: 'text' | 'json' (default 'text')"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {
                            "type": "integer",
                            "description": "Lookback window in days (default 7, max 90)",
                        },
                        "view": {
                            "type": "string",
                            "enum": ["all", "tools", "models", "tasks", "errors"],
                            "description": "Which section to show (default 'all')",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format (default 'text')",
                        },
                    },
                    "required": [],
                },
            },
            handler=_performance_profile,
        )
    ]
