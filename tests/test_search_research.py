# Split from tests/test_search_tool.py to keep test modules readable.
import json
import pathlib
import queue
from unittest.mock import MagicMock, patch
import pytest
from ouroboros.tools.search import INTENT_POLICIES, _build_query_plan, _read_page_findings, _research_run, _web_search, get_tools
from ouroboros.tools.search_transport import run_discovery_transport
from ouroboros.tools.registry import ToolContext
QUERY_CASES = [('what happened with openai today', 'breaking_news'), ('latest news about anthropic', 'breaking_news'), ('что случилось сегодня с nvidia', 'breaking_news'), ('python 3.13 release date', 'fact_lookup'), ('how many parameters does llama 3 8b have', 'fact_lookup'), ('сколько контекста у claude 3.7', 'fact_lookup'), ('openai api rate limit official docs', 'product_docs_api_lookup'), ('anthropic sdk quickstart', 'product_docs_api_lookup'), ('документация telegram bot api endpoint sendDocument', 'product_docs_api_lookup'), ('compare fastapi vs django for internal tools', 'comparison_evaluation'), ('сравни claude и gpt для research', 'comparison_evaluation'), ('benchmark rust vs go web frameworks', 'comparison_evaluation'), ('explain what retrieval augmented generation is', 'background_explainer'), ('что такое vector database', 'background_explainer'), ('how does kv cache work', 'background_explainer'), ('anthropic founders and company history', 'people_company_ecosystem_tracking'), ('openai funding and leadership changes', 'people_company_ecosystem_tracking'), ('экосистема langchain и основные maintainers', 'people_company_ecosystem_tracking')]
@pytest.mark.parametrize(('query', 'side_effect', 'expected_intent', 'expected_policy', 'expected_subqueries', 'expected_first_url'), [('claude research mode', [json.dumps({'query': 'claude research mode', 'status': 'ok', 'backend': 'searxng', 'sources': [{'title': 'Anthropic', 'url': 'https://example.com/a', 'snippet': 'one'}, {'title': 'Community thread', 'url': 'https://reddit.com/r/claude', 'snippet': 'discussion'}], 'answer': '', 'error': None}), json.dumps({'query': 'claude research mode overview', 'status': 'ok', 'backend': 'searxng', 'sources': [{'title': 'Docs', 'url': 'https://example.com/b', 'snippet': 'two'}], 'answer': '', 'error': None}), json.dumps({'query': 'claude research mode common misconceptions', 'status': 'no_results', 'backend': 'searxng', 'sources': [], 'answer': '', 'error': None})], 'background_explainer', {'freshness_priority': 'low', 'search_branches': 3, 'min_sources_before_synthesis': 2, 'require_official_source': False}, 3, 'https://example.com/a'), ('openai api rate limit', [json.dumps({'query': 'openai api rate limit', 'status': 'ok', 'backend': 'searxng', 'sources': [{'title': 'Docs', 'url': 'https://platform.openai.com/docs', 'snippet': 'limits updated 2026'}, {'title': 'Roundup', 'url': 'https://news.google.com/articles/abc', 'snippet': 'aggregated summary'}], 'answer': '', 'error': None}), json.dumps({'query': 'openai api rate limit recent', 'status': 'ok', 'backend': 'searxng', 'sources': [{'title': 'Reference', 'url': 'https://platform.openai.com/docs/api-reference', 'snippet': 'reference updated 2026'}], 'answer': '', 'error': None}), json.dumps({'query': 'openai api rate limit official docs', 'status': 'ok', 'backend': 'searxng', 'sources': [{'title': 'Rate limits', 'url': 'https://platform.openai.com/docs/guides/rate-limits', 'snippet': 'official guide'}], 'answer': '', 'error': None}), json.dumps({'query': 'openai api rate limit reference guide', 'status': 'ok', 'backend': 'searxng', 'sources': [{'title': 'Forum post', 'url': 'https://reddit.com/r/openai/comments/1', 'snippet': 'i think the limit is...'}], 'answer': '', 'error': None})], 'product_docs_api_lookup', {'freshness_priority': 'medium', 'search_branches': 4, 'min_sources_before_synthesis': 2, 'require_official_source': True}, 4, 'https://platform.openai.com/docs/guides/rate-limits')])
@patch('ouroboros.tools.search.expand_search_queries', return_value=[])
@patch('ouroboros.tools.search.save_artifact')
@patch('ouroboros.tools.search._web_search')
def test_research_run_policy_trace_and_scored_candidates(_web, _save, _expand, query, side_effect, expected_intent, expected_policy, expected_subqueries, expected_first_url):
    _web.side_effect = side_effect
    _save.return_value = {'relative_path': 'artifacts/outbox/2026/03/17/task/json/research-run.json', 'bytes': 123}

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
    assert any(('selected_to_read' in page and 'rejected' in page for page in data['visited_pages']))
    assert any((page['ranked_sources'] for page in data['visited_pages'] if page['source_count']))
    assert data['transport']['discovery_backend'] == 'serper'
    assert data['transport']['reading_backend'] == 'urllib'
    assert isinstance(data['transport']['fallback_backends'], list)

