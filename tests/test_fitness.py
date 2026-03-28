import json
from datetime import datetime, timezone
from pathlib import Path

from ouroboros.tools.fitness import (
    _fitness_log_meal,
    _fitness_log_weight,
    _fitness_log_workout,
    _fitness_profile_read,
    _fitness_profile_write,
    _fitness_summary,
    get_tools,
)


class FakeCtx:
    def __init__(self, root: Path):
        self.repo_dir = root
        self.drive_root = root / "drive"
        self.drive_root.mkdir(parents=True, exist_ok=True)

    def drive_path(self, rel: str) -> Path:
        return self.drive_root / rel


def test_get_tools_registers_expected_names(tmp_path):
    ctx = FakeCtx(tmp_path)
    names = [tool.name for tool in get_tools()]
    assert names == [
        "fitness_log_meal",
        "fitness_log_workout",
        "fitness_log_weight",
        "fitness_summary",
        "fitness_profile_read",
        "fitness_profile_write",
    ]
    assert ctx.drive_path("fitness").parent == ctx.drive_root


def test_profile_write_and_read(tmp_path):
    ctx = FakeCtx(tmp_path)
    written = json.loads(_fitness_profile_write(
        ctx,
        height_cm=173,
        weight_kg=84,
        goal="recomposition",
        tdee_kcal=2400,
        daily_calorie_target=2100,
        protein_target_g=150,
        current_program="calisthenics-base",
        notes="initial setup",
    ))
    profile = written["profile"]
    assert profile["height_cm"] == 173.0
    assert profile["weight_kg"] == 84.0
    assert profile["goal"] == "recomposition"
    read_back = json.loads(_fitness_profile_read(ctx))
    assert read_back["current_program"] == "calisthenics-base"
    assert read_back["notes"] == "initial setup"


def test_logging_meal_updates_week_file_and_log(tmp_path):
    ctx = FakeCtx(tmp_path)
    payload = json.loads(_fitness_log_meal(
        ctx,
        description="buckwheat + chicken",
        calories=640,
        protein=45,
        fat=18,
        carbs=70,
        date="2026-03-28T12:00:00+00:00",
    ))
    assert payload["status"] == "ok"
    assert payload["day_totals"]["calories"] == 640.0

    week_file = ctx.drive_path("fitness/2026-W13.json")
    week = json.loads(week_file.read_text(encoding="utf-8"))
    day = week["days"]["2026-03-28"]
    assert len(day["meals"]) == 1
    assert day["totals"]["protein"] == 45.0

    log_path = ctx.drive_path("fitness/logs/fitness.jsonl")
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event_type"] == "meal"
    assert event["description"] == "buckwheat + chicken"


def test_summary_aggregates_macros_workouts_and_weight(tmp_path, monkeypatch):
    ctx = FakeCtx(tmp_path)
    monkeypatch.setattr("ouroboros.tools.fitness._today_utc", lambda: datetime(2026, 3, 28, 21, 0, tzinfo=timezone.utc))
    _fitness_profile_write(ctx, tdee_kcal=2400, daily_calorie_target=2100, goal="cut")
    _fitness_log_meal(ctx, "meal-1", 800, 50, 25, 70, date="2026-03-24T09:00:00+00:00")
    _fitness_log_meal(ctx, "meal-2", 1000, 60, 30, 90, date="2026-03-26T19:00:00+00:00")
    _fitness_log_workout(ctx, "street workout", 55, "calisthenics", date="2026-03-26T20:00:00+00:00")
    _fitness_log_workout(ctx, "long walk", 180, "walk", date="2026-03-27T18:00:00+00:00")
    _fitness_log_weight(ctx, 84.0, date="2026-03-24T08:00:00+00:00")
    _fitness_log_weight(ctx, 83.4, date="2026-03-28T08:00:00+00:00")

    summary = json.loads(_fitness_summary(ctx, period="week"))
    assert summary["period"] == "week"
    assert summary["nutrition"]["calories"] == 1800.0
    assert summary["workouts"]["count"] == 2
    assert summary["workouts"]["duration_min"] == 235
    assert summary["workouts"]["by_type"]["calisthenics"] == 1
    assert summary["workouts"]["by_type"]["walk"] == 1
    assert summary["weight"]["latest_kg"] == 83.4
    assert summary["weight"]["delta_kg"] == -0.6
    assert summary["deficit"]["vs_daily_target"] == 10800.0
    assert summary["deficit"]["vs_tdee"] == 12600.0

    week_summary_path = ctx.drive_path("fitness/2026-W13_summary.json")
    assert week_summary_path.exists()


def test_weight_log_updates_profile_weight(tmp_path):
    ctx = FakeCtx(tmp_path)
    _fitness_profile_write(ctx, weight_kg=84)
    payload = json.loads(_fitness_log_weight(ctx, 83.7, date="2026-03-28T07:00:00+00:00"))
    assert payload["current_profile_weight_kg"] == 83.7
    profile = json.loads(_fitness_profile_read(ctx))
    assert profile["weight_kg"] == 83.7


def test_workout_type_validation(tmp_path):
    ctx = FakeCtx(tmp_path)
    try:
        _fitness_log_workout(ctx, "mystery", 30, "parkour", date="2026-03-28T20:00:00+00:00")
    except ValueError as exc:
        assert "type must be one of" in str(exc)
    else:
        raise AssertionError("Expected ValueError for unsupported workout type")


def test_profile_read_does_not_seed_active_program_before_bootstrap(tmp_path):
    ctx = FakeCtx(tmp_path)
    _fitness_profile_write(
        ctx,
        height_cm=173,
        weight_kg=84,
        goal="recomposition",
        tdee_kcal=2400,
        daily_calorie_target=2100,
    )
    profile = json.loads(_fitness_profile_read(ctx))
    assert profile["current_program"] == ""
    assert profile["active_program"] == {}


def test_profile_write_rebuilds_program_for_pullup_bar_and_custom_days(tmp_path):
    ctx = FakeCtx(tmp_path)
    stored = json.loads(_fitness_profile_write(
        ctx,
        goal="recomposition",
        training_days=["Tue", "Thu", "Sat"],
        has_pullup_bar=True,
    ))["profile"]
    assert stored["training_days"] == ["tue", "thu", "sat"]
    program = stored["active_program"]
    assert program["training_days"] == ["tue", "thu", "sat"]
    assert program["workouts"][1]["main"][0]["exercise"] == "assisted_hang_or_negative_pullup"
