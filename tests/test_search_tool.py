import json
from unittest.mock import patch

from ouroboros.tools.search import _web_search


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
