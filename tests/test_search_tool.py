import json
from unittest.mock import patch

import pytest

from ouroboros.tools.search import (
    INTENT_POLICIES,
    _build_query_plan,
    _classify_intent,
    _clean_sources,
    _read_page_findings,
    _research_run,
    _web_search,
)


@pytest.mark.parametrize(
    ('query', 'side_effect', 'expected_intent', 'expected_policy', 'expected_subqueries', 'expected_first_url'),
    [
        (
            'claude research mode',
            [
                json.dumps({
                    "query": "claude research mode",
                    "status": "ok",
                    "backend": "searxng",
                    "sources": [
                        {"title": "Anthropic", "url": "https://example.com/a", "snippet": "one"},
                        {"title": "Community thread", "url": "https://reddit.com/r/claude", "snippet": "discussion"},
                    ],
                    "answer": "",
                    "error": None,
                }),
                json.dumps({
                    "query": "claude research mode overview",
                    "status": "ok",
                    "backend": "searxng",
                    "sources": [{"title": "Docs", "url": "https://example.com/b", "snippet": "two"}],
                    "answer": "",
                    "error": None,
                }),
                json.dumps({
                    "query": "claude research mode common misconceptions",
                    "status": "no_results",
                    "backend": "searxng",
                    "sources": [],
                    "answer": "",
                    "error": None,
                }),
            ],
            'background_explainer',
            {
                'freshness_priority': 'low',
                'search_branches': 3,
                'min_sources_before_synthesis': 2,
                'require_official_source': False,
            },
            3,
            'https://example.com/a',
        ),
        (
            'openai api rate limit',
            [
                json.dumps({
                    "query": "openai api rate limit",
                    "status": "ok",
                    "backend": "searxng",
                    "sources": [
                        {"title": "Docs", "url": "https://platform.openai.com/docs", "snippet": "limits updated 2026"},
                        {"title": "Roundup", "url": "https://news.google.com/articles/abc", "snippet": "aggregated summary"},
                    ],
                    "answer": "",
                    "error": None,
                }),
                json.dumps({
                    "query": "openai api rate limit recent",
                    "status": "ok",
                    "backend": "searxng",
                    "sources": [{"title": "Reference", "url": "https://platform.openai.com/docs/api-reference", "snippet": "reference updated 2026"}],
                    "answer": "",
                    "error": None,
                }),
                json.dumps({
                    "query": "openai api rate limit official docs",
                    "status": "ok",
                    "backend": "searxng",
                    "sources": [{"title": "Rate limits", "url": "https://platform.openai.com/docs/guides/rate-limits", "snippet": "official guide"}],
                    "answer": "",
                    "error": None,
                }),
                json.dumps({
                    "query": "openai api rate limit reference guide",
                    "status": "ok",
                    "backend": "searxng",
                    "sources": [{"title": "Forum post", "url": "https://reddit.com/r/openai/comments/1", "snippet": "i think the limit is..."}],
                    "answer": "",
                    "error": None,
                }),
            ],
            'product_docs_api_lookup',
            {
                'freshness_priority': 'medium',
                'search_branches': 4,
                'min_sources_before_synthesis': 2,
                'require_official_source': True,
            },
            4,
            'https://platform.openai.com/docs/guides/rate-limits',
        ),
    ],
)
@patch('ouroboros.tools.search.save_artifact')
@patch('ouroboros.tools.search._web_search')
def test_research_run_policy_trace_and_scored_candidates(_web, _save, query, side_effect, expected_intent, expected_policy, expected_subqueries, expected_first_url):
    with patch('ouroboros.tools.search._search_searxng', return_value={
        "query": "test",
        "status": "ok",
        "backend": "searxng",
        "sources": [{"title": "A", "url": "https://example.com", "snippet": "x"}],
        "answer": "",
        "error": None,
    }):
        raw = _web_search(None, 'test')
        data = json.loads(raw)
        assert data['status'] == 'ok'
        assert data['backend'] == 'searxng'
        assert isinstance(data['sources'], list)
        assert data['sources'][0]['url'] == 'https://example.com'

    cleaned = _clean_sources([
        {"title": "A", "url": "https://example.com/a", "snippet": "one"},
        {"title": "A-dup", "url": "https://example.com/a", "snippet": "dup"},
        {"title": "No URL", "url": "", "snippet": "bad"},
        {"title": "Bad URL", "url": "ftp://example.com/file", "snippet": "bad"},
        {"title": "B", "url": "https://example.com/b", "snippet": "two"},
    ])
    assert [row['url'] for row in cleaned] == ['https://example.com/a', 'https://example.com/b']

    with patch('ouroboros.tools.search._search_searxng', return_value={
        "query": "test",
        "status": "no_results",
        "backend": "searxng",
        "sources": [],
        "answer": "",
        "error": "empty",
    }), patch('ouroboros.tools.search._search_openai', return_value={
        "query": "test",
        "status": "ok",
        "backend": "openai",
        "sources": [{"title": "B", "url": "https://example.com/b", "snippet": "two"}],
        "answer": "fallback answer",
        "error": None,
    }):
        merged = json.loads(_web_search(None, 'test'))
    assert merged['status'] == 'degraded'
    assert merged['backend'] == 'searxng+openai'
    assert merged['sources'][0]['url'] == 'https://example.com/b'

    _web.side_effect = side_effect
    _save.return_value = {"relative_path": "artifacts/outbox/2026/03/17/task/json/research-run.json", "bytes": 123}

    class Ctx:
        drive_root = '/tmp'
        task_id = 'task-1'
        current_chat_id = 1

    data = json.loads(_research_run(Ctx(), query))
    assert data['user_query'] == query
    assert data['intent_type'] == expected_intent
    assert data['subqueries']
    assert len(data['visited_pages']) == expected_subqueries
    assert len(data['subqueries']) == expected_subqueries
    assert data['candidate_sources'][0]['url'] == expected_first_url
    assert data['candidate_sources'][0]['decision'] == 'selected'
    assert isinstance(data['candidate_sources'][0]['reasons'], list) and data['candidate_sources'][0]['reasons']
    assert data['intent_policy'] == expected_policy
    assert data['trace']['relative_path'].endswith('.json')
    assert data['query_plan']['branch_budget'] == expected_subqueries
    assert any('selected_to_read' in page and 'rejected' in page for page in data['visited_pages'])
    assert any(page['ranked_sources'] for page in data['visited_pages'] if page['source_count'])


