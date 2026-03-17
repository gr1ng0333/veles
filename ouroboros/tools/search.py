"""Web search tool — structured search plus research-run skeleton."""

from __future__ import annotations

import html
import json
import logging
import os
from operator import itemgetter
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from ouroboros.artifacts import save_artifact
from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

SEARXNG_DEFAULT = "http://localhost:8888"
MAX_RESULTS = 5
DEFAULT_INTENT = "background_explainer"
MAX_SUBQUERIES = 6


@dataclass(frozen=True)
class IntentPolicy:
    freshness_priority: str
    search_branches: int
    min_sources_before_synthesis: int
    require_official_source: bool


@dataclass(frozen=True)
class QueryPlan:
    primary_query: str
    freshness_query: str
    official_docs_query: str
    alternative_wording_query: str
    contradiction_check_query: str
    subqueries: List[str]
    branch_budget: int


INTENT_POLICIES: Dict[str, IntentPolicy] = {
    "breaking_news": IntentPolicy("high", 4, 3, False),
    "fact_lookup": IntentPolicy("medium", 3, 2, False),
    "product_docs_api_lookup": IntentPolicy("medium", 4, 2, True),
    "comparison_evaluation": IntentPolicy("medium", 4, 3, False),
    "background_explainer": IntentPolicy("low", 3, 2, False),
    "people_company_ecosystem_tracking": IntentPolicy("high", 4, 3, False),
}

INTENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "breaking_news",
        (
            "breaking news",
            "latest news",
            "latest updates",
            "today",
            "сегодня",
            "что случилось",
            "только что",
            "recent news",
            "announcement today",
            "new release today",
        ),
    ),
    (
        "comparison_evaluation",
        (
            "compare",
            "comparison",
            "vs",
            "versus",
            "better",
            "best for",
            "tradeoff",
            "benchmark",
            "pros and cons",
            "сравни",
            "разница",
            "лучше",
            "против",
        ),
    ),
    (
        "product_docs_api_lookup",
        (
            "api",
            "sdk",
            "documentation",
            "docs",
            "reference",
            "endpoint",
            "rate limit",
            "oauth",
            "quickstart",
            "guide",
            "install",
            "лимит api",
            "документац",
            "эндпоинт",
            "справк",
        ),
    ),
    (
        "people_company_ecosystem_tracking",
        (
            "founder",
            "ceo",
            "company",
            "startup",
            "funding",
            "layoffs",
            "hiring",
            "team",
            "maintainer",
            "community",
            "ecosystem",
            "roadmap",
            "компан",
            "основател",
            "экосистем",
            "инвест",
            "уволь",
            "команда",
        ),
    ),
    (
        "background_explainer",
        (
            "explain",
            "overview",
            "background",
            "history",
            "why",
            "how does",
            "what is",
            "что такое",
            "объясни",
            "история",
            "почему",
            "как работает",
        ),
    ),
    (
        "fact_lookup",
        (
            "when did",
            "how many",
            "exact",
            "exactly",
            "maximum",
            "default",
            "version",
            "release date",
            "сколько",
            "какой",
            "точн",
            "максим",
            "дефолт",
            "версия",
            "дата релиза",
        ),
    ),
)


@dataclass
class ResearchRun:
    user_query: str
    intent_type: str = DEFAULT_INTENT
    subqueries: List[str] = field(default_factory=list)
    candidate_sources: List[Dict[str, Any]] = field(default_factory=list)
    visited_pages: List[Dict[str, Any]] = field(default_factory=list)
    findings: List[Dict[str, Any]] = field(default_factory=list)
    final_answer: str = ""
    confidence: str = "low"
    query_plan: Dict[str, Any] = field(default_factory=dict)


def _classify_intent(query: str) -> str:
    lowered = str(query or "").strip().lower()
    if not lowered:
        return DEFAULT_INTENT
    for intent_type, keywords in INTENT_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return intent_type
    return "fact_lookup" if any(ch.isdigit() for ch in lowered) else DEFAULT_INTENT


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



