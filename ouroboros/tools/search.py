"""Web search tool — SearXNG primary, OpenAI fallback."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from ouroboros.artifacts import save_artifact
from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

SEARXNG_DEFAULT = "http://localhost:8888"
MAX_RESULTS = 5


def _make_result(
    *,
    query: str,
    backend: str,
    status: str,
    sources: Optional[List[Dict[str, str]]] = None,
    answer: str = "",
    error: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "query": query,
        "status": status,
        "backend": backend,
        "sources": sources or [],
        "answer": answer or "",
        "error": error,
    }




def _clean_sources(raw_sources: Optional[List[Dict[str, Any]]], limit: int = MAX_RESULTS) -> List[Dict[str, str]]:
    cleaned: List[Dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in raw_sources or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen_urls:
            continue
        seen_urls.add(url)
        cleaned.append({
            "title": str(item.get("title") or url).strip() or url,
            "url": url,
            "snippet": str(item.get("snippet") or item.get("content") or "").strip(),
        })
        if len(cleaned) >= limit:
            break
    return cleaned


def _merge_search_results(primary: Dict[str, Any], fallback: Dict[str, Any], query: str) -> Dict[str, Any]:
    primary_sources = _clean_sources(primary.get("sources"))
    fallback_sources = _clean_sources(fallback.get("sources"))

    merged_sources = _clean_sources(primary_sources + fallback_sources)
    answer_parts = [str(primary.get("answer") or "").strip(), str(fallback.get("answer") or "").strip()]
    answer = "\n\n".join(part for part in answer_parts if part)

    errors = [str(primary.get("error") or "").strip(), str(fallback.get("error") or "").strip()]
    error = " | ".join(part for part in errors if part) or None

    statuses = {str(primary.get("status") or ""), str(fallback.get("status") or "")}
    if merged_sources:
        status = "degraded" if primary.get("status") != "ok" or fallback.get("status") == "error" else "ok"
    elif "error" in statuses:
        status = "error"
    else:
        status = "no_results"

    return _make_result(
        query=query,
        backend=f"{primary.get('backend', 'unknown')}+{fallback.get('backend', 'unknown')}",
        status=status,
        sources=merged_sources,
        answer=answer,
        error=error,
    )


def _search_searxng(query: str) -> Optional[Dict[str, Any]]:
    """Try SearXNG. Returns structured result or None on failure."""
    url = os.environ.get("SEARXNG_URL", SEARXNG_DEFAULT)
    if not url:
        return None
    try:
        import urllib.parse
        import urllib.request

        params = urllib.parse.urlencode({"q": query, "format": "json"})
        req = urllib.request.Request(f"{url}/search?{params}", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        results = data.get("results", [])[:MAX_RESULTS]
        sources = _clean_sources([
            {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")}
            for r in results
        ])
        if not sources:
            return _make_result(
                query=query,
                backend="searxng",
                status="no_results",
                sources=[],
                answer="",
                error="SearXNG returned no usable results.",
            )
        return _make_result(
            query=query,
            backend="searxng",
            status="ok",
            sources=sources,
            answer="",
            error=None,
        )
    except Exception as e:
        log.warning(f"SearXNG search failed: {e}")
        return None


def _extract_openai_output(resp_dump: Dict[str, Any]) -> tuple[str, List[Dict[str, str]]]:
    text_parts: List[str] = []
    sources: List[Dict[str, str]] = []
    seen_urls: set[str] = set()

    for item in resp_dump.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for block in item.get("content", []) or []:
            if block.get("type") not in ("output_text", "text"):
                continue
            text = str(block.get("text") or "")
            if text:
                text_parts.append(text)

            annotations = block.get("annotations") or []
            for ann in annotations:
                url = str(
                    ann.get("url")
                    or ann.get("source", {}).get("url")
                    or ann.get("webpage", {}).get("url")
                    or ""
                ).strip()
                if not url or url in seen_urls:
                    continue
                title = str(
                    ann.get("title")
                    or ann.get("source", {}).get("title")
                    or ann.get("webpage", {}).get("title")
                    or url
                ).strip()
                snippet = str(ann.get("text") or ann.get("quote") or "").strip()
                sources.append(_normalize_source(title, url, snippet))
                seen_urls.add(url)

    full_text = "\n\n".join(part for part in text_parts if part).strip()

    if not sources and full_text:
        for url in re.findall(r"https?://\S+", full_text):
            clean_url = url.rstrip(").,;]\"'")
            if clean_url in seen_urls:
                continue
            sources.append(_normalize_source(clean_url, clean_url, "Extracted from model response text."))
            seen_urls.add(clean_url)
            if len(sources) >= MAX_RESULTS:
                break

    return full_text, _clean_sources(sources)


def _search_openai(query: str) -> Dict[str, Any]:
    """Fallback: OpenAI Responses API web search."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return _make_result(
            query=query,
            backend="unavailable",
            status="error",
            sources=[],
            answer="",
            error="Neither SearXNG nor OPENAI_API_KEY available.",
        )
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        resp = client.responses.create(
            model=os.environ.get("OUROBOROS_WEBSEARCH_MODEL", "gpt-5"),
            tools=[{"type": "web_search"}],
            tool_choice="auto",
            input=query,
        )
        dump = resp.model_dump()
        answer, sources = _extract_openai_output(dump)
        return _make_result(
            query=query,
            backend="openai",
            status="ok" if (answer or sources) else "no_results",
            sources=sources,
            answer=answer,
            error=None if (answer or sources) else "OpenAI web search returned empty output.",
        )
    except Exception as e:
        return _make_result(
            query=query,
            backend="openai",
            status="error",
            sources=[],
            answer="",
            error=repr(e),
        )


