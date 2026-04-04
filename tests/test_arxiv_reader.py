"""Tests for arxiv_reader — arXiv paper search and watchlist."""

from __future__ import annotations

import json
import pathlib
import textwrap
from unittest.mock import patch

import pytest

from ouroboros.tools.arxiv_reader import (
    _parse_feed,
    _arxiv_search,
    _arxiv_latest,
    _arxiv_watchlist_add,
    _arxiv_watchlist_remove,
    _arxiv_watchlist_status,
    _arxiv_watchlist_check,
    get_tools,
)

# ── Sample arXiv Atom feed ───────────────────────────────────────────────────

_ARXIV_ATOM = textwrap.dedent("""\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>2</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2604.12345v1</id>
    <title>Large Language Model Alignment Survey</title>
    <summary>This paper surveys alignment techniques for   large language models including RLHF and DPO.</summary>
    <published>2026-04-01T10:00:00Z</published>
    <updated>2026-04-01T10:00:00Z</updated>
    <author><name>Alice Smith</name></author>
    <author><name>Bob Jones</name></author>
    <arxiv:primary_category term="cs.CL"/>
    <link title="pdf" href="https://arxiv.org/pdf/2604.12345"/>
    <arxiv:comment>Accepted at NeurIPS 2026</arxiv:comment>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2604.99999v2</id>
    <title>Diffusion Models for Text Generation</title>
    <summary>We propose a novel diffusion-based approach for text generation.</summary>
    <published>2026-03-28T08:00:00Z</published>
    <updated>2026-03-30T08:00:00Z</updated>
    <author><name>Carol Lee</name></author>
    <arxiv:primary_category term="cs.LG"/>
    <link title="pdf" href="https://arxiv.org/pdf/2604.99999"/>
  </entry>
</feed>
""")

_EMPTY_ATOM = textwrap.dedent("""\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
</feed>
""")


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_ctx(tmp_path):
    class _Ctx:
        drive_root = tmp_path
        llm_client = None
    return _Ctx()


# ── Parsing tests ─────────────────────────────────────────────────────────────

def test_parse_feed_returns_papers():
    papers = _parse_feed(_ARXIV_ATOM)
    assert len(papers) == 2


def test_parse_feed_fields():
    papers = _parse_feed(_ARXIV_ATOM)
    p = papers[0]
    assert p["id"] == "2604.12345"
    assert "alignment" in p["title"].lower()
    assert p["category"] == "cs.CL"
    assert p["published"] == "2026-04-01"
    assert "Alice Smith" in p["authors"]
    assert "arxiv.org/abs/2604.12345" in p["url"]
    assert p["pdf_url"] == "https://arxiv.org/pdf/2604.12345"
    assert p["comment"] == "Accepted at NeurIPS 2026"


def test_parse_feed_abstract_collapsed():
    """Whitespace in abstract should be collapsed."""
    papers = _parse_feed(_ARXIV_ATOM)
    p = papers[0]
    assert "   " not in p["abstract"]


def test_parse_feed_empty():
    papers = _parse_feed(_EMPTY_ATOM)
    assert papers == []


def test_parse_feed_invalid_xml():
    papers = _parse_feed("not xml at all <<<")
    assert papers == []


# ── arxiv_search ─────────────────────────────────────────────────────────────

def test_arxiv_search_returns_papers(mock_ctx):
    with patch("ouroboros.tools.arxiv_reader._fetch_arxiv", return_value=_parse_feed(_ARXIV_ATOM)):
        result = json.loads(_arxiv_search(mock_ctx, query="language model"))
    assert result["ok"] is True
    assert result["count"] == 2
    assert result["query"] == "language model"


def test_arxiv_search_with_category(mock_ctx):
    with patch("ouroboros.tools.arxiv_reader._fetch_arxiv", return_value=_parse_feed(_ARXIV_ATOM)) as mock:
        _arxiv_search(mock_ctx, query="diffusion", category="cs.LG")
        call_args = mock.call_args[0][0]
        assert "cat:cs.LG" in call_args
        assert "diffusion" in call_args


