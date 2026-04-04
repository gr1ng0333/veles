"""Tests for rss_reader — RSS/Atom feed subscription and new-item tracking."""

from __future__ import annotations

import json
import pathlib
import textwrap
from typing import Tuple
from unittest.mock import patch

import pytest

from ouroboros.tools.rss_reader import (
    _parse_feed_xml,
    _slug,
    _strip_html,
    _rss_subscribe,
    _rss_unsubscribe,
    _rss_status,
    _rss_check,
    get_tools,
)

# ── Fixtures ────────────────────────────────────────────────────────────────────

RSS_FEED_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Test Feed</title>
        <link>https://example.com</link>
        <item>
          <title>Item One</title>
          <link>https://example.com/1</link>
          <guid>guid-1</guid>
          <pubDate>Mon, 01 Jan 2024 10:00:00 +0000</pubDate>
          <description>First item text</description>
        </item>
        <item>
          <title>Item Two</title>
          <link>https://example.com/2</link>
          <guid>guid-2</guid>
          <pubDate>Tue, 02 Jan 2024 10:00:00 +0000</pubDate>
          <description>Second item text</description>
        </item>
      </channel>
    </rss>
""")

ATOM_FEED_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Atom Test Feed</title>
      <entry>
        <id>urn:atom:entry-1</id>
        <title>Atom Item One</title>
        <link href="https://example.com/a1"/>
        <published>2024-01-01T10:00:00Z</published>
        <summary>Atom first item</summary>
      </entry>
      <entry>
        <id>urn:atom:entry-2</id>
        <title>Atom Item Two</title>
        <link href="https://example.com/a2"/>
        <published>2024-01-02T10:00:00Z</published>
        <summary>Atom second item</summary>
      </entry>
    </feed>
""")

EMPTY_RSS_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Empty Feed</title>
      </channel>
    </rss>
