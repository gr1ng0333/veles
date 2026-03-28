"""Tests for TikTok tools (tiktok_search, tiktok_metadata, tiktok_profile, tiktok_history)."""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from ouroboros.tools.tiktok import (
    get_tools,
    _tiktok_history,
    _tiktok_search,
    _tiktok_metadata,
    _tiktok_profile,
    _parse_dump_single,
)
from ouroboros.tools.registry import ToolContext


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(
        repo_dir=pathlib.Path("/opt/veles"),
        drive_root=tmp_path,
    )


def test_tool_registration():
    tools = get_tools()
    assert len(tools) == 4
    names = {t.name for t in tools}
    assert names == {"tiktok_search", "tiktok_metadata", "tiktok_profile", "tiktok_history"}


def test_parse_dump_single_basic():
    raw = '{"id": "123", "title": "test"}'
    result = _parse_dump_single(raw)
    assert result == {"id": "123", "title": "test"}


def test_parse_dump_single_multiline():
    raw = "some noise\n{\"id\": \"abc\"}\n"
    result = _parse_dump_single(raw)
    assert result["id"] == "abc"


def test_parse_dump_single_empty():
    assert _parse_dump_single("") is None
    assert _parse_dump_single("not json") is None


def test_history_list_empty(ctx):
    result = json.loads(_tiktok_history(ctx, action="list"))
    assert result["status"] == "ok"
    assert result["count"] == 0
    assert result["urls"] == []


def test_history_add_and_check(ctx):
    url = "https://www.tiktok.com/@user/video/123"
    result = json.loads(_tiktok_history(ctx, action="add", url=url, title="My video"))
    assert result["status"] == "ok"
    assert result["already_present"] is False
    assert result["total"] == 1

    check = json.loads(_tiktok_history(ctx, action="check", url=url))
    assert check["in_history"] is True


def test_history_check_absent(ctx):
    result = json.loads(_tiktok_history(ctx, action="check", url="https://www.tiktok.com/@other/video/999"))
    assert result["in_history"] is False


def test_history_dedup(ctx):
    url = "https://www.tiktok.com/@user/video/123"
    _tiktok_history(ctx, action="add", url=url)
    result = json.loads(_tiktok_history(ctx, action="add", url=url))
    assert result["already_present"] is True
    assert result["total"] == 1


def test_history_list_populated(ctx):
    _tiktok_history(ctx, action="add", url="https://www.tiktok.com/@u/video/1", title="V1")
    _tiktok_history(ctx, action="add", url="https://www.tiktok.com/@u/video/2", title="V2")
    result = json.loads(_tiktok_history(ctx, action="list"))
    assert result["count"] == 2
    assert len(result["entries"]) == 2


def test_history_clear(ctx):
    _tiktok_history(ctx, action="add", url="https://www.tiktok.com/@u/video/1")
    result = json.loads(_tiktok_history(ctx, action="clear"))
    assert result["cleared"] == 1
    after = json.loads(_tiktok_history(ctx, action="list"))
    assert after["count"] == 0


def test_history_invalid_action(ctx):
    result = json.loads(_tiktok_history(ctx, action="bogus"))
    assert result["status"] == "failed"


def test_history_add_missing_url(ctx):
    result = json.loads(_tiktok_history(ctx, action="add", url=""))
    assert result["status"] == "failed"


def test_search_empty_query(ctx):
    result = json.loads(_tiktok_search(ctx, query=""))
    assert result["status"] == "failed"


def test_metadata_empty_url(ctx):
    result = json.loads(_tiktok_metadata(ctx, url=""))
    assert result["status"] == "failed"


def test_profile_empty_username(ctx):
    result = json.loads(_tiktok_profile(ctx, username=""))
    assert result["status"] == "failed"


def test_history_schemas_valid():
    """All 4 tool schemas must have name, description, parameters."""
    for tool in get_tools():
        s = tool.schema
        assert "name" in s, f"{tool.name}: missing 'name' in schema"
        assert "description" in s, f"{tool.name}: missing 'description'"
        assert "parameters" in s, f"{tool.name}: missing 'parameters'"