@pytest.mark.parametrize(('query', 'web_side_effect', 'fetch_side_effect', 'assertion_mode'), [('openai api rate limit', [json.dumps({'query': 'openai api rate limit', 'status': 'ok', 'backend': 'searxng', 'sources': [{'title': 'Docs', 'url': 'https://platform.openai.com/docs/guides/rate-limits', 'snippet': 'Updated 2026-03-17. Tier 1 is 500 RPM.'}, {'title': 'Blog', 'url': 'https://blog.example.com/openai-rate-limit', 'snippet': 'Tier 1 is 300 RPM according to our writeup.'}], 'answer': '', 'error': None}), json.dumps({'query': 'openai api rate limit recent', 'status': 'no_results', 'backend': 'searxng', 'sources': [], 'answer': '', 'error': None}), json.dumps({'query': 'openai api rate limit official docs', 'status': 'no_results', 'backend': 'searxng', 'sources': [], 'answer': '', 'error': None}), json.dumps({'query': 'openai api rate limit reference guide', 'status': 'no_results', 'backend': 'searxng', 'sources': [], 'answer': '', 'error': None})], [{'url': 'https://platform.openai.com/docs/guides/rate-limits', 'status': 'ok', 'content_type': 'text/html', 'text_preview': 'Updated 2026-03-17. The API rate limit for tier 1 is 500 RPM.', 'relevant_sections': ['The API rate limit for tier 1 is 500 RPM.'], 'findings': [{'claim': 'The API rate limit for tier 1 is 500 RPM.', 'evidence_snippet': 'The API rate limit for tier 1 is 500 RPM.', 'source_url': 'https://platform.openai.com/docs/guides/rate-limits', 'source_type': 'docs', 'observed_at': '2026-03-17', 'confidence_local': 'high'}], 'error': None}, {'url': 'https://blog.example.com/openai-rate-limit', 'status': 'ok', 'content_type': 'text/html', 'text_preview': 'Our analysis says the API rate limit for tier 1 is 300 RPM.', 'relevant_sections': ['Our analysis says the API rate limit for tier 1 is 300 RPM.'], 'findings': [{'claim': 'The API rate limit for tier 1 is 300 RPM.', 'evidence_snippet': 'The API rate limit for tier 1 is 300 RPM.', 'source_url': 'https://blog.example.com/openai-rate-limit', 'source_type': 'blog', 'observed_at': '', 'confidence_local': 'medium'}], 'error': None}], 'contradictions'), ('what happened today openai release', [json.dumps({'query': 'what happened today openai release', 'status': 'ok', 'backend': 'searxng', 'sources': [{'title': 'Post 1', 'url': 'https://example.com/post-1', 'snippet': 'OpenAI released something today.'}, {'title': 'Post 2', 'url': 'https://example.com/post-2', 'snippet': 'A new release is discussed.'}], 'answer': '', 'error': None}), json.dumps({'query': 'what happened today openai release latest updates', 'status': 'no_results', 'backend': 'searxng', 'sources': [], 'answer': '', 'error': None}), json.dumps({'query': 'what happened today openai release timeline and reactions', 'status': 'no_results', 'backend': 'searxng', 'sources': [], 'answer': '', 'error': None}), json.dumps({'query': 'what happened today openai release conflicting reports', 'status': 'no_results', 'backend': 'searxng', 'sources': [], 'answer': '', 'error': None})], [{'url': 'https://example.com/post-1', 'status': 'ok', 'content_type': 'text/html', 'text_preview': 'OpenAI released a feature.', 'relevant_sections': ['OpenAI released a feature.'], 'findings': [{'claim': 'OpenAI released a feature.', 'evidence_snippet': 'OpenAI released a feature.', 'source_url': 'https://example.com/post-1', 'source_type': 'news', 'observed_at': '', 'confidence_local': 'high'}], 'error': None}, {'url': 'https://example.com/post-2', 'status': 'ok', 'content_type': 'text/html', 'text_preview': 'The release is discussed by multiple users.', 'relevant_sections': ['The release is discussed by multiple users.'], 'findings': [{'claim': 'The release is discussed by multiple users.', 'evidence_snippet': 'The release is discussed by multiple users.', 'source_url': 'https://example.com/post-2', 'source_type': 'news', 'observed_at': '', 'confidence_local': 'medium'}], 'error': None}], 'freshness')])
@patch('ouroboros.tools.search.save_artifact')
@patch('ouroboros.tools.search._read_page_findings')
@patch('ouroboros.tools.search._web_search')
def test_research_run_uncertainty_modes(_web, _fetch, _save, query, web_side_effect, fetch_side_effect, assertion_mode):
    _web.side_effect = web_side_effect
    _fetch.side_effect = fetch_side_effect
    _save.return_value = {'relative_path': 'artifacts/outbox/trace.json', 'bytes': 123}

    class Ctx:
        drive_root = '/tmp'
        task_id = 'task-u'
        current_chat_id = 1
    data = json.loads(_research_run(Ctx(), query))
    assert data['uncertainty_notes']
    assert data['uncertainty_notes']
    if assertion_mode == 'contradictions':
        assert data['contradictions']
        assert any((item['kind'] == 'numeric_mismatch' for item in data['contradictions']))
        assert 'Источники расходятся' in data['final_answer']
        assert data['confidence'] in {'low', 'medium'}
    else:
        assert data['freshness_summary']['known_dated_findings'] == 0
        assert any(('даты' in note.lower() or 'дата' in note.lower() for note in data['uncertainty_notes']))
        assert data['confidence'] == 'low'

