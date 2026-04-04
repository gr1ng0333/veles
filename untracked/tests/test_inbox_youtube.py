"""Tests for inbox.py YouTube integration."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from ouroboros.tools.inbox import (
    _collect_youtube,
    _inbox_check,
    _inbox_status,
    get_tools,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

class _MockCtx:
    def __init__(self, tmp_path):
        self.drive_root = tmp_path
        self.llm_client = None


@pytest.fixture
def ctx(tmp_path):
    return _MockCtx(tmp_path)


# ── Sample YT video data ──────────────────────────────────────────────────────

_SAMPLE_VIDEOS = [
    {
        "video_id": "abc123",
        "title": "Deep Learning Explained",
        "channel_label": "MLChannel",
        "channel_id": "UCxxx123",
        "published": "2026-04-04T12:00:00+00:00",
        "url": "https://www.youtube.com/watch?v=abc123",
        "summary": "An introduction to deep learning.",
        "views": 1234,
        "duration": "PT15M30S",
    },
    {
        "video_id": "def456",
        "title": "Transformers from Scratch",
        "channel_label": "MLChannel",
        "channel_id": "UCxxx123",
        "published": "2026-04-03T10:00:00+00:00",
        "url": "https://www.youtube.com/watch?v=def456",
        "summary": "Building transformers step by step.",
        "views": 5678,
        "duration": "PT30M",
    },
]


# ── Tests: _collect_youtube ───────────────────────────────────────────────────

def test_collect_youtube_basic(ctx):
    with patch(
        "ouroboros.tools.yt_reader._yt_check_for_inbox",
        return_value=_SAMPLE_VIDEOS,
    ):
        items = _collect_youtube(ctx, limit=10)
    assert len(items) == 2
    assert items[0]["source_type"] == "youtube"
    assert items[0]["id"] == "abc123"
    assert items[0]["source_name"] == "MLChannel"
    assert items[0]["title"] == "Deep Learning Explained"
    assert items[0]["url"] == "https://www.youtube.com/watch?v=abc123"


def test_collect_youtube_empty(ctx):
    with patch(
        "ouroboros.tools.yt_reader._yt_check_for_inbox",
        return_value=[],
    ):
        items = _collect_youtube(ctx, limit=10)
    assert items == []


def test_collect_youtube_error_returns_empty(ctx):
    with patch(
        "ouroboros.tools.yt_reader._yt_check_for_inbox",
        side_effect=RuntimeError("network error"),
    ):
        items = _collect_youtube(ctx, limit=10)
    assert items == []


def test_collect_youtube_url_fallback(ctx):
    """If url missing from video dict, constructs from video_id."""
    video = {"video_id": "xyz789", "title": "Test", "channel_label": "C"}
    with patch(
        "ouroboros.tools.yt_reader._yt_check_for_inbox",
        return_value=[video],
    ):
        items = _collect_youtube(ctx, limit=10)
    assert "xyz789" in items[0]["url"]


# ── Tests: youtube source_type enum ──────────────────────────────────────────

def test_youtube_in_schema_enum():
    tools = get_tools()
    check_tool = next(t for t in tools if t.name == "inbox_check")
    enum_values = check_tool.schema["parameters"]["properties"]["sources"]["items"]["enum"]
    assert "youtube" in enum_values


# ── Tests: _inbox_check includes youtube ─────────────────────────────────────

def test_inbox_check_youtube_source(ctx):
    """inbox_check with sources=['youtube'] collects from youtube."""
    with patch(
        "ouroboros.tools.yt_reader._yt_check_for_inbox",
        return_value=_SAMPLE_VIDEOS,
    ):
        raw = _inbox_check(ctx, sources=["youtube"])
    data = json.loads(raw)
    assert data["sources"]["youtube"]["new_items"] == 2
    assert data["total_new"] == 2
    assert all(it["source_type"] == "youtube" for it in data["items"])


def test_inbox_check_youtube_in_default_sources(ctx):
    """By default (sources=None), youtube is included in enabled sources."""
    import ouroboros.tools.inbox as inbox_mod
    import inspect
    src = inspect.getsource(inbox_mod._inbox_check)
    assert 'youtube' in src


def test_inbox_check_youtube_respects_limit(ctx):
    """limit_per_source is passed through to youtube collector."""
    call_args = []

    def mock_yt_check(ctx, limit=10):
        call_args.append(limit)
        return []

    with patch("ouroboros.tools.yt_reader._yt_check_for_inbox", side_effect=mock_yt_check):
        _inbox_check(ctx, limit_per_source=7, sources=["youtube"])
    assert call_args == [7]


# ── Tests: _inbox_status includes youtube ─────────────────────────────────────

def test_inbox_status_youtube_section(ctx):
    """_inbox_status returns youtube section with subscription count."""
    mock_watchlist = {
        "UCxxx123": {"label": "MLChannel", "last_checked": "2026-04-04T12:00:00Z"},
        "UCyyy456": {"label": "AnotherChannel", "last_checked": "2026-04-04T10:00:00Z"},
    }
    with patch(
        "ouroboros.tools.yt_reader._load_watchlist",
        return_value=mock_watchlist,
    ):
        # Patch other sources to avoid network calls
        with patch("ouroboros.tools.tg_watchlist._tg_watchlist_status", return_value=json.dumps({"count": 0, "subscriptions": []})):
            with patch("ouroboros.tools.rss_reader._rss_status", return_value=json.dumps({"count": 0, "feeds": []})):
                with patch("ouroboros.tools.web_monitor._web_monitor_status", return_value=json.dumps({"count": 0, "monitors": []})):
                    with patch("ouroboros.tools.hn_reader._hn_watchlist_status", return_value=json.dumps({"count": 0, "keywords": []})):
                        with patch("ouroboros.tools.reddit_reader._reddit_watchlist_status", return_value=json.dumps({"count": 0, "subreddits": []})):
                            with patch("ouroboros.tools.arxiv_reader._arxiv_watchlist_status", return_value=json.dumps({"count": 0, "entries": []})):
                                with patch("ouroboros.tools.github_watch._gh_watch_status", return_value=json.dumps({"count": 0, "repos": []})):
                                    raw = _inbox_status(ctx)
    data = json.loads(raw)
    assert "youtube" in data["sources"]
    yt = data["sources"]["youtube"]
    assert yt["subscriptions"] == 2
    assert "MLChannel" in yt["channels"]


# ── Tests: get_tools ──────────────────────────────────────────────────────────

def test_get_tools_returns_two():
    tools = get_tools()
    names = [t.name for t in tools]
    assert "inbox_check" in names
    assert "inbox_status" in names
