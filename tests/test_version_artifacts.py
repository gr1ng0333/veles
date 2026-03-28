"""test_version_artifacts.py

Ensures that VERSION, pyproject.toml, and README.md are always in sync.
This test is the canonical guard against release-metadata desync, a recurring
pattern logged in the Pattern Register.
"""
from pathlib import Path
import re

ROOT = Path(__file__).parent.parent


def _version_file():
    return (ROOT / "VERSION").read_text().strip()


def _pyproject_version():
    txt = (ROOT / "pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', txt, re.MULTILINE)
    assert m, "version field not found in pyproject.toml"
    return m.group(1)


def _readme_badge_version():
    txt = (ROOT / "README.md").read_text()
    m = re.search(r'version-([\d.]+)-green', txt)
    assert m, "version badge not found in README.md"
    return m.group(1)


def _readme_text_version():
    txt = (ROOT / "README.md").read_text()
    m = re.search(r'\*\*Версия:\*\*\s*([\d.]+)', txt)
    assert m, "**Версия:** field not found in README.md"
    return m.group(1)


def _readme_changelog_version():
    """First changelog header in README."""
    txt = (ROOT / "README.md").read_text()
    m = re.search(r'###\s*v?([\d.]+)', txt)
    assert m, "No changelog entry found in README.md"
    return m.group(1)


def test_version_file_vs_pyproject():
    v = _version_file()
    p = _pyproject_version()
    assert v == p, f"VERSION={v!r} != pyproject.toml version={p!r}"


def test_version_file_vs_readme_badge():
    v = _version_file()
    b = _readme_badge_version()
    assert v == b, f"VERSION={v!r} != README badge={b!r}"


def test_version_file_vs_readme_text():
    v = _version_file()
    t = _readme_text_version()
    assert v == t, f"VERSION={v!r} != README text version={t!r}"


def test_readme_changelog_latest_matches_version():
    v = _version_file()
    c = _readme_changelog_version()
    assert v == c, f"VERSION={v!r} but latest changelog entry is v{c!r}"