@pytest.mark.parametrize(('query', 'expected_mode', 'expected_phrase'), [('python 3.13 release date', 'short_factual', 'Короткий ответ:'), ('compare fastapi vs django for internal tools', 'comparison_brief', 'Сопоставление подтверждённых утверждений:'), ('what happened today with openai release', 'timeline', 'Хронология/последовательность по прочитанным источникам:'), ('what is retrieval augmented generation', 'analyst_memo', 'Что подтверждают прочитанные источники:')])
@patch('ouroboros.tools.search.save_artifact')
@patch('ouroboros.tools.search._read_page_findings')
@patch('ouroboros.tools.search._web_search')
def test_research_run_synthesis_modes_and_evidence_trace(_web, _fetch, _save, query, expected_mode, expected_phrase):
    _save.return_value = {'relative_path': 'artifacts/outbox/trace.json', 'bytes': 123}
    _web.side_effect = [json.dumps({'query': query, 'status': 'ok', 'backend': 'searxng', 'sources': [{'title': 'Primary', 'url': 'https://example.com/a', 'snippet': 'Primary evidence snippet.'}, {'title': 'Secondary', 'url': 'https://example.com/b', 'snippet': 'Secondary evidence snippet.'}], 'answer': '', 'error': None}), json.dumps({'query': f'{query} recent', 'status': 'no_results', 'backend': 'searxng', 'sources': [], 'answer': '', 'error': None}), json.dumps({'query': f'{query} official docs', 'status': 'no_results', 'backend': 'searxng', 'sources': [], 'answer': '', 'error': None}), json.dumps({'query': f'{query} overview', 'status': 'no_results', 'backend': 'searxng', 'sources': [], 'answer': '', 'error': None}), json.dumps({'query': f'{query} contradictions', 'status': 'no_results', 'backend': 'searxng', 'sources': [], 'answer': '', 'error': None})]
    _fetch.side_effect = [{'url': 'https://example.com/a', 'status': 'ok', 'content_type': 'text/html', 'text_preview': 'alpha', 'relevant_sections': ['alpha'], 'findings': [{'claim': 'Primary claim for synthesis.', 'evidence_snippet': 'Primary evidence snippet.', 'source_url': 'https://docs.python.org/3/whatsnew/3.13.html', 'source_type': 'docs', 'observed_at': '2026-03-17', 'confidence_local': 'high'}], 'error': None}, {'url': 'https://example.com/b', 'status': 'ok', 'content_type': 'text/html', 'text_preview': 'beta', 'relevant_sections': ['beta'], 'findings': [{'claim': 'Secondary claim for synthesis.', 'evidence_snippet': 'Secondary evidence snippet.', 'source_url': 'https://www.python.org/downloads/release/python-3130/', 'source_type': 'news', 'observed_at': '', 'confidence_local': 'medium'}], 'error': None}]

    class Ctx:
        drive_root = '/tmp'
        task_id = 'task-s'
        current_chat_id = 1
    data = json.loads(_research_run(Ctx(), query))
    assert data['answer_mode'] == expected_mode
    assert data['synthesis']['answer_mode'] == expected_mode
    assert data['synthesis']['short_answer']
    assert data['synthesis']['key_findings']
    assert all((item['evidence_snippet'] and item['source_url'] for item in data['synthesis']['key_findings']))
    assert data['synthesis']['sources']
    assert expected_phrase in data['final_answer']
    assert 'evidence:' in data['final_answer']
    assert 'source:' in data['final_answer']