""")


@pytest.fixture
def tmp_feeds(tmp_path, monkeypatch):
    """Redirect feed storage to a temp directory."""
    monkeypatch.setenv("DRIVE_ROOT", str(tmp_path))
    # Patch module-level _DRIVE_ROOT in rss_reader
    import ouroboros.tools.rss_reader as rss_mod
    monkeypatch.setattr(rss_mod, "_DRIVE_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def mock_ctx():
    """Minimal ToolContext mock."""
    class _Ctx:
        drive_root = pathlib.Path("/tmp")
        llm_client = None
    return _Ctx()


# ── Parsing tests ─────────────────────────────────────────────────────────────

def test_parse_rss_feed():
    title, items = _parse_feed_xml(RSS_FEED_XML)
    assert title == "Test Feed"
    assert len(items) == 2
    assert items[0]["guid"] == "guid-1"
    assert items[0]["title"] == "Item One"
    assert items[0]["link"] == "https://example.com/1"
    assert "2024" in items[0]["date"]


def test_parse_atom_feed():
    title, items = _parse_feed_xml(ATOM_FEED_XML)
    assert title == "Atom Test Feed"
    assert len(items) == 2
    assert items[0]["guid"] == "urn:atom:entry-1"
    assert items[0]["title"] == "Atom Item One"
    assert items[0]["link"] == "https://example.com/a1"
    assert "2024" in items[0]["date"]


def test_parse_empty_feed():
    title, items = _parse_feed_xml(EMPTY_RSS_XML)
    assert title == "Empty Feed"
    assert items == []


def test_parse_invalid_xml():
    with pytest.raises(ValueError, match="XML parse error"):
        _parse_feed_xml("<not valid xml")


# ── Slug + strip_html tests ────────────────────────────────────────────────────

def test_slug_basic():
    assert _slug("My Feed!") == "my_feed_"
    assert _slug("arxiv-ai") == "arxiv-ai"
    assert _slug("  spaces  ") == "spaces"


def test_strip_html_basic():
    result = _strip_html("<p>Hello <b>world</b></p>")
    assert "Hello" in result
    assert "world" in result
    assert "<" not in result


def test_strip_html_entities():
    result = _strip_html("AT&amp;T &lt;test&gt;")
    assert "AT&T" in result
    assert "<" not in result


# ── Subscribe tests ────────────────────────────────────────────────────────────

def test_subscribe_success(tmp_feeds, mock_ctx):
    with patch("ouroboros.tools.rss_reader._fetch_and_parse", return_value=("My Blog", [
        {"guid": "g1", "title": "Old Post", "link": "https://ex.com/1", "date": "", "summary": ""},
    ])):
        result = json.loads(_rss_subscribe(mock_ctx, url="https://ex.com/rss", name="myblog"))

    assert result["status"] == "subscribed"
    assert result["name"] == "myblog"
    assert result["items_found"] == 1
    # Old items should be marked as seen
    assert "myblog" in result["name"]


def test_subscribe_auto_name(tmp_feeds, mock_ctx):
    with patch("ouroboros.tools.rss_reader._fetch_and_parse", return_value=("ArXiv AI", [])):
        result = json.loads(_rss_subscribe(mock_ctx, url="https://arxiv.org/rss/cs.AI"))

    assert result["status"] == "subscribed"
    assert "arxiv" in result["name"]


def test_subscribe_duplicate_url(tmp_feeds, mock_ctx):
    with patch("ouroboros.tools.rss_reader._fetch_and_parse", return_value=("Feed", [])):
        _rss_subscribe(mock_ctx, url="https://ex.com/rss", name="feed1")

    with patch("ouroboros.tools.rss_reader._fetch_and_parse", return_value=("Feed", [])):
        result = json.loads(_rss_subscribe(mock_ctx, url="https://ex.com/rss", name="feed2"))

    assert result["status"] == "already_subscribed"
    assert result["name"] == "feed1"


def test_subscribe_name_conflict(tmp_feeds, mock_ctx):
    with patch("ouroboros.tools.rss_reader._fetch_and_parse", return_value=("Feed", [])):
        _rss_subscribe(mock_ctx, url="https://ex.com/rss1", name="myfeed")

    with patch("ouroboros.tools.rss_reader._fetch_and_parse", return_value=("Feed", [])):
        result = json.loads(_rss_subscribe(mock_ctx, url="https://ex.com/rss2", name="myfeed"))

    assert result["status"] == "name_conflict"


def test_subscribe_empty_url(tmp_feeds, mock_ctx):
    result = json.loads(_rss_subscribe(mock_ctx, url=""))
    assert "error" in result


# ── Status tests ───────────────────────────────────────────────────────────────

def test_status_empty(tmp_feeds, mock_ctx):
    result = json.loads(_rss_status(mock_ctx))
    assert result["count"] == 0
    assert result["subscriptions"] == []


def test_status_shows_subscribed(tmp_feeds, mock_ctx):
    with patch("ouroboros.tools.rss_reader._fetch_and_parse", return_value=("My Feed", [])):
        _rss_subscribe(mock_ctx, url="https://ex.com/rss", name="myfeed")

    result = json.loads(_rss_status(mock_ctx))
    assert result["count"] == 1
    assert result["subscriptions"][0]["name"] == "myfeed"
    assert result["subscriptions"][0]["title"] == "My Feed"


# ── Unsubscribe tests ──────────────────────────────────────────────────────────

def test_unsubscribe_success(tmp_feeds, mock_ctx):
    with patch("ouroboros.tools.rss_reader._fetch_and_parse", return_value=("Feed", [])):
        _rss_subscribe(mock_ctx, url="https://ex.com/rss", name="feed1")

    result = json.loads(_rss_unsubscribe(mock_ctx, name="feed1"))
    assert result["status"] == "unsubscribed"

    status = json.loads(_rss_status(mock_ctx))
    assert status["count"] == 0


def test_unsubscribe_not_found(tmp_feeds, mock_ctx):
    result = json.loads(_rss_unsubscribe(mock_ctx, name="nonexistent"))
    assert result["status"] == "not_found"


# ── Check tests ────────────────────────────────────────────────────────────────

def test_check_empty_feeds(tmp_feeds, mock_ctx):
    result = json.loads(_rss_check(mock_ctx))
    assert result["total_new"] == 0
    assert "message" in result


def test_check_no_new_items(tmp_feeds, mock_ctx):
    """If all items were seen at subscribe time, check returns 0 new."""
    initial_items = [
        {"guid": "g1", "title": "Post 1", "link": "https://ex.com/1", "date": "2024-01-01T00:00:00+00:00", "summary": ""},
    ]
    with patch("ouroboros.tools.rss_reader._fetch_and_parse", return_value=("Feed", initial_items)):
        _rss_subscribe(mock_ctx, url="https://ex.com/rss", name="myfeed")

    # Check: same items, no new guids
    with patch("ouroboros.tools.rss_reader._fetch_and_parse", return_value=("Feed", initial_items)):
        result = json.loads(_rss_check(mock_ctx, name="myfeed"))

    assert result["total_new"] == 0
    assert result["per_feed"]["myfeed"]["new_count"] == 0


def test_check_new_items_appear(tmp_feeds, mock_ctx):
    """New items added after subscribe should appear in check."""
    initial_items = [
        {"guid": "g1", "title": "Old Post", "link": "https://ex.com/1", "date": "2024-01-01T00:00:00+00:00", "summary": ""},
    ]
    with patch("ouroboros.tools.rss_reader._fetch_and_parse", return_value=("Feed", initial_items)):
        _rss_subscribe(mock_ctx, url="https://ex.com/rss", name="myfeed")

    # New item appears in feed
    updated_items = [
        {"guid": "g1", "title": "Old Post", "link": "https://ex.com/1", "date": "2024-01-01T00:00:00+00:00", "summary": ""},
        {"guid": "g2", "title": "New Post", "link": "https://ex.com/2", "date": "2024-01-02T00:00:00+00:00", "summary": "Fresh content"},
    ]
    with patch("ouroboros.tools.rss_reader._fetch_and_parse", return_value=("Feed", updated_items)):
        result = json.loads(_rss_check(mock_ctx, name="myfeed"))

    assert result["total_new"] == 1
    assert result["items"][0]["guid"] == "g2"
    assert result["items"][0]["title"] == "New Post"
    assert result["items"][0]["feed_name"] == "myfeed"


def test_check_watermark_advances(tmp_feeds, mock_ctx):
    """After check, new items become seen and don't reappear."""
    initial_items = [
        {"guid": "g1", "title": "Old Post", "link": "https://ex.com/1", "date": "2024-01-01T00:00:00+00:00", "summary": ""},
    ]
    with patch("ouroboros.tools.rss_reader._fetch_and_parse", return_value=("Feed", initial_items)):
        _rss_subscribe(mock_ctx, url="https://ex.com/rss", name="myfeed")

    updated_items = [
        {"guid": "g1", "title": "Old Post", "link": "https://ex.com/1", "date": "2024-01-01T00:00:00+00:00", "summary": ""},
        {"guid": "g2", "title": "New Post", "link": "https://ex.com/2", "date": "2024-01-02T00:00:00+00:00", "summary": ""},
    ]

    # First check — g2 is new
    with patch("ouroboros.tools.rss_reader._fetch_and_parse", return_value=("Feed", updated_items)):
        result1 = json.loads(_rss_check(mock_ctx, name="myfeed"))
    assert result1["total_new"] == 1

    # Second check — g2 now seen, no new items
    with patch("ouroboros.tools.rss_reader._fetch_and_parse", return_value=("Feed", updated_items)):
        result2 = json.loads(_rss_check(mock_ctx, name="myfeed"))
    assert result2["total_new"] == 0


