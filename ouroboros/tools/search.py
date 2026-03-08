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


def _search_searxng(query: str) -> Optional[Dict[str, Any]]:
    """Try SearXNG. Returns structured result or None on failure."""
    url = os.environ.get("SEARXNG_URL", SEARXNG_DEFAULT)
    if not url:
        return None
    try:
        import urllib.request
        import urllib.parse
        params = urllib.parse.urlencode({"q": query, "format": "json"})
        req = urllib.request.Request(f"{url}/search?{params}", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        results = data.get("results", [])[:MAX_RESULTS]
        if not results:
            return _make_result(
                query=query,
                backend="searxng",
                status="no_results",
                sources=[],
                answer="",
                error="SearXNG returned no results.",
            )

        sources = [
            _normalize_source(r.get("title", ""), r.get("url", ""), r.get("content", ""))
            for r in results
            if r.get("url")
        ]
        return _make_result(
            query=query,
            backend="searxng",
            status="ok" if sources else "no_results",
            sources=sources,
            answer="",
            error=None if sources else "SearXNG returned results without URLs.",
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

    return full_text, sources[:MAX_RESULTS]


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
    # Try SearXNG first (free, fast)
    result = _search_searxng(query)
    if result is None:
        result = _search_openai(query)
    return json.dumps(result, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("web_search", {
            "name": "web_search",
            "description": "Search the web. Returns structured JSON with status, backend, sources, answer, and error.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
            }, "required": ["query"]},
        }, _web_search),
    ]
