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
def test_run_discovery_transport_trace_honesty():
    payload = run_discovery_transport('test query', lambda _query: {'status': 'error', 'backend': 'serper', 'sources': [], 'answer': '', 'error': 'serper down'}, [('searxng', lambda _query: {'status': 'no_results', 'sources': [], 'answer': '', 'error': 'empty'}), ('openai', lambda _query: {'status': 'ok', 'sources': [{'title': 'Backup', 'url': 'https://example.com/openai', 'snippet': 'backup'}], 'answer': '', 'error': None})])
    assert payload['backend'] == 'openai'
    assert payload['transport']['discovery_backend'] == 'serper'
    assert payload['transport']['used_backend'] == 'openai'
    assert payload['transport']['fallback_backend'] == 'openai'
    assert [event['backend'] for event in payload['transport']['events']] == ['serper', 'searxng', 'openai']
    assert payload['transport']['events'][1]['trigger'] == 'serper_error'
    assert payload['transport']['events'][2]['trigger'] == 'searxng_no_results'
