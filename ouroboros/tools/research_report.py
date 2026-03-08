from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
_MAX_SOURCES = 5


@dataclass
class ReportSource:
    title: str
    url: str
    snippet: str


@dataclass
class SearchDiagnostics:
    status: str
    backend: str
    error: str
    answer: str


def _get_llm_client():
    from ouroboros.llm import LLMClient
    return LLMClient()


def _emit_usage(ctx: ToolContext, usage: Dict[str, Any], model: str) -> None:
    if not usage:
        return
    event = {
        "type": "llm_usage",
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "task_id": ctx.task_id,
        "task_type": ctx.current_task_type or "task",
        "category": "research_report",
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "cached_tokens": usage.get("cached_tokens", 0),
            "cost": usage.get("cost", 0.0),
        },
    }
    if ctx.event_queue is not None:
        try:
            ctx.event_queue.put_nowait(event)
            return
        except Exception:
            log.debug("Failed to emit llm_usage to event_queue", exc_info=True)
    ctx.pending_events.append(event)


def _search_web(query: str) -> Dict[str, Any]:
    from ouroboros.tools.search import _web_search

    raw = _web_search(None, query)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {
        "query": query,
        "status": "error",
        "backend": "unknown",
        "sources": [],
        "answer": str(raw),
        "error": "web_search returned non-JSON response",
    }


def _normalize_sources(raw_sources: Any) -> List[ReportSource]:
    sources: List[ReportSource] = []
    for item in raw_sources or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        snippet = str(item.get("snippet") or item.get("content") or "").strip()
        if not url:
            continue
        if not title:
            title = url
        sources.append(ReportSource(title=title, url=url, snippet=snippet))
        if len(sources) >= _MAX_SOURCES:
            break
    return sources


def _build_prompt(topic: str, sources: List[ReportSource], audience: str, report_style: str) -> str:
    source_block = "\n\n".join(
        f"Source {i+1}:\nTitle: {s.title}\nURL: {s.url}\nSnippet: {s.snippet}"
        for i, s in enumerate(sources)
    )
    return (
        "Ты готовишь краткий исследовательский отчёт на русском языке. "
        "Работай строго по источникам ниже, не выдумывай факты. "
        "Если данных мало или есть сомнения — прямо скажи это.\n\n"
        f"Тема: {topic}\n"
        f"Аудитория: {audience}\n"
        f"Стиль: {report_style}\n\n"
        "Верни JSON-объект со структурой:\n"
        "{\n"
        '  "title": string,\n'
        '  "summary": string,\n'
        '  "key_findings": [string, string, string],\n'
        '  "source_notes": [{"title": string, "url": string, "note": string}],\n'
        '  "limitations": [string],\n'
        '  "conclusion": string\n'
        "}\n\n"
        "Источники:\n"
        f"{source_block}"
    )


def _fallback_payload(topic: str, sources: List[ReportSource], diagnostics: SearchDiagnostics) -> Dict[str, Any]:
    limitations: List[str] = []
    if diagnostics.status != "ok":
        limitations.append(f"Поиск отработал со статусом {diagnostics.status} через backend {diagnostics.backend}.")
    if diagnostics.error:
        limitations.append(f"Ошибка поиска: {diagnostics.error}")
    limitations.append("LLM-синтез не вернул валидный JSON, поэтому отчёт собран в деградированном режиме.")
    return {
        "title": f"Краткий отчёт: {topic}",
        "summary": "Автоматический синтез не сработал, поэтому отдаю базовую сводку по найденным источникам.",
        "key_findings": [s.snippet or s.title for s in sources[:3]] or ["Источники найдены, но краткие выводы нужно перечитать вручную."],
        "source_notes": [
            {"title": s.title, "url": s.url, "note": s.snippet or "Без дополнительной заметки."}
            for s in sources
        ],
        "limitations": limitations,
        "conclusion": "Для MVP это приемлемо: файл всё равно доставляется, а источники и диагностика не теряются.",
    }


