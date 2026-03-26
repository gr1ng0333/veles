"""Search query utilities — shortening and expansion.

Pure-Python helpers (zero dependencies) that improve search quality by:
- Removing stop words and trimming overly long queries.
- Preserving important trailing suffixes (benchmark, survey, …).
- Generating query variants for broader research coverage.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

_MAX_QUERY_LEN = 60  # characters

_STOP_WORDS = frozenset({
    "a", "an", "the", "of", "for", "in", "on", "at", "to", "with",
    "and", "or", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "must",
    "this", "that", "these", "those", "it", "its",
    "how", "what", "which", "who", "whom", "where", "when", "why",
    "not", "no", "nor", "but", "yet", "so",
    "very", "just", "also", "about", "more", "most", "some", "any",
    "comprehensive", "novel", "new", "recent", "using", "based",
    "approach", "method", "study", "analysis", "overview",
    "towards", "toward", "into", "exploring", "investigation",
    "effectiveness", "empirical", "via", "by", "from", "as",
})

# Trailing suffixes we want to keep when shortening
_PRESERVE_SUFFIXES = (
    "benchmark", "survey", "comparison", "state of the art",
    "tutorial", "review", "evaluation", "dataset",
    "seminal",
)

_QUERY_TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("policy_docs", ("policy", "privacy", "retention", "terms", "training")),
    ("docs_api", ("docs", "documentation", "api", "reference", "sdk", "quickstart", "endpoint", "rate limit")),
    ("benchmark_compare", ("benchmark", "leaderboard", "latency", "throughput", "eval", "evaluation", "vs", "versus", "compare", "comparison")),
    ("breaking_news", ("today", "latest", "breaking", "announcement", "recent news")),
)

_RECENCY_MARKERS = re.compile(r"\b(2024|2025|2026|today|latest|recent|updated|new|новост|сегодня|обновл)\b", re.I)


def _extract_keywords(text: str) -> List[str]:
    """Extract meaningful keywords from *text*, stripping stop words."""
    return [
        w for w in re.split(r"[^a-zA-Z0-9]+", text)
        if w.lower() not in _STOP_WORDS and len(w) > 1
    ]


def shorten_query(query: str, max_len: int = _MAX_QUERY_LEN) -> str:
    """Shorten a search query to *max_len* chars by removing stop words.

    Preserves important trailing suffixes (benchmark, survey, etc.).
    If the query is already short enough, it is returned unchanged.
    """
    query = query.strip()
    if len(query) <= max_len:
        return query

    # Detect and preserve trailing suffix
    q_lower = query.lower()
    suffix = ""
    q_core = query
    for sfx in _PRESERVE_SUFFIXES:
        if q_lower.endswith(sfx):
            suffix = sfx
            q_core = query[: -len(sfx)].strip()
            break

    # Extract keywords from the core part
    keywords = _extract_keywords(q_core)

    # Take up to 6 keywords, then trim if still over limit
    max_kw = 6
    shortened = " ".join(keywords[:max_kw])
    if suffix:
        shortened = f"{shortened} {suffix}"

    # Fallback: if still over limit, drop keywords from the end
    while len(shortened) > max_len and max_kw > 2:
        max_kw -= 1
        shortened = " ".join(keywords[:max_kw])
        if suffix:
            shortened = f"{shortened} {suffix}"

    return shortened


def expand_search_queries(topic: str) -> List[str]:
    """Generate multiple search query variants from a topic.

    Returns list of queries for broader research coverage.
    Each variant is passed through ``shorten_query()``.
    """
    topic = topic.strip()
    if not topic:
        return []

    queries: List[str] = [shorten_query(topic)]
    seen = {queries[0].lower()}

    topic_words = topic.split()

    # Add suffix variants using first 4 content words
    kw = _extract_keywords(topic)
    short_base = " ".join(kw[:4]) if kw else " ".join(topic_words[:4])

    for suffix in ("survey", "benchmark", "comparison"):
        variant = shorten_query(f"{short_base} {suffix}")
        if variant.lower() not in seen:
            queries.append(variant)
            seen.add(variant.lower())

    # Broader query: first 5 words of topic
    if len(topic_words) > 5:
        broader = shorten_query(" ".join(topic_words[:5]))
        if broader.lower() not in seen:
            queries.append(broader)
            seen.add(broader.lower())

    return queries


def detect_query_type(query: str) -> str:
    lowered = str(query or "").strip().lower()
    if not lowered:
        return "general"
    for query_type, keywords in _QUERY_TYPE_RULES:
        if any(keyword in lowered for keyword in keywords):
            return query_type
    return "general"


def extract_core_subject(query: str) -> str:
    keywords = _extract_keywords(str(query or ""))
    if not keywords:
        return ""
    return " ".join(keywords[:4]).strip()


def relevance_overlap(query: str, *, title: str = "", snippet: str = "", url: str = "") -> float:
    query_terms = {term.lower() for term in _extract_keywords(query) if len(term) > 2}
    if not query_terms:
        return 0.0
    haystack = f"{title} {snippet} {url}".lower()
    overlap = sum(1 for term in query_terms if term in haystack)
    return min(1.0, overlap / max(1, min(len(query_terms), 4)))


def dedupe_signature(*, title: str = "", url: str = "") -> str:
    title_tokens = [token.lower() for token in _extract_keywords(title)[:8]]
    normalized_url = re.sub(r"^https?://", "", str(url or "").strip().lower()).rstrip("/")
    return "|".join([" ".join(title_tokens), normalized_url])


def recency_signal(*, text: str = "", freshness_priority: str = "low") -> float:
    hits = len(_RECENCY_MARKERS.findall(str(text or "")))
    if not hits:
        return 0.0
    weight = {"high": 0.8, "medium": 0.5, "low": 0.2}.get(str(freshness_priority or "low"), 0.2)
    return min(1.0, hits * weight)


def score_result_signals(query: str, *, title: str = "", snippet: str = "", url: str = "", freshness_priority: str = "low") -> Dict[str, Any]:
    query_type = detect_query_type(query)
    core_subject = extract_core_subject(query)
    relevance = relevance_overlap(query, title=title, snippet=snippet, url=url)
    recency = recency_signal(text=f"{title} {snippet} {url}", freshness_priority=freshness_priority)
    signature = dedupe_signature(title=title, url=url)
    return {
        "query_type": query_type,
        "core_subject": core_subject,
        "relevance": relevance,
        "recency": recency,
        "dedupe_signature": signature,
    }
