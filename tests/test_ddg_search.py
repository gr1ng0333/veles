"""Tests for DuckDuckGo HTML scraper and search integration."""

from __future__ import annotations

from ouroboros.tools.search import _parse_ddg_html, clean_sources


# ---------------------------------------------------------------------------
# Sample DDG HTML fixtures
# ---------------------------------------------------------------------------
_DDG_HTML_SAMPLE = """
<div class="results">
  <div class="result results_links results_links_deep web-result">
    <div class="links_main links_deep result__body">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FPython_%28programming_language%29&amp;rut=abc">
          Python (programming language) - <b>Wikipedia</b>
        </a>
      </h2>
      <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FPython_%28programming_language%29&amp;rut=abc">
        Python is a high-level, general-purpose <b>programming</b> language.
      </a>
    </div>
  </div>
  <div class="result results_links results_links_deep web-result">
    <div class="links_main links_deep result__body">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.python.org%2F&amp;rut=def">
          Welcome to Python.org
        </a>
      </h2>
      <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.python.org%2F&amp;rut=def">
        The official home of the Python Programming Language.
      </a>
    </div>
  </div>
  <div class="result results_links results_links_deep web-result">
    <div class="links_main links_deep result__body">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a" href="https://docs.python.org/3/">
          Python 3 Documentation
        </a>
      </h2>
      <a class="result__snippet" href="https://docs.python.org/3/">
        Official Python 3 docs with tutorials and library reference.
      </a>
    </div>
  </div>
</div>
"""


def test_ddg_parse_results():
    """Parse well-formed DDG HTML and extract title, url, snippet."""
    results = _parse_ddg_html(_DDG_HTML_SAMPLE, limit=5)
    assert len(results) >= 2
    first = results[0]
    assert "wikipedia.org" in first["url"]
    assert "Python" in first["title"]
    assert first["snippet"]  # non-empty


def test_ddg_url_extraction():
    """Extract real URL from DDG redirect link."""
    html = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=x">'
        "Example Page</a>"
        '<a class="result__snippet" href="#">Some snippet text</a>'
    )
    results = _parse_ddg_html(html, limit=5)
    assert len(results) == 1
    assert results[0]["url"] == "https://example.com/page"
    assert results[0]["title"] == "Example Page"


def test_ddg_direct_url():
    """Direct https URLs (not DDG redirects) are kept as-is."""
    html = (
        '<a class="result__a" href="https://example.com/direct">'
        "Direct Link</a>"
        '<a class="result__snippet" href="#">Snippet here</a>'
    )
    results = _parse_ddg_html(html, limit=5)
    assert len(results) == 1
    assert results[0]["url"] == "https://example.com/direct"


def test_ddg_empty_results():
    """Graceful handling of empty / malformed HTML."""
    assert _parse_ddg_html("", limit=5) == []
    assert _parse_ddg_html("<html><body>No results</body></html>", limit=5) == []


def test_ddg_html_entities_decoded():
    """HTML entities in title and snippet are unescaped."""
    html = (
        '<a class="result__a" href="https://example.com">'
        "Tom &amp; Jerry&#39;s Page</a>"
        '<a class="result__snippet" href="#">A &lt;b&gt;bold&lt;/b&gt; snippet</a>'
    )
    results = _parse_ddg_html(html, limit=5)
    assert results[0]["title"] == "Tom & Jerry's Page"
    assert "<b>" not in results[0]["snippet"]
    assert "bold" in results[0]["snippet"]


def test_ddg_skip_internal_ddg_links_without_uddg():
    """Links with duckduckgo.com but no uddg param are skipped."""
    html = (
        '<a class="result__a" href="//duckduckgo.com/some/internal">'
        "Internal DDG</a>"
        '<a class="result__snippet" href="#">Internal snippet</a>'
    )
    results = _parse_ddg_html(html, limit=5)
    assert len(results) == 0


def test_ddg_results_through_clean_sources():
    """DDG results pass through clean_sources() without loss."""
    results = _parse_ddg_html(_DDG_HTML_SAMPLE, limit=5)
    cleaned = clean_sources(results)
    assert len(cleaned) >= 2
    for item in cleaned:
        assert item["url"].startswith("https://")
        assert item["title"]


def test_ddg_limit_respected():
    """Limit parameter caps the number of results."""
    results = _parse_ddg_html(_DDG_HTML_SAMPLE, limit=1)
    assert len(results) == 1
