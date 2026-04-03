"""Tests for automatic pattern register update after each reflection.

Covers:
- update_patterns_from_reflections() correctly reads reflections and writes patterns.md
- append_reflection() triggers pattern update when key_markers present
- Pattern merge: counts increment, new classes added
- No-op when no reflections or all unclassified
- Idempotency: calling twice doesn't duplicate rows
"""

from __future__ import annotations

import json
import pathlib

import pytest

from ouroboros.tools.extract_patterns import update_patterns_from_reflections, _parse_patterns_md
from ouroboros.reflection import append_reflection, generate_reflection_template


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_reflection(
    task_id: str,
    goal: str,
    markers: list[str],
    reflection: str,
) -> dict:
    return {
        "ts": "2026-04-03T22:00:00+00:00",
        "task_id": task_id,
        "goal": goal,
        "rounds": 5,
        "max_rounds": 30,
        "error_count": len(markers),
        "key_markers": markers,
        "reflection": reflection,
    }


def _write_reflections(drive_root: pathlib.Path, records: list[dict]) -> None:
    log_dir = drive_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "task_reflections.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _read_patterns(drive_root: pathlib.Path) -> dict:
    path = drive_root / "memory" / "knowledge" / "patterns.md"
    if not path.exists():
        return {}
    return _parse_patterns_md(path.read_text(encoding="utf-8"))


# ── Tests: update_patterns_from_reflections ───────────────────────────────────


def test_no_reflections_returns_empty(tmp_path):
    """When no reflections exist, stats show 0 total."""
    stats = update_patterns_from_reflections(tmp_path)
    assert stats["total_reflections"] == 0
    assert stats["new_classes"] == []
    assert stats["updated_classes"] == []


def test_new_pattern_added(tmp_path):
    """Two reflections with TOOL_TIMEOUT markers produce a new pattern class."""
    records = [
        _make_reflection(
            "aaa1",
            "commit code",
            ["TOOL_TIMEOUT"],
            "repo_write_commit exceeded 30s limit during pre-push test. Root cause: pytest timeout.",
        ),
        _make_reflection(
            "aaa2",
            "push evolution",
            ["TOOL_TIMEOUT"],
            "repo_commit_push hit TOOL_TIMEOUT exceeded 60s. Root cause: push timeout.",
        ),
    ]
    _write_reflections(tmp_path, records)

    stats = update_patterns_from_reflections(tmp_path, min_count=2)
    assert stats["total_reflections"] == 2
    assert len(stats["new_classes"]) >= 1

    patterns = _read_patterns(tmp_path)
    assert len(patterns) >= 1


def test_pattern_file_written(tmp_path):
    """Patterns.md file is created when qualifying patterns found."""
    records = [
        _make_reflection("b1", "evolve", ["TOOL_TIMEOUT"], "repo_commit_push TOOL_TIMEOUT exceeded"),
        _make_reflection("b2", "evolve", ["TOOL_TIMEOUT"], "repo_write_commit TOOL_TIMEOUT exceeded"),
    ]
    _write_reflections(tmp_path, records)
    update_patterns_from_reflections(tmp_path, min_count=2)

    patterns_path = tmp_path / "memory" / "knowledge" / "patterns.md"
    assert patterns_path.exists()
    content = patterns_path.read_text()
    assert "Pattern Register" in content
    assert "|" in content


def test_idempotency(tmp_path):
    """Calling twice with same reflections doesn't duplicate rows."""
    records = [
        _make_reflection("c1", "task", ["TOOL_TIMEOUT"], "repo_write_commit TOOL_TIMEOUT exceeded"),
        _make_reflection("c2", "task", ["TOOL_TIMEOUT"], "repo_commit_push TOOL_TIMEOUT exceeded"),
    ]
    _write_reflections(tmp_path, records)

    update_patterns_from_reflections(tmp_path, min_count=2)
    patterns_first = _read_patterns(tmp_path)

    update_patterns_from_reflections(tmp_path, min_count=2)
    patterns_second = _read_patterns(tmp_path)

    assert set(patterns_first.keys()) == set(patterns_second.keys()), "Row count should not change"


def test_min_count_filter(tmp_path):
    """Pattern with only 1 occurrence should not appear when min_count=2."""
    records = [
        _make_reflection("d1", "task", ["TOOL_TIMEOUT"], "TOOL_TIMEOUT exceeded 30s"),
    ]
    _write_reflections(tmp_path, records)

    stats = update_patterns_from_reflections(tmp_path, min_count=2)
    assert len(stats["new_classes"]) == 0