def _build_query_plan(query: str, intent_type: str) -> QueryPlan:
    base = re.sub(r"\s+", " ", str(query or "").strip())
    policy = INTENT_POLICIES.get(intent_type, INTENT_POLICIES[DEFAULT_INTENT])

    freshness_suffix = {
        "high": "latest updates",
        "medium": "recent",
        "low": "overview",
    }[policy.freshness_priority]
    official_suffix = "official docs" if policy.require_official_source else "official source"
    alternative_suffix = {
        "comparison_evaluation": "tradeoffs and benchmark",
        "breaking_news": "timeline and reactions",
        "product_docs_api_lookup": "reference guide",
        "people_company_ecosystem_tracking": "ecosystem map",
        "fact_lookup": "exact value reference",
        "background_explainer": "overview",
    }.get(intent_type, "alternative wording")
    contradiction_suffix = {
        "breaking_news": "conflicting reports",
        "fact_lookup": "contradicting value",
        "product_docs_api_lookup": "limitations exceptions",
        "comparison_evaluation": "counterarguments",
        "people_company_ecosystem_tracking": "controversy changes",
        "background_explainer": "common misconceptions",
    }.get(intent_type, "contradictions")

    primary_query = base
    freshness_query = f"{base} {freshness_suffix}" if base else freshness_suffix
    official_docs_query = f"{base} {official_suffix}" if base else official_suffix
    alternative_wording_query = f"{base} {alternative_suffix}" if base else alternative_suffix
    contradiction_check_query = f"{base} {contradiction_suffix}" if base else contradiction_suffix

    candidates = [primary_query]
    if policy.freshness_priority in {"high", "medium"}:
        candidates.append(freshness_query)
    if policy.require_official_source:
        candidates.append(official_docs_query)
    candidates.append(alternative_wording_query)
    if policy.search_branches >= 4 or policy.freshness_priority == "low":
        candidates.append(contradiction_check_query)
    if policy.search_branches >= 5 and not policy.require_official_source:
        candidates.append(official_docs_query)

    branch_budget = max(3, min(policy.search_branches, MAX_SUBQUERIES))
    subqueries: List[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        value = re.sub(r"\s+", " ", str(candidate or "").strip())
        if not value:
            continue
        dedupe_key = value.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        subqueries.append(value)
        if len(subqueries) >= branch_budget:
            break
    branch_budget = len(subqueries)

    return QueryPlan(
        primary_query=primary_query,
        freshness_query=freshness_query,
        official_docs_query=official_docs_query,
        alternative_wording_query=alternative_wording_query,
        contradiction_check_query=contradiction_check_query,
        subqueries=subqueries,
        branch_budget=branch_budget,
    )




def _read_page_findings(query: str, source: Dict[str, Any]) -> Dict[str, Any]:
    url = str(source.get("url") or "")
    try:
        import urllib.request

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; VelesResearch/1.0; +https://github.com/gr1ng0333/veles)",
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = resp.read()
            content_type = str(resp.headers.get("Content-Type") or "")
        charset_match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type, re.IGNORECASE)
        charset = charset_match.group(1) if charset_match else "utf-8"
        raw_text = body.decode(charset, errors="replace")
        clean_text = str(raw_text or "")
        if "<" in clean_text and ">" in clean_text:
            clean_text = re.sub(r"<script\b[^>]*>.*?</script>", " ", clean_text, flags=re.IGNORECASE | re.DOTALL)
            clean_text = re.sub(r"<style\b[^>]*>.*?</style>", " ", clean_text, flags=re.IGNORECASE | re.DOTALL)
            clean_text = re.sub(r"<noscript\b[^>]*>.*?</noscript>", " ", clean_text, flags=re.IGNORECASE | re.DOTALL)
            clean_text = re.sub(r"<svg\b[^>]*>.*?</svg>", " ", clean_text, flags=re.IGNORECASE | re.DOTALL)
            clean_text = re.sub(r"<[^>]+>", " ", clean_text)
        clean_text = html.unescape(clean_text)
        clean_text = re.sub(r"\s+", " ", clean_text).strip()
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+", clean_text)
            if sentence.strip()
        ]
        query_terms = [term for term in re.findall(r"[a-zA-Zа-яА-Я0-9_+-]{3,}", str(query or "").lower()) if len(term) >= 3]
        source_host = str(source.get("host") or urlparse(url).netloc).lower().lstrip("www.")
        if any(token in source_host for token in ("docs.", "developer.", "platform.", "api.")):
            source_type = "docs"
        elif any(token in source_host for token in ("news", "blog", "press")):
            source_type = "news"
        else:
            source_type = "page"
        relevant_sections = [
            sentence
            for sentence in sentences
            if any(term in sentence.lower() for term in query_terms)
        ]
        if len(relevant_sections) < 2:
            informative_sentences = [
                sentence
                for sentence in sentences
                if len(sentence) >= 40 and not sentence.lower().startswith(("skip to", "sign in", "cookie", "accept "))
            ]
            for sentence in informative_sentences:
                if sentence not in relevant_sections:
                    relevant_sections.append(sentence)
                if len(relevant_sections) >= 3:
                    break
        relevant_sections = relevant_sections[:3]
        findings: List[Dict[str, Any]] = []
        seen_claims: set[str] = set()
        observed_at_match = re.search(r"\b(2024|2025|2026)[-/.](\d{1,2})[-/.](\d{1,2})\b", clean_text)
        observed_at = observed_at_match.group(0) if observed_at_match else ""
        for sentence in relevant_sections:
            lowered = sentence.lower()
            overlap = sum(1 for term in query_terms if term in lowered)
            score = overlap + min(1.5, len(sentence) / 240.0)
            if any(ch.isdigit() for ch in sentence):
                score += 0.4
            if any(marker in lowered for marker in ("updated", "released", "supports", "limit", "version", "announced", "docs", "api", "rate", "rpm", "quota")):
                score += 0.6
            claim = sentence[:220].strip(" -:;,.\n\t")
            dedupe_key = re.sub(r"\W+", " ", claim.casefold()).strip()
            if not dedupe_key or dedupe_key in seen_claims:
                continue
            seen_claims.add(dedupe_key)
            findings.append({
                "claim": claim,
                "evidence_snippet": sentence[:320].strip(),
                "source_url": url,
                "source_type": source_type,
                "observed_at": observed_at,
                "confidence_local": "high" if score >= 4.0 else "medium" if score >= 2.5 else "low",
            })
        return {
            "url": url,
            "status": "ok",
            "content_type": content_type,
            "text_preview": clean_text[:400],
            "relevant_sections": relevant_sections,
            "findings": findings,
            "error": None,
        }
    except Exception as exc:
        return {
            "url": url,
            "status": "error",
            "content_type": "",
            "text_preview": "",
            "relevant_sections": [],
            "findings": [],
            "error": repr(exc),
        }


