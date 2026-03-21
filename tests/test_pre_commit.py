"""Tests for pre-commit checks and CHECKLISTS.md."""

import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from ouroboros.tools.git import _pre_commit_checks


@pytest.fixture
def tmp_repo(tmp_path):
    """Create a minimal repo structure in a temp directory."""
    (tmp_path / "VERSION").write_text("6.79.0", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "veles"\nversion = "6.79.0"\n', encoding="utf-8"
    )
    return tmp_path


def test_pre_commit_detects_version_desync(tmp_repo):
    """Should warn when VERSION != pyproject.toml version."""
    (tmp_repo / "VERSION").write_text("6.80.0", encoding="utf-8")
    issues = _pre_commit_checks(tmp_repo, [])
    assert any("out of sync" in w for w in issues)


def test_pre_commit_passes_when_synced(tmp_repo):
    """Should pass when VERSION matches pyproject.toml."""
    issues = _pre_commit_checks(tmp_repo, [])
    version_issues = [w for w in issues if "out of sync" in w]
    assert version_issues == []


def test_pre_commit_detects_secrets(tmp_repo):
    """Should warn about potential secrets in code files."""
    py_file = tmp_repo / "example.py"
    py_file.write_text('TOKEN = "sk-abcdefghijklmnopqrstuvwxyz1234"\n', encoding="utf-8")
    issues = _pre_commit_checks(tmp_repo, ["example.py"])
    assert any("secret" in w.lower() or "sk-" in w for w in issues)


def test_pre_commit_skips_non_code_files(tmp_repo):
    """Should not check binary/non-code files for secrets."""
    bin_file = tmp_repo / "data.bin"
    bin_file.write_bytes(b"sk-abcdefghijklmnopqrstuvwxyz1234")
    issues = _pre_commit_checks(tmp_repo, ["data.bin"])
    secret_issues = [w for w in issues if "secret" in w.lower()]
    assert secret_issues == []


def test_pre_commit_detects_import_errors(tmp_repo):
    """Should warn about broken imports in changed files."""
    bad_file = tmp_repo / "broken_module.py"
    bad_file.write_text("import nonexistent_xyzzy_package\n", encoding="utf-8")
    issues = _pre_commit_checks(tmp_repo, ["broken_module.py"])
    assert any("Import error" in w for w in issues)


def test_pre_commit_skips_test_files(tmp_repo):
    """Should not import-check test files."""
    test_file = tmp_repo / "tests" / "test_foo.py"
    test_file.parent.mkdir(exist_ok=True)
    test_file.write_text("import nonexistent_xyzzy_package\n", encoding="utf-8")
    issues = _pre_commit_checks(tmp_repo, ["tests/test_foo.py"])
    import_issues = [w for w in issues if "Import error" in w]
    assert import_issues == []


def test_checklists_md_exists():
    """CHECKLISTS.md should exist and contain critical items."""
    # Resolve relative to repo root
    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / "prompts" / "CHECKLISTS.md"
    assert path.exists(), f"CHECKLISTS.md not found at {path}"
    content = path.read_text(encoding="utf-8")
    content_lower = content.lower()
    assert "bible" in content_lower
    assert "version" in content_lower
    assert "tests" in content_lower
    assert "secret" in content_lower or "no_secrets" in content_lower
    assert "critical" in content_lower
    assert "advisory" in content_lower


def test_checklists_md_compact():
    """CHECKLISTS.md should be under 2500 chars for static context budget."""
    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / "prompts" / "CHECKLISTS.md"
    content = path.read_text(encoding="utf-8")
    assert len(content) < 2500, f"CHECKLISTS.md too large: {len(content)} chars"
