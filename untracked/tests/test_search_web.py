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
@pytest.mark.parametrize(('serper_result', 'searx_result', 'openai_result', 'expected_backend', 'expected_fallback_backend', 'expected_event_backends', 'expected_trigger', 'expected_source_url'), [({'query': 'openai api rate limit', 'status': 'ok', 'backend': 'serper', 'sources': [{'title': 'Docs', 'url': 'https://platform.openai.com/docs/guides/rate-limits', 'snippet': 'official docs'}], 'answer': '', 'error': None}, {'query': 'openai api rate limit', 'status': 'ok', 'backend': 'searxng', 'sources': [{'title': 'Fallback', 'url': 'https://example.com/fallback', 'snippet': 'fallback'}], 'answer': '', 'error': None}, {'query': 'openai api rate limit', 'status': 'ok', 'backend': 'openai', 'sources': [{'title': 'Backup', 'url': 'https://example.com/openai', 'snippet': 'backup'}], 'answer': '', 'error': None}, 'serper', None, ['serper'], None, 'https://platform.openai.com/docs/guides/rate-limits'), ({'query': 'test', 'status': 'no_results', 'backend': 'serper', 'sources': [], 'answer': '', 'error': 'Serper returned no usable results.'}, {'query': 'test', 'status': 'ok', 'backend': 'searxng', 'sources': [{'title': 'Fallback', 'url': 'https://example.com/a', 'snippet': 'fallback'}], 'answer': '', 'error': None}, {'query': 'test', 'status': 'ok', 'backend': 'openai', 'sources': [{'title': 'Backup', 'url': 'https://example.com/b', 'snippet': 'backup'}], 'answer': '', 'error': None}, 'searxng', 'searxng', ['serper', 'searxng'], 'serper_no_results', 'https://example.com/a'), ({'query': 'test', 'status': 'error', 'backend': 'serper', 'sources': [], 'answer': '', 'error': 'boom'}, {'query': 'test', 'status': 'error', 'backend': 'searxng', 'sources': [], 'answer': '', 'error': 'still boom'}, {'query': 'test', 'status': 'ok', 'backend': 'openai', 'sources': [{'title': 'OpenAI', 'url': 'https://example.com/b', 'snippet': 'backup'}], 'answer': '', 'error': None}, 'openai', 'openai', ['serper', 'searxng', 'openai'], 'searxng_error', 'https://example.com/b')])
def test_web_search_transport_paths(serper_result, searx_result, openai_result, expected_backend, expected_fallback_backend, expected_event_backends, expected_trigger, expected_source_url):
    query = serper_result['query']
    payload = run_discovery_transport(query, lambda _query: serper_result, [('searxng', lambda _query: searx_result), ('openai', lambda _query: openai_result)])
    assert payload['status'] == 'ok'
    assert payload['backend'] == expected_backend
    assert payload['sources'][0]['url'] == expected_source_url
    assert payload['transport']['discovery_backend'] == 'serper'
    assert payload['transport']['used_backend'] == expected_backend
    assert payload['transport']['fallback_backend'] == expected_fallback_backend
    assert [event['backend'] for event in payload['transport']['events']] == expected_event_backends
    if expected_trigger is None:
        assert len(payload['transport']['events']) == 1
    else:
        assert payload['transport']['events'][-1]['trigger'] == expected_trigger

@patch('os.environ.get')
@patch('urllib.request.urlopen')
def test_web_search_serper_success_normalizes_sources(mock_urlopen, mock_env_get):

    class _Resp:

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({'organic': [{'title': 'Docs', 'link': 'https://platform.openai.com/docs/guides/rate-limits', 'snippet': 'official docs'}, {'title': 'Docs duplicate', 'link': 'https://platform.openai.com/docs/guides/rate-limits', 'snippet': 'duplicate'}, {'title': 'Bad', 'link': 'javascript:void(0)', 'snippet': 'bad'}], 'answerBox': {'answer': 'Rate limits depend on tier.'}}).encode()
    mock_urlopen.return_value = _Resp()
    mock_env_get.side_effect = lambda key, default=None: {'SERPER_API_KEY': 'test-key', 'SERPER_URL': 'https://google.serper.dev/search'}.get(key, default)
    payload = json.loads(_web_search(ToolContext(repo_dir='/tmp', drive_root='/tmp', task_id='task-1', current_chat_id=1), 'openai rate limits'))
    assert payload['status'] == 'ok'
    assert payload['backend'] == 'serper'
    assert payload['answer'] == 'Rate limits depend on tier.'
    assert payload['sources'] == [{'title': 'Docs', 'url': 'https://platform.openai.com/docs/guides/rate-limits', 'snippet': 'official docs'}]
