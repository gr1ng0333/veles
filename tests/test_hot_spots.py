"""Tests for hot_spots tool."""
from __future__ import annotations

import json
import math
import pathlib
import subprocess
import tempfile
import textwrap
from unittest.mock import patch, MagicMock

import pytest

from ouroboros.tools.hot_spots import (
    _git_churn,
    _scan_complexity,
    _parse_pattern_register,
    _compute_hot_spots,
    _normalize,
    _format_text,
    _hot_spots,
    get_tools,
)
from ouroboros.tools.registry import ToolEntry, ToolContext


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_ctx(repo_dir: pathlib.Path, drive_root: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=repo_dir, drive_root=drive_root)


def _write(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


# ── Signal 1: git churn ───────────────────────────────────────────────────────


def test_git_churn_parses_output() -> None:
    """_git_churn should count commit touches per Python file."""
    fake_output = (
        "\nouroboros/loop_runtime.py\nouterboros/context.py\n\n"
        "ouroboros/loop_runtime.py\nREADME.md\nVERSION\n"
    )
    with patch("ouroboros.tools.hot_spots.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout=fake_output, returncode=0)
        churn = _git_churn(pathlib.Path("/tmp/repo"), days=7)

    # loop_runtime appears twice → count 2
    assert churn.get("ouroboros/loop_runtime.py") == 2
    # context appears once → count 1
    assert churn.get("outerboros/context.py") == 1
    # README.md / VERSION are skipped (release tails)
    assert "README.md" not in churn
    assert "VERSION" not in churn


def test_git_churn_skips_non_python() -> None:
    """Non-.py / non-.md / non-.toml files are ignored."""
    fake_output = "ouroboros/foo.so\nouroboros/bar.png\nouroboros/real.py\n"
    with patch("ouroboros.tools.hot_spots.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout=fake_output, returncode=0)
        churn = _git_churn(pathlib.Path("/tmp/repo"), days=7)
    assert "ouroboros/foo.so" not in churn
    assert "ouroboros/bar.png" not in churn
    assert "ouroboros/real.py" in churn


def test_git_churn_handles_timeout() -> None:
    """TimeoutExpired → returns empty dict, no exception."""
    with patch("ouroboros.tools.hot_spots.subprocess.run",
               side_effect=subprocess.TimeoutExpired(["git"], 15)):
        churn = _git_churn(pathlib.Path("/tmp/repo"), days=7)
    assert churn == {}


# ── Signal 2: complexity ──────────────────────────────────────────────────────


def test_scan_complexity_detects_oversized_module(tmp_path: pathlib.Path) -> None:
    """Module with >1000 lines should be flagged."""
    ouro = tmp_path / "ouroboros"
    ouro.mkdir()
    big_file = ouro / "big_module.py"
    big_file.write_text("x = 1\n" * 1100)
    result = _scan_complexity(tmp_path)
    oversized_paths = [p for p, _ in result["oversized_modules"]]
    assert "ouroboros/big_module.py" in oversized_paths


def test_scan_complexity_detects_normal_module(tmp_path: pathlib.Path) -> None:
    """Module with <1000 lines should NOT be in oversized_modules."""
    ouro = tmp_path / "ouroboros"
    ouro.mkdir()
    small_file = ouro / "small_module.py"
    small_file.write_text("x = 1\n" * 200)
    result = _scan_complexity(tmp_path)
    oversized_paths = [p for p, _ in result["oversized_modules"]]
    assert "ouroboros/small_module.py" not in oversized_paths
    # But it should appear in module_lines
    assert "ouroboros/small_module.py" in result["module_lines"]


def test_scan_complexity_detects_long_function(tmp_path: pathlib.Path) -> None:
    """Function longer than 150 lines should be in oversized_functions."""
    ouro = tmp_path / "ouroboros"
    ouro.mkdir()
    func_body = "def my_long_func():\n" + "    pass\n" * 160
    (ouro / "funcy.py").write_text(func_body)
    result = _scan_complexity(tmp_path)
    func_names = [fn for _, fn, _, _ in result["oversized_functions"]]
    assert "my_long_func" in func_names


def test_scan_complexity_empty_dir(tmp_path: pathlib.Path) -> None:
    """Empty scan dir → empty results, no exception."""
    result = _scan_complexity(tmp_path)
    assert result["oversized_modules"] == []
    assert result["oversized_functions"] == []
    assert result["module_lines"] == {}


# ── Signal 3: pattern register ────────────────────────────────────────────────


def test_parse_pattern_register(tmp_path: pathlib.Path) -> None:
    """Should parse markdown table rows and sort by count desc."""
    kb_dir = tmp_path / "memory" / "knowledge"
    kb_dir.mkdir(parents=True)
    _write(
        kb_dir / "patterns.md",
        """\
        # Pattern Register

        | Class | Count | Evidence | Root cause | Fix |
        |---|---:|---|---|---|
        | tool timeout | 198+ | foo | bad timeout | increase cap |
        | wrong file path | 45+ | bar | path mismatch | verify paths |
        | Copilot 500 | 5+ | baz | brittle retry | better recovery |
        """,
    )
    patterns = _parse_pattern_register(tmp_path)
    assert len(patterns) == 3
    # Sorted by count descending
    assert patterns[0]["count"] == 198
    assert patterns[1]["count"] == 45
    assert patterns[2]["count"] == 5
    assert patterns[0]["class"] == "tool timeout"


def test_parse_pattern_register_missing(tmp_path: pathlib.Path) -> None:
    """Missing patterns.md → empty list, no exception."""
    result = _parse_pattern_register(tmp_path)
    assert result == []


# ── Scoring ───────────────────────────────────────────────────────────────────


def test_normalize_basic() -> None:
    values = [0.0, 5.0, 10.0]
    normed = _normalize(values)
    assert normed[0] == pytest.approx(0.0)
    assert normed[2] == pytest.approx(1.0)
    assert normed[1] == pytest.approx(0.5)


def test_normalize_uniform() -> None:
    """All-equal values → 0.5 for each."""
    normed = _normalize([3.0, 3.0, 3.0])
    assert all(v == pytest.approx(0.5) for v in normed)


def test_compute_hot_spots_basic() -> None:
    """Files with high churn + many lines should rank at top."""
    churn = {
        "ouroboros/loop_runtime.py": 13,
        "ouroboros/context.py": 7,
        "ouroboros/tools/small.py": 1,
    }
    complexity = {
        "oversized_modules": [
            ("ouroboros/loop_runtime.py", 1200),
            ("ouroboros/context.py", 800),
        ],
        "oversized_functions": [],
        "module_lines": {
            "ouroboros/loop_runtime.py": 1200,
            "ouroboros/context.py": 800,
            "ouroboros/tools/small.py": 50,
        },
    }
    spots = _compute_hot_spots(churn, complexity, top_k=3)
    assert len(spots) <= 3
    # loop_runtime should rank first (highest churn + largest)
    assert spots[0]["path"] == "ouroboros/loop_runtime.py"
    # Scores are in [0, 1]
    for s in spots:
        assert 0.0 <= s["hot_score"] <= 1.0


def test_compute_hot_spots_empty() -> None:
    """Empty churn + complexity → empty result, no exception."""
    result = _compute_hot_spots({}, {"oversized_modules": [], "oversized_functions": [], "module_lines": {}})
    assert result == []


# ── Output formats ────────────────────────────────────────────────────────────


def test_format_text_contains_score() -> None:
    spots = [{"path": "foo.py", "hot_score": 0.88, "churn": 5, "lines": 300,
              "oversized_functions": []}]
    patterns = [{"class": "tool timeout", "count": 198, "root_cause": "bad timeout"}]
    text = _format_text(spots, patterns, days=7)
    assert "foo.py" in text
    assert "0.88" in text
    assert "tool timeout" in text


def test_hot_spots_json_format(tmp_path: pathlib.Path) -> None:
    """JSON format should parse without error and contain required keys."""
    ctx = _make_ctx(tmp_path, tmp_path)

    with (
        patch("ouroboros.tools.hot_spots._REPO_DIR", str(tmp_path)),
        patch("ouroboros.tools.hot_spots._DRIVE_ROOT", str(tmp_path)),
        patch("ouroboros.tools.hot_spots._git_churn", return_value={}),
    ):
        result = _hot_spots(ctx, days=7, top_k=5, format="json")

    data = json.loads(result)
    assert "hot_spots" in data
    assert "patterns" in data
    assert "meta" in data


# ── Registry ──────────────────────────────────────────────────────────────────


def test_get_tools_returns_entry() -> None:
    tools = get_tools()
    assert len(tools) == 1
    assert isinstance(tools[0], ToolEntry)
    assert tools[0].name == "hot_spots"
    schema = tools[0].schema
    assert schema["name"] == "hot_spots"
    # Schema has parameters
    assert "parameters" in schema
    props = schema["parameters"]["properties"]
    assert "days" in props
    assert "top_k" in props
    assert "format" in props
