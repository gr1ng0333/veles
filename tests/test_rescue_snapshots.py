"""Tests for rescue snapshot cleanup in supervisor/git_ops.py."""

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def rescue_env(tmp_path):
    """Set up a temporary DRIVE_ROOT with rescue directories."""
    drive_root = tmp_path / "drive"
    rescue_root = drive_root / "archive" / "rescue"
    rescue_root.mkdir(parents=True)
    return drive_root, rescue_root


def _create_fake_rescue(rescue_root: Path, name: str) -> Path:
    """Create a fake rescue snapshot directory."""
    d = rescue_root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "changes.diff").write_text("fake diff", encoding="utf-8")
    (d / "rescue_meta.json").write_text(
        json.dumps({"ts": "2026-01-01T00:00:00Z", "reason": "test"}),
        encoding="utf-8",
    )
    return d


def test_rescue_snapshot_cleanup_removes_old(rescue_env):
    """Should remove old snapshots when >20 exist."""
    drive_root, rescue_root = rescue_env

    # Create 25 snapshots
    for i in range(25):
        _create_fake_rescue(rescue_root, f"20260101_{i:06d}_test")

    assert len(list(rescue_root.iterdir())) == 25

    with patch("supervisor.git_ops.DRIVE_ROOT", drive_root):
        from supervisor.git_ops import _cleanup_old_rescue_snapshots, MAX_RESCUE_SNAPSHOTS
        _cleanup_old_rescue_snapshots()

    remaining = list(rescue_root.iterdir())
    assert len(remaining) == MAX_RESCUE_SNAPSHOTS


def test_rescue_snapshot_cleanup_keeps_all_when_under_limit(rescue_env):
    """Should not remove anything when count <= 20."""
    drive_root, rescue_root = rescue_env

    for i in range(5):
        _create_fake_rescue(rescue_root, f"20260101_{i:06d}_test")

    assert len(list(rescue_root.iterdir())) == 5

    with patch("supervisor.git_ops.DRIVE_ROOT", drive_root):
        from supervisor.git_ops import _cleanup_old_rescue_snapshots
        _cleanup_old_rescue_snapshots()

    assert len(list(rescue_root.iterdir())) == 5


def test_rescue_snapshot_cleanup_noop_when_no_dir(rescue_env):
    """Should not crash when rescue directory doesn't exist."""
    drive_root, rescue_root = rescue_env
    shutil.rmtree(rescue_root)

    with patch("supervisor.git_ops.DRIVE_ROOT", drive_root):
        from supervisor.git_ops import _cleanup_old_rescue_snapshots
        _cleanup_old_rescue_snapshots()  # should not raise


def test_rescue_snapshot_keeps_newest(rescue_env):
    """Should keep the newest snapshots and remove the oldest."""
    drive_root, rescue_root = rescue_env

    names = [f"2026010{i}_{j:06d}_test" for i in range(1, 4) for j in range(8)]
    for name in names:
        _create_fake_rescue(rescue_root, name)

    assert len(list(rescue_root.iterdir())) == 24

    with patch("supervisor.git_ops.DRIVE_ROOT", drive_root):
        from supervisor.git_ops import _cleanup_old_rescue_snapshots, MAX_RESCUE_SNAPSHOTS
        _cleanup_old_rescue_snapshots()

    remaining = sorted(d.name for d in rescue_root.iterdir())
    assert len(remaining) == MAX_RESCUE_SNAPSHOTS
    # Newest should survive (sorted by name = by timestamp)
    all_sorted = sorted(names)
    expected_survivors = all_sorted[-MAX_RESCUE_SNAPSHOTS:]
    assert remaining == expected_survivors