def test_check_limit_per_feed(tmp_feeds, mock_ctx):
    """limit_per_feed is respected."""
    initial_items = []
    with patch("ouroboros.tools.rss_reader._fetch_and_parse", return_value=("Feed", initial_items)):
        _rss_subscribe(mock_ctx, url="https://ex.com/rss", name="myfeed")

    # 10 new items appear
    new_items = [
        {"guid": f"g{i}", "title": f"Post {i}", "link": f"https://ex.com/{i}", "date": f"2024-01-{i:02d}T00:00:00+00:00", "summary": ""}
        for i in range(1, 11)
    ]
    with patch("ouroboros.tools.rss_reader._fetch_and_parse", return_value=("Feed", new_items)):
        result = json.loads(_rss_check(mock_ctx, name="myfeed", limit_per_feed=3))

    assert len(result["items"]) == 3


def test_check_unknown_name(tmp_feeds, mock_ctx):
    result = json.loads(_rss_check(mock_ctx, name="nonexistent"))
    assert "error" in result


def test_check_fetch_error_graceful(tmp_feeds, mock_ctx):
    """Fetch error is captured in per_feed summary, not raised."""
    import urllib.error
    with patch("ouroboros.tools.rss_reader._fetch_and_parse", return_value=("Feed", [])):
        _rss_subscribe(mock_ctx, url="https://ex.com/rss", name="myfeed")

    with patch("ouroboros.tools.rss_reader._fetch_and_parse", side_effect=urllib.error.URLError("Network unreachable")):
        result = json.loads(_rss_check(mock_ctx, name="myfeed"))

    assert result["total_new"] == 0
    assert result["per_feed"]["myfeed"]["error"] is not None


# ── Tool registration ──────────────────────────────────────────────────────────

def test_get_tools_returns_all_four():
    tools = get_tools()
    names = {t.name for t in tools}
    assert names == {"rss_subscribe", "rss_unsubscribe", "rss_status", "rss_check"}


def test_tool_schemas_valid():
    for tool in get_tools():
        s = tool.schema
        assert s["name"] == tool.name
        assert "description" in s
        assert "parameters" in s
        assert s["parameters"]["type"] == "object"
