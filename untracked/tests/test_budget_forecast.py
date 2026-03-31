"""Tests for budget_forecast tool."""
import json
import os
import tempfile
import pathlib
from unittest.mock import patch

import pytest

from ouroboros.tools.budget_forecast import _budget_forecast, get_tools


class FakeCtx:
    pass


# ── Fixtures ─────────────────────────────────────────────────────────────────

SAMPLE_EVENTS = [
    {"ts": "2026-03-01T10:00:00+00:00", "type": "llm_usage", "task_id": "t1",
     "category": "task", "model": "codex/gpt-5.4", "shadow_cost": 5.0,
     "prompt_tokens": 10000, "completion_tokens": 500, "cost": 0.0},
    {"ts": "2026-03-15T10:00:00+00:00", "type": "llm_usage", "task_id": "t2",
     "category": "evolution", "model": "copilot/claude-sonnet-4.6",
     "shadow_cost": 10.0, "prompt_tokens": 50000, "completion_tokens": 800, "cost": 0.0},
    {"ts": "2026-03-30T10:00:00+00:00", "type": "llm_usage", "task_id": "t3",
     "category": "consciousness", "model": "codex/gpt-5.4-mini",
     "shadow_cost": 2.5, "prompt_tokens": 5000, "completion_tokens": 200, "cost": 0.0},
    # Non-llm event (should be filtered out)
    {"ts": "2026-03-30T11:00:00+00:00", "type": "task_done", "task_id": "t3"},
]

SAMPLE_STATE = {
    "budget_total_usd": 2800.0,
    "spent_usd": 1234.0,
    "spent_tokens_prompt": 792000000,
    "spent_tokens_completion": 14000000,
    "spent_tokens_cached": 75000000,
    "session_spent_snapshot": 1200.0,
}