QUERY_CASES = [
    ("what happened with openai today", "breaking_news"),
    ("latest news about anthropic", "breaking_news"),
    ("что случилось сегодня с nvidia", "breaking_news"),
    ("python 3.13 release date", "fact_lookup"),
    ("how many parameters does llama 3 8b have", "fact_lookup"),
    ("сколько контекста у claude 3.7", "fact_lookup"),
    ("openai api rate limit official docs", "product_docs_api_lookup"),
    ("anthropic sdk quickstart", "product_docs_api_lookup"),
    ("документация telegram bot api endpoint sendDocument", "product_docs_api_lookup"),
    ("compare fastapi vs django for internal tools", "comparison_evaluation"),
    ("сравни claude и gpt для research", "comparison_evaluation"),
    ("benchmark rust vs go web frameworks", "comparison_evaluation"),
    ("explain what retrieval augmented generation is", "background_explainer"),
    ("что такое vector database", "background_explainer"),
    ("how does kv cache work", "background_explainer"),
    ("anthropic founders and company history", "people_company_ecosystem_tracking"),
    ("openai funding and leadership changes", "people_company_ecosystem_tracking"),
    ("экосистема langchain и основные maintainers", "people_company_ecosystem_tracking"),
]


@pytest.mark.parametrize(('query', 'expected_intent'), QUERY_CASES)
def test_intent_policy_table_and_classification_contract(query, expected_intent):
    assert set(INTENT_POLICIES) == {
        'breaking_news',
        'fact_lookup',
        'product_docs_api_lookup',
        'comparison_evaluation',
        'background_explainer',
        'people_company_ecosystem_tracking',
    }
    for policy in INTENT_POLICIES.values():
        assert policy.freshness_priority in {'low', 'medium', 'high'}
        assert policy.search_branches >= 3
        assert policy.min_sources_before_synthesis >= 2
        assert isinstance(policy.require_official_source, bool)
    assert _classify_intent(query) == expected_intent

    planner_cases = [
        ('openai api rate limit', 'product_docs_api_lookup', 4),
        ('claude vs gpt for research', 'comparison_evaluation', 4),
        ('what happened today with xAI', 'breaking_news', 4),
        ('what is retrieval augmented generation', 'background_explainer', 3),
    ]
    for planned_query, planned_intent, expected_budget in planner_cases:
        plan = _build_query_plan(planned_query, planned_intent)
        assert plan.branch_budget == expected_budget
        assert 3 <= len(plan.subqueries) <= 6
        assert len(plan.subqueries) == expected_budget
        assert all(item.strip() for item in plan.subqueries)
        assert len({item.casefold() for item in plan.subqueries}) == len(plan.subqueries)
        assert plan.primary_query == planned_query


