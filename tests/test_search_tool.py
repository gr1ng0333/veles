import json
from unittest.mock import patch

from ouroboros.tools.search import _clean_sources, _merge_search_results, _research_run, _web_search


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



def test_search_helpers_cover_clean_and_merge_paths():
    cleaned = _clean_sources([
        {"title": "A", "url": "https://example.com/a", "snippet": "one"},
        {"title": "A-dup", "url": "https://example.com/a", "snippet": "dup"},
        {"title": "No URL", "url": "", "snippet": "bad"},
        {"title": "Bad URL", "url": "ftp://example.com/file", "snippet": "bad"},
        {"title": "B", "url": "https://example.com/b", "snippet": "two"},
    ])
    assert [row['url'] for row in cleaned] == ['https://example.com/a', 'https://example.com/b']

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
            "backend": "openai",
            "sources": [{"title": "B", "url": "https://example.com/b", "snippet": "two"}],
            "answer": "fallback answer",
            "error": None,
        },
        'test',
    )
    assert merged['status'] == 'degraded'
    assert merged['backend'] == 'searxng+openai'
    assert merged['sources'][0]['url'] == 'https://example.com/b'


@patch('ouroboros.tools.search.save_artifact', return_value={"relative_path": "artifacts/outbox/2026/03/17/task/json/research-run-test.json", "bytes": 123})
@patch('ouroboros.tools.search._web_search')
def test_research_run_returns_trace_and_persists_artifact(_web, _save):
    _web.side_effect = [
        json.dumps({
            "query": "claude research mode",
            "status": "ok",
            "backend": "searxng",
            "sources": [{"title": "Anthropic", "url": "https://example.com/a", "snippet": "one"}],
            "answer": "",
            "error": None,
        }),
        json.dumps({
            "query": "claude research mode official",
            "status": "ok",
            "backend": "searxng",
            "sources": [{"title": "Docs", "url": "https://example.com/b", "snippet": "two"}],
            "answer": "",
            "error": None,
        }),
        json.dumps({
            "query": "claude research mode overview",
            "status": "no_results",
            "backend": "searxng",
            "sources": [],
            "answer": "",
            "error": None,
        }),
    ]

    class Ctx:
        drive_root = '/tmp'
        task_id = 'task-1'
        current_chat_id = 1

    raw = _research_run(Ctx(), 'claude research mode')
    data = json.loads(raw)
    assert data['user_query'] == 'claude research mode'
    assert data['subqueries']
    assert len(data['visited_pages']) == 3
    assert data['candidate_sources'][0]['url'] == 'https://example.com/a'
    assert data['trace']['relative_path'].endswith('.json')
