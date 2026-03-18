import json
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.tools.search import (
    INTENT_POLICIES,
    _build_query_plan,
    _read_page_findings,
    _research_run,
    _web_search,
    get_tools,
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
    with patch.dict('os.environ', {'SERPER_API_KEY': ''}, clear=False), patch('ouroboros.tools.search._search_searxng', return_value={
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
        assert data['backend'] in {'serper', 'searxng'}
        assert isinstance(data['sources'], list)
        assert data['sources'][0]['url'] == 'https://example.com'

    with patch.dict('os.environ', {'SERPER_API_KEY': ''}, clear=False), patch('ouroboros.tools.search._search_searxng', return_value={
        "query": "test query",
        "status": "ok",
        "backend": "searxng",
        "sources": [
            {"title": "A", "url": "https://example.com/a", "snippet": "x"},
            {"title": "A2", "url": "https://example.com/a", "snippet": "dup"},
            {"title": "Bad", "url": "ftp://bad.example.com", "snippet": "skip"},
            {"title": "", "url": "https://example.com/b", "snippet": "y"},
        ],
        "answer": "",
        "error": None,
    }):
        payload = json.loads(_web_search(None, 'test query'))
        assert payload['sources'] == [
            {'url': 'https://example.com/a', 'title': 'A', 'snippet': 'x'},
            {'url': 'https://example.com/b', 'title': 'https://example.com/b', 'snippet': 'y'},
        ]

    with patch.dict('os.environ', {'SERPER_API_KEY': ''}, clear=False), patch('ouroboros.tools.search._search_searxng', return_value={
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
def test_intent_policy_and_followup_contract(query, expected_intent):
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

    class Ctx:
        drive_root = '/tmp'
        task_id = 'task-i'
        current_chat_id = 1

    with patch('ouroboros.tools.search._web_search', return_value=json.dumps({
        "query": query,
        "status": "no_results",
        "backend": "searxng",
        "sources": [],
        "answer": "",
        "error": None,
    })), patch('ouroboros.tools.search.save_artifact', return_value={"relative_path": "artifacts/outbox/trace.json", "bytes": 1}):
        data = json.loads(_research_run(Ctx(), query))
    assert data['intent_type'] == expected_intent

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


@pytest.mark.parametrize('mode', ['source_scoring', 'deep_reading'])
@patch('ouroboros.tools.search.save_artifact')
def test_source_selection_and_deep_reading_contours(_save, mode):
    _save.return_value = {"relative_path": "artifacts/outbox/trace.json", "bytes": 123}

    class Ctx:
        drive_root = '/tmp'
        task_id = 'task-1'
        current_chat_id = 1

    if mode == 'source_scoring':
        with patch('ouroboros.tools.search._web_search') as _web:
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
                json.dumps({"query": "openai api rate limit official docs", "status": "no_results", "backend": "searxng", "sources": [], "answer": "", "error": None}),
                json.dumps({"query": "openai api rate limit reference guide", "status": "no_results", "backend": "searxng", "sources": [], "answer": "", "error": None}),
            ]
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
        return

    with patch('ouroboros.tools.search._web_search') as _web, patch('ouroboros.tools.search._read_page_findings') as _fetch:
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
            json.dumps({"query": "openai api rate limit official docs", "status": "no_results", "backend": "searxng", "sources": [], "answer": "", "error": None}),
            json.dumps({"query": "openai api rate limit reference guide", "status": "no_results", "backend": "searxng", "sources": [], "answer": "", "error": None}),
        ]
        _fetch.side_effect = [
            {"url": "https://platform.openai.com/docs/guides/rate-limits", "status": "ok", "content_type": "text/html", "text_preview": "Updated 2026-03-17. The API rate limit for tier 1 is 500 RPM.", "relevant_sections": ["The API rate limit for tier 1 is 500 RPM."], "findings": [{"claim": "The API rate limit for tier 1 is 500 RPM.", "evidence_snippet": "The API rate limit for tier 1 is 500 RPM.", "source_url": "https://platform.openai.com/docs/guides/rate-limits", "source_type": "docs", "observed_at": "2026-03-17", "confidence_local": "high"}], "error": None},
            {"url": "https://example.com/news/openai-rate-limit", "status": "ok", "content_type": "text/html", "text_preview": "Today OpenAI announced revised limits.", "relevant_sections": ["Today OpenAI announced revised limits."], "findings": [{"claim": "Today OpenAI announced revised limits.", "evidence_snippet": "Today OpenAI announced revised limits.", "source_url": "https://example.com/news/openai-rate-limit", "source_type": "news", "observed_at": "", "confidence_local": "medium"}], "error": None},
            {"url": "https://blog.example.com/openai-rate-limit-analysis", "status": "ok", "content_type": "text/html", "text_preview": "This blog explains the API rate limit for tier 1 is 500 RPM.", "relevant_sections": ["This blog explains the API rate limit for tier 1 is 500 RPM."], "findings": [{"claim": "This blog explains the API rate limit for tier 1 is 500 RPM.", "evidence_snippet": "This blog explains the API rate limit for tier 1 is 500 RPM.", "source_url": "https://blog.example.com/openai-rate-limit-analysis", "source_type": "news", "observed_at": "", "confidence_local": "medium"}], "error": None},
        ]
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


@pytest.mark.parametrize(
    ('query', 'web_side_effect', 'fetch_side_effect', 'assertion_mode'),
    [
        (
            'openai api rate limit',
            [
                json.dumps({
                    "query": "openai api rate limit",
                    "status": "ok",
                    "backend": "searxng",
                    "sources": [
                        {"title": "Docs", "url": "https://platform.openai.com/docs/guides/rate-limits", "snippet": "Updated 2026-03-17. Tier 1 is 500 RPM."},
                        {"title": "Blog", "url": "https://blog.example.com/openai-rate-limit", "snippet": "Tier 1 is 300 RPM according to our writeup."},
                    ],
                    "answer": "",
                    "error": None,
                }),
                json.dumps({"query": "openai api rate limit recent", "status": "no_results", "backend": "searxng", "sources": [], "answer": "", "error": None}),
                json.dumps({"query": "openai api rate limit official docs", "status": "no_results", "backend": "searxng", "sources": [], "answer": "", "error": None}),
                json.dumps({"query": "openai api rate limit reference guide", "status": "no_results", "backend": "searxng", "sources": [], "answer": "", "error": None}),
            ],
            [
                {"url": "https://platform.openai.com/docs/guides/rate-limits", "status": "ok", "content_type": "text/html", "text_preview": "Updated 2026-03-17. The API rate limit for tier 1 is 500 RPM.", "relevant_sections": ["The API rate limit for tier 1 is 500 RPM."], "findings": [{"claim": "The API rate limit for tier 1 is 500 RPM.", "evidence_snippet": "The API rate limit for tier 1 is 500 RPM.", "source_url": "https://platform.openai.com/docs/guides/rate-limits", "source_type": "docs", "observed_at": "2026-03-17", "confidence_local": "high"}], "error": None},
                {"url": "https://blog.example.com/openai-rate-limit", "status": "ok", "content_type": "text/html", "text_preview": "Our analysis says the API rate limit for tier 1 is 300 RPM.", "relevant_sections": ["Our analysis says the API rate limit for tier 1 is 300 RPM."], "findings": [{"claim": "The API rate limit for tier 1 is 300 RPM.", "evidence_snippet": "The API rate limit for tier 1 is 300 RPM.", "source_url": "https://blog.example.com/openai-rate-limit", "source_type": "blog", "observed_at": "", "confidence_local": "medium"}], "error": None},
            ],
            'contradictions',
        ),
        (
            'what happened today openai release',
            [
                json.dumps({
                    "query": "what happened today openai release",
                    "status": "ok",
                    "backend": "searxng",
                    "sources": [
                        {"title": "Post 1", "url": "https://example.com/post-1", "snippet": "OpenAI released something today."},
                        {"title": "Post 2", "url": "https://example.com/post-2", "snippet": "A new release is discussed."},
                    ],
                    "answer": "",
                    "error": None,
                }),
                json.dumps({"query": "what happened today openai release latest updates", "status": "no_results", "backend": "searxng", "sources": [], "answer": "", "error": None}),
                json.dumps({"query": "what happened today openai release timeline and reactions", "status": "no_results", "backend": "searxng", "sources": [], "answer": "", "error": None}),
                json.dumps({"query": "what happened today openai release conflicting reports", "status": "no_results", "backend": "searxng", "sources": [], "answer": "", "error": None}),
            ],
            [
                {"url": "https://example.com/post-1", "status": "ok", "content_type": "text/html", "text_preview": "OpenAI released a feature.", "relevant_sections": ["OpenAI released a feature."], "findings": [{"claim": "OpenAI released a feature.", "evidence_snippet": "OpenAI released a feature.", "source_url": "https://example.com/post-1", "source_type": "news", "observed_at": "", "confidence_local": "high"}], "error": None},
                {"url": "https://example.com/post-2", "status": "ok", "content_type": "text/html", "text_preview": "The release is discussed by multiple users.", "relevant_sections": ["The release is discussed by multiple users."], "findings": [{"claim": "The release is discussed by multiple users.", "evidence_snippet": "The release is discussed by multiple users.", "source_url": "https://example.com/post-2", "source_type": "news", "observed_at": "", "confidence_local": "medium"}], "error": None},
            ],
            'freshness',
        ),
    ],
)
@patch('ouroboros.tools.search.save_artifact')
@patch('ouroboros.tools.search._read_page_findings')
@patch('ouroboros.tools.search._web_search')
def test_research_run_uncertainty_modes(_web, _fetch, _save, query, web_side_effect, fetch_side_effect, assertion_mode):
    _web.side_effect = web_side_effect
    _fetch.side_effect = fetch_side_effect
    _save.return_value = {"relative_path": "artifacts/outbox/trace.json", "bytes": 123}

    class Ctx:
        drive_root = '/tmp'
        task_id = 'task-u'
        current_chat_id = 1

    data = json.loads(_research_run(Ctx(), query))
    assert data['uncertainty_notes']
    assert data['uncertainty_notes']
    if assertion_mode == 'contradictions':
        assert data['contradictions']
        assert any(item['kind'] == 'numeric_mismatch' for item in data['contradictions'])
        assert 'Источники расходятся' in data['final_answer']
        assert data['confidence'] in {'low', 'medium'}
    else:
        assert data['freshness_summary']['known_dated_findings'] == 0
        assert any('даты' in note.lower() or 'дата' in note.lower() for note in data['uncertainty_notes'])
        assert data['confidence'] == 'low'


@pytest.mark.parametrize(
    ('query', 'expected_mode', 'expected_phrase'),
    [
        ('python 3.13 release date', 'short_factual', 'Короткий ответ:'),
        ('compare fastapi vs django for internal tools', 'comparison_brief', 'Сопоставление подтверждённых утверждений:'),
        ('what happened today with openai release', 'timeline', 'Хронология/последовательность по прочитанным источникам:'),
        ('what is retrieval augmented generation', 'analyst_memo', 'Что подтверждают прочитанные источники:'),
    ],
)
@patch('ouroboros.tools.search.save_artifact')
@patch('ouroboros.tools.search._read_page_findings')
@patch('ouroboros.tools.search._web_search')
def test_research_run_synthesis_modes_and_evidence_trace(_web, _fetch, _save, query, expected_mode, expected_phrase):
    _save.return_value = {"relative_path": "artifacts/outbox/trace.json", "bytes": 123}
    _web.side_effect = [
        json.dumps({
            "query": query,
            "status": "ok",
            "backend": "searxng",
            "sources": [
                {"title": "Primary", "url": "https://example.com/a", "snippet": "Primary evidence snippet."},
                {"title": "Secondary", "url": "https://example.com/b", "snippet": "Secondary evidence snippet."},
            ],
            "answer": "",
            "error": None,
        }),
        json.dumps({"query": f"{query} recent", "status": "no_results", "backend": "searxng", "sources": [], "answer": "", "error": None}),
        json.dumps({"query": f"{query} official docs", "status": "no_results", "backend": "searxng", "sources": [], "answer": "", "error": None}),
        json.dumps({"query": f"{query} overview", "status": "no_results", "backend": "searxng", "sources": [], "answer": "", "error": None}),
        json.dumps({"query": f"{query} contradictions", "status": "no_results", "backend": "searxng", "sources": [], "answer": "", "error": None}),
    ]
    _fetch.side_effect = [
        {"url": "https://example.com/a", "status": "ok", "content_type": "text/html", "text_preview": "alpha", "relevant_sections": ["alpha"], "findings": [{"claim": "Primary claim for synthesis.", "evidence_snippet": "Primary evidence snippet.", "source_url": "https://docs.python.org/3/whatsnew/3.13.html", "source_type": "docs", "observed_at": "2026-03-17", "confidence_local": "high"}], "error": None},
        {"url": "https://example.com/b", "status": "ok", "content_type": "text/html", "text_preview": "beta", "relevant_sections": ["beta"], "findings": [{"claim": "Secondary claim for synthesis.", "evidence_snippet": "Secondary evidence snippet.", "source_url": "https://www.python.org/downloads/release/python-3130/", "source_type": "news", "observed_at": "", "confidence_local": "medium"}], "error": None},
    ]

    class Ctx:
        drive_root = '/tmp'
        task_id = 'task-s'
        current_chat_id = 1

    data = json.loads(_research_run(Ctx(), query))
    assert data['answer_mode'] == expected_mode
    assert data['synthesis']['answer_mode'] == expected_mode
    assert data['synthesis']['short_answer']
    assert data['synthesis']['key_findings']
    assert all(item['evidence_snippet'] and item['source_url'] for item in data['synthesis']['key_findings'])
    assert data['synthesis']['sources']
    assert expected_phrase in data['final_answer']
    assert 'evidence:' in data['final_answer']
    assert 'source:' in data['final_answer']


@patch('ouroboros.tools.search.save_artifact')
@patch('ouroboros.tools.search._read_page_findings')
@patch('ouroboros.tools.search._web_search')
@pytest.mark.parametrize(
    ('query', 'budget_mode', 'task_id', 'serp_query', 'fetch_side_effect', 'assertion_mode'),
    [
        (
            'python api limits',
            'cheap',
            'task-budget-cheap',
            'python api limits',
            [
                {"url": f"https://docs.example.com/{idx}", "status": "ok", "content_type": "text/html", "text_preview": "limit docs", "relevant_sections": ["limit docs"], "findings": [{"claim": f"Claim {idx}", "evidence_snippet": f"Evidence {idx}", "source_url": f"https://docs.example.com/{idx}", "source_type": "docs", "observed_at": "2026-03-18", "confidence_local": "medium"}], "error": None}
                for idx in range(1, 7)
            ],
            'budget-cheap',
        ),
        (
            'python api limits',
            'deep',
            'task-budget-deep',
            'python api limits',
            [
                {"url": f"https://docs.example.com/{idx}", "status": "ok", "content_type": "text/html", "text_preview": "limit docs", "relevant_sections": ["limit docs"], "findings": [{"claim": f"Claim {idx}", "evidence_snippet": f"Evidence {idx}", "source_url": f"https://docs.example.com/{idx}", "source_type": "docs", "observed_at": "2026-03-18", "confidence_local": "medium"}], "error": None}
                for idx in range(1, 7)
            ],
            'budget-deep',
        ),
        (
            'openai release today',
            'balanced',
            'task-stop',
            'openai release today',
            [
                {"url": "https://news.example.com/a", "status": "ok", "content_type": "text/html", "text_preview": "alpha", "relevant_sections": ["alpha"], "findings": [{"claim": "OpenAI released feature X.", "evidence_snippet": "Release confirmed on the official blog.", "source_url": "https://news.example.com/a", "source_type": "news", "observed_at": "2026-03-18", "confidence_local": "high"}], "error": None},
                {"url": "https://news.example.com/b", "status": "ok", "content_type": "text/html", "text_preview": "beta", "relevant_sections": ["beta"], "findings": [{"claim": "Feature X is now available.", "evidence_snippet": "Availability confirmed by rollout note.", "source_url": "https://news.example.com/b", "source_type": "news", "observed_at": "2026-03-18", "confidence_local": "high"}], "error": None},
            ],
            'early-stop',
        ),
    ],
)
def test_research_run_budget_and_early_stop_behaviour(_web, _fetch, _save, query, budget_mode, task_id, serp_query, fetch_side_effect, assertion_mode):
    _save.return_value = {"relative_path": "artifacts/outbox/trace.json", "bytes": 123}
    serp = json.dumps({
        "query": serp_query,
        "status": "ok",
        "backend": "serper",
        "sources": [
            {"title": "One", "url": "https://docs.example.com/1", "snippet": "API rate limit docs"},
            {"title": "Two", "url": "https://docs.example.com/2", "snippet": "API rate limit docs"},
            {"title": "Three", "url": "https://docs.example.com/3", "snippet": "API rate limit docs"},
            {"title": "Four", "url": "https://docs.example.com/4", "snippet": "API rate limit docs"},
            {"title": "Five", "url": "https://docs.example.com/5", "snippet": "API rate limit docs"},
        ] if 'python api limits' in serp_query else [
            {"title": "A", "url": "https://news.example.com/a", "snippet": "Release confirmed today"},
            {"title": "B", "url": "https://news.example.com/b", "snippet": "Release confirmed today"},
            {"title": "C", "url": "https://news.example.com/c", "snippet": "Release confirmed today"},
        ],
        "answer": "",
        "error": None,
    })
    _web.side_effect = [serp] * 12
    _fetch.side_effect = fetch_side_effect

    class Ctx:
        drive_root = '/tmp'
        current_chat_id = 1

    Ctx.task_id = task_id
    data = json.loads(_research_run(Ctx(), query, budget_mode))
    if assertion_mode == 'budget-cheap':
        assert data['budget_mode'] == 'cheap'
        assert data['budget_limits']['max_pages_read'] == 2
        assert data['budget_trace']['pages_read'] <= 2
        assert data['budget_trace']['subqueries_executed'] <= 3
        assert data['budget_trace']['early_stop_reason'] in {'enough-evidence', 'page-budget-exhausted', 'subquery-budget-exhausted'}
    elif assertion_mode == 'budget-deep':
        assert data['budget_mode'] == 'deep'
        assert data['budget_limits']['max_pages_read'] == 6
        assert data['budget_trace']['pages_read'] <= 6
        assert data['budget_trace']['subqueries_executed'] <= 6
    else:
        assert data['budget_trace']['early_stop_triggered'] is True
        assert data['budget_trace']['early_stop_reason'] == 'enough-evidence'
        assert data['budget_trace']['pages_read'] <= data['budget_limits']['max_pages_read']
        assert data['budget_trace']['synthesis_rounds_used'] == 1



@patch('ouroboros.tools.search.save_artifact')
@patch('ouroboros.tools.search._read_page_findings')
@patch('ouroboros.tools.search._web_search')
def test_comparison_prefers_primary_benchmark_retrieval(_web, _fetch, _save):
    _save.return_value = {"relative_path": "artifacts/outbox/trace.json", "bytes": 123}
    query = 'compare claude vs gpt benchmark latency'
    _web.side_effect = [
        json.dumps({
            "query": query,
            "status": "ok",
            "backend": "serper",
            "sources": [
                {"title": "Big roundup", "url": "https://medium.com/@bench/review", "snippet": "comparison overview opinions"},
                {"title": "Anthropic benchmark methodology", "url": "https://docs.anthropic.com/en/docs/build-with-claude/benchmarks", "snippet": "benchmark methodology latency evaluation"},
                {"title": "OpenAI evals guide", "url": "https://platform.openai.com/docs/guides/evals", "snippet": "evaluation methodology benchmark guidance"},
            ],
            "answer": "",
            "error": None,
        }),
        json.dumps({
            "query": f"{query} recent",
            "status": "ok",
            "backend": "serper",
            "sources": [
                {"title": "News recap", "url": "https://example.com/news/benchmarks", "snippet": "latest benchmark recap"},
            ],
            "answer": "",
            "error": None,
        }),
        json.dumps({
            "query": f"{query} official benchmark methodology maintainers",
            "status": "ok",
            "backend": "serper",
            "sources": [
                {"title": "Anthropic benchmark methodology", "url": "https://docs.anthropic.com/en/docs/build-with-claude/benchmarks", "snippet": "official benchmark methodology latency evaluation"},
                {"title": "OpenAI evals guide", "url": "https://platform.openai.com/docs/guides/evals", "snippet": "official evaluation methodology benchmark guidance"},
                {"title": "Forum summary", "url": "https://reddit.com/r/LocalLLaMA/comments/bench", "snippet": "community benchmark discussion"},
            ],
            "answer": "",
            "error": None,
        }),
        json.dumps({
            "query": f"{query} tradeoffs benchmark methodology independent results",
            "status": "ok",
            "backend": "serper",
            "sources": [
                {"title": "Independent lab", "url": "https://example.com/lab/benchmarks", "snippet": "independent benchmark methodology throughput latency"},
            ],
            "answer": "",
            "error": None,
        }),
        json.dumps({
            "query": f"{query} benchmark disagreement counterarguments",
            "status": "ok",
            "backend": "serper",
            "sources": [
                {"title": "Counterpoint", "url": "https://example.com/counterpoint", "snippet": "benchmark disagreement methodology critique"},
            ],
            "answer": "",
            "error": None,
        }),
    ]
    _fetch.side_effect = [
        {"url": "https://docs.anthropic.com/en/docs/build-with-claude/benchmarks", "status": "ok", "content_type": "text/html", "text_preview": "anthropic", "relevant_sections": ["anthropic"], "findings": [{"claim": "Anthropic publishes benchmark methodology for Claude evaluations.", "evidence_snippet": "The benchmarks page explains methodology and caveats.", "source_url": "https://docs.anthropic.com/en/docs/build-with-claude/benchmarks", "source_type": "docs", "observed_at": "2026-03-18", "confidence_local": "high"}], "error": None},
        {"url": "https://platform.openai.com/docs/guides/evals", "status": "ok", "content_type": "text/html", "text_preview": "openai", "relevant_sections": ["openai"], "findings": [{"claim": "OpenAI documents eval methodology rather than promising a universal benchmark winner.", "evidence_snippet": "The evals guide focuses on evaluation design and limits.", "source_url": "https://platform.openai.com/docs/guides/evals", "source_type": "docs", "observed_at": "2026-03-18", "confidence_local": "high"}], "error": None},
        {"url": "https://example.com/lab/benchmarks", "status": "ok", "content_type": "text/html", "text_preview": "lab", "relevant_sections": ["lab"], "findings": [{"claim": "Independent latency results depend heavily on prompt design and serving setup.", "evidence_snippet": "The lab notes prompt shape and hardware strongly affect results.", "source_url": "https://example.com/lab/benchmarks", "source_type": "blog", "observed_at": "2026-03-18", "confidence_local": "medium"}], "error": None},
        {"url": "https://example.com/counterpoint", "status": "ok", "content_type": "text/html", "text_preview": "counter", "relevant_sections": ["counter"], "findings": [{"claim": "Cross-vendor benchmark comparisons are not directly apples-to-apples.", "evidence_snippet": "The critique argues methodology differences can invert conclusions.", "source_url": "https://example.com/counterpoint", "source_type": "analysis", "observed_at": "2026-03-18", "confidence_local": "medium"}], "error": None},
    ]

    class Ctx:
        drive_root = '/tmp'
        task_id = 'task-comparison-primary'
        current_chat_id = 1

    data = json.loads(_research_run(Ctx(), query))
    assert data['intent_type'] == 'comparison_evaluation'
    assert data['intent_policy']['require_official_source'] is False
    assert data['query_plan']['branch_budget'] == 4
    assert data['query_plan']['official_docs_query'].endswith('official benchmark methodology maintainers')
    assert any(item['authority'] in {'official', 'primary'} for item in data['candidate_sources'][:2])
    top_urls = [item['url'] for item in data['candidate_sources'][:2]]
    assert 'https://docs.anthropic.com/en/docs/build-with-claude/benchmarks' in top_urls
    assert 'https://platform.openai.com/docs/guides/evals' in top_urls
    assert any(('benchmark-primary:' in reason) or ('primary-benchmark-branch:+0.8' in reason) for item in data['candidate_sources'][:2] for reason in item['reasons'])
    assert any('primary-benchmark-branch:+0.8' in reason or 'official-branch:+1.0' in reason for item in data['candidate_sources'][:2] for reason in item['reasons'])
    assert 'Сопоставление подтверждённых утверждений:' in data['final_answer']


@patch('ouroboros.tools.search.save_artifact')
@patch('ouroboros.tools.search._read_page_findings')
@patch('ouroboros.tools.search._web_search')
def test_benchmark_domain_priors_trace_by_primary_type(_web, _fetch, _save):
    _save.return_value = {"relative_path": "artifacts/outbox/trace.json", "bytes": 123}
    query = 'compare claude vs gpt benchmark leaderboard methodology'
    _web.side_effect = [
        json.dumps({
            "query": query,
            "status": "ok",
            "backend": "serper",
            "sources": [
                {"title": "HF leaderboard", "url": "https://huggingface.co/spaces/open-llm-leaderboard/open_llm_leaderboard", "snippet": "leaderboard benchmark scores arena"},
                {"title": "Anthropic benchmark methodology", "url": "https://docs.anthropic.com/en/docs/build-with-claude/benchmarks", "snippet": "official benchmark methodology latency evaluation"},
                {"title": "OpenAI evals guide", "url": "https://platform.openai.com/docs/guides/evals", "snippet": "official evaluation methodology benchmark guidance"},
                {"title": "GPT paper", "url": "https://arxiv.org/abs/2501.12345", "snippet": "paper benchmark evaluation details"},
                {"title": "GitHub eval harness", "url": "https://github.com/openai/evals", "snippet": "evaluation harness methodology benchmark repo"},
            ],
            "answer": "",
            "error": None,
        }),
        json.dumps({"query": f"{query} recent", "status": "no_results", "backend": "serper", "sources": [], "answer": "", "error": None}),
        json.dumps({"query": f"{query} official benchmark methodology maintainers", "status": "ok", "backend": "serper", "sources": [], "answer": "", "error": None}),
        json.dumps({"query": f"{query} tradeoffs benchmark methodology independent results", "status": "no_results", "backend": "serper", "sources": [], "answer": "", "error": None}),
        json.dumps({"query": f"{query} benchmark disagreement counterarguments", "status": "no_results", "backend": "serper", "sources": [], "answer": "", "error": None}),
    ]
    _fetch.side_effect = [
        {"url": "https://docs.anthropic.com/en/docs/build-with-claude/benchmarks", "status": "ok", "content_type": "text/html", "text_preview": "anthropic", "relevant_sections": ["anthropic"], "findings": [{"claim": "Anthropic documents benchmark methodology and caveats.", "evidence_snippet": "The page explains methodology and caveats.", "source_url": "https://docs.anthropic.com/en/docs/build-with-claude/benchmarks", "source_type": "docs", "observed_at": "2026-03-18", "confidence_local": "high"}], "error": None},
        {"url": "https://platform.openai.com/docs/guides/evals", "status": "ok", "content_type": "text/html", "text_preview": "openai", "relevant_sections": ["openai"], "findings": [{"claim": "OpenAI frames evals as methodology, not universal leaderboard truth.", "evidence_snippet": "The guide focuses on eval design and limitations.", "source_url": "https://platform.openai.com/docs/guides/evals", "source_type": "docs", "observed_at": "2026-03-18", "confidence_local": "high"}], "error": None},
    ]

    class Ctx:
        drive_root = '/tmp'
        task_id = 'task-benchmark-priors'
        current_chat_id = 1

    data = json.loads(_research_run(Ctx(), query))
    ranked = {}
    for page in data['visited_pages']:
        for item in page.get('ranked_sources', []):
            ranked[item['url']] = item
    assert ranked['https://docs.anthropic.com/en/docs/build-with-claude/benchmarks']['benchmark_primary_type'] == 'vendor_docs'
    assert ranked['https://platform.openai.com/docs/guides/evals']['benchmark_primary_type'] == 'vendor_docs'
    assert ranked['https://huggingface.co/spaces/open-llm-leaderboard/open_llm_leaderboard']['benchmark_primary_type'] == 'leaderboard'
    assert ranked['https://arxiv.org/abs/2501.12345']['benchmark_primary_type'] == 'paper'
    assert ranked['https://github.com/openai/evals']['benchmark_primary_type'] == 'repo_methodology'
    assert any('benchmark-primary:vendor_docs' in reason for reason in ranked['https://docs.anthropic.com/en/docs/build-with-claude/benchmarks']['reasons'])
    assert any('benchmark-primary:leaderboard' in reason for reason in ranked['https://huggingface.co/spaces/open-llm-leaderboard/open_llm_leaderboard']['reasons'])
    assert any('benchmark-primary:paper' in reason for reason in ranked['https://arxiv.org/abs/2501.12345']['reasons'])
    assert any('benchmark-primary:repo_methodology' in reason for reason in ranked['https://github.com/openai/evals']['reasons'])


@patch('ouroboros.tools.search.save_artifact')
@patch('ouroboros.tools.search._read_page_findings')
@patch('ouroboros.tools.search._web_search')
def test_policy_data_usage_prefers_official_policy_paths(_web, _fetch, _save):
    _save.return_value = {"relative_path": "artifacts/outbox/trace.json", "bytes": 123}
    query = 'OpenAI API data usage policy official source'
    _web.side_effect = [
        json.dumps({
            "query": query,
            "status": "ok",
            "backend": "serper",
            "sources": [
                {"title": "Blog summary", "url": "https://example.com/openai-data-usage-summary", "snippet": "data usage policy overview"},
                {"title": "OpenAI policy", "url": "https://openai.com/policies/how-your-data-is-used-to-improve-model-performance/", "snippet": "policy data usage training privacy"},
                {"title": "API help article", "url": "https://help.openai.com/en/articles/5722486-how-your-data-is-used-to-improve-model-performance", "snippet": "how your data is used api business services"},
            ],
            "answer": "",
            "error": None,
        }),
        json.dumps({
            "query": f"{query} recent",
            "status": "ok",
            "backend": "serper",
            "sources": [
                {"title": "OpenAI privacy", "url": "https://openai.com/policies/privacy-policy/", "snippet": "privacy policy retention training"},
            ],
            "answer": "",
            "error": None,
        }),
        json.dumps({
            "query": f"{query} official policy data usage retention privacy docs",
            "status": "ok",
            "backend": "serper",
            "sources": [
                {"title": "OpenAI policy", "url": "https://openai.com/policies/how-your-data-is-used-to-improve-model-performance/", "snippet": "official policy data usage"},
                {"title": "API help article", "url": "https://help.openai.com/en/articles/5722486-how-your-data-is-used-to-improve-model-performance", "snippet": "help center data usage business"},
            ],
            "answer": "",
            "error": None,
        }),
        json.dumps({
            "query": f"{query} privacy policy data retention help center official guidance",
            "status": "ok",
            "backend": "serper",
            "sources": [
                {"title": "OpenAI privacy", "url": "https://openai.com/policies/privacy-policy/", "snippet": "official privacy policy retention training"},
            ],
            "answer": "",
            "error": None,
        }),
    ]
    _fetch.side_effect = [
        {"url": "https://openai.com/policies/how-your-data-is-used-to-improve-model-performance/", "status": "ok", "content_type": "text/html", "text_preview": "policy", "relevant_sections": ["policy"], "findings": [{"claim": "OpenAI documents how API and business data may be handled and when training use differs by service.", "evidence_snippet": "The policy page explains how data usage differs across services.", "source_url": "https://openai.com/policies/how-your-data-is-used-to-improve-model-performance/", "source_type": "policy", "observed_at": "2026-03-18", "confidence_local": "high"}], "error": None},
        {"url": "https://help.openai.com/en/articles/5722486-how-your-data-is-used-to-improve-model-performance", "status": "ok", "content_type": "text/html", "text_preview": "help center", "relevant_sections": ["help center"], "findings": [{"claim": "OpenAI help center clarifies service-specific data handling behavior.", "evidence_snippet": "The help article explains business and API service handling.", "source_url": "https://help.openai.com/en/articles/5722486-how-your-data-is-used-to-improve-model-performance", "source_type": "policy", "observed_at": "2026-03-18", "confidence_local": "medium"}], "error": None},
        {"url": "https://openai.com/policies/privacy-policy/", "status": "ok", "content_type": "text/html", "text_preview": "privacy", "relevant_sections": ["privacy"], "findings": [{"claim": "OpenAI publishes privacy-policy details relevant to retention and processing.", "evidence_snippet": "The privacy policy describes retention and processing terms.", "source_url": "https://openai.com/policies/privacy-policy/", "source_type": "policy", "observed_at": "2026-03-18", "confidence_local": "high"}], "error": None},
    ]

    class Ctx:
        drive_root = '/tmp'
        task_id = 'task-policy-official'
        current_chat_id = 1

    data = json.loads(_research_run(Ctx(), query))
    assert data['intent_type'] == 'product_docs_api_lookup'
    assert data['intent_policy']['require_official_source'] is True
    assert data['query_plan']['official_docs_query'].endswith('official policy data usage retention privacy docs')
    assert data['candidate_sources'][0]['url'].startswith('https://openai.com/policies/')
    assert data['candidate_sources'][0]['authority'] == 'official'
    assert any('official-policy-path' in reason or 'policy-primary-path:+1.5' in reason for reason in data['candidate_sources'][0]['reasons'])
    assert all(item['authority'] == 'official' for item in data['candidate_sources'])


@patch('ouroboros.tools.search.save_artifact')
@patch('ouroboros.tools.search._read_page_findings')
@patch('ouroboros.tools.search._web_search')
def test_docs_lookup_rejects_nonofficial_even_if_primary_brand_domain(_web, _fetch, _save):
    _save.return_value = {"relative_path": "artifacts/outbox/trace.json", "bytes": 123}
    query = 'Find official OpenAI API rate limits documentation'
    _web.side_effect = [
        json.dumps({
            "query": query,
            "status": "ok",
            "backend": "serper",
            "sources": [
                {"title": "OpenAI blog", "url": "https://openai.com/index/new-api-updates/", "snippet": "api updates and limits summary"},
                {"title": "OpenAI docs", "url": "https://platform.openai.com/docs/guides/rate-limits", "snippet": "official docs rate limits"},
                {"title": "OpenAI reference", "url": "https://platform.openai.com/docs/api-reference/rate-limits", "snippet": "reference rate limits"},
            ],
            "answer": "",
            "error": None,
        }),
        json.dumps({"query": f"{query} recent", "status": "no_results", "backend": "serper", "sources": [], "answer": "", "error": None}),
        json.dumps({"query": f"{query} official docs", "status": "ok", "backend": "serper", "sources": [{"title": "OpenAI docs", "url": "https://platform.openai.com/docs/guides/rate-limits", "snippet": "official docs rate limits"}], "answer": "", "error": None}),
        json.dumps({"query": f"{query} reference guide", "status": "ok", "backend": "serper", "sources": [{"title": "OpenAI reference", "url": "https://platform.openai.com/docs/api-reference/rate-limits", "snippet": "reference rate limits"}], "answer": "", "error": None}),
    ]
    _fetch.side_effect = [
        {"url": "https://platform.openai.com/docs/guides/rate-limits", "status": "ok", "content_type": "text/html", "text_preview": "docs", "relevant_sections": ["docs"], "findings": [{"claim": "OpenAI documents API rate limits in the official guide.", "evidence_snippet": "The guide describes current API rate-limit behavior.", "source_url": "https://platform.openai.com/docs/guides/rate-limits", "source_type": "docs", "observed_at": "2026-03-18", "confidence_local": "high"}], "error": None},
        {"url": "https://platform.openai.com/docs/api-reference/rate-limits", "status": "ok", "content_type": "text/html", "text_preview": "reference", "relevant_sections": ["reference"], "findings": [{"claim": "The API reference links to official limit guidance.", "evidence_snippet": "The reference material points to the rate-limit guide.", "source_url": "https://platform.openai.com/docs/api-reference/rate-limits", "source_type": "docs", "observed_at": "2026-03-18", "confidence_local": "high"}], "error": None},
    ]

    class Ctx:
        drive_root = '/tmp'
        task_id = 'task-docs-official'
        current_chat_id = 1

    data = json.loads(_research_run(Ctx(), query))
    ranked = {}
    for page in data['visited_pages']:
        for item in page.get('ranked_sources', []):
            ranked[item['url']] = item
    assert ranked['https://openai.com/index/new-api-updates/']['authority'] == 'primary'
    assert ranked['https://openai.com/index/new-api-updates/']['decision'] == 'reject'
    assert 'selection-policy:official-needed' in ranked['https://openai.com/index/new-api-updates/']['reasons']
    assert ranked['https://platform.openai.com/docs/guides/rate-limits']['authority'] == 'official'
    assert all(item['authority'] == 'official' for item in data['candidate_sources'])


@patch('ouroboros.tools.search.save_artifact')
@patch('ouroboros.tools.search._read_page_findings')
@patch('ouroboros.tools.search._web_search')
def test_comparison_preferred_source_upgrade_rewards_vendor_compare_pages(_web, _fetch, _save):
    _save.return_value = {"relative_path": "artifacts/outbox/trace.json", "bytes": 123}
    query = 'Compare GitHub Copilot vs Cursor for developer workflows'
    _web.side_effect = [
        json.dumps({
            "query": query,
            "status": "ok",
            "backend": "serper",
            "sources": [
                {"title": "Generic roundup", "url": "https://medium.com/@dev/copilot-vs-cursor", "snippet": "comparison roundup opinions"},
                {"title": "GitHub Copilot docs", "url": "https://docs.github.com/en/copilot/get-started/what-is-github-copilot", "snippet": "official docs developer workflows"},
                {"title": "Cursor pricing", "url": "https://www.cursor.com/pricing", "snippet": "official pricing and product plans"},
            ],
            "answer": "",
            "error": None,
        }),
        json.dumps({"query": f"{query} recent", "status": "no_results", "backend": "serper", "sources": [], "answer": "", "error": None}),
        json.dumps({"query": f"{query} tradeoffs benchmark methodology independent results", "status": "ok", "backend": "serper", "sources": [{"title": "Cursor docs", "url": "https://docs.cursor.com/get-started/overview", "snippet": "official docs overview workflows"}], "answer": "", "error": None}),
        json.dumps({"query": f"{query} benchmark disagreement counterarguments", "status": "no_results", "backend": "serper", "sources": [], "answer": "", "error": None}),
    ]
    _fetch.side_effect = [
        {"url": "https://docs.github.com/en/copilot/get-started/what-is-github-copilot", "status": "ok", "content_type": "text/html", "text_preview": "copilot", "relevant_sections": ["copilot"], "findings": [{"claim": "GitHub documents Copilot workflow integration in official docs.", "evidence_snippet": "The docs explain where Copilot fits into developer workflows.", "source_url": "https://docs.github.com/en/copilot/get-started/what-is-github-copilot", "source_type": "docs", "observed_at": "2026-03-18", "confidence_local": "high"}], "error": None},
        {"url": "https://www.cursor.com/pricing", "status": "ok", "content_type": "text/html", "text_preview": "cursor", "relevant_sections": ["cursor"], "findings": [{"claim": "Cursor publishes official product-plan information on its pricing page.", "evidence_snippet": "The pricing page describes plan differences relevant to workflow comparison.", "source_url": "https://www.cursor.com/pricing", "source_type": "docs", "observed_at": "2026-03-18", "confidence_local": "medium"}], "error": None},
        {"url": "https://docs.cursor.com/get-started/overview", "status": "ok", "content_type": "text/html", "text_preview": "cursor docs", "relevant_sections": ["cursor docs"], "findings": [{"claim": "Cursor docs describe the product workflow model directly from the vendor.", "evidence_snippet": "The overview explains core workflow concepts.", "source_url": "https://docs.cursor.com/get-started/overview", "source_type": "docs", "observed_at": "2026-03-18", "confidence_local": "high"}], "error": None},
    ]

    class Ctx:
        drive_root = '/tmp'
        task_id = 'task-comparison-preferred'
        current_chat_id = 1

    data = json.loads(_research_run(Ctx(), query))
    top_urls = [item['url'] for item in data['candidate_sources'][:3]]
    assert 'https://docs.github.com/en/copilot/get-started/what-is-github-copilot' in top_urls
    assert 'https://docs.cursor.com/get-started/overview' in top_urls or 'https://www.cursor.com/pricing' in top_urls
    preferred_reasons = [reason for item in data['candidate_sources'][:3] for reason in item['reasons']]
    assert any('comparison-preferred-source:+0.8' in reason for reason in preferred_reasons)
    medium_entry = next(item for item in data['visited_pages'][0]['ranked_sources'] if item['url'] == 'https://medium.com/@dev/copilot-vs-cursor')
    assert 'comparison-aggregator:-0.6' in medium_entry['reasons']