@patch('ouroboros.tools.search.save_artifact')
@patch('ouroboros.tools.search._web_search')
def test_source_scoring_rejects_duplicate_aggregator_and_social_noise(_web, _save):
    _web.side_effect = [
        json.dumps({
            "query": "openai api rate limit",
            "status": "ok",
            "backend": "searxng",
            "sources": [
                {"title": "Docs", "url": "https://platform.openai.com/docs/guides/rate-limits", "snippet": "official updated 2026 rate limits"},
                {"title": "HN mirror", "url": "https://news.ycombinator.com/item?id=1", "snippet": "roundup"},
                {"title": "Reddit", "url": "https://reddit.com/r/openai/comments/xyz", "snippet": "forum guess"},
            ],
            "answer": "",
            "error": None,
        }),
        json.dumps({
            "query": "openai api rate limit recent",
            "status": "ok",
            "backend": "searxng",
            "sources": [
                {"title": "Docs duplicate", "url": "https://platform.openai.com/docs/guides/rate-limits", "snippet": "official updated 2026 rate limits"},
                {"title": "API reference", "url": "https://platform.openai.com/docs/api-reference", "snippet": "reference updated 2026"},
            ],
            "answer": "",
            "error": None,
        }),
        json.dumps({
            "query": "openai api rate limit official docs",
            "status": "no_results",
            "backend": "searxng",
            "sources": [],
            "answer": "",
            "error": None,
        }),
        json.dumps({
            "query": "openai api rate limit reference guide",
            "status": "no_results",
            "backend": "searxng",
            "sources": [],
            "answer": "",
            "error": None,
        }),
    ]
    _save.return_value = {"relative_path": "artifacts/outbox/trace.json", "bytes": 123}

    class Ctx:
        drive_root = '/tmp'
        task_id = 'task-1'
        current_chat_id = 1

    data = json.loads(_research_run(Ctx(), 'openai api rate limit'))
    assert data['candidate_sources'][0]['url'] == 'https://platform.openai.com/docs/guides/rate-limits'
    assert data['candidate_sources'][0]['score'] >= data['candidate_sources'][-1]['score']
    first_page = data['visited_pages'][0]
    rejected_urls = {item['url'] for item in first_page['rejected']}
    assert 'https://news.ycombinator.com/item?id=1' in rejected_urls
    assert 'https://reddit.com/r/openai/comments/xyz' in rejected_urls
    duplicate_entry = next(item for page in data['visited_pages'] for item in page['ranked_sources'] if item['url'] == 'https://platform.openai.com/docs/guides/rate-limits' and any('duplicate:' in reason for reason in item['reasons']))
    assert duplicate_entry['decision'] == 'reject'
    assert any('official-source' in reason or 'primary-source' in reason for reason in data['candidate_sources'][0]['reasons'])


