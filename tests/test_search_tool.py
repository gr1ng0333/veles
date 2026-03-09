import json
from unittest.mock import patch

from ouroboros.tools.search import _clean_sources, _merge_search_results, _web_search


@patch('ouroboros.tools.search._search_searxng', return_value={
    "query": "test",
    "status": "ok",
    "backend": "searxng",
    "sources": [{"title": "A", "url": "https://example.com", "snippet": "x"}],
    "answer": "",
    "error": None,
})
def test_web_search_returns_structured_json(_searx):
    raw = _web_search(None, 'test')
    data = json.loads(raw)
    assert data['status'] == 'ok'
    assert data['backend'] == 'searxng'
    assert isinstance(data['sources'], list)
    assert data['sources'][0]['url'] == 'https://example.com'


def test_clean_sources_deduplicates_and_filters_invalid_rows():
    cleaned = _clean_sources([
        {"title": "A", "url": "https://example.com/a", "snippet": "one"},
        {"title": "A-dup", "url": "https://example.com/a", "snippet": "dup"},
        {"title": "No URL", "url": "", "snippet": "bad"},
        {"title": "Bad URL", "url": "ftp://example.com/file", "snippet": "bad"},
        {"title": "B", "url": "https://example.com/b", "snippet": "two"},
    ])
    assert [row['url'] for row in cleaned] == ['https://example.com/a', 'https://example.com/b']


def test_merge_search_results_marks_degraded_when_fallback_needed():
    merged = _merge_search_results(
        {
            "query": "test",
            "status": "no_results",
            "backend": "searxng",
            "sources": [],
            "answer": "",
            "error": "empty",
        },
        {
            "query": "test",
            "status": "ok",
            "backend": "serper",
            "sources": [{"title": "B", "url": "https://example.com/b", "snippet": "two"}],
            "answer": "fallback answer",
            "error": None,
        },
        'test',
    )
    assert merged['status'] == 'degraded'
    assert merged['backend'] == 'searxng+serper'
    assert merged['sources'][0]['url'] == 'https://example.com/b'


@patch('ouroboros.tools.search._search_searxng', return_value=None)
@patch('ouroboros.tools.search._search_api_fallback', return_value={
    "query": "test",
    "status": "ok",
    "backend": "serper",
    "sources": [{"title": "S", "url": "https://serper.example/result", "snippet": "serper snippet"}],
    "answer": "serper answer",
    "error": None,
})
def test_web_search_uses_serper_fallback_when_searxng_unavailable(_fallback, _searx):
    raw = _web_search(None, 'test')
    data = json.loads(raw)
    assert data['status'] == 'ok'
    assert data['backend'] == 'serper'
    assert data['sources'][0]['url'] == 'https://serper.example/result'


@patch('ouroboros.tools.search._search_searxng', return_value={
    "query": "test",
    "status": "no_results",
    "backend": "searxng",
    "sources": [],
    "answer": "",
    "error": "empty",
})
@patch('ouroboros.tools.search._search_api_fallback', return_value={
    "query": "test",
    "status": "ok",
    "backend": "serper",
    "sources": [{"title": "B", "url": "https://example.com/b", "snippet": "two"}],
    "answer": "fallback answer",
    "error": None,
})
def test_web_search_merges_searxng_and_api_fallback(_fallback, _searx):
    raw = _web_search(None, 'test')
    data = json.loads(raw)
    assert data['status'] == 'degraded'
    assert data['backend'] == 'searxng+serper'
    assert data['sources'][0]['url'] == 'https://example.com/b'
