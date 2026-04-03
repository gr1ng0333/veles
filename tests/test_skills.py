"""Tests for skills system step 1: skill_load tool + active_skills state management."""

from __future__ import annotations

import json
import pathlib
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeCtx:
    def __init__(self, tmp_path: pathlib.Path):
        self.drive_root = tmp_path / "drive"
        self.repo_dir = tmp_path / "repo"
        (self.drive_root / "state").mkdir(parents=True)
        (self.repo_dir / "prompts" / "skills").mkdir(parents=True)
        # Write a fake skill file
        (self.repo_dir / "prompts" / "skills" / "test-skill.md").write_text(
            "# Test Skill\nSome context about test domain.\n", encoding="utf-8"
        )
        # Also write a fake state.json with active_skills
        state_path = self.drive_root / "state" / "state.json"
        state_path.write_text(json.dumps({"active_skills": []}), encoding="utf-8")

    def drive_path(self, rel: str) -> pathlib.Path:
        return self.drive_root / rel

    def repo_path(self, rel: str) -> pathlib.Path:
        return self.repo_dir / rel


# ---------------------------------------------------------------------------
# Tests: skill_load
# ---------------------------------------------------------------------------

def test_skill_load_success(tmp_path):
    """skill_load should update active_skills in state and return confirmation."""
    from ouroboros.tools.skills import _skill_load

    ctx = _FakeCtx(tmp_path)

    # Patch supervisor.state to use our fake drive_root
    import supervisor.state as ss
    orig_state_path = ss.STATE_PATH
    ss.STATE_PATH = ctx.drive_root / "state" / "state.json"
    ss.STATE_LOCK_PATH = ctx.drive_root / "locks" / "state.lock"
    (ctx.drive_root / "locks").mkdir(exist_ok=True)

    try:
        result = _skill_load(ctx, "test-skill")
        assert "✅" in result, f"Expected success, got: {result}"
        assert "test-skill" in result

        # Verify state was updated
        state = json.loads((ctx.drive_root / "state" / "state.json").read_text())
        assert "test-skill" in state.get("active_skills", [])
    finally:
        ss.STATE_PATH = orig_state_path


def test_skill_load_not_found(tmp_path):
    """skill_load should report clearly when skill doesn't exist."""
    from ouroboros.tools.skills import _skill_load

    ctx = _FakeCtx(tmp_path)

    import supervisor.state as ss
    orig_state_path = ss.STATE_PATH
    ss.STATE_PATH = ctx.drive_root / "state" / "state.json"
    ss.STATE_LOCK_PATH = ctx.drive_root / "locks" / "state.lock"
    (ctx.drive_root / "locks").mkdir(exist_ok=True)

    try:
        result = _skill_load(ctx, "nonexistent")
        assert "⚠️" in result
        assert "nonexistent" in result
    finally:
        ss.STATE_PATH = orig_state_path


def test_skill_load_invalid_name(tmp_path):
    """skill_load should reject names with path traversal or special chars."""
    from ouroboros.tools.skills import _skill_load
    ctx = _FakeCtx(tmp_path)

    for bad in ["../evil", "a b", "", "a" * 200, "a/b"]:
        result = _skill_load(ctx, bad)
        assert "⚠️" in result, f"Expected error for '{bad}', got: {result}"


def test_skill_load_dedup(tmp_path):
    """Loading the same skill twice should not duplicate in active_skills."""
    from ouroboros.tools.skills import _skill_load

    ctx = _FakeCtx(tmp_path)

    import supervisor.state as ss
    orig_state_path = ss.STATE_PATH
    ss.STATE_PATH = ctx.drive_root / "state" / "state.json"
    ss.STATE_LOCK_PATH = ctx.drive_root / "locks" / "state.lock"
    (ctx.drive_root / "locks").mkdir(exist_ok=True)

    try:
        _skill_load(ctx, "test-skill")
        _skill_load(ctx, "test-skill")

        state = json.loads((ctx.drive_root / "state" / "state.json").read_text())
        skills = state.get("active_skills", [])
        assert skills.count("test-skill") == 1
    finally:
        ss.STATE_PATH = orig_state_path


# ---------------------------------------------------------------------------
# Tests: skill_list
# ---------------------------------------------------------------------------

def test_skill_list(tmp_path):
    """skill_list should return skill names from prompts/skills/."""
    from ouroboros.tools.skills import _skill_list
    ctx = _FakeCtx(tmp_path)

    result = _skill_list(ctx)
    assert "test-skill" in result


def test_skill_list_empty(tmp_path):
    """skill_list on empty dir should report gracefully."""
    from ouroboros.tools.skills import _skill_list

    ctx = _FakeCtx(tmp_path)
    # Remove the test skill
    (ctx.repo_dir / "prompts" / "skills" / "test-skill.md").unlink()

    result = _skill_list(ctx)
    assert "no skills" in result.lower() or "(no skills" in result


# ---------------------------------------------------------------------------
# Tests: active_skills default in state
# ---------------------------------------------------------------------------

def test_active_skills_default():
    """ensure_state_defaults must set active_skills to []."""
    from supervisor.state import ensure_state_defaults
    st = ensure_state_defaults({})
    assert "active_skills" in st
    assert st["active_skills"] == []


def test_active_skills_preserved():
    """ensure_state_defaults must not overwrite existing active_skills."""
    from supervisor.state import ensure_state_defaults
    st = ensure_state_defaults({"active_skills": ["3xui", "vpn"]})
    assert st["active_skills"] == ["3xui", "vpn"]


# ---------------------------------------------------------------------------
# Tests: tool registration
# ---------------------------------------------------------------------------

def test_skills_tools_registered():
    """get_tools() must return skill_load and skill_list."""
    from ouroboros.tools.skills import get_tools
    tools = get_tools()
    names = [t.name for t in tools]
    assert "skill_load" in names
    assert "skill_list" in names
