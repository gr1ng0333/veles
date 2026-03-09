"""Web search tool — Serper-only backend."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

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


def _http_json_request(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 15,
) -> Dict[str, Any]:
    import urllib.request

    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


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


def _web_search(ctx: ToolContext, query: str) -> str:
    del ctx
    result = _search_serper(query)
    result["sources"] = _clean_sources(result.get("sources"))
    return json.dumps(result, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            "web_search",
            {
                "name": "web_search",
                "description": "Search the web via Serper.dev. Returns structured JSON with status, backend, sources, answer, and error.",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            },
            _web_search,
        ),
    ]
