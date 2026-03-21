"""Tests for prompts/ARCHITECTURE.md."""

from pathlib import Path


def test_architecture_md_exists():
    """ARCHITECTURE.md should exist in prompts/."""
    assert Path("prompts/ARCHITECTURE.md").exists()


def test_architecture_md_has_key_sections():
    content = Path("prompts/ARCHITECTURE.md").read_text(encoding="utf-8")
    # Process model
    assert "process" in content.lower() or "supervisor" in content.lower()
    # Transport
    assert "transport" in content.lower() or "codex" in content.lower()
    # Data layout
    assert "data" in content.lower() or "/opt/veles" in content
    # Context assembly
    assert "context" in content.lower()
    # Memory subsystems
    assert "memory" in content.lower()
    # Tool registry
    assert "tool" in content.lower() or "registry" in content.lower()


def test_architecture_md_not_too_large():
    """Should be compact — loaded into static context block."""
    content = Path("prompts/ARCHITECTURE.md").read_text(encoding="utf-8")
    assert len(content) < 20000  # < 20K chars


def test_architecture_md_has_module_table():
    """Should contain module table with real files."""
    content = Path("prompts/ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "agent.py" in content
    assert "loop.py" in content
    assert "context.py" in content
    assert "llm.py" in content
    assert "pricing.py" in content
