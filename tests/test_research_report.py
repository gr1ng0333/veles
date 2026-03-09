import json
import pathlib
import tempfile
from unittest.mock import patch

from ouroboros.tools.research_report import (
    _build_prompt,
    _build_search_query,
    _normalize_sources,
    _research_report,
)
from ouroboros.tools.registry import ToolContext


class DummyLLM:
    def chat(self, messages, model, max_tokens):
        prompt = messages[0]['content']
        if 'Преобразуй тему для веб-поиска' in prompt:
            payload = {"query": "openclaw web search skills", "reason": "translate and add key terms"}
            return {"content": json.dumps(payload, ensure_ascii=False)}, {"prompt_tokens": 5, "completion_tokens": 7, "cost": 0.001}
        payload = {
            "title": "Тестовый отчёт",
            "summary": "Короткое резюме по теме. [1]",
            "key_findings": ["Вывод 1 [1]", "Вывод 2 [2]", "Вывод 3 [1][2]"],
            "source_notes": [
                {"title": "Source A", "url": "https://example.com/a", "note": "Наблюдение A"},
                {"title": "Source B", "url": "https://example.com/b", "note": "Наблюдение B"},
            ],
            "limitations": ["Источники ограничены."],
            "conclusion": "Итоговый вывод. [1]",
        }
        return {"content": json.dumps(payload, ensure_ascii=False)}, {"prompt_tokens": 10, "completion_tokens": 20, "cost": 0.01}


class BrokenLLM:
    def chat(self, messages, model, max_tokens):
        return {"content": "not-json"}, {"prompt_tokens": 7, "completion_tokens": 9, "cost": 0.005}


def make_ctx():
    tmp = pathlib.Path(tempfile.mkdtemp())
    return ToolContext(repo_dir=tmp, drive_root=tmp, current_chat_id=12345)


def test_normalize_sources_deduplicates_and_ranks_sources():
    raw_sources = [
        {"title": "Wiki", "url": "https://ru.wikipedia.org/wiki/Test", "snippet": "Alpha snippet."},
        {"title": "", "url": "https://example.com/a", "snippet": "Beta snippet with some length."},
        {"title": "Dup", "url": "https://example.com/a", "snippet": "dup"},
        {"title": "Bad", "url": "ftp://example.com/file", "snippet": "bad"},
        {"title": "Gov", "url": "https://data.gov/test", "snippet": "Official data source."},
    ]
    sources = _normalize_sources(raw_sources)
    assert len(sources) == 3
    assert sources[0].url == "https://data.gov/test"
    assert sources[1].url == "https://ru.wikipedia.org/wiki/Test"
    assert sources[2].title == "https://example.com/a"

def test_normalize_sources_filters_blocked_domains_and_prioritizes_docs_and_edu():
    raw_sources = [
        {"title": "Pinterest", "url": "https://pinterest.com/pin/1", "snippet": "noise"},
        {"title": "VK", "url": "https://vk.com/page", "snippet": "noise"},
        {"title": "GitHub Repo", "url": "https://github.com/org/repo", "snippet": "official code"},
        {"title": "Docs", "url": "https://docs.example.com/guide", "snippet": "official docs"},
        {"title": "University", "url": "https://cs.mit.edu/paper", "snippet": "research"},
        {"title": "Example", "url": "https://example.com/article", "snippet": "generic"},
    ]
    sources = _normalize_sources(raw_sources)
    urls = [source.url for source in sources]
    assert "https://pinterest.com/pin/1" not in urls
    assert "https://vk.com/page" not in urls
    assert urls[0] == "https://github.com/org/repo"
    assert "https://docs.example.com/guide" in urls


def test_normalize_sources_handles_empty_list():
    assert _normalize_sources([]) == []


@patch('ouroboros.tools.research_report._get_llm_client', return_value=DummyLLM())
def test_build_search_query_translates_russian_topic_via_llm(_llm):
    query = _build_search_query('скилы openclaw для веб поиска', 'anthropic/claude-haiku-4.5')
    assert query == 'openclaw web search skills'