def _generate_payload(
    ctx: ToolContext,
    topic: str,
    sources: List[ReportSource],
    audience: str,
    report_style: str,
    model: str,
    diagnostics: SearchDiagnostics,
) -> Dict[str, Any]:
    prompt = _build_prompt(topic, sources, audience, report_style)
    client = _get_llm_client()
    response, usage = client.chat(
        messages=[{"role": "user", "content": prompt}],
        model=model,
        max_tokens=1800,
    )
    _emit_usage(ctx, usage or {}, model)
    content = (response or {}).get("content", "")
    if not content:
        return _fallback_payload(topic, sources, diagnostics)
    try:
        return json.loads(content)
    except Exception:
        fenced = re.search(r"```json\s*(\{.*?\})\s*```", content, re.S)
        if fenced:
            try:
                return json.loads(fenced.group(1))
            except Exception:
                pass
        inline = re.search(r"(\{.*\})", content, re.S)
        if inline:
            try:
                return json.loads(inline.group(1))
            except Exception:
                pass
    return _fallback_payload(topic, sources, diagnostics)


def _render_html(payload: Dict[str, Any], topic: str, sources: List[ReportSource], diagnostics: SearchDiagnostics) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = html.escape(payload.get("title") or f"Отчёт: {topic}")
    summary = html.escape(payload.get("summary") or "")
    conclusion = html.escape(payload.get("conclusion") or "")
    key_findings = payload.get("key_findings") or []
    source_notes = payload.get("source_notes") or []
    limitations = payload.get("limitations") or []

    findings_html = "".join(f"<li>{html.escape(str(item))}</li>" for item in key_findings)
    limitations_html = "".join(f"<li>{html.escape(str(item))}</li>" for item in limitations)

    notes_rows = []
    for item in source_notes:
        url = html.escape(str(item.get("url", "")))
        src_title = html.escape(str(item.get("title", "Источник")))
        note = html.escape(str(item.get("note", "")))
        notes_rows.append(f"<tr><td><a href='{url}'>{src_title}</a></td><td>{note}</td></tr>")
    if not notes_rows:
        for s in sources:
            notes_rows.append(
                f"<tr><td><a href='{html.escape(s.url)}'>{html.escape(s.title)}</a></td><td>{html.escape(s.snippet)}</td></tr>"
            )

    diagnostics_items = [
        ("Статус поиска", diagnostics.status),
        ("Backend", diagnostics.backend),
        ("Ошибка", diagnostics.error or "—"),
        ("Сырой answer", diagnostics.answer or "—"),
    ]
    diagnostics_rows = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>"
        for label, value in diagnostics_items
    )

    return f"""<!doctype html>
<html lang='ru'>
<head>
  <meta charset='utf-8'>
  <title>{title}</title>
  <style>
    body {{ font-family: Inter, Arial, sans-serif; margin: 40px auto; max-width: 920px; color: #111827; line-height: 1.55; }}
    h1, h2 {{ color: #0f172a; }}
    .meta {{ color: #475569; margin-bottom: 24px; }}
    .card {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 14px; padding: 18px 20px; margin: 18px 0; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
    th, td {{ border: 1px solid #cbd5e1; padding: 10px; vertical-align: top; text-align: left; }}
    th {{ background: #e2e8f0; }}
    code {{ background: #eef2ff; padding: 2px 6px; border-radius: 6px; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <div class='meta'>Тема: <code>{html.escape(topic)}</code> · Сформировано: {now} · Источников: {len(sources)}</div>

  <div class='card'>
    <h2>Краткое резюме</h2>
    <p>{summary}</p>
  </div>

  <div class='card'>
    <h2>Ключевые выводы</h2>
    <ul>{findings_html}</ul>
  </div>

  <div class='card'>
    <h2>Разбор источников</h2>
    <table>
      <thead><tr><th>Источник</th><th>Заметка</th></tr></thead>
      <tbody>{''.join(notes_rows)}</tbody>
    </table>
  </div>

  <div class='card'>
    <h2>Диагностика поиска</h2>
    <table>
      <tbody>{diagnostics_rows}</tbody>
    </table>
  </div>

  <div class='card'>
    <h2>Ограничения</h2>
    <ul>{limitations_html}</ul>
  </div>

  <div class='card'>
    <h2>Вывод</h2>
    <p>{conclusion}</p>
  </div>
</body>
</html>
"""


