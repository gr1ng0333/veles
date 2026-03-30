"""budget_forecast — single-call budget observability tool.

Returns:
  - spent / remaining / total budget
  - burn rates: last 1d, 3d, 7d, 14d, 30d (daily average, shadow cost)
  - runway: how many days at each burn rate until budget exhausted
  - breakdown: spend by category (task/evolution/consciousness/review/other)
  - breakdown: spend by model (top 10)
  - daily spend: last N days (default 14) for trend visualization
  - peak day: most expensive single day
  - session spend: current session cost vs total

Growth tool: replaces ad-hoc log_query + state.json reads with a single call.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")


def _load_jsonl_tail(path: Path, tail_bytes: int = 5_000_000) -> List[Dict[str, Any]]:
    """Load JSONL file, reading tail_bytes from the end to keep it fast."""
    if not path.exists():
        return []
    file_size = path.stat().st_size
    try:
        with open(path, "rb") as f:
            if file_size > tail_bytes:
                f.seek(-tail_bytes, 2)
                f.readline()  # skip partial first line
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
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


def _load_state() -> Dict[str, Any]:
    state_path = Path(_DRIVE_ROOT) / "state" / "state.json"
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text())
    except Exception:
        return {}


def _cost(e: Dict[str, Any]) -> float:
    """Extract the best available cost from a llm_usage event."""
    sc = e.get("shadow_cost")
    if sc is not None:
        return float(sc)
    c = e.get("cost")
    if c is not None:
        return float(c)
    return 0.0


def _budget_forecast(
    ctx: ToolContext,
    daily_history_days: int = 14,
) -> str:
    """Compute budget forecast from events log + state."""
    drive = Path(_DRIVE_ROOT)
    events_path = drive / "logs" / "events.jsonl"

    events = _load_jsonl_tail(events_path)
    llm_events = [
        e for e in events
        if e.get("type") == "llm_usage" and e.get("ts")
    ]
    llm_events.sort(key=lambda e: e["ts"])

    state = _load_state()
    now = datetime.now(timezone.utc)

    # ── Budget totals ───────────────────────────────────────────────────────
    total_budget = float(state.get("budget_total_usd") or 2800.0)
    spent_usd = float(state.get("spent_usd") or 0.0)
    remaining_usd = max(0.0, total_budget - spent_usd)
    spent_pct = (spent_usd / total_budget * 100) if total_budget else 0.0

    session_snapshot = float(state.get("session_spent_snapshot") or 0.0)
    session_spend = max(0.0, spent_usd - session_snapshot)

    # ── Burn rates: 1d / 3d / 7d / 14d / 30d ───────────────────────────────
    burn_windows = [1, 3, 7, 14, 30]
    burn_rates: Dict[str, float] = {}
    for w in burn_windows:
        cutoff = (now - timedelta(days=w)).isoformat()
        subset_cost = sum(
            _cost(e) for e in llm_events if e.get("ts", "") > cutoff
        )
        avg = subset_cost / w
        burn_rates[f"{w}d_daily_avg"] = round(avg, 3)

    # ── Runway ──────────────────────────────────────────────────────────────
    runway: Dict[str, Any] = {}
    for w in burn_windows:
        key = f"{w}d_daily_avg"
        avg = burn_rates.get(key, 0.0)
        if avg > 0:
            runway[f"at_{w}d_rate_days"] = round(remaining_usd / avg, 1)
        else:
            runway[f"at_{w}d_rate_days"] = None

    # ── Breakdown by category ───────────────────────────────────────────────
    by_category: Dict[str, float] = defaultdict(float)
    by_category_7d: Dict[str, float] = defaultdict(float)
    cutoff_7d = (now - timedelta(days=7)).isoformat()
    for e in llm_events:
        cat = e.get("category") or "unknown"
        c = _cost(e)
        by_category[cat] += c
        if e.get("ts", "") > cutoff_7d:
            by_category_7d[cat] += c

    # ── Breakdown by model (top 10 by total spend) ──────────────────────────
    by_model: Dict[str, float] = defaultdict(float)
    by_model_7d: Dict[str, float] = defaultdict(float)
    for e in llm_events:
        model = e.get("model") or e.get("requested_model") or "unknown"
        c = _cost(e)
        by_model[model] += c
        if e.get("ts", "") > cutoff_7d:
            by_model_7d[model] += c

    top_models_all = sorted(by_model.items(), key=lambda x: -x[1])[:10]
    top_models_7d = sorted(by_model_7d.items(), key=lambda x: -x[1])[:10]

    # ── Daily history ───────────────────────────────────────────────────────
    daily: Dict[str, float] = defaultdict(float)
    for e in llm_events:
        ts = e.get("ts", "")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
            day = dt.date().isoformat()
            daily[day] += _cost(e)
        except Exception:
            pass

    cutoff_history = (now - timedelta(days=daily_history_days)).date().isoformat()
    history_days = sorted(
        [(d, round(v, 4)) for d, v in daily.items() if d >= cutoff_history]
    )

    peak_day: Optional[Tuple[str, float]] = None
    if daily:
        pd_key = max(daily, key=lambda k: daily[k])
        peak_day = (pd_key, round(daily[pd_key], 4))

    # ── Tokens total ────────────────────────────────────────────────────────
    total_prompt = int(state.get("spent_tokens_prompt") or 0)
    total_completion = int(state.get("spent_tokens_completion") or 0)
    total_cached = int(state.get("spent_tokens_cached") or 0)
    cache_hit_rate: Optional[float] = None
    if total_prompt > 0:
        cache_hit_rate = round(total_cached / total_prompt * 100, 1)

    # ── First/last event timestamps ─────────────────────────────────────────
    first_ts = llm_events[0]["ts"] if llm_events else None
    last_ts = llm_events[-1]["ts"] if llm_events else None
    days_active: Optional[float] = None
    if first_ts and last_ts:
        try:
            t0 = datetime.fromisoformat(first_ts)
            t1 = datetime.fromisoformat(last_ts)
            days_active = round((t1 - t0).total_seconds() / 86400, 1)
        except Exception:
            pass

    result = {
        "as_of": now.isoformat(),
        # ── Budget summary
        "budget": {
            "total_usd": round(total_budget, 2),
            "spent_usd": round(spent_usd, 4),
            "remaining_usd": round(remaining_usd, 4),
            "spent_pct": round(spent_pct, 1),
            "session_spend_usd": round(session_spend, 4),
        },
        # ── Burn rates (shadow cost, daily averages)
        "burn_rates_usd_per_day": burn_rates,
        # ── Runway (days until budget exhausted at each burn rate)
        "runway_days": runway,
        # ── Category breakdown (all-time + 7d)
        "by_category": {
            "all_time": {k: round(v, 4) for k, v in sorted(by_category.items(), key=lambda x: -x[1])},
            "last_7d": {k: round(v, 4) for k, v in sorted(by_category_7d.items(), key=lambda x: -x[1])},
        },
        # ── Model breakdown (top 10)
        "by_model": {
            "all_time": {m: round(c, 4) for m, c in top_models_all},
            "last_7d": {m: round(c, 4) for m, c in top_models_7d},
        },
        # ── Daily history
        "daily_history": history_days,
        "peak_day": {"date": peak_day[0], "cost_usd": peak_day[1]} if peak_day else None,
        # ── Token totals
        "tokens": {
            "prompt_total": total_prompt,
            "completion_total": total_completion,
            "cached_total": total_cached,
            "cache_hit_rate_pct": cache_hit_rate,
        },
        # ── Meta
        "events_analyzed": len(llm_events),
        "days_active": days_active,
        "first_event_ts": first_ts,
        "last_event_ts": last_ts,
    }

    return json.dumps(result, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="budget_forecast",
            schema={
                "name": "budget_forecast",
                "description": (
                    "Single-call budget observability: burn rate, runway, spend breakdown by category and model, "
                    "daily history, peak day, and token efficiency. "
                    "Returns:\n"
                    "- budget: total/spent/remaining/session\n"
                    "- burn_rates: daily averages over last 1/3/7/14/30 days\n"
                    "- runway_days: how many days of budget left at each burn rate\n"
                    "- by_category: spend breakdown (task/evolution/consciousness/review) all-time and 7d\n"
                    "- by_model: top-10 models by spend all-time and 7d\n"
                    "- daily_history: per-day spend for the last N days\n"
                    "- peak_day: most expensive single day\n"
                    "- tokens: total prompt/completion/cached + cache hit rate\n"
                    "Use when you need a quick budget health check without running multiple log_query calls."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "daily_history_days": {
                            "type": "integer",
                            "description": "How many days of daily history to include. Default: 14.",
                        },
                    },
                    "required": [],
                },
            },
            handler=_budget_forecast,
        )
    ]
