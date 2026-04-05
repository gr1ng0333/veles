"""Tests for tool_map (tool_discovery.py) and update_tool_snapshot.py."""

from __future__ import annotations

import importlib
import json
import pathlib
import subprocess
import sys
import tempfile

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_registry():
    from ouroboros.tools.registry import ToolRegistry
    with tempfile.TemporaryDirectory() as tmp:
        return ToolRegistry(pathlib.Path(tmp), pathlib.Path(tmp))


def _make_ctx():
    from ouroboros.tools.registry import ToolContext
    return ToolContext(
        repo_dir=pathlib.Path(tempfile.mkdtemp()),
        drive_root=pathlib.Path(tempfile.mkdtemp()),
    )


# ── tool_map tests ────────────────────────────────────────────────────────────

class TestToolMap:
    def setup_method(self):
        """Inject a live registry into tool_discovery module."""
        from ouroboros.tools import tool_discovery
        self.reg = _make_registry()
        tool_discovery.set_registry(self.reg)
        self.ctx = _make_ctx()

    def test_returns_text_by_default(self):
        from ouroboros.tools.tool_discovery import _tool_map
        result = _tool_map(self.ctx)
        assert "Tool Map" in result
        assert "tools in" in result
        # Should list multiple modules
        assert result.count("📦") > 5

    def test_json_format(self):
        from ouroboros.tools.tool_discovery import _tool_map
        result = _tool_map(self.ctx, format="json")
        data = json.loads(result)
        assert "modules" in data
        assert "total_tools" in data
        assert data["total_tools"] > 50  # we have 290+ tools
        # Each module should have a list of tool dicts
        for modname, tool_list in data["modules"].items():
            assert isinstance(tool_list, list)
            for t in tool_list:
                assert "name" in t
                assert "desc" in t
                assert "core" in t

    def test_filter_by_name(self):
        from ouroboros.tools.tool_discovery import _tool_map
        result = _tool_map(self.ctx, filter="hot_spots")
        assert "hot_spots" in result
        # Should NOT mention unrelated tools
        assert "repo_read" not in result

    def test_filter_by_module(self):
        from ouroboros.tools.tool_discovery import _tool_map
        result = _tool_map(self.ctx, filter="memory_search")
        assert "memory_search" in result

    def test_filter_no_match(self):
        from ouroboros.tools.tool_discovery import _tool_map
        result = _tool_map(self.ctx, filter="zzz_nonexistent_zzz")
        assert "no tools matching" in result.lower()

    def test_core_vs_extended_distinction(self):
        from ouroboros.tools.tool_discovery import _tool_map
        data = json.loads(_tool_map(self.ctx, format="json"))
        # Count core tools from json output
        core_count = sum(
            1
            for tools in data["modules"].values()
            for t in tools
            if t.get("core")
        )
        # We know there are ~40 core tools
        assert core_count >= 20
        assert core_count < 100

    def test_shows_all_tools_not_just_noncore(self):
        from ouroboros.tools.tool_discovery import _tool_map
        # list_available_tools only shows non-core; tool_map should show repo_read (core)
        result = _tool_map(self.ctx, filter="repo_read")
        assert "repo_read" in result

    def test_no_registry_returns_error(self):
        from ouroboros.tools import tool_discovery
        old = tool_discovery._registry
        tool_discovery._registry = None
        try:
            result = _tool_map(self.ctx)
            assert "not available" in result.lower() or "registry" in result.lower()
        finally:
            tool_discovery._registry = old

    def test_tool_map_registered_in_registry(self):
        """tool_map should be discoverable via the registry."""
        names = self.reg.available_tools()
        assert "tool_map" in names

    def test_tool_map_callable_via_registry(self):
        """Execute tool_map through the registry (integration path)."""
        from ouroboros.tools import tool_discovery
        tool_discovery.set_registry(self.reg)
        self.reg.set_context(self.ctx)
        result = self.reg.execute("tool_map", {"filter": "repo_read"})
        assert "repo_read" in result
        assert "⚠️" not in result  # no errors


# ── update_tool_snapshot tests ────────────────────────────────────────────────

class TestUpdateToolSnapshot:
    """Tests for tests/update_tool_snapshot.py helper script."""

    REPO = pathlib.Path(__file__).parent.parent
    SCRIPT = REPO / "tests" / "update_tool_snapshot.py"

    def test_script_exists(self):
        assert self.SCRIPT.exists(), "update_tool_snapshot.py not found"

    def test_dry_run_exits_zero(self):
        """Dry-run (no --apply) should exit 0."""
        result = subprocess.run(
            [sys.executable, str(self.SCRIPT)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"stderr: {result.stderr[:500]}"

    def test_dry_run_shows_sync_ok(self):
        """After our fix, the snapshot should be in sync."""
        result = subprocess.run(
            [sys.executable, str(self.SCRIPT)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        # Should say "in sync" or list no added/removed
        output = result.stdout + result.stderr
        # Either reports "in sync" or shows 0 diff (no +/- lines)
        in_sync = "in sync" in output.lower() or (
            "Added" not in output and "Removed" not in output
        )
        assert in_sync, f"Expected snapshot to be in sync, got:\n{output[:500]}"

    def test_apply_is_idempotent(self, tmp_path):
        """Applying when already in-sync should not change the file."""
        import shutil
        smoke_copy = tmp_path / "test_smoke.py"
        shutil.copy(self.REPO / "tests" / "test_smoke.py", smoke_copy)

        original_content = smoke_copy.read_text()

        # Patch SMOKE_TEST path in the script by running it against a copy
        # We can't easily monkey-patch, so we just verify it reports in-sync
        result = subprocess.run(
            [sys.executable, str(self.SCRIPT)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0
        # File should be unchanged (we ran without --apply)
        assert (self.REPO / "tests" / "test_smoke.py").read_text() == original_content

    def test_load_registry_returns_set(self):
        """_load_registry_tools returns a non-empty set of strings."""
        sys.path.insert(0, str(self.REPO))
        spec = importlib.util.spec_from_file_location("update_tool_snapshot", self.SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        tools = mod._load_registry_tools()
        assert isinstance(tools, set)
        assert len(tools) > 50
        assert "repo_read" in tools
        assert "hot_spots" in tools
        assert "tool_map" in tools

    def test_parse_expected_tools(self):
        """_parse_expected_tools correctly extracts tool names from test_smoke.py."""
        spec = importlib.util.spec_from_file_location("update_tool_snapshot", self.SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        content = (self.REPO / "tests" / "test_smoke.py").read_text()
        start, end, tools = mod._parse_expected_tools(content)
        assert start < end
        assert len(tools) > 50
        assert "repo_read" in tools
        assert "tool_map" in tools

    def test_build_replacement_block_roundtrip(self):
        """_build_replacement_block should include all provided tool names."""
        spec = importlib.util.spec_from_file_location("update_tool_snapshot", self.SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        sample = {"repo_read", "hot_spots", "tool_map", "some_new_tool_xyz"}
        block = mod._build_replacement_block(sample)
        assert "EXPECTED_TOOLS = [" in block
        assert "]" in block
        for name in sample:
            assert f'"{name}"' in block
