"""Tests for skills step 2: context.py loads active skills into Block 1."""

from __future__ import annotations

import json
import pathlib
import pytest


class _FakeEnv:
    def __init__(self, tmp_path: pathlib.Path):
        self.repo_dir = tmp_path / "repo"
        self.drive_root = tmp_path / "drive"
        (self.repo_dir / "prompts" / "skills").mkdir(parents=True)
        (self.drive_root / "state").mkdir(parents=True)
        (self.drive_root / "state" / "state.json").write_text(
            json.dumps({"active_skills": []}), encoding="utf-8"
        )

    def repo_path(self, rel: str) -> pathlib.Path:
        return self.repo_dir / rel

    def drive_path(self, rel: str) -> pathlib.Path:
        return self.drive_root / rel


# ---------------------------------------------------------------------------
# Unit tests: _build_active_skills_sections
# ---------------------------------------------------------------------------

def test_no_active_skills_returns_empty(tmp_path):
    """When active_skills is empty, no skill sections returned."""
    from ouroboros.context import _build_active_skills_sections
    env = _FakeEnv(tmp_path)
    result = _build_active_skills_sections(env)
    assert result == []


def test_active_skill_loaded_into_section(tmp_path):
    """When active_skills has a name, section with skill content is returned."""
    from ouroboros.context import _build_active_skills_sections
    env = _FakeEnv(tmp_path)
    skill_file = env.repo_dir / "prompts" / "skills" / "3xui.md"
    skill_file.write_text("# 3x-ui Skill\nwebBasePath gotcha here.", encoding="utf-8")
    state_path = env.drive_root / "state" / "state.json"
    state_path.write_text(json.dumps({"active_skills": ["3xui"]}), encoding="utf-8")

    result = _build_active_skills_sections(env)
    assert len(result) == 1
    assert "## Skill: 3xui" in result[0]
    assert "webBasePath gotcha here" in result[0]


def test_missing_skill_file_ignored(tmp_path):
    """If skill file does not exist, it is silently ignored (no crash)."""
    from ouroboros.context import _build_active_skills_sections
    env = _FakeEnv(tmp_path)
    state_path = env.drive_root / "state" / "state.json"
    state_path.write_text(json.dumps({"active_skills": ["nonexistent"]}), encoding="utf-8")

    result = _build_active_skills_sections(env)
    assert result == []


def test_multiple_skills_loaded(tmp_path):
    """Multiple active skills all load into separate sections."""
    from ouroboros.context import _build_active_skills_sections
    env = _FakeEnv(tmp_path)
    for name, content in [("skill-a", "Content A"), ("skill-b", "Content B")]:
        (env.repo_dir / "prompts" / "skills" / f"{name}.md").write_text(
            content, encoding="utf-8"
        )
    state_path = env.drive_root / "state" / "state.json"
    state_path.write_text(
        json.dumps({"active_skills": ["skill-a", "skill-b"]}), encoding="utf-8"
    )

    result = _build_active_skills_sections(env)
    assert len(result) == 2
    assert any("Content A" in s for s in result)
    assert any("Content B" in s for s in result)


def test_missing_state_file_returns_empty(tmp_path):
    """If state.json missing, gracefully return empty (no crash)."""
    from ouroboros.context import _build_active_skills_sections
    env = _FakeEnv(tmp_path)
    # Remove state.json
    (env.drive_root / "state" / "state.json").unlink()

    result = _build_active_skills_sections(env)
    assert result == []


# ---------------------------------------------------------------------------
# Integration smoke: skill content appears in Block 1 of build_llm_messages
# ---------------------------------------------------------------------------

def test_skills_appear_in_semi_stable_block(tmp_path):
    """Active skill content must land in semi_stable_text (Block 1, index 1)."""
    from ouroboros.context import build_llm_messages
    from unittest.mock import MagicMock, patch

    env = _FakeEnv(tmp_path)
    prompts_dir = env.repo_dir / "prompts"

    # Minimal stub files
    (env.repo_dir / "BIBLE.md").write_text("# Bible", encoding="utf-8")
    (prompts_dir / "SYSTEM.md").write_text("# System", encoding="utf-8")
    (env.repo_dir / "README.md").write_text("# Readme", encoding="utf-8")
    (prompts_dir / "ARCHITECTURE.md").write_text("", encoding="utf-8")
    (prompts_dir / "CHECKLISTS.md").write_text("", encoding="utf-8")
    (env.drive_root / "memory").mkdir(exist_ok=True)

    # Skill
    skill_file = prompts_dir / "skills" / "test-skill.md"
    skill_file.write_text("UNIQUE_SKILL_CONTENT_XYZ", encoding="utf-8")
    state_path = env.drive_root / "state" / "state.json"
    state_path.write_text(
        json.dumps({"active_skills": ["test-skill"]}), encoding="utf-8"
    )

    # Minimal memory mock
    memory = MagicMock()
    memory.load_scratchpad.return_value = "notes"
    memory.load_identity.return_value = "identity"
    memory.summarize_chat.return_value = ""
    memory.summarize_progress.return_value = ""
    memory.summarize_tools.return_value = ""
    memory.summarize_events.return_value = ""
    memory.summarize_supervisor.return_value = ""
    memory.read_jsonl_tail.return_value = []
    memory.drive_root = env.drive_root
    memory.ensure_files.return_value = None

    with (
        patch("ouroboros.context._build_health_invariants", return_value=""),
        patch("ouroboros.context._build_recent_sections", return_value=[]),
        patch("ouroboros.plans.get_active_plan", return_value=None, create=True),
        patch("ouroboros.consolidator.DialogueConsolidator") as mock_cons,
    ):
        mock_cons.return_value.render_for_context.return_value = ""

        task = {"id": "test-1", "type": "task", "text": "hello"}
        messages = build_llm_messages(env, memory, task)

    system_msg = messages[0]
    blocks = system_msg["content"]
    semi_stable_text = blocks[1]["text"]  # Block 1
    assert "UNIQUE_SKILL_CONTENT_XYZ" in semi_stable_text, (
        "Skill content should be in semi_stable block (Block 1)"
    )
