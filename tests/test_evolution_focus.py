"""Tests for evolution_focus tool — cross-cycle strategic goal memory."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.tools.evolution_focus import (
    _add_focus_note,
    _clear_evolution_focus,
    _complete_focus_cycle,
    _get_evolution_focus,
    _set_evolution_focus,
    format_focus_for_context,
    load_evolution_focus,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_drive(tmp_path: Path) -> Path:
    (tmp_path / "state").mkdir()
    return tmp_path


@pytest.fixture
def ctx(tmp_drive: Path) -> MagicMock:
    c = MagicMock()
    c.drive_root = tmp_drive
    return c


# ---------------------------------------------------------------------------
# set_evolution_focus
# ---------------------------------------------------------------------------

class TestSetEvolutionFocus:
    def test_creates_focus_file(self, ctx):
        result = _set_evolution_focus(ctx, "Improve memory search accuracy")
        assert "set" in result.lower() or "focus" in result.lower()
        focus_file = ctx.drive_root / "state" / "evolution_focus.json"
        assert focus_file.exists()

    def test_goal_persisted(self, ctx):
        _set_evolution_focus(ctx, "Add streaming support", horizon_cycles=3)
        data = json.loads((ctx.drive_root / "state" / "evolution_focus.json").read_text())
        assert data["goal"] == "Add streaming support"
        assert data["horizon_cycles"] == 3

    def test_optional_initial_note(self, ctx):
        _set_evolution_focus(ctx, "Refactor context.py", note="Starting with Block 1")
        data = json.loads((ctx.drive_root / "state" / "evolution_focus.json").read_text())
        assert len(data["notes"]) == 1
        assert "Block 1" in data["notes"][0]["text"]

    def test_preserves_cycles_completed_on_update(self, ctx):
        _set_evolution_focus(ctx, "Goal A", horizon_cycles=4)
        # Simulate some completed cycles
        focus_file = ctx.drive_root / "state" / "evolution_focus.json"
        data = json.loads(focus_file.read_text())
        data["cycles_completed"] = 2
        focus_file.write_text(json.dumps(data))

        # Update goal — cycles_completed should survive
        _set_evolution_focus(ctx, "Goal B (revised)", horizon_cycles=6)
        data2 = json.loads(focus_file.read_text())
        assert data2["cycles_completed"] == 2
        assert data2["goal"] == "Goal B (revised)"

    def test_empty_goal_rejected(self, ctx):
        result = _set_evolution_focus(ctx, "   ")
        assert "⚠️" in result

    def test_zero_horizon_rejected(self, ctx):
        result = _set_evolution_focus(ctx, "Valid goal", horizon_cycles=0)
        assert "⚠️" in result


# ---------------------------------------------------------------------------
# get_evolution_focus
# ---------------------------------------------------------------------------

class TestGetEvolutionFocus:
    def test_no_focus_returns_helpful_message(self, ctx):
        result = _get_evolution_focus(ctx)
        assert "no active" in result.lower() or "set_evolution_focus" in result

    def test_returns_goal_when_set(self, ctx):
        _set_evolution_focus(ctx, "Build semantic memory layer", horizon_cycles=5)
        result = _get_evolution_focus(ctx)
        assert "Build semantic memory layer" in result

    def test_shows_cycle_counts(self, ctx):
        _set_evolution_focus(ctx, "My goal", horizon_cycles=7)
        result = _get_evolution_focus(ctx)
        assert "7" in result


# ---------------------------------------------------------------------------
# add_focus_note
# ---------------------------------------------------------------------------

class TestAddFocusNote:
    def test_adds_note(self, ctx):
        _set_evolution_focus(ctx, "My goal")
        _add_focus_note(ctx, "Implemented BM25 scoring")
        data = json.loads((ctx.drive_root / "state" / "evolution_focus.json").read_text())
        texts = [n["text"] for n in data["notes"]]
        assert any("BM25" in t for t in texts)

    def test_multiple_notes_accumulate(self, ctx):
        _set_evolution_focus(ctx, "My goal")
        _add_focus_note(ctx, "Note 1")
        _add_focus_note(ctx, "Note 2")
        _add_focus_note(ctx, "Note 3")
        data = json.loads((ctx.drive_root / "state" / "evolution_focus.json").read_text())
        assert len(data["notes"]) == 3

    def test_empty_note_rejected(self, ctx):
        _set_evolution_focus(ctx, "My goal")
        result = _add_focus_note(ctx, "")
        assert "⚠️" in result

    def test_no_focus_fails_gracefully(self, ctx):
        result = _add_focus_note(ctx, "Some note")
        assert "⚠️" in result or "no active" in result.lower()


# ---------------------------------------------------------------------------
# complete_focus_cycle
# ---------------------------------------------------------------------------

class TestCompleteFocusCycle:
    def test_increments_counter(self, ctx):
        _set_evolution_focus(ctx, "My goal", horizon_cycles=5)
        _complete_focus_cycle(ctx, "Done iteration 1")
        data = json.loads((ctx.drive_root / "state" / "evolution_focus.json").read_text())
        assert data["cycles_completed"] == 1

    def test_multiple_completions(self, ctx):
        _set_evolution_focus(ctx, "My goal", horizon_cycles=10)
        for i in range(4):
            _complete_focus_cycle(ctx, f"Cycle {i+1}")
        data = json.loads((ctx.drive_root / "state" / "evolution_focus.json").read_text())
        assert data["cycles_completed"] == 4

    def test_horizon_reached_message(self, ctx):
        _set_evolution_focus(ctx, "My goal", horizon_cycles=2)
        _complete_focus_cycle(ctx)
        result = _complete_focus_cycle(ctx)
        assert "horizon reached" in result.lower() or "complete" in result.lower()

    def test_optional_note_saved(self, ctx):
        _set_evolution_focus(ctx, "My goal", horizon_cycles=5)
        _complete_focus_cycle(ctx, note="Delivered memory_search BM25")
        data = json.loads((ctx.drive_root / "state" / "evolution_focus.json").read_text())
        assert any("BM25" in n["text"] for n in data["notes"])

    def test_no_focus_fails_gracefully(self, ctx):
        result = _complete_focus_cycle(ctx)
        assert "⚠️" in result or "no active" in result.lower()


# ---------------------------------------------------------------------------
# clear_evolution_focus
# ---------------------------------------------------------------------------

class TestClearEvolutionFocus:
    def test_removes_file(self, ctx):
        _set_evolution_focus(ctx, "My goal")
        _clear_evolution_focus(ctx)
        assert not (ctx.drive_root / "state" / "evolution_focus.json").exists()

    def test_returns_goal_summary(self, ctx):
        _set_evolution_focus(ctx, "Improve copilot billing efficiency", horizon_cycles=3)
        _complete_focus_cycle(ctx)
        result = _clear_evolution_focus(ctx)
        assert "copilot billing" in result.lower() or "Improve copilot" in result

    def test_no_focus_safe(self, ctx):
        result = _clear_evolution_focus(ctx)
        assert "no active" in result.lower()


# ---------------------------------------------------------------------------
# load_evolution_focus / format_focus_for_context
# ---------------------------------------------------------------------------

class TestLoadAndFormat:
    def test_load_returns_empty_when_no_file(self, tmp_drive):
        result = load_evolution_focus(tmp_drive)
        assert result == {}

    def test_load_returns_dict(self, tmp_drive):
        data = {"goal": "Test", "horizon_cycles": 3, "cycles_completed": 1, "notes": []}
        (tmp_drive / "state" / "evolution_focus.json").write_text(json.dumps(data))
        result = load_evolution_focus(tmp_drive)
        assert result["goal"] == "Test"

    def test_format_empty_returns_empty_string(self):
        assert format_focus_for_context({}) == ""

    def test_format_includes_goal(self):
        focus = {
            "goal": "Build fuzzy memory search",
            "horizon_cycles": 5,
            "cycles_completed": 2,
            "notes": [{"ts": "2026-04-05T00:00:00", "text": "BM25 done"}],
        }
        text = format_focus_for_context(focus)
        assert "Build fuzzy memory search" in text
        assert "5" in text
        assert "2" in text
        assert "BM25 done" in text

    def test_format_limits_notes_to_five(self):
        notes = [{"ts": f"2026-04-05T{i:02d}:00:00", "text": f"Note {i}"} for i in range(10)]
        focus = {"goal": "G", "horizon_cycles": 10, "cycles_completed": 0, "notes": notes}
        text = format_focus_for_context(focus)
        # Should show last 5 notes
        assert "Note 9" in text
        assert "Note 5" in text or "Note 6" in text
        # Should NOT show very early notes
        assert "Note 0" not in text

    def test_format_no_notes(self):
        focus = {"goal": "G", "horizon_cycles": 3, "cycles_completed": 0}
        text = format_focus_for_context(focus)
        assert "G" in text
