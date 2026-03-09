"""Web search tool — SearXNG primary, API fallback."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

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


def _normalize_source(title: Any, url: Any, snippet: Any) -> Dict[str, str]:
    return {
        "title": str(title or "").strip(),
        "url": str(url or "").strip(),
        "snippet": str(snippet or "").strip(),
    }


def _clean_sources(raw_sources: Optional[List[Dict[str, Any]]], limit: int = MAX_RESULTS) -> List[Dict[str, str]]:
    cleaned: List[Dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in raw_sources or []:
        if not isinstance(item, dict):
            continue
        source = _normalize_source(item.get("title"), item.get("url"), item.get("snippet") or item.get("content"))
        url = source["url"]
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            continue
        if url in seen_urls:
            continue
        if not source["title"]:
            source["title"] = url
        cleaned.append(source)
        seen_urls.add(url)
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


def _http_json_request(url: str, *, method: str = "GET", headers: Optional[Dict[str, str]] = None, payload: Optional[Dict[str, Any]] = None, timeout: int = 10) -> Dict[str, Any]:
    import urllib.request

    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _search_searxng(query: str) -> Optional[Dict[str, Any]]:
    """Try SearXNG. Returns structured result or None on transport failure."""
    url = os.environ.get("SEARXNG_URL", SEARXNG_DEFAULT)
    if not url:
        return None
    try:
        import urllib.parse

        params = urllib.parse.urlencode({"q": query, "format": "json"})
        data = _http_json_request(
            f"{url}/search?{params}",
            headers={"Accept": "application/json"},
            timeout=10,
        )
        results = data.get("results", [])[:MAX_RESULTS]
        sources = _clean_sources(
            [_normalize_source(r.get("title", ""), r.get("url", ""), r.get("content", "")) for r in results]
        )
        if not sources:
            engine_notes = []
            for item in data.get("unresponsive_engines") or []:
                if isinstance(item, list) and len(item) >= 2:
                    engine_notes.append(f"{item[0]}: {item[1]}")
                elif isinstance(item, str):
                    engine_notes.append(item)
            error = "SearXNG returned no usable results."
            if engine_notes:
                error = f"{error} Unresponsive engines: {'; '.join(engine_notes[:5])}"
            return _make_result(
                query=query,
                backend="searxng",
                status="no_results",
                sources=[],
                answer="",
                error=error,
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
        log.warning("SearXNG search failed: %s", e)
        return None


def _search_serper(query: str) -> Dict[str, Any]:
    api_key = os.environ.get("SERPER_API_KEY", "").strip()
    if not api_key:
        return _make_result(
            query=query,
            backend="serper",
            status="error",
            sources=[],
            answer="",
            error="SERPER_API_KEY is not configured.",
        )
    try:
        data = _http_json_request(
            "https://google.serper.dev/search",
            method="POST",
            headers={"X-API-KEY": api_key},
            payload={"q": query, "num": MAX_RESULTS},
            timeout=15,
        )
        raw_sources: List[Dict[str, Any]] = []
        answer_parts: List[str] = []

        answer_box = data.get("answerBox")
        if isinstance(answer_box, dict):
            answer_text = answer_box.get("answer") or answer_box.get("snippet") or answer_box.get("title") or ""
            if answer_text:
                answer_parts.append(str(answer_text).strip())
            link = answer_box.get("link") or answer_box.get("url")
            if link:
                raw_sources.append(
                    _normalize_source(answer_box.get("title") or link, link, answer_box.get("snippet") or answer_text)
                )

        knowledge_graph = data.get("knowledgeGraph")
        if isinstance(knowledge_graph, dict):
            kg_url = knowledge_graph.get("website") or knowledge_graph.get("url")
            kg_snippet = knowledge_graph.get("description") or knowledge_graph.get("title") or ""
            if kg_snippet:
                answer_parts.append(str(kg_snippet).strip())
            if kg_url:
                raw_sources.append(_normalize_source(knowledge_graph.get("title") or kg_url, kg_url, kg_snippet))

        for item in data.get("organic") or []:
            if not isinstance(item, dict):
                continue
            raw_sources.append(_normalize_source(item.get("title"), item.get("link"), item.get("snippet")))

        sources = _clean_sources(raw_sources)
        return _make_result(
            query=query,
            backend="serper",
            status="ok" if sources or answer_parts else "no_results",
            sources=sources,
            answer="\n\n".join(part for part in answer_parts if part),
            error=None if sources or answer_parts else "Serper returned no usable results.",
        )
    except Exception as e:
        return _make_result(
            query=query,
            backend="serper",
            status="error",
            sources=[],
            answer="",
            error=repr(e),
        )


def _search_brave(query: str) -> Dict[str, Any]:
    api_key = os.environ.get("BRAVE_API_KEY", "").strip()
    if not api_key:
        return _make_result(
            query=query,
            backend="brave",
            status="error",
            sources=[],
            answer="",
            error="BRAVE_API_KEY is not configured.",
        )
    try:
        import urllib.parse

        params = urllib.parse.urlencode({"q": query, "count": MAX_RESULTS, "text_decorations": False})
        data = _http_json_request(
            f"https://api.search.brave.com/res/v1/web/search?{params}",
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            timeout=15,
        )
        raw_sources: List[Dict[str, Any]] = []
        web_results = ((data.get("web") or {}).get("results") or [])
        for item in web_results:
            if not isinstance(item, dict):
                continue
            raw_sources.append(_normalize_source(item.get("title"), item.get("url"), item.get("description") or item.get("snippet")))
        sources = _clean_sources(raw_sources)
        return _make_result(
            query=query,
            backend="brave",
            status="ok" if sources else "no_results",
            sources=sources,
            answer="",
            error=None if sources else "Brave Search returned no usable results.",
        )
    except Exception as e:
        return _make_result(
            query=query,
            backend="brave",
            status="error",
            sources=[],
            answer="",
            error=repr(e),
        )


def _search_api_fallback(query: str) -> Dict[str, Any]:
    serper_key = os.environ.get("SERPER_API_KEY", "").strip()
    brave_key = os.environ.get("BRAVE_API_KEY", "").strip()

    if serper_key:
        serper_result = _search_serper(query)
        if serper_result.get("status") in {"ok", "no_results"}:
            return serper_result
        if brave_key:
            brave_result = _search_brave(query)
            return _merge_search_results(serper_result, brave_result, query)
        return serper_result

    if brave_key:
        return _search_brave(query)

    return _make_result(
        query=query,
        backend="unavailable",
        status="error",
        sources=[],
        answer="",
        error="No API fallback configured. Set SERPER_API_KEY or BRAVE_API_KEY.",
    )


def _web_search(ctx: ToolContext, query: str) -> str:
    primary = _search_searxng(query)
    if primary is None:
        result = _search_api_fallback(query)
        return json.dumps(result, ensure_ascii=False, indent=2)

    primary_sources = _clean_sources(primary.get("sources"))
    primary_status = str(primary.get("status") or "")
    if primary_sources and primary_status == "ok":
        primary["sources"] = primary_sources
        return json.dumps(primary, ensure_ascii=False, indent=2)

    fallback = _search_api_fallback(query)
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
    ]