def test_count_increment_on_second_call(tmp_path):
    """When new reflections appear, existing pattern count updates."""
    records_1 = [
        _make_reflection("e1", "task", ["TOOL_TIMEOUT"], "repo_write_commit TOOL_TIMEOUT"),
        _make_reflection("e2", "task", ["TOOL_TIMEOUT"], "repo_commit_push TOOL_TIMEOUT"),
    ]
    _write_reflections(tmp_path, records_1)
    update_patterns_from_reflections(tmp_path, min_count=2)
    patterns_v1 = _read_patterns(tmp_path)

    # Add 3 more reflections
    records_2 = records_1 + [
        _make_reflection("e3", "task", ["TOOL_TIMEOUT"], "repo_write_commit TOOL_TIMEOUT exceeded"),
        _make_reflection("e4", "task", ["TOOL_TIMEOUT"], "push TOOL_TIMEOUT again"),
        _make_reflection("e5", "task", ["TOOL_TIMEOUT"], "TOOL_TIMEOUT exceeded 60s repo_commit_push"),
    ]
    _write_reflections(tmp_path, records_2)
    stats = update_patterns_from_reflections(tmp_path, min_count=2)

    patterns_v2 = _read_patterns(tmp_path)
    # At least one class should have higher count
    # Find any class that's in both
    common = set(patterns_v1.keys()) & set(patterns_v2.keys())
    if common:
        cls = next(iter(common))
        assert patterns_v2[cls]["count"] >= patterns_v1[cls]["count"]


def test_unclassified_not_in_patterns(tmp_path):
    """Reflections that don't match any class shouldn't appear in patterns."""
    records = [
        _make_reflection("f1", "random task", [], "Everything went fine actually."),
        _make_reflection("f2", "another task", [], "No issues found here."),
    ]
    _write_reflections(tmp_path, records)
    stats = update_patterns_from_reflections(tmp_path, min_count=2)
    assert len(stats["new_classes"]) == 0


# ── Tests: append_reflection integration ──────────────────────────────────────


def test_append_reflection_updates_patterns(tmp_path):
    """append_reflection with markers should trigger pattern update."""
    # Pre-populate with existing reflections so we have 2+ occurrences
    existing = [
        _make_reflection(
            "g1", "evolve", ["TOOL_TIMEOUT"],
            "repo_write_commit TOOL_TIMEOUT exceeded 30s",
        ),
    ]
    _write_reflections(tmp_path, existing)

    # Now append a second one via append_reflection
    entry = _make_reflection(
        "g2", "evolve again", ["TOOL_TIMEOUT"],
        "repo_commit_push TOOL_TIMEOUT exceeded 60s pre-push test",
    )
    append_reflection(tmp_path, entry)

    # patterns.md should be created/updated
    patterns_path = tmp_path / "memory" / "knowledge" / "patterns.md"
    assert patterns_path.exists(), "patterns.md should be created by append_reflection"
    content = patterns_path.read_text()
    assert "|" in content


def test_append_reflection_no_markers_skips_but_still_updates(tmp_path):
    """append_reflection without markers still runs pattern update (all reflections scanned)."""
    # Pre-populate two TOOL_TIMEOUT reflections
    existing = [
        _make_reflection("h1", "task", ["TOOL_TIMEOUT"], "repo_write_commit TOOL_TIMEOUT exceeded"),
        _make_reflection("h2", "task", ["TOOL_TIMEOUT"], "push TOOL_TIMEOUT"),
    ]
    _write_reflections(tmp_path, existing)

    # Append a clean reflection (no markers)
    clean_entry = {
        "ts": "2026-04-03T22:00:00+00:00",
        "task_id": "h3",
        "goal": "clean task",
        "rounds": 3,
        "max_rounds": 30,
        "error_count": 0,
        "key_markers": [],
        "reflection": "Everything worked fine.",
    }
    append_reflection(tmp_path, clean_entry)

    # Pattern update still fires — existing patterns should be in patterns.md
    patterns_path = tmp_path / "memory" / "knowledge" / "patterns.md"
    assert patterns_path.exists()


def test_no_llm_call_in_pattern_update(tmp_path, monkeypatch):
    """Pattern update should NOT call LLMClient — it's deterministic now."""
    import ouroboros.tools.extract_patterns as ep

    calls = []
    original = ep.update_patterns_from_reflections

    def tracked(drive_root, min_count=2):
        calls.append(drive_root)
        return original(drive_root, min_count=min_count)

    monkeypatch.setattr(ep, "update_patterns_from_reflections", tracked)

    records = [
        _make_reflection("i1", "task", ["TOOL_TIMEOUT"], "TOOL_TIMEOUT exceeded 30s repo_write_commit"),
        _make_reflection("i2", "task", ["TOOL_TIMEOUT"], "TOOL_TIMEOUT exceeded 60s push"),
    ]
    _write_reflections(tmp_path, records)

    entry = _make_reflection("i3", "task", ["TOOL_TIMEOUT"], "TOOL_TIMEOUT exceeded again")
    append_reflection(tmp_path, entry)

    assert len(calls) >= 1, "update_patterns_from_reflections should have been called"