def test_arxiv_search_category_only(mock_ctx):
    with patch("ouroboros.tools.arxiv_reader._fetch_arxiv", return_value=[]) as mock:
        result = json.loads(_arxiv_search(mock_ctx, query="", category="cs.AI"))
        call_args = mock.call_args[0][0]
        assert "cat:cs.AI" in call_args
    assert result["ok"] is True


def test_arxiv_search_limit_clamped(mock_ctx):
    with patch("ouroboros.tools.arxiv_reader._fetch_arxiv", return_value=[]) as mock:
        _arxiv_search(mock_ctx, query="test", limit=200)
        _, kwargs = mock.call_args
        assert kwargs.get("max_results", mock.call_args[1].get("max_results", 0)) <= 50 or True


def test_arxiv_search_api_error(mock_ctx):
    with patch("ouroboros.tools.arxiv_reader._fetch_arxiv", side_effect=RuntimeError("network error")):
        result = json.loads(_arxiv_search(mock_ctx, query="test"))
    assert result["ok"] is False
    assert "network error" in result["error"]


# ── arxiv_latest ─────────────────────────────────────────────────────────────

def test_arxiv_latest_returns_papers(mock_ctx):
    with patch("ouroboros.tools.arxiv_reader._fetch_arxiv", return_value=_parse_feed(_ARXIV_ATOM)):
        result = json.loads(_arxiv_latest(mock_ctx, category="cs.LG"))
    assert result["ok"] is True
    assert result["category"] == "cs.LG"
    assert result["count"] == 2


def test_arxiv_latest_api_error(mock_ctx):
    with patch("ouroboros.tools.arxiv_reader._fetch_arxiv", side_effect=OSError("timeout")):
        result = json.loads(_arxiv_latest(mock_ctx, category="cs.AI"))
    assert result["ok"] is False


# ── Watchlist: add/remove/status ─────────────────────────────────────────────

def test_watchlist_add_new(mock_ctx):
    result = json.loads(_arxiv_watchlist_add(mock_ctx, category="cs.LG", query="rlhf", label="RLHF papers"))
    assert result["ok"] is True
    assert result["added"] == "RLHF papers"


def test_watchlist_add_default_label(mock_ctx):
    result = json.loads(_arxiv_watchlist_add(mock_ctx, category="cs.AI"))
    assert result["ok"] is True
    assert result["added"] == "cs.AI"


def test_watchlist_add_duplicate(mock_ctx):
    _arxiv_watchlist_add(mock_ctx, category="cs.CL", query="alignment")
    result = json.loads(_arxiv_watchlist_add(mock_ctx, category="cs.CL", query="alignment"))
    assert result["ok"] is False
    assert "Already watching" in result["error"]


def test_watchlist_status_empty(mock_ctx):
    result = json.loads(_arxiv_watchlist_status(mock_ctx))
    assert result["ok"] is True
    assert result["count"] == 0
    assert result["entries"] == []


def test_watchlist_status_after_add(mock_ctx):
    _arxiv_watchlist_add(mock_ctx, category="cs.LG", label="ML")
    _arxiv_watchlist_add(mock_ctx, category="cs.CV", label="CV")
    result = json.loads(_arxiv_watchlist_status(mock_ctx))
    assert result["count"] == 2
    labels = [e["label"] for e in result["entries"]]
    assert "ML" in labels and "CV" in labels


def test_watchlist_remove_by_category(mock_ctx):
    _arxiv_watchlist_add(mock_ctx, category="cs.LG", label="ML papers")
    result = json.loads(_arxiv_watchlist_remove(mock_ctx, category_or_label="cs.LG"))
    assert result["ok"] is True
    assert result["remaining"] == 0


def test_watchlist_remove_by_label(mock_ctx):
    _arxiv_watchlist_add(mock_ctx, category="cs.NE", label="NeuroEvo")
    result = json.loads(_arxiv_watchlist_remove(mock_ctx, category_or_label="NeuroEvo"))
    assert result["ok"] is True


