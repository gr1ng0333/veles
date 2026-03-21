"""Tests for per-tool timeout configuration."""

from ouroboros.tools.registry import TOOL_TIMEOUT_OVERRIDES


def _get_tool_timeout(name: str) -> int:
    """Simulate registry.get_timeout() logic for testing overrides."""
    return TOOL_TIMEOUT_OVERRIDES.get(name, 120)


def test_known_tools_have_timeouts():
    """All commonly used tools should have explicit timeouts."""
    for tool in ["repo_read", "run_shell", "browse_page", "web_search"]:
        assert _get_tool_timeout(tool) > 0


def test_default_timeout():
    """Unknown tools should get default timeout (120 from ToolEntry)."""
    assert _get_tool_timeout("nonexistent_tool") == 120


def test_browse_page_longer_than_repo_read():
    """Browser tools should have longer timeout than read tools."""
    assert _get_tool_timeout("browse_page") > _get_tool_timeout("repo_read")


def test_fast_tools_are_15s():
    """Read-only tools should have 15s timeout."""
    fast = ["repo_read", "repo_list", "drive_read", "drive_list",
            "knowledge_read", "knowledge_list", "git_status", "git_diff",
            "chat_history"]
    for tool in fast:
        assert TOOL_TIMEOUT_OVERRIDES[tool] == 15, f"{tool} should be 15s"


def test_medium_tools_are_30s():
    """Write/search tools should have 30s timeout."""
    medium = ["repo_write_commit", "repo_commit_push", "run_shell",
              "web_search", "update_scratchpad"]
    for tool in medium:
        assert TOOL_TIMEOUT_OVERRIDES[tool] == 30, f"{tool} should be 30s"


def test_slow_tools_are_60s():
    """Browser tools should have 60s timeout."""
    slow = ["browse_page", "browser_action", "analyze_screenshot"]
    for tool in slow:
        assert TOOL_TIMEOUT_OVERRIDES[tool] == 60, f"{tool} should be 60s"


def test_override_dict_no_negative_values():
    """All timeouts must be positive."""
    for name, timeout in TOOL_TIMEOUT_OVERRIDES.items():
        assert timeout > 0, f"{name} has non-positive timeout: {timeout}"
