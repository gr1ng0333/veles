"""version_sync — atomic sync of all three version truth sources.

Problem: VERSION / pyproject.toml / README.md badge frequently drift apart,
causing test_version_artifacts.py failures that block every evolution commit.
Pattern seen in evolution #162–165 (all hit TESTS_FAILED for this reason).

This tool collapses the 3-read + 2-write workflow into a single call:
    version_sync()              — inspect current state, report mismatches
    version_sync(bump="patch")  — bump patch and write all three atomically
    version_sync(bump="minor")  — bump minor
    version_sync(bump="major")  — bump major
    version_sync(set_version="7.2.0")  — set exact version in all three

After a successful sync, test_version_artifacts.py passes immediately.
No external dependencies — pure stdlib.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

_REPO_DIR = Path(os.environ.get("OUROBOROS_REPO_DIR", "/opt/veles"))

# ── readers ───────────────────────────────────────────────────────────────────

def _read_version_file(repo: Path) -> str:
    """Read bare version from VERSION file."""
    p = repo / "VERSION"
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8").strip()


def _read_pyproject_version(repo: Path) -> str:
    """Extract version = "x.y.z" from pyproject.toml."""
    p = repo / "pyproject.toml"
    if not p.exists():
        return ""
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r'^version\s*=\s*"([^"]+)"', line.strip())
        if m:
            return m.group(1)
    return ""


def _read_readme_version(repo: Path) -> str:
    """Extract version from the badge line in README.md."""
    p = repo / "README.md"
    if not p.exists():
        return ""
    for line in p.read_text(encoding="utf-8").splitlines():
        # Badge: [![Version](https://img.shields.io/badge/version-X.Y.Z-green)]
        m = re.search(r"version-(\d+\.\d+\.\d+)-", line)
        if m:
            return m.group(1)
    # Also check plain text "**Версия:** x.y.z"
    content = p.read_text(encoding="utf-8")
    m = re.search(r"\*\*Версия:\*\*\s*(\d+\.\d+\.\d+)", content)
    if m:
        return m.group(1)
    return ""


# ── writers ───────────────────────────────────────────────────────────────────

def _write_version_file(repo: Path, version: str) -> None:
    (repo / "VERSION").write_text(version + "\n", encoding="utf-8")


def _write_pyproject_version(repo: Path, version: str) -> None:
    p = repo / "pyproject.toml"
    text = p.read_text(encoding="utf-8")
    new_text = re.sub(
        r'^(version\s*=\s*)"[^"]+"',
        f'\\1"{version}"',
        text,
        flags=re.MULTILINE,
    )
    p.write_text(new_text, encoding="utf-8")


def _write_readme_version(repo: Path, old_version: str, new_version: str) -> None:
    """Replace version references in README.md (badge + text markers)."""
    p = repo / "README.md"
    text = p.read_text(encoding="utf-8")

    # Badge: version-X.Y.Z-green
    if old_version:
        text = text.replace(f"version-{old_version}-", f"version-{new_version}-")
    else:
        # Fallback: replace any badge version pattern
        text = re.sub(
            r"(version-)(\d+\.\d+\.\d+)(-green)",
            f"\\g<1>{new_version}\\3",
            text,
        )

    # Plain text marker: **Версия:** X.Y.Z
    if old_version:
        text = text.replace(f"**Версия:** {old_version}", f"**Версия:** {new_version}")
    else:
        text = re.sub(
            r"\*\*Версия:\*\*\s*\d+\.\d+\.\d+",
            f"**Версия:** {new_version}",
            text,
        )

    # Also handle "**Version:** X.Y.Z" (English variant)
    if old_version:
        text = text.replace(f"**Version:** {old_version}", f"**Version:** {new_version}")

    p.write_text(text, encoding="utf-8")


# ── version bumper ────────────────────────────────────────────────────────────

def _bump(version: str, part: str) -> str:
    """Bump major/minor/patch of a semver string."""
    parts = version.split(".")
    if len(parts) != 3:
        raise ValueError(f"Cannot parse version: {version!r}")
    major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    if part == "major":
        major += 1
        minor = 0
        patch = 0
    elif part == "minor":
        minor += 1
        patch = 0
    elif part == "patch":
        patch += 1
    else:
        raise ValueError(f"Unknown bump part: {part!r} (must be major/minor/patch)")
    return f"{major}.{minor}.{patch}"


# ── git latest tag check ──────────────────────────────────────────────────────

def _latest_git_tag(repo: Path) -> str:
    """Return the latest semver git tag (without 'v' prefix), or ''."""
    try:
        result = subprocess.run(
            ["git", "tag", "--list", "v*", "--sort=-version:refname"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        for line in result.stdout.splitlines():
            tag = line.strip().lstrip("v")
            if re.match(r"^\d+\.\d+\.\d+$", tag):
                return tag
    except Exception:
        pass
    return ""


# ── main logic ────────────────────────────────────────────────────────────────

def _version_sync(
    ctx: ToolContext,
    bump: Optional[str] = None,
    set_version: Optional[str] = None,
) -> str:
    repo = Path(ctx.repo_dir)

    v_file = _read_version_file(repo)
    v_pyproject = _read_pyproject_version(repo)
    v_readme = _read_readme_version(repo)
    v_tag = _latest_git_tag(repo)

    # ── Status mode (no bump/set) ─────────────────────────────────────────────
    if not bump and not set_version:
        synced = (v_file == v_pyproject == v_readme) if v_readme else (v_file == v_pyproject)
        lines = [
            "## version_sync status",
            f"  VERSION file:    {v_file or '(missing)'}",
            f"  pyproject.toml:  {v_pyproject or '(missing)'}",
            f"  README.md badge: {v_readme or '(not found)'}",
            f"  Latest git tag:  {v_tag or '(none)'}",
            "",
        ]
        if synced:
            lines.append("✅ All sources in sync.")
        else:
            lines.append("❌ MISMATCH detected!")
            mismatches = []
            if v_file != v_pyproject:
                mismatches.append(f"  VERSION ({v_file}) ≠ pyproject ({v_pyproject})")
            if v_readme and v_file != v_readme:
                mismatches.append(f"  VERSION ({v_file}) ≠ README badge ({v_readme})")
            lines.extend(mismatches)
            lines.append("")
            lines.append("💡 Fix: version_sync(set_version=\"<target>\") or version_sync(bump=\"patch\")")
        if v_tag and v_tag != v_file:
            lines.append(f"⚠️  Latest git tag ({v_tag}) ≠ VERSION ({v_file})")
        return "\n".join(lines)

    # ── Determine target version ──────────────────────────────────────────────
    if set_version:
        if not re.match(r"^\d+\.\d+\.\d+$", set_version):
            return f"❌ Invalid version format: {set_version!r} (expected X.Y.Z)"
        target = set_version
    else:
        if not v_file:
            return "❌ Cannot bump: VERSION file missing."
        try:
            target = _bump(v_file, bump)
        except ValueError as e:
            return f"❌ {e}"

    old_version = v_file or v_pyproject or v_readme

    # ── Apply ─────────────────────────────────────────────────────────────────
    changed = []

    if v_file != target:
        _write_version_file(repo, target)
        changed.append(f"VERSION: {v_file} → {target}")

    if v_pyproject != target:
        _write_pyproject_version(repo, target)
        changed.append(f"pyproject.toml: {v_pyproject} → {target}")

    if v_readme and v_readme != target:
        _write_readme_version(repo, old_version, target)
        changed.append(f"README.md: {v_readme} → {target}")
    elif not v_readme:
        # Try writing anyway (may add new badge text)
        _write_readme_version(repo, old_version, target)
        changed.append(f"README.md: (badge not found, attempted update)")

    # ── Verify ────────────────────────────────────────────────────────────────
    v_file_after = _read_version_file(repo)
    v_pyproject_after = _read_pyproject_version(repo)
    v_readme_after = _read_readme_version(repo)

    lines = ["## version_sync result"]
    for ch in changed:
        lines.append(f"  ✏️  {ch}")
    if not changed:
        lines.append("  (no changes needed)")
    lines.append("")

    all_ok = (v_file_after == target == v_pyproject_after)
    if v_readme_after:
        all_ok = all_ok and (v_readme_after == target)

    if all_ok:
        lines.append(f"✅ All sources now at {target}")
    else:
        lines.append("⚠️  Post-write verification:")
        lines.append(f"  VERSION:        {v_file_after}")
        lines.append(f"  pyproject.toml: {v_pyproject_after}")
        lines.append(f"  README badge:   {v_readme_after}")

    return "\n".join(lines)


# ── Tool registration ─────────────────────────────────────────────────────────

def get_tools() -> list:
    schema = {
        "name": "version_sync",
        "description": (
            "Atomic sync of all three version truth sources: VERSION file, "
            "pyproject.toml, and README.md badge. "
            "Eliminates the most common evolution blocker: "
            "test_version_artifacts.py failures caused by version drift. "
            "Modes:\n"
            "  version_sync()                   — inspect, show mismatches\n"
            "  version_sync(bump='patch')        — bump patch in all three\n"
            "  version_sync(bump='minor')        — bump minor in all three\n"
            "  version_sync(bump='major')        — bump major in all three\n"
            "  version_sync(set_version='7.2.0') — set exact version everywhere\n"
            "After sync, test_version_artifacts.py passes immediately."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "bump": {
                    "type": "string",
                    "enum": ["patch", "minor", "major"],
                    "description": "Semver part to bump (optional)",
                },
                "set_version": {
                    "type": "string",
                    "description": "Explicit version to set, e.g. '7.2.0' (optional)",
                },
            },
            "required": [],
        },
    }
    return [ToolEntry("version_sync", schema, lambda ctx, **kw: _version_sync(ctx, **kw))]