def _safe_filename(topic: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ_-]+", "-", topic).strip("-").lower()
    slug = slug[:60] or "report"
    return f"research-report-{slug}.html"


def _research_report(
    ctx: ToolContext,
    topic: str,
    audience: str = "Андрей",
    report_style: str = "краткий аналитический briefing",
    search_query: str = "",
    deliver: bool = True,
    model: str = "",
) -> str:
    query = (search_query or topic).strip()
    if not query:
        return "⚠️ topic is required"

    search_result = _search_web(query)
    diagnostics = SearchDiagnostics(
        status=str(search_result.get("status") or "unknown"),
        backend=str(search_result.get("backend") or "unknown"),
        error=str(search_result.get("error") or "").strip(),
        answer=str(search_result.get("answer") or "").strip(),
    )
    sources = _normalize_sources(search_result.get("sources"))

    if not sources:
        result = {
            "status": "degraded",
            "topic": topic,
            "query": query,
            "model": "",
            "sources": [],
            "report_path": "",
            "filename": "",
            "delivered": False,
            "title": "",
            "summary": "",
            "search": {
                "status": diagnostics.status,
                "backend": diagnostics.backend,
                "error": diagnostics.error,
                "answer": diagnostics.answer,
            },
            "error": "Поиск не вернул структурированных источников",
        }
        return json.dumps(result, ensure_ascii=False)

    chosen_model = model or os.environ.get("OUROBOROS_MODEL_LIGHT", _DEFAULT_MODEL) or _DEFAULT_MODEL
    payload = _generate_payload(
        ctx,
        topic=topic,
        sources=sources,
        audience=audience,
        report_style=report_style,
        model=chosen_model,
        diagnostics=diagnostics,
    )
    html_report = _render_html(payload, topic=topic, sources=sources, diagnostics=diagnostics)
    filename = _safe_filename(topic)

    report_dir = ctx.drive_path("reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    out_path = report_dir / filename
    out_path.write_text(html_report, encoding="utf-8")

    delivered = False
    if deliver and ctx.current_chat_id:
        ctx.pending_events.append({
            "type": "send_document",
            "chat_id": int(ctx.current_chat_id or 0),
            "filename": filename,
            "caption": f"Research report: {topic}",
            "mime_type": "text/html",
            "file_base64": base64.b64encode(html_report.encode("utf-8")).decode("ascii"),
        })
        delivered = True

    result = {
        "status": "ok",
        "topic": topic,
        "query": query,
        "model": chosen_model,
        "sources": [s.__dict__ for s in sources],
        "report_path": str(out_path),
        "filename": filename,
        "delivered": delivered,
        "title": payload.get("title", ""),
        "summary": payload.get("summary", ""),
        "search": {
            "status": diagnostics.status,
            "backend": diagnostics.backend,
            "error": diagnostics.error,
            "answer": diagnostics.answer,
        },
    }
    return json.dumps(result, ensure_ascii=False)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="research_report",
            schema={
                "name": "research_report",
                "description": (
                    "Search the web, synthesize a short research report, render it as a polished HTML file, "
                    "save it locally, and optionally send it to the current Telegram chat as a document."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string", "description": "Topic or question for the report"},
                        "audience": {"type": "string", "description": "Target audience for tone/level"},
                        "report_style": {"type": "string", "description": "Desired report style"},
                        "search_query": {"type": "string", "description": "Optional custom web search query"},
                        "deliver": {"type": "boolean", "description": "Whether to send the resulting HTML file to Telegram", "default": True},
                        "model": {"type": "string", "description": "Optional LLM model override for synthesis"},
                    },
                    "required": ["topic"],
                },
            },
            handler=_research_report,
            timeout_sec=120,
        )
    ]