@patch('ouroboros.tools.search.save_artifact')
@patch('ouroboros.tools.search._read_page_findings')
@patch('ouroboros.tools.search._web_search')
@pytest.mark.parametrize(('query', 'budget_mode', 'task_id', 'serp_query', 'fetch_side_effect', 'assertion_mode'), [('python api limits', 'cheap', 'task-budget-cheap', 'python api limits', [{'url': f'https://docs.example.com/{idx}', 'status': 'ok', 'content_type': 'text/html', 'text_preview': 'limit docs', 'relevant_sections': ['limit docs'], 'findings': [{'claim': f'Claim {idx}', 'evidence_snippet': f'Evidence {idx}', 'source_url': f'https://docs.example.com/{idx}', 'source_type': 'docs', 'observed_at': '2026-03-18', 'confidence_local': 'medium'}], 'error': None} for idx in range(1, 7)], 'budget-cheap'), ('python api limits', 'deep', 'task-budget-deep', 'python api limits', [{'url': f'https://docs.example.com/{idx}', 'status': 'ok', 'content_type': 'text/html', 'text_preview': 'limit docs', 'relevant_sections': ['limit docs'], 'findings': [{'claim': f'Claim {idx}', 'evidence_snippet': f'Evidence {idx}', 'source_url': f'https://docs.example.com/{idx}', 'source_type': 'docs', 'observed_at': '2026-03-18', 'confidence_local': 'medium'}], 'error': None} for idx in range(1, 7)], 'budget-deep'), ('openai release today', 'balanced', 'task-stop', 'openai release today', [{'url': 'https://news.example.com/a', 'status': 'ok', 'content_type': 'text/html', 'text_preview': 'alpha', 'relevant_sections': ['alpha'], 'findings': [{'claim': 'OpenAI released feature X.', 'evidence_snippet': 'Release confirmed on the official blog.', 'source_url': 'https://news.example.com/a', 'source_type': 'news', 'observed_at': '2026-03-18', 'confidence_local': 'high'}], 'error': None}, {'url': 'https://news.example.com/b', 'status': 'ok', 'content_type': 'text/html', 'text_preview': 'beta', 'relevant_sections': ['beta'], 'findings': [{'claim': 'Feature X is now available.', 'evidence_snippet': 'Availability confirmed by rollout note.', 'source_url': 'https://news.example.com/b', 'source_type': 'news', 'observed_at': '2026-03-18', 'confidence_local': 'high'}], 'error': None}], 'early-stop')])
def test_research_run_budget_and_early_stop_behaviour(_web, _fetch, _save, query, budget_mode, task_id, serp_query, fetch_side_effect, assertion_mode):
    _save.return_value = {'relative_path': 'artifacts/outbox/trace.json', 'bytes': 123}
    serp = json.dumps({'query': serp_query, 'status': 'ok', 'backend': 'serper', 'sources': [{'title': 'One', 'url': 'https://docs.example.com/1', 'snippet': 'API rate limit docs'}, {'title': 'Two', 'url': 'https://docs.example.com/2', 'snippet': 'API rate limit docs'}, {'title': 'Three', 'url': 'https://docs.example.com/3', 'snippet': 'API rate limit docs'}, {'title': 'Four', 'url': 'https://docs.example.com/4', 'snippet': 'API rate limit docs'}, {'title': 'Five', 'url': 'https://docs.example.com/5', 'snippet': 'API rate limit docs'}] if 'python api limits' in serp_query else [{'title': 'A', 'url': 'https://news.example.com/a', 'snippet': 'Release confirmed today'}, {'title': 'B', 'url': 'https://news.example.com/b', 'snippet': 'Release confirmed today'}, {'title': 'C', 'url': 'https://news.example.com/c', 'snippet': 'Release confirmed today'}], 'answer': '', 'error': None})
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