@dataclass
class ResearchRun:
    user_query: str
    intent_type: str = "general"
    subqueries: List[str] = field(default_factory=list)
    candidate_sources: List[Dict[str, str]] = field(default_factory=list)
    visited_pages: List[Dict[str, Any]] = field(default_factory=list)
    findings: List[Dict[str, Any]] = field(default_factory=list)
    final_answer: str = ""
    confidence: str = "low"


def _research_run(ctx: ToolContext, query: str) -> str:
    run = ResearchRun(user_query=str(query or '').strip())
    lowered = run.user_query.lower()
    if any(token in lowered for token in ('compare', 'vs', 'difference', 'лучше', 'сравни')):
        run.intent_type = 'comparison'
    elif any(token in lowered for token in ('latest', 'news', 'recent', 'новост', 'сегодня', '2026', '2025')):
        run.intent_type = 'freshness_sensitive'
    elif any(token in lowered for token in ('how', 'guide', 'tutorial', 'как', 'шаг', 'setup')):
        run.intent_type = 'how_to'

    base = run.user_query
    variants = [base]
    if run.intent_type == 'comparison':
        variants.extend((f'{base} benchmark', f'{base} official documentation'))
    elif run.intent_type == 'freshness_sensitive':
        variants.extend((f'{base} latest updates', f'{base} official announcement'))
    elif run.intent_type == 'how_to':
        variants.extend((f'{base} step by step', f'{base} official docs'))
    else:
        variants.extend((f'{base} official', f'{base} overview'))
    seen: set[str] = set()
    for item in variants:
        value = item.strip()
        if value and value not in seen:
            run.subqueries.append(value)
            seen.add(value)
        if len(run.subqueries) >= 3:
            break

    for subquery in run.subqueries:
        result = json.loads(_web_search(ctx, subquery))
        sources = _clean_sources(result.get('sources'))
        run.candidate_sources.extend(sources)
        run.visited_pages.append({
            'query': subquery,
            'status': result.get('status'),
            'backend': result.get('backend'),
            'source_count': len(sources),
        })
        if sources:
            run.findings.append({
                'query': subquery,
                'summary': result.get('answer') or f'Collected {len(sources)} candidate sources.',
                'top_source': sources[0],
            })

    run.candidate_sources = _clean_sources(run.candidate_sources, limit=10)
    if run.findings:
        run.final_answer = 'Research skeleton run complete. Candidate sources collected; deeper synthesis comes in later commits.'
        run.confidence = 'medium' if run.candidate_sources else 'low'
    else:
        run.final_answer = 'Research skeleton run complete, but no usable sources were found.'
        run.confidence = 'low'

    artifact = save_artifact(
        ctx,
        filename=f"research-run-{re.sub(r'-+', '-', re.sub(r'[^a-z0-9._-]+', '-', run.user_query.lower())).strip('-._') or 'query'}.json",
        content=json.dumps(asdict(run), ensure_ascii=False, indent=2),
        content_kind='json',
        source='research_run',
        mime_type='application/json',
        caption='Research run trace',
        metadata={'tool': 'research_run', 'intent_type': run.intent_type},
    )
    payload = asdict(run)
    payload['trace'] = artifact if isinstance(artifact, dict) else {'status': 'error', 'message': str(artifact)}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _web_search(ctx: ToolContext, query: str) -> str:
    primary = _search_searxng(query)
    if primary is None:
        result = _search_openai(query)
        return json.dumps(result, ensure_ascii=False, indent=2)

    primary_sources = _clean_sources(primary.get("sources"))
    primary_status = str(primary.get("status") or "")
    if primary_sources and primary_status == "ok":
        primary["sources"] = primary_sources
        return json.dumps(primary, ensure_ascii=False, indent=2)

    fallback = _search_openai(query)
    result = _merge_search_results(primary, fallback, query)
    return json.dumps(result, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            "web_search",
            {
                "name": "web_search",
                "description": "Search the web. Returns structured JSON with status, backend, sources, answer, and error.",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            },
            _web_search,
        ),
        ToolEntry(
            "research_run",
            {
                "name": "research_run",
                "description": "Run a structured research skeleton: infer intent, generate subqueries, collect candidate sources, and save a readable JSON trace.",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            },
            _research_run,
        ),
    ]
