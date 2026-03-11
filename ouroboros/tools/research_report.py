from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
_MAX_SOURCES = 5


@dataclass
class ReportSource:
    title: str
    url: str
    snippet: str
    domain: str
    score: int


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


def _domain_from_url(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _score_source(title: str, url: str, snippet: str) -> int:
    score = 0
    domain = _domain_from_url(url)
    if domain:
        score += 20
        if domain.endswith(".edu") or ".edu." in domain:
            score += 15
        if domain.endswith(".gov") or ".gov." in domain:
            score += 15
        if domain.endswith(".org"):
            score += 8
        if domain.startswith("en.wikipedia.org") or domain.startswith("ru.wikipedia.org"):
            score += 5
    if title:
        score += min(len(title.strip()), 80) // 8
    if snippet:
        score += min(len(snippet.strip()), 240) // 24
    if url.startswith("https://"):
        score += 5
    return score


def _normalize_sources(raw_sources: Any) -> List[ReportSource]:
    normalized: List[ReportSource] = []
    seen_urls: set[str] = set()
    for item in raw_sources or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        snippet = str(item.get("snippet") or item.get("content") or "").strip()
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            continue
        if url in seen_urls:
            continue
        if not title:
            title = url
        domain = _domain_from_url(url)
        score = _score_source(title, url, snippet)
        normalized.append(ReportSource(title=title, url=url, snippet=snippet, domain=domain, score=score))
        seen_urls.add(url)

    normalized.sort(key=lambda src: (-src.score, src.url))
    return normalized[:_MAX_SOURCES]


def _build_prompt(topic: str, sources: List[ReportSource], audience: str, report_style: str) -> str:
    source_block = "\n\n".join(
        f"Source {i+1}:\nTitle: {s.title}\nURL: {s.url}\nDomain: {s.domain}\nQuality score: {s.score}\nSnippet: {s.snippet}"
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
        "key_findings": [s.snippet or s.title for s in sources[:3]]
        or ["Источники найдены, но краткие выводы нужно перечитать вручную."],
        "source_notes": [{"title": s.title, "url": s.url, "note": s.snippet or "Без дополнительной заметки."} for s in sources],
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




def _reliability_label(diagnostics: SearchDiagnostics, source_count: int) -> str:
    if diagnostics.status == "ok" and source_count >= 4:
        return "Высокая"
    if source_count:
        return "Средняя"
    return "Низкая"


def _limitation_notice(diagnostics: SearchDiagnostics) -> str:
    if diagnostics.status == "ok":
        return "Поиск дал достаточную основу для короткого отчёта."
    return "Поиск работал с деградацией; выводы стоит перепроверить по источникам."

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
    notes_by_url = {str(item.get("url", "")): item for item in source_notes if isinstance(item, dict)}
    for s in sources:
        item = notes_by_url.get(s.url, {})
        note = html.escape(str(item.get("note") or s.snippet or "Без дополнительной заметки."))
        notes_rows.append(
            f"<tr><td><a href='{html.escape(s.url)}'>{html.escape(s.title)}</a><br><small>{html.escape(s.domain or 'unknown')}</small></td><td>{s.score}</td><td>{note}</td></tr>"
        )

    diagnostics_items = [
        ("Статус поиска", diagnostics.status),
        ("Backend", diagnostics.backend),
        ("Ошибка", diagnostics.error or "—"),
        ("Сырой answer", diagnostics.answer or "—"),
    ]
    diagnostics_rows = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>" for label, value in diagnostics_items
    )

    reliability = _reliability_label(diagnostics, len(sources))
    limitation_notice = _limitation_notice(diagnostics)
    diagnostic_class = "good" if diagnostics.status == "ok" else "bad"

    return f"""<!doctype html>
<html lang='ru'>
<head>
  <meta charset='utf-8'>
  <title>{title}</title>
  <style>
    body {{ font-family: Inter, Arial, sans-serif; margin: 40px auto; max-width: 960px; color: #111827; line-height: 1.6; background: #f8fafc; }}
    .page {{ background: white; border-radius: 20px; padding: 36px 40px; box-shadow: 0 14px 40px rgba(15, 23, 42, 0.08); }}
    h1, h2 {{ color: #0f172a; }}
    h1 {{ margin-bottom: 8px; }}
    .meta {{ color: #475569; margin-bottom: 24px; }}
    .hero {{ background: linear-gradient(135deg, #e0f2fe, #ede9fe); border: 1px solid #cbd5e1; border-radius: 18px; padding: 20px 24px; margin-bottom: 22px; }}
    .grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 18px; }}
    .metric {{ background: rgba(255,255,255,0.7); border-radius: 14px; padding: 12px 14px; border: 1px solid rgba(148,163,184,0.35); }}
    .card {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 14px; padding: 18px 20px; margin: 18px 0; }}
    .diagnostic.bad {{ border-color: #fca5a5; background: #fef2f2; }}
    .diagnostic.good {{ border-color: #93c5fd; background: #eff6ff; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
    th, td {{ border: 1px solid #cbd5e1; padding: 10px; vertical-align: top; text-align: left; }}
    th {{ background: #e2e8f0; }}
    code {{ background: #eef2ff; padding: 2px 6px; border-radius: 6px; }}
    small {{ color: #64748b; }}
  </style>
</head>
<body>
  <div class='page'>
    <div class='hero'>
      <h1>{title}</h1>
      <div class='meta'>Тема: <code>{html.escape(topic)}</code> · Сформировано: {now}</div>
      <div class='grid'>
        <div class='metric'><strong>Источники</strong><br>{len(sources)}</div>
        <div class='metric'><strong>Надёжность</strong><br>{html.escape(reliability)}</div>
        <div class='metric'><strong>Поиск</strong><br>{html.escape(diagnostics.backend)} / {html.escape(diagnostics.status)}</div>
      </div>
    </div>

    <div class='card'>
      <h2>Краткое резюме</h2>
      <p>{summary}</p>
    </div>

    <div class='card'>
      <h2>Ключевые выводы</h2>
      <ul>{findings_html}</ul>
    </div>

    <div class='card'>
      <h2>Ограничения и надёжность</h2>
      <p>{html.escape(limitation_notice)}</p>
      <ul>{limitations_html}</ul>
    </div>

    <div class='card'>
      <h2>Таблица источников</h2>
      <table>
        <thead><tr><th>Источник</th><th>Оценка</th><th>Заметка</th></tr></thead>
        <tbody>{''.join(notes_rows)}</tbody>
      </table>
    </div>

    <div class='card diagnostic {diagnostic_class}'>
      <h2>Диагностика поиска</h2>
      <table>
        <tbody>{diagnostics_rows}</tbody>
      </table>
    </div>

    <div class='card'>
      <h2>Вывод</h2>
      <p>{conclusion}</p>
    </div>
  </div>
</body>
</html>
"""


def _render_markdown(payload: Dict[str, Any], topic: str, sources: List[ReportSource], diagnostics: SearchDiagnostics) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = str(payload.get("title") or f"Отчёт: {topic}")
    summary = str(payload.get("summary") or "").strip()
    conclusion = str(payload.get("conclusion") or "").strip()
    key_findings = [str(item) for item in (payload.get("key_findings") or [])]
    limitations = [str(item) for item in (payload.get("limitations") or [])]
    source_notes = payload.get("source_notes") or []
    notes_by_url = {str(item.get("url", "")): item for item in source_notes if isinstance(item, dict)}

    lines = [
        f"# {title}",
        "",
        f"- Тема: `{topic}`",
        f"- Сформировано: {now}",
        f"- Источники: {len(sources)}",
        f"- Надёжность: {_reliability_label(diagnostics, len(sources))}",
        f"- Поиск: `{diagnostics.backend}` / `{diagnostics.status}`",
        "",
        "## Краткое резюме",
        "",
        summary or "—",
        "",
        "## Ключевые выводы",
        "",
    ]
    if key_findings:
        lines.extend(f"- {item}" for item in key_findings)
    else:
        lines.append("- —")

    lines.extend([
        "",
        "## Ограничения и надёжность",
        "",
        _limitation_notice(diagnostics),
        "",
    ])
    if limitations:
        lines.extend(f"- {item}" for item in limitations)
    else:
        lines.append("- —")

    lines.extend([
        "",
        "## Источники",
        "",
    ])
    for idx, source in enumerate(sources, start=1):
        note_item = notes_by_url.get(source.url, {})
        note = str(note_item.get("note") or source.snippet or "Без дополнительной заметки.").strip()
        lines.extend([
            f"### {idx}. {source.title}",
            f"- URL: {source.url}",
            f"- Домен: {source.domain or 'unknown'}",
            f"- Оценка: {source.score}",
            f"- Заметка: {note}",
            "",
        ])

    lines.extend([
        "## Диагностика поиска",
        "",
        f"- Статус поиска: {diagnostics.status}",
        f"- Backend: {diagnostics.backend}",
        f"- Ошибка: {diagnostics.error or '—'}",
        f"- Сырой answer: {diagnostics.answer or '—'}",
        "",
        "## Вывод",
        "",
        conclusion or "—",
        "",
    ])
    return "\n".join(lines)


def _build_report_artifact(payload: Dict[str, Any], topic: str, sources: List[ReportSource], diagnostics: SearchDiagnostics, output_format: str) -> Tuple[str, str, str]:
    fmt = (output_format or "html").strip().lower()
    if fmt == "md":
        return _render_markdown(payload, topic=topic, sources=sources, diagnostics=diagnostics), "md", "text/markdown"
    return _render_html(payload, topic=topic, sources=sources, diagnostics=diagnostics), "html", "text/html"


def _safe_filename(topic: str, extension: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ_-]+", "-", topic).strip("-").lower()
    slug = slug[:60] or "report"
    ext = (extension or "html").strip(".").lower() or "html"
    return f"research-report-{slug}.{ext}"


def _research_report(
    ctx: ToolContext,
    topic: str,
    audience: str = "Андрей",
    report_style: str = "краткий аналитический briefing",
    search_query: str = "",
    deliver: bool = True,
    model: str = "",
    output_format: str = "html",
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
            "output_format": (output_format or "html").strip().lower() or "html",
            "mime_type": "",
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
    report_text, resolved_format, mime_type = _build_report_artifact(
        payload,
        topic=topic,
        sources=sources,
        diagnostics=diagnostics,
        output_format=output_format,
    )
    filename = _safe_filename(topic, resolved_format)

    report_dir = ctx.drive_path("reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    out_path = report_dir / filename
    out_path.write_text(report_text, encoding="utf-8")

    delivered = False
    if deliver and ctx.current_chat_id:
        ctx.pending_events.append(
            {
                "type": "send_document",
                "chat_id": int(ctx.current_chat_id or 0),
                "filename": filename,
                "caption": f"Research report: {topic}",
                "mime_type": mime_type,
                "file_base64": base64.b64encode(report_text.encode("utf-8")).decode("ascii"),
            }
        )
        delivered = True

    result = {
        "status": "ok" if diagnostics.status == "ok" else "degraded",
        "topic": topic,
        "query": query,
        "model": chosen_model,
        "sources": [s.__dict__ for s in sources],
        "report_path": str(out_path),
        "filename": filename,
        "delivered": delivered,
        "output_format": resolved_format,
        "mime_type": mime_type,
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
                    "Search the web, synthesize a short research report, render it as a polished HTML or Markdown file, "
                    "save it locally, and optionally send it to the current Telegram chat as a document."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string", "description": "Topic or question for the report"},
                        "audience": {"type": "string", "description": "Target audience for tone/level"},
                        "report_style": {"type": "string", "description": "Desired report style"},
                        "search_query": {"type": "string", "description": "Optional custom web search query"},
                        "deliver": {
                            "type": "boolean",
                            "description": "Whether to send the resulting HTML file to Telegram",
                            "default": True,
                        },
                        "model": {"type": "string", "description": "Optional LLM model override for synthesis"},
                        "output_format": {
                            "type": "string",
                            "enum": ["html", "md"],
                            "description": "Output file format for the saved and delivered report",
                            "default": "html"
                        },
                    },
                    "required": ["topic"],
                },
            },
            handler=_research_report,
            timeout_sec=120,
        )
    ]
