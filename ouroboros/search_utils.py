"""Search query utilities — shortening and expansion.

Pure-Python helpers (zero dependencies) that improve search quality by:
- Removing stop words and trimming overly long queries.
- Preserving important trailing suffixes (benchmark, survey, …).
- Generating query variants for broader research coverage.
"""

from __future__ import annotations

import re
from typing import List

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
        broad = shorten_query(" ".join(topic_words[:5]))
        if broad.lower() not in seen:
            queries.append(broad)
            seen.add(broad.lower())

    return queries
