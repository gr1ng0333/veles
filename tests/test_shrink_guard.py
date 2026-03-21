"""Tests for shrink guard in repo_write_commit tool."""

import pathlib
import tempfile

import pytest

from ouroboros.tools.git import (
    _check_shrink_guard,
    SHRINK_GUARD_THRESHOLD,
    SHRINK_GUARD_MIN_SIZE,
)


@pytest.fixture
def tmp_dir(tmp_path):
    return tmp_path


def _write_file(path: pathlib.Path, size: int, char: str = "x") -> None:
    """Create a file with exactly `size` bytes of ASCII content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((char * size).encode("utf-8"))


class TestShrinkGuard:

    def test_blocks_truncation(self, tmp_dir):
        """Should block write when new content < 30% of original."""
        f = tmp_dir / "big_file.py"
        _write_file(f, 1000)
        new_content = "x" * 100  # 10% of original
        result = _check_shrink_guard(f, new_content)
        assert result is not None
        assert "SHRINK GUARD" in result

    def test_allows_normal_write(self, tmp_dir):
        """Should allow write when new content >= 30% of original."""
        f = tmp_dir / "file.py"
        _write_file(f, 1000)
        new_content = "x" * 800  # 80% of original
        result = _check_shrink_guard(f, new_content)
        assert result is None

    def test_allows_new_file(self, tmp_dir):
        """Should allow creating new files without restriction."""
        f = tmp_dir / "new_file.py"
        assert not f.exists()
        result = _check_shrink_guard(f, "x" * 10)
        assert result is None

    def test_skips_small_files(self, tmp_dir):
        """Should skip guard for files < 100 bytes."""
        f = tmp_dir / "tiny.py"
        _write_file(f, 50)
        new_content = "x" * 5  # 10% but file is tiny
        result = _check_shrink_guard(f, new_content)
        assert result is None

    def test_skips_markdown(self, tmp_dir):
        """Should skip guard for .md files."""
        f = tmp_dir / "README.md"
        _write_file(f, 1000)
        new_content = "x" * 50  # 5% of original but it's markdown
        result = _check_shrink_guard(f, new_content)
        assert result is None

    def test_exact_threshold_boundary(self, tmp_dir):
        """Exactly 30% ratio should pass (not strictly less than)."""
        f = tmp_dir / "edge.py"
        _write_file(f, 1000)
        new_content = "x" * 300  # exactly 30%
        result = _check_shrink_guard(f, new_content)
        assert result is None

    def test_just_below_threshold(self, tmp_dir):
        """Just below 30% should be blocked."""
        f = tmp_dir / "edge2.py"
        _write_file(f, 1000)
        new_content = "x" * 299  # 29.9%
        result = _check_shrink_guard(f, new_content)
        assert result is not None
        assert "SHRINK GUARD" in result

    def test_empty_original_file(self, tmp_dir):
        """Empty original file should not trigger guard."""
        f = tmp_dir / "empty.py"
        f.write_text("")
        result = _check_shrink_guard(f, "new content")
        assert result is None