def test_build_prompt_demands_citations_and_no_hallucinations():
    sources = _normalize_sources([
        {"title": "GitHub Repo", "url": "https://github.com/org/repo", "snippet": "official repo"},
        {"title": "Docs", "url": "https://docs.example.com/guide", "snippet": "official docs"},
    ])
    prompt = _build_prompt('test topic', sources, 'Андрей', 'briefing')
    assert 'Не галлюцинируй' in prompt
    assert 'обязан содержать ссылку вида [1], [2]' in prompt
    assert '[1] GitHub Repo — https://github.com/org/repo' in prompt




@patch('ouroboros.tools.research_report._get_llm_client', return_value=DummyLLM())
@patch('ouroboros.tools.research_report._search_web', return_value={
    "status": "ok",
    "backend": "serper",
    "error": None,
    "answer": "",
    "sources": [
        {"title": "Source A", "url": "https://example.com/a", "snippet": "Alpha snippet."},
        {"title": "Source B", "url": "https://example.com/b", "snippet": "Beta snippet."},
    ],
})
def test_research_report_writes_html_and_queues_document(_search, _llm):
    ctx = make_ctx()
    raw = _research_report(ctx, topic="test topic")
    result = json.loads(raw)

    assert result["status"] == "ok"
    report_path = pathlib.Path(result["report_path"])
    assert report_path.exists()
    html_text = report_path.read_text(encoding='utf-8')
    assert "Тестовый отчёт" in html_text
    assert "Диагностика поиска" in html_text
    assert "Таблица источников" in html_text
    assert "serper" in html_text
    doc_events = [event for event in ctx.pending_events if event.get("type") == "send_document"]
    assert doc_events
    assert doc_events[0].get("file_base64")
    assert doc_events[0].get("mime_type") == "text/html"
    usage_events = [event for event in ctx.pending_events if event.get("type") == "llm_usage"]
    assert usage_events
    assert "usage" in usage_events[0]
    assert usage_events[0]["usage"]["prompt_tokens"] == 10


@patch('ouroboros.tools.research_report._get_llm_client', return_value=DummyLLM())
@patch('ouroboros.tools.research_report._search_web', return_value={
    "status": "degraded",
    "backend": "serper",
    "error": "searx timeout",
    "answer": "fallback answer",
    "sources": [
        {"title": "Source A", "url": "https://example.com/a", "snippet": "Alpha snippet."},
    ],
})
def test_research_report_marks_degraded_but_still_builds_file(_search, _llm):
    ctx = make_ctx()
    raw = _research_report(ctx, topic="test topic")
    result = json.loads(raw)
    assert result["status"] == "degraded"
    html_text = pathlib.Path(result["report_path"]).read_text(encoding='utf-8')
    assert "Ограничения и надёжность" in html_text
    assert "serper" in html_text
    assert "fallback answer" in html_text


@patch('ouroboros.tools.research_report._get_llm_client', return_value=BrokenLLM())
@patch('ouroboros.tools.research_report._search_web', return_value={
    "status": "ok",
    "backend": "serper",
    "error": None,
    "answer": "",
    "sources": [
        {"title": "Source A", "url": "https://example.com/a", "snippet": "Alpha snippet."},
    ],
})
def test_research_report_falls_back_when_llm_json_is_invalid(_search, _llm):
    ctx = make_ctx()
    raw = _research_report(ctx, topic="test topic")
    result = json.loads(raw)
    assert result["status"] == "ok"
    html_text = pathlib.Path(result["report_path"]).read_text(encoding='utf-8')
    assert "Краткий отчёт: test topic" in html_text
    assert "деградированном режиме" in html_text


@patch('ouroboros.tools.research_report._search_web', return_value={
    "status": "error",
    "backend": "serper",
    "error": "backend timeout",
    "answer": "",
    "sources": [],
})
def test_research_report_returns_degraded_result_without_sources(_search):
    ctx = make_ctx()
    raw = _research_report(ctx, topic="test topic")
    result = json.loads(raw)

    assert result["status"] == "degraded"
    assert result["search"]["backend"] == "serper"
    assert result["error"]
