"""Web search tool — SearXNG primary, OpenAI fallback."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

SEARXNG_DEFAULT = "http://localhost:8888"
MAX_RESULTS = 5


def _search_searxng(query: str) -> Optional[str]:
    """Try SearXNG. Returns formatted JSON string or None on failure."""
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
            return None
        lines = []
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. **{r.get('title', '')}**")
            lines.append(f"   URL: {r.get('url', '')}")
            lines.append(f"   {r.get('content', '')}")
            lines.append("")
        answer = "\n".join(lines).strip()
        return json.dumps({"answer": answer}, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"SearXNG search failed: {e}")
        return None


def _search_openai(query: str) -> str:
    """Fallback: OpenAI Responses API web search."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return json.dumps({"error": "Neither SearXNG nor OPENAI_API_KEY available."})
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp = client.responses.create(
            model=os.environ.get("OUROBOROS_WEBSEARCH_MODEL", "gpt-5"),
            tools=[{"type": "web_search"}],
            tool_choice="auto",
            input=query,
        )
        d = resp.model_dump()
        text = ""
        for item in d.get("output", []) or []:
            if item.get("type") == "message":
                for block in item.get("content", []) or []:
                    if block.get("type") in ("output_text", "text"):
                        text += block.get("text", "")
        return json.dumps({"answer": text or "(no answer)"}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": repr(e)}, ensure_ascii=False)


def _web_search(ctx: ToolContext, query: str) -> str:
    # Try SearXNG first (free, fast)
    result = _search_searxng(query)
    if result:
        return result
    # Fallback to OpenAI
    return _search_openai(query)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("web_search", {
            "name": "web_search",
            "description": "Search the web. Returns JSON with answer + sources.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
            }, "required": ["query"]},
        }, _web_search),
    ]