def _search_searxng(query: str) -> Optional[Dict[str, Any]]:
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
        sources = _clean_sources(
            [{"title": row.get("title", ""), "url": row.get("url", ""), "content": row.get("content", "")} for row in data.get("results", [])[:MAX_RESULTS]]
        )
        if not sources:
            return {"query": query, "status": "no_results", "backend": "searxng", "sources": [], "answer": "", "error": "SearXNG returned no usable results."}
        return {"query": query, "status": "ok", "backend": "searxng", "sources": sources, "answer": "", "error": None}
    except Exception as exc:
        log.warning("SearXNG search failed: %s", exc)
        return None




def _search_openai(query: str) -> Dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return {
            "query": query,
            "status": "error",
            "backend": "unavailable",
            "sources": [],
            "answer": "",
            "error": "Neither SearXNG nor OPENAI_API_KEY available.",
        }
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        resp = client.responses.create(
            model=os.environ.get("OUROBOROS_WEBSEARCH_MODEL", "gpt-5"),
            tools=[{"type": "web_search"}],
            tool_choice="auto",
            input=query,
        )
        resp_dump = resp.model_dump()
        text_parts: List[str] = []
        sources: List[Dict[str, str]] = []
        seen_urls: set[str] = set()
        for item in resp_dump.get("output", []) or []:
            if item.get("type") != "message":
                continue
            for block in item.get("content", []) or []:
                if block.get("type") not in ("output_text", "text"):
                    continue
                text_value = str(block.get("text") or "")
                if text_value:
                    text_parts.append(text_value)
                for ann in block.get("annotations") or []:
                    url = str(
                        ann.get("url")
                        or ann.get("source", {}).get("url")
                        or ann.get("webpage", {}).get("url")
                        or ""
                    ).strip()
                    if not url or url in seen_urls:
                        continue
                    sources.append(
                        {
                            "title": str(
                                ann.get("title")
                                or ann.get("source", {}).get("title")
                                or ann.get("webpage", {}).get("title")
                                or url
                            ).strip()
                            or url,
                            "url": url,
                            "snippet": str(ann.get("text") or ann.get("quote") or "").strip(),
                        }
                    )
                    seen_urls.add(url)
        answer = "\n\n".join(part for part in text_parts if part).strip()
        if not sources and answer:
            for url in re.findall(r"https?://\S+", answer):
                clean_url = url.rstrip(").,;]\"'")
                if clean_url in seen_urls:
                    continue
                sources.append(
                    {
                        "title": clean_url,
                        "url": clean_url,
                        "snippet": "Extracted from model response text.",
                    }
                )
                seen_urls.add(clean_url)
                if len(sources) >= MAX_RESULTS:
                    break
        sources = _clean_sources(sources)
        return {
            "query": query,
            "status": "ok" if (answer or sources) else "no_results",
            "backend": "openai",
            "sources": sources,
            "answer": answer,
            "error": None if (answer or sources) else "OpenAI web search returned empty output.",
        }
    except Exception as exc:
        return {
            "query": query,
            "status": "error",
            "backend": "openai",
            "sources": [],
            "answer": "",
            "error": repr(exc),
        }

