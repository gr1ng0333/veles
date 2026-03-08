"""Web search tool — SearXNG primary, OpenAI fallback."""

from __future__ import annotations

import json
import logging
import os
import re
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
        sources = _clean_sources(
            [_normalize_source(r.get("title", ""), r.get("url", ""), r.get("content", "")) for r in results]
        )
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
    ]