def test_research_run_can_be_superseded_by_new_owner_request(tmp_path):
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, task_id='task-interrupt', current_chat_id=1, incoming_messages=queue.Queue())
    search_payload = json.dumps({'query': 'compare claude and gpt for research', 'status': 'ok', 'backend': 'serper', 'sources': [{'title': 'Official docs A', 'url': 'https://platform.openai.com/docs/guides/rate-limits', 'snippet': 'official docs'}, {'title': 'Official docs B', 'url': 'https://docs.anthropic.com/en/docs/overview', 'snippet': 'official docs'}], 'answer': '', 'error': None, 'transport': {'discovery_backend': 'serper', 'used_backend': 'serper', 'reading_backend': None, 'fallback_backend': None, 'events': [{'backend': 'serper', 'status': 'ok', 'stage': 'discovery', 'used': True, 'trigger': 'primary', 'reason': None}]}})
    read_result = {'status': 'ok', 'findings': [{'claim': 'claim from injected source', 'evidence_snippet': 'evidence', 'source_type': 'page', 'observed_at': '2026-03-19', 'confidence_local': 'high'}]}
    with patch('ouroboros.tools.search._web_search', return_value=search_payload), patch('ouroboros.tools.search._read_page_findings', side_effect=lambda _query, source, timeout_sec=15: (ctx.incoming_messages.put('сравни лучше ещё с Gemini и перезапусти'), {**read_result, 'url': source.get('url'), 'findings': [{**read_result['findings'][0], 'claim': f'claim from {source.get('url')}', 'source_url': source.get('url')}]})[1]), patch('ouroboros.tools.search.save_artifact', return_value={'relative_path': 'artifacts/outbox/trace.json', 'bytes': 1}):
        payload = json.loads(_research_run(ctx, 'compare claude and gpt for research', budget_mode='balanced', output_mode='comparison'))
    assert payload['interrupted'] is True
    assert payload['status'] == 'superseded_by_new_request'
    assert payload['interrupt_reason'] == 'superseded_by_new_request'
    assert payload['interrupt_stage'] == 'page_read_complete'
    assert 'новый запрос владельца' in payload['final_answer'].lower()
    assert payload['budget_trace']['pages_read'] == 1
    assert len(payload['findings']) == 1
    assert any((event.get('type') == 'tool_interrupt_checkpoint' and event.get('reason') == 'superseded_by_new_request' for event in ctx.pending_events))