def _web_search(ctx: ToolContext, query: str) -> str:
    del ctx
    primary = _search_searxng(query)
    if primary is None:
        return json.dumps(_search_openai(query), ensure_ascii=False, indent=2)
    primary_sources = _clean_sources(primary.get("sources"))
    if primary_sources and str(primary.get("status") or "") == "ok":
        primary["sources"] = primary_sources
        return json.dumps(primary, ensure_ascii=False, indent=2)
    fallback = _search_openai(query)
    merged_sources = _clean_sources(_clean_sources(primary.get("sources")) + _clean_sources(fallback.get("sources")))
    answer = "\n\n".join(
        part for part in (str(primary.get("answer") or "").strip(), str(fallback.get("answer") or "").strip()) if part
    )
    error = " | ".join(
        part for part in (str(primary.get("error") or "").strip(), str(fallback.get("error") or "").strip()) if part
    ) or None
    if merged_sources:
        status = "degraded" if primary.get("status") != "ok" or fallback.get("status") == "error" else "ok"
    else:
        status = "error" if "error" in {str(primary.get("status") or ""), str(fallback.get("status") or "")} else "no_results"
    return json.dumps({
        "query": query,
        "status": status,
        "backend": f"{primary.get('backend', 'unknown')}+{fallback.get('backend', 'unknown')}",
        "sources": merged_sources,
        "answer": answer,
        "error": error,
    }, ensure_ascii=False, indent=2)


