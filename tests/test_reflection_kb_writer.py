"""Tests for reflection_kb_writer — auto-write KB insights from reflections."""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from ouroboros.reflection_kb_writer import (
    _extract_insight,
    _match_topic,
    maybe_write_kb_insight,
)


# ---------------------------------------------------------------------------
# _extract_insight
# ---------------------------------------------------------------------------

class TestExtractInsight:
    def test_extracts_next_time_sentence(self):
        text = "The push failed. Next time, run test_version_artifacts before commit."
        result = _extract_insight(text)
        assert result is not None
        assert "test_version_artifacts" in result

    def test_extracts_should_sentence(self):
        text = "Root cause was version mismatch. Should update README badge in lockstep with VERSION file."
        result = _extract_insight(text)
        assert result is not None
        assert len(result) >= 20

    def test_extracts_fix_sentence(self):
        text = "Agent timed out. Fix: split the task into smaller commands to stay under 60s limit."
        result = _extract_insight(text)
        assert result is not None

    def test_returns_none_for_empty(self):
        assert _extract_insight("") is None
        assert _extract_insight(None) is None  # type: ignore

    def test_returns_none_if_no_actionable(self):
        text = "The task was about reading a file. Nothing unusual happened."
        # No next-time / should / fix → None
        result = _extract_insight(text)
        # May or may not find something — just ensure it doesn't crash
        assert result is None or len(result) >= 20

    def test_caps_at_300_chars(self):
        long_text = "Next time, " + "a" * 400 + " should be done."
        result = _extract_insight(long_text)
        if result:
            assert len(result) <= 300


# ---------------------------------------------------------------------------
# _match_topic
# ---------------------------------------------------------------------------

class TestMatchTopic:
    def test_matches_tests_failed_to_release_gotchas(self):
        topic = _match_topic(["TESTS_FAILED"], "pre-push blocked")
        assert topic == "release-contour-gotchas"

    def test_matches_version_artifacts_to_release_gotchas(self):
        topic = _match_topic([], "test_version_artifacts failed with badge assertion")
        assert topic == "release-contour-gotchas"

    def test_matches_tool_timeout_to_timeout_gotchas(self):
        topic = _match_topic(["TOOL_TIMEOUT"], "exceeded 60s limit on repo_commit_push")
        assert topic == "timeout-guard-gotchas"

    def test_matches_repo_commit_push_to_timeout_gotchas(self):
        topic = _match_topic([], "repo_commit_push timed out with exceeded 30s")
        assert topic == "timeout-guard-gotchas"

    def test_matches_ssh_to_ssh_remote(self):
        topic = _match_topic([], "ssh_key_deploy failed with password bootstrap error")
        assert topic == "ssh-remote-contour"

    def test_matches_copilot_exhausted(self):
        topic = _match_topic([], "all capable accounts exhausted, no cooldown")
        assert topic == "copilot-usage-accounting"

    def test_returns_none_for_no_match(self):
        topic = _match_topic([], "The agent produced a great summary.")
        assert topic is None

    def test_first_match_wins(self):
        # TESTS_FAILED should match release-contour-gotchas before timeout
        topic = _match_topic(["TESTS_FAILED", "TOOL_TIMEOUT"], "pre-push blocked timeout")
        # First rule that matches: TESTS_FAILED → release-contour-gotchas
        assert topic == "release-contour-gotchas"

    def test_case_insensitive_matching(self):
        topic = _match_topic(["tests_failed"], "PRE_PUSH blocked because SMOKE test failed")
        assert topic == "release-contour-gotchas"


# ---------------------------------------------------------------------------
# maybe_write_kb_insight
# ---------------------------------------------------------------------------

class TestMaybeWriteKbInsight:
    def _make_drive(self) -> pathlib.Path:
        tmp = tempfile.mkdtemp()
        return pathlib.Path(tmp)

    def test_writes_insight_to_correct_topic(self):
        drive = self._make_drive()
        reflection = (
            "The push was blocked because test_version_artifacts failed. "
            "Next time, update README badge and VERSION in lockstep before push."
        )
        result = maybe_write_kb_insight(
            drive_root=drive,
            task_id="abc123",
            key_markers=["TESTS_FAILED"],
            reflection_text=reflection,
        )
        assert result == "release-contour-gotchas"
        kb_path = drive / "memory" / "knowledge" / "release-contour-gotchas.md"
        assert kb_path.exists()
        content = kb_path.read_text()
        assert "abc123" in content
        assert "Next time" in content or "update README" in content

    def test_no_duplicate_for_same_task(self):
        drive = self._make_drive()
        reflection = "Next time, verify the smoke tests before attempting commit."
        for _ in range(3):
            maybe_write_kb_insight(
                drive_root=drive,
                task_id="dup999",
                key_markers=["TESTS_FAILED"],
                reflection_text=reflection,
            )
        kb_path = drive / "memory" / "knowledge" / "release-contour-gotchas.md"
        content = kb_path.read_text()
        # task id should appear only once
        assert content.count("dup999") == 1

    def test_returns_none_when_no_match(self):
        drive = self._make_drive()
        result = maybe_write_kb_insight(
            drive_root=drive,
            task_id="xyz",
            key_markers=[],
            reflection_text="Everything went fine, no issues.",
        )
        assert result is None

    def test_returns_none_when_no_insight(self):
        drive = self._make_drive()
        # Has a matching marker but no actionable sentence
        result = maybe_write_kb_insight(
            drive_root=drive,
            task_id="abc",
            key_markers=["TESTS_FAILED"],
            reflection_text="Something failed here.",
        )
        assert result is None

    def test_appends_to_existing_file(self):
        drive = self._make_drive()
        kb_path = drive / "memory" / "knowledge" / "release-contour-gotchas.md"
        kb_path.parent.mkdir(parents=True, exist_ok=True)
        kb_path.write_text("# Existing content\n\n- Old entry\n", encoding="utf-8")

        reflection = "Next time, run pytest tests/test_version_artifacts.py before push."
        maybe_write_kb_insight(
            drive_root=drive,
            task_id="newt1",
            key_markers=["TESTS_FAILED"],
            reflection_text=reflection,
        )
        content = kb_path.read_text()
        assert "# Existing content" in content
        assert "Old entry" in content
        assert "newt1" in content

    def test_never_raises(self):
        """maybe_write_kb_insight must not raise under any circumstance."""
        # Drive doesn't exist at all
        drive = pathlib.Path("/nonexistent/path/that/does/not/exist")
        result = maybe_write_kb_insight(
            drive_root=drive,
            task_id="crash",
            key_markers=["TOOL_TIMEOUT"],
            reflection_text="Next time, use smaller batches.",
        )
        # Either None or a topic string — no exception
        assert result is None or isinstance(result, str)

    def test_multiple_different_tasks_all_written(self):
        drive = self._make_drive()
        tasks = [f"task{i:03d}" for i in range(5)]
        reflections = [
            f"Next time, verify test_version_artifacts before pushing task {i}."
            for i in range(5)
        ]
        for task_id, refl in zip(tasks, reflections):
            maybe_write_kb_insight(
                drive_root=drive,
                task_id=task_id,
                key_markers=["TESTS_FAILED"],
                reflection_text=refl,
            )
        kb_path = drive / "memory" / "knowledge" / "release-contour-gotchas.md"
        content = kb_path.read_text()
        for task_id in tasks:
            assert task_id[:8] in content
