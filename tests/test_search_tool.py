import json
import pytest
from unittest.mock import patch

from ouroboros.tools.search import (
    INTENT_POLICIES,
    _build_query_plan,
    _classify_intent,
    _clean_sources,
    _dedupe_nonempty_queries,
    _merge_search_results,
    _research_run,
    _web_search,
)


def test_search_result_contract_helpers():
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
                    "sources": [{"title": "Anthropic", "url": "https://example.com/a", "snippet": "one"}],
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
                    "query": "claude research mode official source",
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
                    "sources": [{"title": "Docs", "url": "https://platform.openai.com/docs", "snippet": "limits"}],
                    "answer": "",
                    "error": None,
                }),
                json.dumps({
                    "query": "openai api rate limit recent",
                    "status": "ok",
                    "backend": "searxng",
                    "sources": [{"title": "Reference", "url": "https://platform.openai.com/docs/api-reference", "snippet": "reference"}],
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
            ],
            'product_docs_api_lookup',
            {
                'freshness_priority': 'medium',
                'search_branches': 4,
                'min_sources_before_synthesis': 2,
                'require_official_source': True,
            },
            4,
            'https://platform.openai.com/docs',
        ),
    ],
)
@patch('ouroboros.tools.search.save_artifact')
@patch('ouroboros.tools.search._web_search')
def test_research_run_policy_and_trace_contract(_web, _save, query, side_effect, expected_intent, expected_policy, expected_subqueries, expected_first_url):
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
    assert data['intent_policy'] == expected_policy
    assert data['trace']['relative_path'].endswith('.json')
    assert data['query_plan']['branch_budget'] == expected_subqueries


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


@pytest.mark.parametrize(
    ('query', 'intent_type', 'expected_budget'),
    [
        ('openai api rate limit', 'product_docs_api_lookup', 4),
        ('claude vs gpt for research', 'comparison_evaluation', 4),
        ('what happened today with xAI', 'breaking_news', 4),
        ('what is retrieval augmented generation', 'background_explainer', 3),
    ],
)
def test_query_planner_generates_bounded_nonempty_branches(query, intent_type, expected_budget):
    plan = _build_query_plan(query, intent_type)
    assert plan.branch_budget == expected_budget
    assert 3 <= len(plan.subqueries) <= 6
    assert len(plan.subqueries) == expected_budget
    assert all(item.strip() for item in plan.subqueries)
    assert len({item.casefold() for item in plan.subqueries}) == len(plan.subqueries)
    assert plan.primary_query == query


@pytest.mark.parametrize(
    ('candidates', 'limit', 'expected'),
    [
        ([' test ', '', 'TEST', 'test  ', 'other query'], 5, ['test', 'other query']),
        (['a', 'b', 'c', 'd'], 3, ['a', 'b', 'c']),
        (['', '   '], 6, []),
    ],
)
def test_query_planner_dedupes_and_drops_empty_queries(candidates, limit, expected):
    assert _dedupe_nonempty_queries(candidates, limit=limit) == expected