def _research_run(ctx: ToolContext, query: str) -> str:
    run = ResearchRun(user_query=str(query or "").strip())
    run.intent_type = _classify_intent(run.user_query)
    policy = asdict(INTENT_POLICIES.get(run.intent_type, INTENT_POLICIES[DEFAULT_INTENT]))
    plan = _build_query_plan(run.user_query, run.intent_type)
    run.subqueries = list(plan.subqueries)
    run.query_plan = asdict(plan)
    domain_scores = {
        "docs.python.org": 26,
        "developer.mozilla.org": 24,
        "platform.openai.com": 24,
        "docs.anthropic.com": 24,
        "openai.com": 18,
        "anthropic.com": 18,
        "github.com": 16,
        "wikipedia.org": 8,
        "medium.com": -4,
        "substack.com": -4,
        "reddit.com": -10,
        "news.ycombinator.com": -8,
        "x.com": -12,
        "twitter.com": -12,
        "linkedin.com": -6,
        "facebook.com": -10,
    }
    aggregator_domains = {"news.google.com", "news.ycombinator.com", "alltop.com", "feedly.com", "ycombinator.com", "techmeme.com"}
    social_domains = {"reddit.com", "x.com", "twitter.com", "facebook.com", "linkedin.com", "t.me", "discord.com"}
    seen_urls: set[str] = set()
    ranked_sources: List[Dict[str, Any]] = []
    query_terms = {term for term in re.findall(r"[a-zA-Zа-яА-Я0-9_+-]{3,}", run.user_query.lower()) if len(term) >= 3}

    for subquery in run.subqueries:
        result = json.loads(_web_search(ctx, subquery))
        sources = _clean_sources(result.get("sources"), limit=10)
        page_trace = {
            "query": subquery,
            "status": result.get("status"),
            "backend": result.get("backend"),
            "source_count": len(sources),
            "intent_type": run.intent_type,
            "policy": policy,
            "ranked_sources": [],
            "selected_to_read": [],
            "rejected": [],
        }
        for index, source in enumerate(sources):
            url = str(source.get("url") or "").strip()
            lowered_url = url.lower()
            host_match = re.match(r"https?://([^/]+)", lowered_url)
            host = (host_match.group(1) if host_match else "").lstrip("www.")
            title = str(source.get("title") or "").strip()
            snippet = str(source.get("snippet") or "").strip()
            haystack = f"{title} {snippet} {url}".lower()
            score = 0.0
            reasons: List[str] = []
            official = policy["require_official_source"] and any(
                needle in host for needle in ("docs.", "developer.", "platform.")
            )
            primary = any(token in host for token in ["openai.com", "anthropic.com", "github.com", "python.org", "mozilla.org"])
            if official:
                score += 3.0
                reasons.append("official-source")
            elif primary:
                score += 2.0
                reasons.append("primary-source")
            domain_bonus = 0.0
            for domain, value in domain_scores.items():
                if host == domain or host.endswith(f".{domain}"):
                    domain_bonus = value / 10.0
                    break
            if domain_bonus:
                score += domain_bonus
                reasons.append(f"domain-trust:{domain_bonus:+.1f}")
            freshness_hits = len(re.findall(r"\b(2024|2025|2026|today|latest|recent|updated|новост|сегодня|обновл)\b", haystack))
            freshness_weight = {"high": 0.8, "medium": 0.5, "low": 0.2}[policy["freshness_priority"]]
            if freshness_hits:
                freshness_score = min(1.5, freshness_hits * freshness_weight)
                score += freshness_score
                reasons.append(f"freshness:{freshness_score:+.1f}")
            overlap = sum(1 for term in query_terms if term in haystack)
            if overlap:
                topical_score = min(3.0, overlap * 0.6)
                score += topical_score
                reasons.append(f"topical:{topical_score:+.1f}")
            is_duplicate = url in seen_urls
            duplicate_penalty = -2.5 if is_duplicate else 0.0
            if duplicate_penalty:
                score += duplicate_penalty
                reasons.append(f"duplicate:{duplicate_penalty:.1f}")
            aggregator_penalty = -1.7 if host in aggregator_domains else 0.0
            if aggregator_penalty:
                score += aggregator_penalty
                reasons.append(f"aggregator:{aggregator_penalty:.1f}")
            social_penalty = -1.3 if host in social_domains else 0.0
            if social_penalty:
                score += social_penalty
                reasons.append(f"forum-social:{social_penalty:.1f}")
            if index == 0:
                score += 0.4
                reasons.append("serp-position:+0.4")
            if official and ("official docs" in subquery.lower() or "reference guide" in subquery.lower()):
                score += 1.0
                reasons.append("official-branch:+1.0")
            decision = "selected"
            if is_duplicate:
                decision = "reject"
                reasons.append("selection-policy:duplicate-url")
            elif policy["require_official_source"] and not (official or primary) and score < 2.5:
                decision = "reject"
                reasons.append("selection-policy:official-needed")
            elif score < 0.4:
                decision = "reject"
                reasons.append("selection-policy:low-score")
            entry = {
                "title": title or url,
                "url": url,
                "snippet": snippet,
                "score": round(score, 3),
                "reasons": reasons,
                "decision": decision,
                "host": host,
                "query": subquery,
            }
            page_trace["ranked_sources"].append(entry)
            if decision == "selected":
                ranked_sources.append(entry)
                page_trace["selected_to_read"].append({"url": url, "score": entry["score"], "reasons": reasons})
            else:
                page_trace["rejected"].append({"url": url, "score": entry["score"], "reasons": reasons})
            seen_urls.add(url)
        page_trace["ranked_sources"].sort(key=itemgetter("score"), reverse=True)
        page_trace["selected_to_read"].sort(key=itemgetter("score"), reverse=True)
        page_trace["rejected"].sort(key=itemgetter("score"), reverse=True)
        page_trace["read_results"] = []
        for selected in page_trace["selected_to_read"][:2]:
            ranked_entry = next((item for item in page_trace["ranked_sources"] if item["url"] == selected["url"]), None)
            if not ranked_entry:
                continue
            read_result = _read_page_findings(run.user_query, ranked_entry)
            page_trace["read_results"].append(read_result)
            run.findings.extend(read_result.get("findings") or [])
        run.visited_pages.append(page_trace)
    ranked_sources.sort(key=itemgetter("score"), reverse=True)
    selected_limit = max(policy["min_sources_before_synthesis"], min(6, len(ranked_sources)))
    run.candidate_sources = ranked_sources[:selected_limit]
    deduped_findings: List[Dict[str, Any]] = []
    seen_finding_keys: set[str] = set()
    for finding in run.findings:
        key = re.sub(r"\W+", " ", f"{finding.get('claim', '')} {finding.get('evidence_snippet', '')}".casefold()).strip()
        if not key or key in seen_finding_keys:
            continue
        seen_finding_keys.add(key)
        deduped_findings.append(finding)
    run.findings = deduped_findings

    top_findings = sorted(run.findings, key=lambda item: {"high": 3, "medium": 2, "low": 1}.get(str(item.get("confidence_local") or "low"), 1), reverse=True)[:3]
    if top_findings:
        summary_parts = []
        for finding in top_findings:
            claim = str(finding.get("claim") or "").strip()
            source_type = str(finding.get("source_type") or "page").strip()
            source_url = str(finding.get("source_url") or "").strip()
            if not claim:
                continue
            source_note = f" [{source_type}]" if source_type else ""
            if source_url:
                source_note += f" {source_url}"
            summary_parts.append(f"- {claim}{source_note}")
        run.final_answer = "Key findings:\n" + "\n".join(summary_parts) if summary_parts else "Research run completed, but extracted findings were too weak to summarize."
    else:
        run.final_answer = "Research run completed, but deep reading did not produce reliable findings."

    read_pages_ok = sum(1 for page in run.visited_pages for result in page.get("read_results", []) if result.get("status") == "ok")
    high_conf_findings = sum(1 for finding in run.findings if finding.get("confidence_local") == "high")
    strong_findings = sum(1 for finding in run.findings if finding.get("confidence_local") in {"high", "medium"})
    if read_pages_ok >= policy["min_sources_before_synthesis"] and high_conf_findings >= 1:
        run.confidence = "high"
    elif read_pages_ok >= 1 and strong_findings >= policy["min_sources_before_synthesis"]:
        run.confidence = "medium"
    else:
        run.confidence = "low"

    artifact = save_artifact(ctx, filename=f"research-run-{re.sub(r'-+', '-', re.sub(r'[^a-z0-9._-]+', '-', run.user_query.lower())).strip('-._') or 'query'}.json", content=json.dumps(asdict(run), ensure_ascii=False, indent=2), content_kind="json", source="research_run", mime_type="application/json", caption="Research run trace", metadata={"tool": "research_run", "intent_type": run.intent_type, "policy": policy, "query_plan": run.query_plan})
    payload = asdict(run)
    payload["intent_policy"] = policy
    payload["trace"] = artifact if isinstance(artifact, dict) else {"status": "error", "message": str(artifact)}
    return json.dumps(payload, ensure_ascii=False, indent=2)


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
                "description": "Run a structured research skeleton: infer intent, generate a multi-branch query plan, collect candidate sources, and save a readable JSON trace.",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            },
            _research_run,
        ),
    ]

