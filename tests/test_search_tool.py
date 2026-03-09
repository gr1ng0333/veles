import json
from unittest.mock import patch

from ouroboros.tools.search import _clean_sources, _search_serper, _web_search


@patch('ouroboros.tools.search._search_serper', return_value={
    "query": "test",
    "status": "ok",
    "backend": "serper",
    "sources": [{"title": "A", "url": "https://example.com", "snippet": "x"}],
    "answer": "",
    "error": None,
})
def test_web_search_returns_structured_json(_serper):
    raw = _web_search(None, 'test')
    data = json.loads(raw)
    assert data['status'] == 'ok'
    assert data['backend'] == 'serper'
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


@patch.dict('os.environ', {}, clear=True)
def test_search_serper_returns_error_when_api_key_missing():
    data = _search_serper('test')
    assert data['status'] == 'error'
    assert data['backend'] == 'serper'
    assert data['error'] == 'SERPER_API_KEY is not configured.'


@patch('ouroboros.tools.search._http_json_request', return_value={
    "answerBox": {
        "title": "Diminishing returns",
        "answer": "Output rises at a decreasing rate.",
        "link": "https://example.com/answer",
        "snippet": "Economics concept.",
    },
    "organic": [
        {"title": "Result 1", "link": "https://example.com/1", "snippet": "One"},
        {"title": "Result 2", "link": "https://example.com/2", "snippet": "Two"},
    ],
})
@patch.dict('os.environ', {'SERPER_API_KEY': 'test-key'}, clear=True)
def test_search_serper_returns_non_empty_results(_http):
    data = _search_serper('diminishing returns economics')
    assert data['status'] == 'ok'
    assert data['backend'] == 'serper'
    assert len(data['sources']) == 3
    assert data['sources'][0]['url'] == 'https://example.com/answer'
    assert 'decreasing rate' in data['answer']