@pytest.mark.parametrize('timeout_kind', ['discovery', 'page_read'])
@patch('ouroboros.tools.search.save_artifact', return_value={'relative_path': 'artifacts/outbox/trace.json', 'bytes': 1})
@patch('ouroboros.tools.search._read_page_findings')
@patch('ouroboros.tools.search._web_search')
def test_research_run_records_timeout_events(_web, _fetch, _save, timeout_kind):
    if timeout_kind == 'discovery':
        payload = run_discovery_transport('timeout query', lambda _query: {'status': 'timeout', 'backend': 'serper', 'sources': [], 'answer': '', 'error': 'discovery_timeout', 'timeout_limit': 20}, [('searxng', lambda _query: {'status': 'ok', 'sources': [{'title': 'Fallback', 'url': 'https://example.com/fallback', 'snippet': 'ok'}], 'answer': '', 'error': None})])
        assert payload['status'] == 'ok'
        assert payload['backend'] == 'searxng'
        assert payload['transport']['events'][0]['status'] == 'timeout'
        assert payload['transport']['events'][0]['reason'] == 'discovery_timeout'
        assert payload['transport']['events'][0]['timeout_limit'] == 20
        assert payload['transport']['events'][1]['trigger'] == 'serper_timeout'

    class Ctx:
        drive_root = '/tmp'
        task_id = f'task-timeout-{timeout_kind}'
        current_chat_id = 1
    _web.side_effect = [json.dumps({'query': 'openai api rate limit', 'status': 'timeout' if timeout_kind == 'discovery' else 'ok', 'backend': 'serper', 'sources': [] if timeout_kind == 'discovery' else [{'title': 'Docs', 'url': 'https://platform.openai.com/docs/guides/rate-limits', 'snippet': 'official'}], 'answer': '', 'error': 'discovery_timeout' if timeout_kind == 'discovery' else None, 'timeout_limit': 20 if timeout_kind == 'discovery' else None}), json.dumps({'query': 'openai api rate limit recent', 'status': 'no_results', 'backend': 'serper', 'sources': [], 'answer': '', 'error': None}), json.dumps({'query': 'openai api rate limit official docs', 'status': 'no_results', 'backend': 'serper', 'sources': [], 'answer': '', 'error': None}), json.dumps({'query': 'openai api rate limit reference guide', 'status': 'no_results', 'backend': 'serper', 'sources': [], 'answer': '', 'error': None})]
    _fetch.return_value = {'url': 'https://platform.openai.com/docs/guides/rate-limits', 'status': 'timeout', 'content_type': '', 'text_preview': '', 'relevant_sections': [], 'findings': [], 'error': 'page_read_timeout', 'timeout_limit': 15}
    data = json.loads(_research_run(Ctx(), 'openai api rate limit'))
    expected_error = 'discovery_timeout' if timeout_kind == 'discovery' else 'page_read_timeout'
    assert any((item['error_type'] == expected_error for item in data['timeout_events']))
    assert data['timeout_profile']['overall_run_timeout_sec'] >= data['timeout_profile']['discovery_timeout_sec']
    assert data['discovery_backend_used'] == 'serper'
    if timeout_kind == 'discovery':
        assert any((event.get('status') == 'timeout' for event in data['transport']['events']))
    else:
        assert data['transport']['reading_backend'] == 'urllib'
        assert data['reading_backend_used'] == 'urllib'
        assert data['fallback_chain'] == ['serper']
        assert data['pages_attempted'] == 1
        assert data['pages_succeeded'] == 0
        assert data['pages_failed'] == 1
        assert data['degraded_mode'] is True
        assert data['owner_interrupt_seen'] is False
        assert len(data['interruption_checks']) >= 2
        summary = data['debug_summary']
        assert summary['discovery_backend_used'] == 'serper'
        assert summary['reading_backend_used'] == 'urllib'
        assert summary['pages_attempted'] == 1
        assert summary['pages_succeeded'] == 0
        assert summary['pages_failed'] == 1
        assert summary['degraded_mode'] is True
        read_results = data['visited_pages'][0]['read_results']
        assert read_results
        reasons = read_results[0]['read_reason']
        assert any(('selected-for-reading:score=' in item for item in reasons))
        assert read_results[0]['browser_used'] is False
        assert 'browser_not_used' in read_results[0]['browser_reason']

@patch('ouroboros.tools.search.save_artifact')
@patch('ouroboros.tools.search._read_page_findings')
@patch('ouroboros.tools.search._web_search')
def test_research_run_tracks_query_signals_in_candidate_sources(_web, _fetch, _save):
    _save.return_value = {'relative_path': 'artifacts/outbox/trace.json', 'bytes': 123}
    _web.side_effect = [json.dumps({'query': 'openai api rate limit', 'status': 'ok', 'backend': 'searxng', 'sources': [{'title': 'OpenAI API docs', 'url': 'https://platform.openai.com/docs/guides/rate-limits', 'snippet': 'latest updated 2026 rate limits'}, {'title': 'OpenAI API docs mirror', 'url': 'https://platform.openai.com/docs/guides/rate-limits/', 'snippet': 'latest updated 2026 rate limits'}], 'answer': '', 'error': None}), json.dumps({'query': 'openai api rate limit recent', 'status': 'no_results', 'backend': 'searxng', 'sources': [], 'answer': '', 'error': None}), json.dumps({'query': 'openai api rate limit platform.openai.com openai official docs api reference vendor documentation', 'status': 'no_results', 'backend': 'searxng', 'sources': [], 'answer': '', 'error': None}), json.dumps({'query': 'openai api rate limit platform.openai.com openai reference guide vendor documentation api reference', 'status': 'no_results', 'backend': 'searxng', 'sources': [], 'answer': '', 'error': None})]
    _fetch.return_value = {'url': 'https://platform.openai.com/docs/guides/rate-limits', 'status': 'ok', 'content_type': 'text/html', 'text_preview': 'docs', 'relevant_sections': ['rate limits'], 'findings': [], 'error': None}

    class Ctx:
        drive_root = '/tmp'
        task_id = 'task-query-signals'
        current_chat_id = 1
    data = json.loads(_research_run(Ctx(), 'openai api rate limit'))
    assert data['query_plan']['query_type'] == 'docs_api'
    assert data['candidate_sources'][0]['query_type'] == 'docs_api'
    assert data['candidate_sources'][0]['signal_trace']['recency'] > 0
    assert data['candidate_sources'][0]['dedupe_signature']
    assert data['candidate_sources'][0]['signal_trace']['relevance'] > 0
