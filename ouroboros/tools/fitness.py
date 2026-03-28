"""Fitness data-layer tools with isolated storage under /opt/veles-data/fitness/."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

_FITNESS_DIR = "fitness"
_PROFILE_FILE = "profile.json"
_LOGS_DIR = "logs"
_LOG_FILE = "fitness.jsonl"
_ALLOWED_SUMMARY_PERIODS = {"today", "week", "month"}
_ALLOWED_WORKOUT_TYPES = {
    "calisthenics",
    "walk",
    "run",
    "mobility",
    "stretching",
    "strength",
    "cardio",
    "mixed",
    "other",
}


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _fitness_root(ctx: ToolContext) -> Path:
    root = ctx.drive_path(_FITNESS_DIR)
    root.mkdir(parents=True, exist_ok=True)
    (root / _LOGS_DIR).mkdir(parents=True, exist_ok=True)
    return root


def _profile_path(ctx: ToolContext) -> Path:
    return _fitness_root(ctx) / _PROFILE_FILE


def _log_path(ctx: ToolContext) -> Path:
    return _fitness_root(ctx) / _LOGS_DIR / _LOG_FILE


def _today_utc() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_iso_datetime(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("date is required")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _resolve_event_datetime(date_value: str = "") -> datetime:
    if date_value:
        return _coerce_iso_datetime(date_value)
    return _today_utc()


def _date_parts(dt: datetime) -> Tuple[str, str, str]:
    iso_year, iso_week, _ = dt.isocalendar()
    week_key = f"{iso_year}-W{iso_week:02d}"
    day_key = dt.date().isoformat()
    month_key = f"{dt.year:04d}-{dt.month:02d}"
    return week_key, day_key, month_key


def _week_path(ctx: ToolContext, week_key: str) -> Path:
    return _fitness_root(ctx) / f"{week_key}.json"


def _month_summary_path(ctx: ToolContext, month_key: str) -> Path:
    return _fitness_root(ctx) / f"{month_key}_summary.json"


def _week_summary_path(ctx: ToolContext, week_key: str) -> Path:
    return _fitness_root(ctx) / f"{week_key}_summary.json"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n")


def _default_profile() -> Dict[str, Any]:
    return {
        "height_cm": None,
        "weight_kg": None,
        "goal": "",
        "goal_weight_kg": None,
        "tdee_kcal": None,
        "daily_calorie_target": None,
        "protein_target_g": None,
        "fat_target_g": None,
        "carbs_target_g": None,
        "current_program": "",
        "notes": "",
        "updated_at": None,
    }


def _read_profile(ctx: ToolContext) -> Dict[str, Any]:
    profile = _read_json(_profile_path(ctx), _default_profile())
    if not isinstance(profile, dict):
        return _default_profile()
    merged = _default_profile()
    merged.update(profile)
    return merged


def _write_profile(ctx: ToolContext, profile: Dict[str, Any]) -> Dict[str, Any]:
    profile = dict(profile)
    profile["updated_at"] = _today_utc().isoformat()
    _write_json(_profile_path(ctx), profile)
    return profile


def _empty_week(week_key: str) -> Dict[str, Any]:
    return {
        "week": week_key,
        "days": {},
        "updated_at": None,
    }


def _read_week(ctx: ToolContext, week_key: str) -> Dict[str, Any]:
    week = _read_json(_week_path(ctx, week_key), _empty_week(week_key))
    if not isinstance(week, dict):
        return _empty_week(week_key)
    week.setdefault("week", week_key)
    week.setdefault("days", {})
    return week


def _write_week(ctx: ToolContext, week: Dict[str, Any]) -> None:
    week = dict(week)
    week["updated_at"] = _today_utc().isoformat()
    _write_json(_week_path(ctx, str(week.get("week") or "unknown-week")), week)


def _ensure_day(week: Dict[str, Any], day_key: str) -> Dict[str, Any]:
    days = week.setdefault("days", {})
    day = days.get(day_key)
    if not isinstance(day, dict):
        day = {
            "date": day_key,
            "meals": [],
            "workouts": [],
            "weight_logs": [],
            "totals": {"calories": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0},
        }
        days[day_key] = day
    day.setdefault("meals", [])
    day.setdefault("workouts", [])
    day.setdefault("weight_logs", [])
    day.setdefault("totals", {"calories": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0})
    return day


def _to_float(value: Any, field: str, min_value: Optional[float] = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number")
    if min_value is not None and number < min_value:
        raise ValueError(f"{field} must be >= {min_value}")
    return round(number, 2)


def _to_int(value: Any, field: str, min_value: Optional[int] = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer")
    if min_value is not None and number < min_value:
        raise ValueError(f"{field} must be >= {min_value}")
    return number


def _normalize_workout_type(value: str) -> str:
    kind = str(value or "").strip().lower()
    if not kind:
        raise ValueError("type is required")
    if kind not in _ALLOWED_WORKOUT_TYPES:
        raise ValueError(f"type must be one of: {', '.join(sorted(_ALLOWED_WORKOUT_TYPES))}")
    return kind


def _build_event(event_type: str, dt: datetime, payload: Dict[str, Any]) -> Dict[str, Any]:
    week_key, day_key, month_key = _date_parts(dt)
    return {
        "event_type": event_type,
        "timestamp": dt.isoformat(),
        "week": week_key,
        "day": day_key,
        "month": month_key,
        **payload,
    }


def _record_meal(ctx: ToolContext, description: str, calories: Any, protein: Any, fat: Any, carbs: Any, date: str = "") -> Dict[str, Any]:
    text = str(description or "").strip()
    if not text:
        raise ValueError("description is required")

    dt = _resolve_event_datetime(date)
    week_key, day_key, _ = _date_parts(dt)
    week = _read_week(ctx, week_key)
    day = _ensure_day(week, day_key)

    entry = {
        "description": text,
        "calories": _to_float(calories, "calories", 0),
        "protein": _to_float(protein, "protein", 0),
        "fat": _to_float(fat, "fat", 0),
        "carbs": _to_float(carbs, "carbs", 0),
        "logged_at": dt.isoformat(),
    }
    day["meals"].append(entry)
    day["totals"]["calories"] = round(float(day["totals"].get("calories", 0.0)) + entry["calories"], 2)
    day["totals"]["protein"] = round(float(day["totals"].get("protein", 0.0)) + entry["protein"], 2)
    day["totals"]["fat"] = round(float(day["totals"].get("fat", 0.0)) + entry["fat"], 2)
    day["totals"]["carbs"] = round(float(day["totals"].get("carbs", 0.0)) + entry["carbs"], 2)
    _write_week(ctx, week)

    event = _build_event("meal", dt, entry)
    _append_jsonl(_log_path(ctx), event)
    return {
        "status": "ok",
        "logged": event,
        "day_totals": day["totals"],
    }


def _record_workout(ctx: ToolContext, description: str, duration_min: Any, type: str, date: str = "") -> Dict[str, Any]:
    text = str(description or "").strip()
    if not text:
        raise ValueError("description is required")

    dt = _resolve_event_datetime(date)
    week_key, day_key, _ = _date_parts(dt)
    week = _read_week(ctx, week_key)
    day = _ensure_day(week, day_key)

    entry = {
        "description": text,
        "duration_min": _to_int(duration_min, "duration_min", 1),
        "type": _normalize_workout_type(type),
        "logged_at": dt.isoformat(),
    }
    day["workouts"].append(entry)
    _write_week(ctx, week)

    event = _build_event("workout", dt, entry)
    _append_jsonl(_log_path(ctx), event)
    return {
        "status": "ok",
        "logged": event,
        "workouts_today": len(day["workouts"]),
    }


def _record_weight(ctx: ToolContext, kg: Any, date: str = "") -> Dict[str, Any]:
    dt = _resolve_event_datetime(date)
    week_key, day_key, _ = _date_parts(dt)
    week = _read_week(ctx, week_key)
    day = _ensure_day(week, day_key)

    entry = {
        "kg": _to_float(kg, "kg", 1),
        "logged_at": dt.isoformat(),
    }
    day["weight_logs"].append(entry)
    _write_week(ctx, week)

    profile = _read_profile(ctx)
    profile["weight_kg"] = entry["kg"]
    _write_profile(ctx, profile)

    event = _build_event("weight", dt, entry)
    _append_jsonl(_log_path(ctx), event)
    return {
        "status": "ok",
        "logged": event,
        "current_profile_weight_kg": profile["weight_kg"],
    }


def _daterange(start: datetime, end: datetime) -> List[datetime]:
    days: List[datetime] = []
    cursor = start
    while cursor.date() <= end.date():
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _collect_days(ctx: ToolContext, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    collected: List[Dict[str, Any]] = []
    seen_weeks: Dict[str, Dict[str, Any]] = {}
    for dt in _daterange(start, end):
        week_key, day_key, _ = _date_parts(dt)
        week = seen_weeks.get(week_key)
        if week is None:
            week = _read_week(ctx, week_key)
            seen_weeks[week_key] = week
        day = week.get("days", {}).get(day_key)
        if isinstance(day, dict):
            collected.append(day)
    return collected


def _weight_delta(weights: List[float]) -> Optional[float]:
    if len(weights) < 2:
        return None
    return round(weights[-1] - weights[0], 2)


def _period_bounds(period: str) -> Tuple[datetime, datetime]:
    now = _today_utc()
    start_today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    if period == "today":
        return start_today, now
    if period == "week":
        start = start_today - timedelta(days=start_today.weekday())
        return start, now
    if period == "month":
        start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        return start, now
    raise ValueError(f"period must be one of: {', '.join(sorted(_ALLOWED_SUMMARY_PERIODS))}")


def _generate_summary(ctx: ToolContext, period: str) -> Dict[str, Any]:
    normalized = str(period or "today").strip().lower()
    start, end = _period_bounds(normalized)
    days = _collect_days(ctx, start, end)
    profile = _read_profile(ctx)

    totals = {"calories": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0}
    workout_count = 0
    workout_minutes = 0
    workout_types: Dict[str, int] = {}
    weights: List[float] = []

    for day in days:
        day_totals = day.get("totals", {}) if isinstance(day, dict) else {}
        totals["calories"] = round(totals["calories"] + float(day_totals.get("calories", 0.0) or 0.0), 2)
        totals["protein"] = round(totals["protein"] + float(day_totals.get("protein", 0.0) or 0.0), 2)
        totals["fat"] = round(totals["fat"] + float(day_totals.get("fat", 0.0) or 0.0), 2)
        totals["carbs"] = round(totals["carbs"] + float(day_totals.get("carbs", 0.0) or 0.0), 2)

        for workout in day.get("workouts", []):
            workout_count += 1
            workout_minutes += int(workout.get("duration_min", 0) or 0)
            kind = str(workout.get("type") or "other")
            workout_types[kind] = workout_types.get(kind, 0) + 1

        for weight in day.get("weight_logs", []):
            try:
                weights.append(float(weight.get("kg")))
            except (TypeError, ValueError):
                continue

    days_count = max((end.date() - start.date()).days + 1, 1)
    calorie_target = profile.get("daily_calorie_target")
    tdee = profile.get("tdee_kcal")

    calories_avg = round(totals["calories"] / days_count, 2)
    deficit_vs_target = round((float(calorie_target) * days_count) - totals["calories"], 2) if calorie_target not in (None, "") else None
    deficit_vs_tdee = round((float(tdee) * days_count) - totals["calories"], 2) if tdee not in (None, "") else None

    summary = {
        "period": normalized,
        "from": start.date().isoformat(),
        "to": end.date().isoformat(),
        "days_covered": days_count,
        "days_with_data": len(days),
        "nutrition": {
            **totals,
            "avg_calories_per_day": calories_avg,
        },
        "deficit": {
            "vs_daily_target": deficit_vs_target,
            "vs_tdee": deficit_vs_tdee,
        },
        "workouts": {
            "count": workout_count,
            "duration_min": workout_minutes,
            "by_type": workout_types,
        },
        "weight": {
            "entries": len(weights),
            "latest_kg": weights[-1] if weights else profile.get("weight_kg"),
            "delta_kg": _weight_delta(weights),
        },
        "profile_snapshot": {
            "goal": profile.get("goal"),
            "daily_calorie_target": profile.get("daily_calorie_target"),
            "tdee_kcal": profile.get("tdee_kcal"),
            "current_program": profile.get("current_program"),
        },
    }

    if normalized == "week":
        current_week, _, _ = _date_parts(end)
        _write_json(_week_summary_path(ctx, current_week), summary)
    elif normalized == "month":
        _, _, month_key = _date_parts(end)
        _write_json(_month_summary_path(ctx, month_key), summary)

    return summary


def _fitness_profile_read(ctx: ToolContext) -> str:
    return json.dumps(_read_profile(ctx), ensure_ascii=False, indent=2)


def _fitness_profile_write(
    ctx: ToolContext,
    height_cm: Any = None,
    weight_kg: Any = None,
    goal: str = "",
    goal_weight_kg: Any = None,
    tdee_kcal: Any = None,
    daily_calorie_target: Any = None,
    protein_target_g: Any = None,
    fat_target_g: Any = None,
    carbs_target_g: Any = None,
    current_program: str = "",
    notes: str = "",
) -> str:
    profile = _read_profile(ctx)

    updates: Dict[str, Any] = {}
    if height_cm is not None:
        updates["height_cm"] = _to_float(height_cm, "height_cm", 1)
    if weight_kg is not None:
        updates["weight_kg"] = _to_float(weight_kg, "weight_kg", 1)
    if goal:
        updates["goal"] = str(goal).strip()
    if goal_weight_kg is not None:
        updates["goal_weight_kg"] = _to_float(goal_weight_kg, "goal_weight_kg", 1)
    if tdee_kcal is not None:
        updates["tdee_kcal"] = _to_float(tdee_kcal, "tdee_kcal", 1)
    if daily_calorie_target is not None:
        updates["daily_calorie_target"] = _to_float(daily_calorie_target, "daily_calorie_target", 1)
    if protein_target_g is not None:
        updates["protein_target_g"] = _to_float(protein_target_g, "protein_target_g", 0)
    if fat_target_g is not None:
        updates["fat_target_g"] = _to_float(fat_target_g, "fat_target_g", 0)
    if carbs_target_g is not None:
        updates["carbs_target_g"] = _to_float(carbs_target_g, "carbs_target_g", 0)
    if current_program:
        updates["current_program"] = str(current_program).strip()
    if notes:
        updates["notes"] = str(notes).strip()

    profile.update(updates)
    written = _write_profile(ctx, profile)
    return json.dumps({"status": "ok", "profile": written}, ensure_ascii=False, indent=2)


def _fitness_log_meal(
    ctx: ToolContext,
    description: str,
    calories: Any,
    protein: Any,
    fat: Any,
    carbs: Any,
    date: str = "",
) -> str:
    return json.dumps(_record_meal(ctx, description, calories, protein, fat, carbs, date), ensure_ascii=False, indent=2)


def _fitness_log_workout(
    ctx: ToolContext,
    description: str,
    duration_min: Any,
    type: str,
    date: str = "",
) -> str:
    return json.dumps(_record_workout(ctx, description, duration_min, type, date), ensure_ascii=False, indent=2)


def _fitness_log_weight(ctx: ToolContext, kg: Any, date: str = "") -> str:
    return json.dumps(_record_weight(ctx, kg, date), ensure_ascii=False, indent=2)


def _fitness_summary(ctx: ToolContext, period: str = "today") -> str:
    return json.dumps(_generate_summary(ctx, period), ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="fitness_log_meal",
            schema={
                "type": "function",
                "function": {
                    "name": "fitness_log_meal",
                    "description": "Log a meal into the isolated fitness store with calories and macros.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "calories": {"type": "number"},
                            "protein": {"type": "number"},
                            "fat": {"type": "number"},
                            "carbs": {"type": "number"},
                            "date": {"type": "string", "description": "Optional ISO8601 timestamp in UTC or with timezone."},
                        },
                        "required": ["description", "calories", "protein", "fat", "carbs"],
                    },
                },
            },
            handler=_fitness_log_meal,
        ),
        ToolEntry(
            name="fitness_log_workout",
            schema={
                "type": "function",
                "function": {
                    "name": "fitness_log_workout",
                    "description": "Log a workout into the isolated fitness store.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "duration_min": {"type": "integer"},
                            "type": {"type": "string", "description": "Workout type such as calisthenics, walk, cardio, mobility."},
                            "date": {"type": "string", "description": "Optional ISO8601 timestamp in UTC or with timezone."},
                        },
                        "required": ["description", "duration_min", "type"],
                    },
                },
            },
            handler=_fitness_log_workout,
        ),
        ToolEntry(
            name="fitness_log_weight",
            schema={
                "type": "function",
                "function": {
                    "name": "fitness_log_weight",
                    "description": "Log body weight into the isolated fitness store and refresh the profile snapshot.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "kg": {"type": "number"},
                            "date": {"type": "string", "description": "Optional ISO8601 timestamp in UTC or with timezone."},
                        },
                        "required": ["kg"],
                    },
                },
            },
            handler=_fitness_log_weight,
        ),
        ToolEntry(
            name="fitness_summary",
            schema={
                "type": "function",
                "function": {
                    "name": "fitness_summary",
                    "description": "Return nutrition, deficit, workout, and weight summary for today, current week, or current month.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "period": {
                                "type": "string",
                                "enum": ["today", "week", "month"],
                                "default": "today",
                            },
                        },
                    },
                },
            },
            handler=_fitness_summary,
        ),
        ToolEntry(
            name="fitness_profile_read",
            schema={
                "type": "function",
                "function": {
                    "name": "fitness_profile_read",
                    "description": "Read the isolated fitness profile.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            handler=_fitness_profile_read,
        ),
        ToolEntry(
            name="fitness_profile_write",
            schema={
                "type": "function",
                "function": {
                    "name": "fitness_profile_write",
                    "description": "Update the isolated fitness profile with body metrics, targets, and current program.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "height_cm": {"type": "number"},
                            "weight_kg": {"type": "number"},
                            "goal": {"type": "string"},
                            "goal_weight_kg": {"type": "number"},
                            "tdee_kcal": {"type": "number"},
                            "daily_calorie_target": {"type": "number"},
                            "protein_target_g": {"type": "number"},
                            "fat_target_g": {"type": "number"},
                            "carbs_target_g": {"type": "number"},
                            "current_program": {"type": "string"},
                            "notes": {"type": "string"},
                        },
                    },
                },
            },
            handler=_fitness_profile_write,
        ),
    ]
