"""OpenAlex academic search tool — 250M+ scientific works, zero external deps.

Uses stdlib ``urllib`` + ``json`` only.  Polite pool (mailto) gives 10K req/day.

Public API
----------
- ``_academic_search(ctx, query, max_results)`` — tool handler
- ``get_tools()`` — registry hook
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from ouroboros.circuit_breaker import CircuitBreaker
from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_BASE_URL = "https://api.openalex.org/works"
_POLITE_EMAIL = "veles-agent@users.noreply.github.com"
_TIMEOUT_SEC = 15
_RATE_LIMIT_SEC = 0.5
_MAX_RETRIES = 3
_MAX_PER_REQUEST = 20

# Rate-limit state
_last_request_time: float = 0.0
_rate_lock = threading.Lock()

# Circuit breaker for OpenAlex
_openalex_breaker = CircuitBreaker(
    "openalex", failure_threshold=3, recovery_timeout=120,
)


# ------------------------------------------------------------------
# Abstract inverted-index → plain text
# ------------------------------------------------------------------


def _invert_abstract(inverted_index: dict | None) -> str:
    """Convert OpenAlex inverted-index abstract to plain text."""
    if not inverted_index or not isinstance(inverted_index, dict):
        return ""
    words: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        if not isinstance(positions, list):
            continue
        for pos in positions:
            words.append((pos, word))
    words.sort(key=lambda x: x[0])
    return " ".join(w for _, w in words)


# ------------------------------------------------------------------
# HTTP request helper
# ------------------------------------------------------------------


def _request_with_retry(url: str) -> dict[str, Any] | None:
    """GET *url* with exponential back-off retries."""
    for attempt in range(_MAX_RETRIES):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": f"Veles/1.0 (mailto:{_POLITE_EMAIL})",
                },
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:  # noqa: S310
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                wait = min(2 ** (attempt + 1), 60)
                log.warning(
                    "OpenAlex 429 — waiting %.0fs (attempt %d/%d)",
                    wait, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            if exc.code in (500, 502, 503, 504):
                wait = 2 ** attempt
                log.warning(
                    "OpenAlex HTTP %d — retry %d/%d in %ds",
                    exc.code, attempt + 1, _MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            log.warning("OpenAlex HTTP %d for %s", exc.code, url)
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            wait = min(2 ** attempt, 60)
            log.warning(
                "OpenAlex request failed (%s) — retry %d/%d in %ds",
                exc, attempt + 1, _MAX_RETRIES, wait,
            )
            time.sleep(wait)
    log.error("OpenAlex request exhausted retries for: %s", url)
    return None


# ------------------------------------------------------------------
# Response parsing
# ------------------------------------------------------------------


def _parse_work(item: dict[str, Any]) -> Dict[str, Any]:
    """Parse a single OpenAlex work JSON into a result dict."""
    title = re.sub(r"\s+", " ", str(item.get("title") or "").strip())

    # Authors
    authorships = item.get("authorships") or []
    authors = [
        str(a.get("author", {}).get("display_name", "Unknown"))
        for a in authorships
        if isinstance(a, dict)
    ]

    year = int(item.get("publication_year") or 0)
    cited_by_count = int(item.get("cited_by_count") or 0)

    # DOI
    raw_doi = str(item.get("doi") or "").strip()
    doi = raw_doi.replace("https://doi.org/", "").replace("http://doi.org/", "")

    # Abstract
    abstract = _invert_abstract(item.get("abstract_inverted_index"))

    # Open-access URL
    oa = item.get("open_access") or {}
    oa_url = str(oa.get("oa_url") or "").strip()

    # Fallback URL: OA → DOI → OpenAlex page
    url = oa_url or (f"https://doi.org/{doi}" if doi else str(item.get("id") or ""))

    return {
        "title": title,
        "authors": authors,
        "year": year,
        "cited_by_count": cited_by_count,
        "doi": doi,
        "abstract": abstract,
        "url": url,
        "source": "openalex",
    }


# ------------------------------------------------------------------
# Core search function
# ------------------------------------------------------------------


def search_openalex(query: str, *, limit: int = 5) -> List[Dict[str, Any]]:
    """Search OpenAlex for papers matching *query*.

    Returns list of result dicts.  Empty list on failure.
    """
    global _last_request_time  # noqa: PLW0603

    if not _openalex_breaker.allow_request():
        log.debug("Circuit breaker OPEN for openalex, skipping")
        return []

    # Rate limiting
    with _rate_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < _RATE_LIMIT_SEC:
            time.sleep(_RATE_LIMIT_SEC - elapsed)
        _last_request_time = time.monotonic()

    limit = max(1, min(limit, _MAX_PER_REQUEST))

    params: dict[str, str] = {
        "search": query,
        "per_page": str(limit),
        "mailto": _POLITE_EMAIL,
        "select": (
            "id,title,authorships,publication_year,"
            "cited_by_count,doi,abstract_inverted_index,open_access"
        ),
    }
    url = f"{_BASE_URL}?{urllib.parse.urlencode(params)}"

    data = _request_with_retry(url)
    if data is None:
        _openalex_breaker.record_failure()
        return []

    results = data.get("results", [])
    if not isinstance(results, list):
        _openalex_breaker.record_failure()
        return []

    _openalex_breaker.record_success()

    papers: list[Dict[str, Any]] = []
    for item in results:
        try:
            papers.append(_parse_work(item))
        except Exception:  # noqa: BLE001
            log.debug("Failed to parse OpenAlex work: %s", item.get("id", "?"))
    return papers


# ------------------------------------------------------------------
# Tool handler
# ------------------------------------------------------------------


def _academic_search(ctx: ToolContext, query: str, max_results: int = 5) -> str:
    """academic_search tool handler."""
    del ctx
    limit = max(1, min(int(max_results or 5), 20))
    results = search_openalex(query, limit=limit)
    if not results:
        return json.dumps(
            {"query": query, "status": "no_results", "results": [], "count": 0},
            ensure_ascii=False,
        )
    # Trim abstracts for readability
    for r in results:
        if len(r.get("abstract", "")) > 500:
            r["abstract"] = r["abstract"][:500] + "…"
    return json.dumps(
        {"query": query, "status": "ok", "results": results, "count": len(results)},
        ensure_ascii=False,
        indent=2,
    )


# ------------------------------------------------------------------
# Registry hook
# ------------------------------------------------------------------


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            "academic_search",
            {
                "name": "academic_search",
                "description": (
                    "Search academic papers and scientific publications via "
                    "OpenAlex (250M+ works). Use for research questions, "
                    "scientific topics, benchmarks, state-of-the-art methods. "
                    "Returns titles, authors, abstracts, citation counts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query — topic, method name, research question",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Number of results (default 5, max 20)",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
            },
            _academic_search,
        ),
    ]
