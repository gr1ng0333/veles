import json
import pathlib
import tempfile
from unittest.mock import patch

from ouroboros.tools.research_report import _parse_sources, _research_report
from ouroboros.tools.registry import ToolContext


class DummyLLM:
    def chat(self, messages, model, max_tokens):
        payload = {
            "title": "Тестовый отчёт",
            "summary": "Короткое резюме по теме.",
            "key_findings": ["Вывод 1", "Вывод 2", "Вывод 3"],
            "source_notes": [
                {"title": "Source A", "url": "https://example.com/a", "note": "Наблюдение A"},
                {"title": "Source B", "url": "https://example.com/b", "note": "Наблюдение B"},
            ],
            "limitations": ["Источники ограничены."],
            "conclusion": "Итоговый вывод.",
        }
        return {"content": json.dumps(payload, ensure_ascii=False)}, {"prompt_tokens": 10, "completion_tokens": 20, "cost": 0.01}


def make_ctx():
    tmp = pathlib.Path(tempfile.mkdtemp())
    return ToolContext(repo_dir=tmp, drive_root=tmp, current_chat_id=12345)


def test_parse_sources_from_search_answer():
    answer = """
1. **Source A**
   URL: https://example.com/a
   Alpha snippet.

2. **Source B**
   URL: https://example.com/b
   Beta snippet.
"""
    sources = _parse_sources(answer)
    assert len(sources) == 2
    assert sources[0].title == "Source A"
    assert sources[1].url == "https://example.com/b"


@patch('ouroboros.tools.research_report._get_llm_client', return_value=DummyLLM())
@patch('ouroboros.tools.research_report._search_web', return_value={"answer": """
1. **Source A**
   URL: https://example.com/a
   Alpha snippet.

2. **Source B**
   URL: https://example.com/b
   Beta snippet.
"""})
def test_research_report_writes_html_and_queues_document(_search, _llm):
    ctx = make_ctx()
    raw = _research_report(ctx, topic="test topic")
    result = json.loads(raw)

    assert result["status"] == "ok"
    report_path = pathlib.Path(result["report_path"])
    assert report_path.exists()
    html_text = report_path.read_text(encoding='utf-8')
    assert "Тестовый отчёт" in html_text
    doc_events = [event for event in ctx.pending_events if event.get("type") == "send_document"]
    assert doc_events
    assert doc_events[0].get("file_base64")
    assert doc_events[0].get("mime_type") == "text/html"
    assert any(event.get("type") == "llm_usage" for event in ctx.pending_events)