@patch('ouroboros.tools.search.save_artifact')
@patch('ouroboros.tools.search._read_page_findings')
@patch('ouroboros.tools.search._web_search')
def test_deep_reading_extracts_findings_from_docs_news_and_blog(_web, _fetch, _save):
    _web.side_effect = [
        json.dumps({
            "query": "openai api rate limit",
            "status": "ok",
            "backend": "searxng",
            "sources": [
                {"title": "Docs", "url": "https://platform.openai.com/docs/guides/rate-limits", "snippet": "Updated 2026 rate limits for API usage."},
                {"title": "News", "url": "https://example.com/news/openai-rate-limit", "snippet": "Today OpenAI updated rate limit guidance."},
            ],
            "answer": "",
            "error": None,
        }),
        json.dumps({
            "query": "openai api rate limit recent",
            "status": "ok",
            "backend": "searxng",
            "sources": [
                {"title": "Blog", "url": "https://blog.example.com/openai-rate-limit-analysis", "snippet": "API rate limit analysis and examples."},
            ],
            "answer": "",
            "error": None,
        }),
        json.dumps({
            "query": "openai api rate limit official docs",
            "status": "no_results",
            "backend": "searxng",
            "sources": [],
            "answer": "",
            "error": None,
        }),
        json.dumps({
            "query": "openai api rate limit reference guide",
            "status": "no_results",
            "backend": "searxng",
            "sources": [],
            "answer": "",
            "error": None,
        }),
    ]
    _fetch.side_effect = [
        {"url": "https://platform.openai.com/docs/guides/rate-limits", "status": "ok", "content_type": "text/html", "text_preview": "Updated 2026-03-17. The API rate limit for tier 1 is 500 RPM.", "relevant_sections": ["The API rate limit for tier 1 is 500 RPM."], "findings": [{"claim": "The API rate limit for tier 1 is 500 RPM.", "evidence_snippet": "The API rate limit for tier 1 is 500 RPM.", "source_url": "https://platform.openai.com/docs/guides/rate-limits", "source_type": "docs", "observed_at": "2026-03-17", "confidence_local": "high"}], "error": None},
        {"url": "https://example.com/news/openai-rate-limit", "status": "ok", "content_type": "text/html", "text_preview": "Today OpenAI announced revised limits.", "relevant_sections": ["Today OpenAI announced revised limits."], "findings": [{"claim": "Today OpenAI announced revised limits.", "evidence_snippet": "Today OpenAI announced revised limits.", "source_url": "https://example.com/news/openai-rate-limit", "source_type": "news", "observed_at": "", "confidence_local": "medium"}], "error": None},
        {"url": "https://blog.example.com/openai-rate-limit-analysis", "status": "ok", "content_type": "text/html", "text_preview": "This blog explains the API rate limit for tier 1 is 500 RPM.", "relevant_sections": ["This blog explains the API rate limit for tier 1 is 500 RPM."], "findings": [{"claim": "This blog explains the API rate limit for tier 1 is 500 RPM.", "evidence_snippet": "This blog explains the API rate limit for tier 1 is 500 RPM.", "source_url": "https://blog.example.com/openai-rate-limit-analysis", "source_type": "news", "observed_at": "", "confidence_local": "medium"}], "error": None},
    ]
    _save.return_value = {"relative_path": "artifacts/outbox/trace.json", "bytes": 123}

    class Ctx:
        drive_root = '/tmp'
        task_id = 'task-1'
        current_chat_id = 1

    data = json.loads(_research_run(Ctx(), 'openai api rate limit'))
    assert data['findings']
    assert any(f['source_type'] == 'docs' for f in data['findings'])
    assert any(f['source_type'] == 'news' for f in data['findings'])
    assert any(page['read_results'] for page in data['visited_pages'])
    first_read = next(page['read_results'][0] for page in data['visited_pages'] if page['read_results'])
    assert first_read['findings']
    assert first_read['relevant_sections']
    assert any('500 RPM' in f['claim'] or '500 RPM' in f['evidence_snippet'] for f in data['findings'])

    import urllib.request
    from unittest.mock import MagicMock

    fake_response = MagicMock()
    fake_response.read.return_value = b'<html><body><script>bad()</script><p>Updated 2026-03-17.</p><p>API rate limit is 500 RPM.</p><p>API rate limit is 500 RPM.</p></body></html>'
    fake_response.headers = {"Content-Type": 'text/html; charset=utf-8'}
    fake_response.__enter__.return_value = fake_response
    fake_response.__exit__.return_value = False

    original = urllib.request.urlopen
    urllib.request.urlopen = MagicMock(return_value=fake_response)
    try:
        result = _read_page_findings('openai api rate limit', {'url': 'https://platform.openai.com/docs/guides/rate-limits', 'host': 'platform.openai.com'})
    finally:
        urllib.request.urlopen = original

    assert result['status'] == 'ok'
    assert 'script' not in result['text_preview'].lower()
    assert result['relevant_sections']
    assert result['findings']
    assert result['findings'][0]['source_type'] == 'docs'
    assert result['findings'][0]['confidence_local'] in {'low', 'medium', 'high'}