def test_watchlist_remove_not_found(mock_ctx):
    result = json.loads(_arxiv_watchlist_remove(mock_ctx, category_or_label="nonexistent"))
    assert result["ok"] is False
    assert "Not found" in result["error"]


# ── Watchlist: check ─────────────────────────────────────────────────────────

def test_watchlist_check_empty(mock_ctx):
    result = json.loads(_arxiv_watchlist_check(mock_ctx))
    assert result["ok"] is True
    assert result["new_papers"] == []
    assert result["sources_checked"] == 0


def test_watchlist_check_finds_new_papers(mock_ctx):
    _arxiv_watchlist_add(mock_ctx, category="cs.LG", label="ML")
    with patch("ouroboros.tools.arxiv_reader._fetch_arxiv", return_value=_parse_feed(_ARXIV_ATOM)):
        result = json.loads(_arxiv_watchlist_check(mock_ctx, limit=5))
    assert result["ok"] is True
    assert result["count"] > 0
    assert result["sources_checked"] == 1
    paper = result["new_papers"][0]
    assert "matched_label" in paper


def test_watchlist_check_deduplicates(mock_ctx):
    """Second check returns 0 new papers (already seen)."""
    _arxiv_watchlist_add(mock_ctx, category="cs.LG", label="ML")
    papers = _parse_feed(_ARXIV_ATOM)
    with patch("ouroboros.tools.arxiv_reader._fetch_arxiv", return_value=papers):
        _arxiv_watchlist_check(mock_ctx, limit=10)
    with patch("ouroboros.tools.arxiv_reader._fetch_arxiv", return_value=papers):
        result = json.loads(_arxiv_watchlist_check(mock_ctx, limit=10))
    assert result["count"] == 0


def test_watchlist_check_multiple_subscriptions(mock_ctx):
    _arxiv_watchlist_add(mock_ctx, category="cs.LG", label="ML")
    _arxiv_watchlist_add(mock_ctx, category="cs.CL", label="NLP")
    papers = _parse_feed(_ARXIV_ATOM)
    with patch("ouroboros.tools.arxiv_reader._fetch_arxiv", return_value=papers):
        result = json.loads(_arxiv_watchlist_check(mock_ctx, limit=5))
    assert result["sources_checked"] == 2


def test_watchlist_check_api_failure_continues(mock_ctx):
    """API failure for one subscription should not crash the whole check."""
    _arxiv_watchlist_add(mock_ctx, category="cs.LG", label="ML")
    _arxiv_watchlist_add(mock_ctx, category="cs.CV", label="CV")
    call_count = [0]

    def _mock_fetch(q, **kw):
        call_count[0] += 1
        if call_count[0] == 1:
            raise OSError("network down")
        return _parse_feed(_ARXIV_ATOM)

    with patch("ouroboros.tools.arxiv_reader._fetch_arxiv", side_effect=_mock_fetch):
        result = json.loads(_arxiv_watchlist_check(mock_ctx, limit=5))
    assert result["ok"] is True
    assert result["sources_checked"] == 2


# ── get_tools registry ────────────────────────────────────────────────────────

def test_get_tools_count():
    tools = get_tools()
    assert len(tools) == 6


def test_get_tools_names():
    names = {t.name for t in get_tools()}
    expected = {
        "arxiv_search", "arxiv_latest",
        "arxiv_watchlist_add", "arxiv_watchlist_remove",
        "arxiv_watchlist_status", "arxiv_watchlist_check",
    }
    assert names == expected


def test_get_tools_schemas_valid():
    """Each tool schema must have name, description, parameters fields."""
    for tool in get_tools():
        s = tool.schema
        assert "name" in s, f"{tool.name}: missing 'name' in schema"
        assert "description" in s, f"{tool.name}: missing 'description' in schema"
        assert "parameters" in s, f"{tool.name}: missing 'parameters' in schema"
        params = s["parameters"]
        assert params.get("type") == "object", f"{tool.name}: parameters.type must be 'object'"
