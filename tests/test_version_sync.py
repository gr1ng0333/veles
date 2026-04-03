"""Tests for version_sync tool.

Covers: status mode, bump patch/minor/major, set_version, mismatch detection,
README badge update, pyproject.toml update, verify after sync.
"""

from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path

import pytest

# ── bootstrap import without real env ─────────────────────────────────────────

sys.path.insert(0, str(Path(__file__).parent.parent))

from ouroboros.tools.version_sync import (
    _read_version_file,
    _read_pyproject_version,
    _read_readme_version,
    _write_version_file,
    _write_pyproject_version,
    _write_readme_version,
    _bump,
    _version_sync,
    get_tools,
)
from ouroboros.tools.registry import ToolContext


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_ctx(repo: Path) -> ToolContext:
    ctx = types.SimpleNamespace()
    ctx.repo_dir = str(repo)
    ctx.drive_root = Path("/tmp")
    return ctx  # type: ignore[return-value]


def _setup_repo(tmp_path: Path, version: str = "7.1.10") -> Path:
    """Create minimal repo with VERSION, pyproject.toml, README.md."""
    (tmp_path / "VERSION").write_text(version + "\n")
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "veles"\nversion = "{version}"\ntarget-version = "py310"\n'
    )
    (tmp_path / "README.md").write_text(
        f"# Veles\n\n"
        f'[![Version](https://img.shields.io/badge/version-{version}-green)](https://example.com)\n\n'
        f"**Версия:** {version} | Repo: ...\n"
    )
    return tmp_path


# ── _bump ─────────────────────────────────────────────────────────────────────

def test_bump_patch():
    assert _bump("7.1.10", "patch") == "7.1.11"


def test_bump_minor():
    assert _bump("7.1.10", "minor") == "7.2.0"


def test_bump_major():
    assert _bump("7.1.10", "major") == "8.0.0"


def test_bump_invalid_part():
    with pytest.raises(ValueError, match="Unknown bump part"):
        _bump("7.1.10", "micro")


def test_bump_invalid_version():
    with pytest.raises(ValueError, match="Cannot parse version"):
        _bump("7.1", "patch")


# ── readers / writers round-trip ──────────────────────────────────────────────

def test_version_file_roundtrip(tmp_path: Path):
    _write_version_file(tmp_path, "7.2.0")
    assert _read_version_file(tmp_path) == "7.2.0"


def test_pyproject_roundtrip(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text('version = "7.1.0"\ntarget-version = "py310"\n')
    _write_pyproject_version(tmp_path, "7.2.0")
    assert _read_pyproject_version(tmp_path) == "7.2.0"


def test_readme_badge_roundtrip(tmp_path: Path):
    (tmp_path / "README.md").write_text(
        "[![Version](https://img.shields.io/badge/version-7.1.0-green)](https://x.com)\n"
    )
    _write_readme_version(tmp_path, "7.1.0", "7.2.0")
    assert _read_readme_version(tmp_path) == "7.2.0"


def test_readme_text_roundtrip(tmp_path: Path):
    (tmp_path / "README.md").write_text("**Версия:** 7.1.0 | Repo: ...\n")
    _write_readme_version(tmp_path, "7.1.0", "7.2.0")
    content = (tmp_path / "README.md").read_text()
    assert "7.2.0" in content


# ── status mode ───────────────────────────────────────────────────────────────

def test_status_synced(tmp_path: Path):
    _setup_repo(tmp_path, "7.1.10")
    ctx = _make_ctx(tmp_path)
    result = _version_sync(ctx)
    assert "✅" in result
    assert "7.1.10" in result


def test_status_mismatch(tmp_path: Path):
    _setup_repo(tmp_path, "7.1.10")
    # Manually break pyproject
    p = tmp_path / "pyproject.toml"
    p.write_text(p.read_text().replace("7.1.10", "7.1.9"))
    ctx = _make_ctx(tmp_path)
    result = _version_sync(ctx)
    assert "❌" in result or "MISMATCH" in result


# ── bump mode ─────────────────────────────────────────────────────────────────

def test_bump_patch_mode(tmp_path: Path):
    _setup_repo(tmp_path, "7.1.10")
    ctx = _make_ctx(tmp_path)
    result = _version_sync(ctx, bump="patch")
    assert "7.1.11" in result
    assert _read_version_file(tmp_path) == "7.1.11"
    assert _read_pyproject_version(tmp_path) == "7.1.11"
    assert _read_readme_version(tmp_path) == "7.1.11"


def test_bump_minor_mode(tmp_path: Path):
    _setup_repo(tmp_path, "7.1.10")
    ctx = _make_ctx(tmp_path)
    result = _version_sync(ctx, bump="minor")
    assert "7.2.0" in result
    assert _read_version_file(tmp_path) == "7.2.0"


def test_bump_major_mode(tmp_path: Path):
    _setup_repo(tmp_path, "7.1.10")
    ctx = _make_ctx(tmp_path)
    result = _version_sync(ctx, bump="major")
    assert "8.0.0" in result
    assert _read_version_file(tmp_path) == "8.0.0"


# ── set_version mode ──────────────────────────────────────────────────────────

def test_set_version(tmp_path: Path):
    _setup_repo(tmp_path, "7.1.10")
    ctx = _make_ctx(tmp_path)
    result = _version_sync(ctx, set_version="7.5.0")
    assert "7.5.0" in result
    assert _read_version_file(tmp_path) == "7.5.0"
    assert _read_pyproject_version(tmp_path) == "7.5.0"
    assert _read_readme_version(tmp_path) == "7.5.0"


def test_set_version_invalid_format(tmp_path: Path):
    _setup_repo(tmp_path, "7.1.10")
    ctx = _make_ctx(tmp_path)
    result = _version_sync(ctx, set_version="bad-version")
    assert "❌" in result


# ── fixes drift atomically ────────────────────────────────────────────────────

def test_sync_fixes_drift(tmp_path: Path):
    """Simulate the exact failure from evolution #162–165."""
    # VERSION says 7.1.31, pyproject drifted to 7.1.30
    (tmp_path / "VERSION").write_text("7.1.31\n")
    (tmp_path / "pyproject.toml").write_text(
        'version = "7.1.30"\ntarget-version = "py310"\n'
    )
    (tmp_path / "README.md").write_text(
        "[![Version](https://img.shields.io/badge/version-7.1.31-green)](https://x.com)\n"
        "**Версия:** 7.1.31 | ...\n"
    )
    ctx = _make_ctx(tmp_path)
    # set_version to canonical
    result = _version_sync(ctx, set_version="7.1.31")
    assert "✅" in result
    assert _read_pyproject_version(tmp_path) == "7.1.31"


# ── no-op when already synced ─────────────────────────────────────────────────

def test_set_version_no_change(tmp_path: Path):
    """set_version to current value should be a no-op (no writes needed)."""
    _setup_repo(tmp_path, "7.1.31")
    ctx = _make_ctx(tmp_path)
    result = _version_sync(ctx, set_version="7.1.31")
    # Should report success and mention no changes or just ✅
    assert "7.1.31" in result


# ── tool registration ─────────────────────────────────────────────────────────

def test_get_tools():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "version_sync"


def test_tool_schema():
    schema = get_tools()[0].schema
    assert schema["name"] == "version_sync"
    props = schema["parameters"]["properties"]
    assert "bump" in props
    assert "set_version" in props
    assert props["bump"]["enum"] == ["patch", "minor", "major"]
