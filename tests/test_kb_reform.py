"""Tests for KB reform (skills step 6).

Verifies that:
- Domain-specific topics that are now covered by skills are NOT in _index.md
  permanent section (so they don't pollute context)
- Universal topics that should always be loaded ARE present
- The skills map correctly declares the same domains covered by removed topics
"""

from __future__ import annotations

import pathlib

import pytest

DRIVE_ROOT = pathlib.Path("/opt/veles-data")
REPO_ROOT = pathlib.Path("/opt/veles")

KB_INDEX = DRIVE_ROOT / "memory" / "knowledge" / "_index.md"
SKILLS_MAP = REPO_ROOT / "prompts" / "skills" / "_map.md"


# ---------------------------------------------------------------------------
# Topics that must NOT be in permanent KB (moved to skills)
# ---------------------------------------------------------------------------

MOVED_TO_SKILLS = [
    "remote-xui-coexistence",
    "ssh-remote-contour",
    "ssh_password_bootstrap_gotcha",
    "remote_filesystem_guardrails",
    "remote_service_management",
]

# Domain-specific topics archived (not permanently loaded)
ARCHIVED_TOPICS = [
    "android-vpnservice",
    "fcaptcha-antibot-detection-signals",
    "fitness-agent-architecture",
    "fitness-bot-porting",
    "short_video_pack_pipeline",
]


# ---------------------------------------------------------------------------
# Topics that MUST still be in permanent KB (universal patterns)
# ---------------------------------------------------------------------------

MUST_STAY_UNIVERSAL = [
    "browser-login-toolkit",
    "copilot-usage-accounting",
    "cost-optimization",
    "observability-gaps",
    "patterns",
    "release-contour-gotchas",
    "tech-radar-march-2026",
    "telegram-bot-deploy-gotchas",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_index() -> str:
    assert KB_INDEX.exists(), f"KB index not found: {KB_INDEX}"
    return KB_INDEX.read_text(encoding="utf-8")


def _load_skills_map() -> str:
    assert SKILLS_MAP.exists(), f"Skills map not found: {SKILLS_MAP}"
    return SKILLS_MAP.read_text(encoding="utf-8")


def _permanent_section(index_text: str) -> str:
    """Extract the permanent (top) section before any '---' separator."""
    parts = index_text.split("---")
    return parts[0] if parts else index_text


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_kb_index_exists():
    """KB index file must exist on drive."""
    assert KB_INDEX.exists()


def test_moved_topics_not_in_permanent_section():
    """Topics moved to skills must not appear as active bullet points in permanent KB section."""
    index = _load_index()
    permanent = _permanent_section(index)

    for topic in MOVED_TO_SKILLS:
        # They may appear in the "moved to skills" table (after ---), but not
        # as active bullet points (- **topic**:) in the permanent section
        assert f"- **{topic}**:" not in permanent, (
            f"Topic '{topic}' still in permanent KB section — should be moved to skills. "
            "Remove it from the active bullet list."
        )


def test_archived_topics_not_in_permanent_section():
    """Domain-specific archived topics must not be active bullet points in permanent KB."""
    index = _load_index()
    permanent = _permanent_section(index)

    for topic in ARCHIVED_TOPICS:
        assert f"- **{topic}**:" not in permanent, (
            f"Topic '{topic}' still in permanent KB section — should be archived. "
            "Remove it from the active bullet list."
        )


def test_universal_topics_still_present():
    """Universal pattern topics must remain in KB index."""
    index = _load_index()
    for topic in MUST_STAY_UNIVERSAL:
        assert topic in index, (
            f"Universal topic '{topic}' missing from KB index — it should always be loaded."
        )


def test_kb_index_has_moved_section():
    """KB index must document which topics were moved to skills."""
    index = _load_index()
    assert "moved to skills" in index.lower() or "moved_to_skills" in index.lower(), (
        "KB index should have a 'moved to skills' section documenting the migration."
    )


def test_skills_map_covers_ssh_domain():
    """Skills map must have ssh-servers skill (covers moved SSH topics)."""
    skills_map = _load_skills_map()
    assert "ssh-servers" in skills_map, "Skills map must include ssh-servers skill."


def test_skills_map_covers_3xui_domain():
    """Skills map must have 3xui skill (covers moved x-ui topics)."""
    skills_map = _load_skills_map()
    assert "3xui" in skills_map, "Skills map must include 3xui skill."


def test_kb_index_has_archived_section():
    """KB index must have an archived section for domain-specific topics."""
    index = _load_index()
    assert "archived" in index.lower(), (
        "KB index should have an 'archived' section for domain-specific topics."
    )