def _make_temp_drive(events: list, state: dict) -> pathlib.Path:
    """Create a minimal drive structure in a temp directory."""
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "logs").mkdir()
    (d / "state").mkdir()
    with open(d / "logs" / "events.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    with open(d / "state" / "state.json", "w") as f:
        json.dump(state, f)
    return d


# ── Tests ────────────────────────────────────────────────────────────────────

def test_get_tools_returns_one_tool():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "budget_forecast"


def test_schema_has_required_fields():
    schema = get_tools()[0].schema
    assert schema["name"] == "budget_forecast"
    assert "description" in schema
    assert "parameters" in schema


def test_budget_totals(tmp_path):
    drive = _make_temp_drive(SAMPLE_EVENTS, SAMPLE_STATE)
    with patch("ouroboros.tools.budget_forecast._DRIVE_ROOT", str(drive)):
        result = json.loads(_budget_forecast(FakeCtx()))
    b = result["budget"]
    assert b["total_usd"] == 2800.0
    assert b["spent_usd"] == 1234.0
    assert b["remaining_usd"] == pytest.approx(1566.0, abs=1.0)
    assert 44.0 < b["spent_pct"] < 45.0
    assert b["session_spend_usd"] == pytest.approx(34.0, abs=0.1)


def test_events_analyzed_count(tmp_path):
    drive = _make_temp_drive(SAMPLE_EVENTS, SAMPLE_STATE)
    with patch("ouroboros.tools.budget_forecast._DRIVE_ROOT", str(drive)):
        result = json.loads(_budget_forecast(FakeCtx()))
    # Only 3 llm_usage events, not the task_done
    assert result["events_analyzed"] == 3


def test_by_category_all_time(tmp_path):
    drive = _make_temp_drive(SAMPLE_EVENTS, SAMPLE_STATE)
    with patch("ouroboros.tools.budget_forecast._DRIVE_ROOT", str(drive)):
        result = json.loads(_budget_forecast(FakeCtx()))
    cats = result["by_category"]["all_time"]
    assert "task" in cats
    assert "evolution" in cats
    assert "consciousness" in cats
    assert cats["task"] == pytest.approx(5.0, abs=0.01)
    assert cats["evolution"] == pytest.approx(10.0, abs=0.01)


def test_by_model_all_time(tmp_path):
    drive = _make_temp_drive(SAMPLE_EVENTS, SAMPLE_STATE)
    with patch("ouroboros.tools.budget_forecast._DRIVE_ROOT", str(drive)):
        result = json.loads(_budget_forecast(FakeCtx()))
    models = result["by_model"]["all_time"]
    assert "codex/gpt-5.4" in models
    assert "copilot/claude-sonnet-4.6" in models


def test_daily_history_default_14_days(tmp_path):
    drive = _make_temp_drive(SAMPLE_EVENTS, SAMPLE_STATE)
    with patch("ouroboros.tools.budget_forecast._DRIVE_ROOT", str(drive)):
        result = json.loads(_budget_forecast(FakeCtx()))
    # daily_history should be a list of [date, cost] pairs
    hist = result["daily_history"]
    assert isinstance(hist, list)
    # All entries should be within last 14 days
    for entry in hist:
        assert len(entry) == 2  # [date_str, cost_float]


def test_daily_history_custom_days(tmp_path):
    drive = _make_temp_drive(SAMPLE_EVENTS, SAMPLE_STATE)
    with patch("ouroboros.tools.budget_forecast._DRIVE_ROOT", str(drive)):
        result = json.loads(_budget_forecast(FakeCtx(), daily_history_days=30))
    hist = result["daily_history"]
    assert isinstance(hist, list)


def test_burn_rates_structure(tmp_path):
    drive = _make_temp_drive(SAMPLE_EVENTS, SAMPLE_STATE)
    with patch("ouroboros.tools.budget_forecast._DRIVE_ROOT", str(drive)):
        result = json.loads(_budget_forecast(FakeCtx()))
    rates = result["burn_rates_usd_per_day"]
    for w in ["1d", "3d", "7d", "14d", "30d"]:
        key = f"{w}_daily_avg"
        assert key in rates
        assert rates[key] >= 0.0


def test_runway_structure(tmp_path):
    drive = _make_temp_drive(SAMPLE_EVENTS, SAMPLE_STATE)
    with patch("ouroboros.tools.budget_forecast._DRIVE_ROOT", str(drive)):
        result = json.loads(_budget_forecast(FakeCtx()))
    runway = result["runway_days"]
    for w in ["1d", "3d", "7d", "14d", "30d"]:
        key = f"at_{w}_rate_days"
        assert key in runway


def test_tokens_section(tmp_path):
    drive = _make_temp_drive(SAMPLE_EVENTS, SAMPLE_STATE)
    with patch("ouroboros.tools.budget_forecast._DRIVE_ROOT", str(drive)):
        result = json.loads(_budget_forecast(FakeCtx()))
    tok = result["tokens"]
    assert tok["prompt_total"] == 792000000
    assert tok["cached_total"] == 75000000
    assert tok["cache_hit_rate_pct"] == pytest.approx(75000000 / 792000000 * 100, abs=0.1)


def test_peak_day(tmp_path):
    drive = _make_temp_drive(SAMPLE_EVENTS, SAMPLE_STATE)
    with patch("ouroboros.tools.budget_forecast._DRIVE_ROOT", str(drive)):
        result = json.loads(_budget_forecast(FakeCtx()))
    peak = result["peak_day"]
    assert peak is not None
    assert "date" in peak
    assert "cost_usd" in peak
    assert peak["cost_usd"] >= 0


def test_empty_events(tmp_path):
    drive = _make_temp_drive([], SAMPLE_STATE)
    with patch("ouroboros.tools.budget_forecast._DRIVE_ROOT", str(drive)):
        result = json.loads(_budget_forecast(FakeCtx()))
    assert result["events_analyzed"] == 0
    assert result["daily_history"] == []
    assert result["peak_day"] is None
    # Burn rates should all be zero
    for v in result["burn_rates_usd_per_day"].values():
        assert v == 0.0


def test_missing_state(tmp_path):
    drive = tmp_path / "empty_drive"
    (drive / "logs").mkdir(parents=True)
    (drive / "state").mkdir()
    with open(drive / "logs" / "events.jsonl", "w") as f:
        pass  # empty
    with patch("ouroboros.tools.budget_forecast._DRIVE_ROOT", str(drive)):
        result = json.loads(_budget_forecast(FakeCtx()))
    # Should not raise; should return sensible defaults
    assert result["budget"]["total_usd"] == 2800.0
    assert result["budget"]["spent_usd"] == 0.0


def test_fallback_to_cost_field(tmp_path):
    """If shadow_cost is absent, should use cost field."""
    events = [
        {"ts": "2026-03-30T10:00:00+00:00", "type": "llm_usage",
         "category": "task", "model": "openrouter/gpt-4",
         "cost": 3.0, "prompt_tokens": 5000, "completion_tokens": 200},
    ]
    drive = _make_temp_drive(events, SAMPLE_STATE)
    with patch("ouroboros.tools.budget_forecast._DRIVE_ROOT", str(drive)):
        result = json.loads(_budget_forecast(FakeCtx()))
    cats = result["by_category"]["all_time"]
    assert cats.get("task", 0) == pytest.approx(3.0, abs=0.01)
