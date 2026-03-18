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
MAX_SUBQUERIES, MAX_PAGES_READ, MAX_BROWSE_DEPTH, MAX_SYNTHESIS_ROUNDS = 6, 6, 2, 1

@dataclass(frozen=True)
class IntentPolicy:
    freshness_priority: str
    search_branches: int
    min_sources_before_synthesis: int
    require_official_source: bool

@dataclass(frozen=True)
class ResearchBudgetProfile:
    max_subqueries: int
    max_pages_read: int
    max_browse_depth: int
    max_synthesis_rounds: int
    early_stop_min_read_pages: int
    early_stop_min_findings: int

@dataclass(frozen=True)
class QueryPlan:
    primary_query: str
    freshness_query: str
    official_docs_query: str
    alternative_wording_query: str
    contradiction_check_query: str
    subqueries: List[str]
    branch_budget: int

BUDGET_PROFILES: Dict[str, ResearchBudgetProfile] = {
    "cheap": ResearchBudgetProfile(3, 2, 1, 1, 1, 2),
    "balanced": ResearchBudgetProfile(4, 4, 2, 1, 2, 2),
    "deep": ResearchBudgetProfile(MAX_SUBQUERIES, MAX_PAGES_READ, MAX_BROWSE_DEPTH, MAX_SYNTHESIS_ROUNDS, 3, 4),
}
DOMAIN_SCORES: Dict[str, float] = {
    "docs.python.org": 2.6, "platform.openai.com": 2.8, "openai.com": 2.4, "docs.anthropic.com": 2.8,
    "anthropic.com": 2.4, "developer.mozilla.org": 2.5, "developers.google.com": 2.6, "github.com": 1.8,
    "techcrunch.com": 1.2, "theverge.com": 1.1,
}
AGGREGATOR_DOMAINS = {"www.reddit.com", "reddit.com", "news.ycombinator.com", "hn.algolia.com", "medium.com", "towardsdatascience.com", "www.linkedin.com", "linkedin.com"}
SOCIAL_DOMAINS = {"x.com", "twitter.com", "www.x.com", "www.twitter.com", "facebook.com", "www.facebook.com"}
OFFICIAL_HOST_MARKERS = ("docs.", "developer.", "developers.", "platform.", "api.")
PRIMARY_HOST_MARKERS = ("openai.com", "anthropic.com", "github.com", "python.org", "mozilla.org", "google.com")
ANTI_SYCOPHANCY_PHRASES = (
    "отличный вопрос",
    "классный вопрос",
    "крутой вопрос",
    "great question",
    "awesome question",
    "you are right",
)
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
    freshness_summary: Dict[str, Any] = field(default_factory=dict)
    contradictions: List[Dict[str, Any]] = field(default_factory=list)
    uncertainty_notes: List[str] = field(default_factory=list)
    answer_mode: str = "short_factual"
    synthesis: Dict[str, Any] = field(default_factory=dict)
    budget_mode: str = "balanced"
    budget_limits: Dict[str, Any] = field(default_factory=dict)
    budget_trace: Dict[str, Any] = field(default_factory=dict)
def _source_authority(host: str, require_official: bool) -> tuple[str, float, list[str]]:
    authority = "secondary"
    score = 0.0
    reasons: List[str] = []
    if require_official and any(marker in host for marker in OFFICIAL_HOST_MARKERS):
        authority = "official"
        score = 3.0
        reasons.append("official-source")
    elif any(token in host for token in PRIMARY_HOST_MARKERS):
        authority = "primary"
        score = 2.0
        reasons.append("primary-source")
    elif host in AGGREGATOR_DOMAINS or host in SOCIAL_DOMAINS:
        authority = "community"
    return authority, score, reasons

READING_PRIORITY = lambda entry: ({"official": 0, "primary": 1, "secondary": 2, "community": 3}.get(str(entry.get("authority") or "secondary"), 2), -float(entry.get("score") or 0.0))
NEUTRALIZE_TEXT = lambda value: re.sub(r"\s+", " ", __import__("functools").reduce(lambda acc, phrase: re.sub(re.escape(phrase), "", acc, flags=re.IGNORECASE), ANTI_SYCOPHANCY_PHRASES, str(value or ""))).strip(" ,.!?:;\n\t")

def _compose_uncertain_short_answer(run: ResearchRun, policy: IntentPolicy) -> str:
    if run.contradictions:
        return "Источники расходятся; уверенный вывод без дополнительной проверки делать нельзя."
    if policy.require_official_source and not any(item.get("authority") == "official" for item in run.candidate_sources):
        return "Официальный первоисточник не подтверждён; надёжный ответ пока не собран."
    if not run.findings:
        return "После чтения выбранных страниц надёжных утверждений пока недостаточно."
    return "Данных пока недостаточно для уверенного вывода."

def _detect_contradictions(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    numeric_findings, status_findings = [], []
    for finding in findings:
        claim = str(finding.get("claim") or "").strip()
        lowered = claim.lower()
        cleaned = re.sub(r"[^a-zа-я0-9\s]", " ", claim.casefold())
        cleaned = re.sub(r"\b(19|20)\d{2}\b", " ", cleaned)
        cleaned = re.sub(r"\b\d+(?:[.,]\d+)?\b", " ", cleaned)
        cleaned = re.sub(r"\b(v|version|ver|rpm|ms|s|sec|seconds|minutes|percent|%)\b", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        tokens = [tok for tok in cleaned.split() if len(tok) >= 3]
        stop = {"the", "and", "for", "with", "from", "that", "this", "our", "says", "according", "guide"}
        primary_tokens = [tok for tok in tokens if tok not in stop]
        topic_key = " ".join((primary_tokens or tokens)[:8]) if tokens else ""
        numbers = re.findall(r"\b\d+(?:[.,]\d+)?\b", claim)
        if topic_key and numbers:
            numeric_findings.append((topic_key, tuple(numbers[:2]), finding))
        if topic_key and any(token in lowered for token in ("available", "unavailable", "deprecated", "supported", "unsupported", "announced", "cancelled", "delayed", "released", "planned", "removed")):
            status_findings.append((topic_key, finding))
    contradictions = []
    for idx, (topic_a, nums_a, finding_a) in enumerate(numeric_findings):
        for topic_b, nums_b, finding_b in numeric_findings[idx + 1:]:
            if topic_a != topic_b or nums_a == nums_b:
                continue
            contradictions.append({"kind": "numeric_mismatch", "topic": topic_a, "claim_a": finding_a.get("claim"), "claim_b": finding_b.get("claim"), "source_a": finding_a.get("source_url"), "source_b": finding_b.get("source_url"), "observed_at_a": finding_a.get("observed_at"), "observed_at_b": finding_b.get("observed_at")})
    opposite_pairs = {"available": "unavailable", "supported": "unsupported", "released": "planned", "announced": "cancelled"}
    for idx, (topic_a, finding_a) in enumerate(status_findings):
        claim_a = str(finding_a.get("claim") or "").lower()
        for topic_b, finding_b in status_findings[idx + 1:]:
            claim_b = str(finding_b.get("claim") or "").lower()
            if topic_a != topic_b:
                continue
            if any((left in claim_a and right in claim_b) or (left in claim_b and right in claim_a) for left, right in opposite_pairs.items()):
                contradictions.append({"kind": "status_conflict", "topic": topic_a, "claim_a": finding_a.get("claim"), "claim_b": finding_b.get("claim"), "source_a": finding_a.get("source_url"), "source_b": finding_b.get("source_url"), "observed_at_a": finding_a.get("observed_at"), "observed_at_b": finding_b.get("observed_at")})
    deduped, seen = [], set()
    for item in contradictions:
        key = re.sub(r"\W+", " ", f"{item.get('kind','')} {item.get('topic','')} {item.get('claim_a','')} {item.get('claim_b','')}".casefold()).strip()
        rev = re.sub(r"\W+", " ", f"{item.get('kind','')} {item.get('topic','')} {item.get('claim_b','')} {item.get('claim_a','')}".casefold()).strip()
        if not key or key in seen or rev in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:5]

def _render_synthesis(run: ResearchRun, policy: IntentPolicy) -> None:
    source_authority_map = {str(item.get("url") or ""): str(item.get("authority") or "secondary") for item in run.candidate_sources}
    ranked_findings = sorted(run.findings, key=lambda item: ({"official": 4, "primary": 3, "secondary": 2, "community": 1}.get(source_authority_map.get(str(item.get("source_url") or ""), "secondary"), 2), {"high": 3, "medium": 2, "low": 1}.get(str(item.get("confidence_local") or "low"), 1), 1 if str(item.get("observed_at") or "").strip() else 0, len(str(item.get("evidence_snippet") or ""))), reverse=True)
    unique_source_rows, seen_source_urls = [], set()
    for finding in ranked_findings:
        source_url = str(finding.get("source_url") or "").strip()
        if not source_url or source_url in seen_source_urls:
            continue
        seen_source_urls.add(source_url)
        unique_source_rows.append({"url": source_url, "source_type": str(finding.get("source_type") or "page").strip() or "page", "observed_at": str(finding.get("observed_at") or "").strip(), "claim": NEUTRALIZE_TEXT(str(finding.get("claim") or "").strip()), "evidence_snippet": NEUTRALIZE_TEXT(str(finding.get("evidence_snippet") or "").strip()), "authority": source_authority_map.get(source_url, "secondary")})
    key_finding_rows = []
    for finding in ranked_findings[:4]:
        claim = NEUTRALIZE_TEXT(str(finding.get("claim") or "").strip())
        evidence = NEUTRALIZE_TEXT(str(finding.get("evidence_snippet") or "").strip())
        source_url = str(finding.get("source_url") or "").strip()
        if claim and evidence and source_url:
            key_finding_rows.append({"claim": claim, "evidence_snippet": evidence, "source_url": source_url, "source_type": str(finding.get("source_type") or "page").strip() or "page", "observed_at": str(finding.get("observed_at") or "").strip(), "confidence_local": str(finding.get("confidence_local") or "low"), "authority": source_authority_map.get(source_url, "secondary")})
    if not key_finding_rows:
        run.synthesis = {"answer_mode": run.answer_mode, "short_answer": _compose_uncertain_short_answer(run, policy), "key_findings": [], "evidence_backed_explanation": "После чтения выбранных страниц не набралось утверждений с достаточной опорой на evidence.", "uncertainty_caveats": list(dict.fromkeys(run.uncertainty_notes)), "sources": unique_source_rows}
        run.final_answer = run.synthesis["short_answer"]
        return
    primary_rows = [item for item in key_finding_rows if item.get("authority") in {"official", "primary"}]
    support_rows = [item for item in key_finding_rows if item.get("authority") not in {"official", "primary"}]
    ordered_rows = primary_rows + support_rows
    if run.answer_mode == "timeline":
        ordered_rows = sorted(ordered_rows, key=lambda item: (item["observed_at"] or "9999-99-99", item["authority"] not in {"official", "primary"}, item["confidence_local"] != "high"))
    short_answer = ordered_rows[0]["claim"] if run.answer_mode == "short_factual" else {"comparison_brief": "Сравнение по прочитанным источникам: " + "; ".join(item["claim"] for item in ordered_rows[:2]), "analyst_memo": "По прочитанным источникам картина такая: " + "; ".join(item["claim"] for item in ordered_rows[:2]), "timeline": ordered_rows[0]["claim"]}.get(run.answer_mode, ordered_rows[0]["claim"])
    explanation_prefix = {"short_factual": "Что подтверждают прочитанные источники:", "analyst_memo": "Что подтверждают прочитанные источники:", "comparison_brief": "Сопоставление подтверждённых утверждений:", "timeline": "Хронология/последовательность по прочитанным источникам:"}[run.answer_mode]
    evidence_lines = [f"- {item['claim']}\n  evidence: {item['evidence_snippet']}\n  source: {item['source_url']} [{item['source_type']}, {item['authority']}{(' @ ' + item['observed_at']) if item['observed_at'] else ''}]" for item in ordered_rows]
    caveats = list(dict.fromkeys(note for note in run.uncertainty_notes if note))
    run.synthesis = {"answer_mode": run.answer_mode, "short_answer": short_answer, "key_findings": ordered_rows, "evidence_backed_explanation": explanation_prefix + "\n" + "\n".join(evidence_lines), "uncertainty_caveats": caveats, "sources": unique_source_rows}
    final_blocks = [f"Режим ответа: {run.answer_mode}", "", "Короткий ответ:", short_answer, "", "Ключевые находки:"]
    final_blocks.extend(f"- {item['claim']}\n  evidence: {item['evidence_snippet']}\n  source: {item['source_url']} [{item['authority']}]" for item in ordered_rows)
    final_blocks += ["", explanation_prefix, *evidence_lines]
    if caveats:
        final_blocks += ["", "Неопределённость / caveats:", *(f"- {note}" for note in caveats)]
    if unique_source_rows:
        final_blocks += ["", "Sources:", *(f"- {item['url']} [{item['source_type']}, {item['authority']}]" for item in unique_source_rows)]
    run.final_answer = "\n".join(final_blocks)

def _build_query_plan(query: str, intent_type: str, max_subqueries: int = MAX_SUBQUERIES, freshness_priority_override: str | None = None) -> QueryPlan:
    base = re.sub(r"\s+", " ", str(query or "").strip())
    policy = INTENT_POLICIES.get(intent_type, INTENT_POLICIES[DEFAULT_INTENT])
    freshness_priority = freshness_priority_override if freshness_priority_override in {"low", "medium", "high"} else policy.freshness_priority

    freshness_suffix = {
        "high": "latest updates",
        "medium": "recent",
        "low": "overview",
    }[freshness_priority]
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
    if freshness_priority in {"high", "medium"}:
        candidates.append(freshness_query)
    if policy.require_official_source:
        candidates.append(official_docs_query)
    candidates.append(alternative_wording_query)
    if policy.search_branches >= 4 or freshness_priority == "low":
        candidates.append(contradiction_check_query)
    if policy.search_branches >= 5 and not policy.require_official_source:
        candidates.append(official_docs_query)

    branch_budget = max(1, min(policy.search_branches, max_subqueries, MAX_SUBQUERIES))
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
        observed_at_patterns = [
            r"\b(2024|2025|2026)[-/.](\d{1,2})[-/.](\d{1,2})\b",
            r"\b(2024|2025|2026)\b",
        ]
        observed_at = ""
        for pattern in observed_at_patterns:
            match = re.search(pattern, clean_text)
            if match:
                observed_at = match.group(0)
                break
        freshness_markers = [marker for marker in ("today", "latest", "recent", "updated", "announced", "released", "new", "сегодня", "обновл", "анонс", "релиз") if marker in clean_text.lower()]
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
                "freshness_signals": freshness_markers,
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
def _apply_research_quality(run: ResearchRun, policy: IntentPolicy, output_mode_override: str | None = None) -> None:
    freshness_known = sum(1 for finding in run.findings if str(finding.get("observed_at") or "").strip())
    freshness_unknown = max(0, len(run.findings) - freshness_known)
    run.freshness_summary = {"known_dated_findings": freshness_known, "undated_findings": freshness_unknown, "freshness_priority": policy.freshness_priority}
    if freshness_unknown and policy.freshness_priority in {"high", "medium"}:
        run.uncertainty_notes.append("Часть найденных утверждений без явной даты публикации или обновления.")
    run.contradictions = _detect_contradictions(run.findings)
    if run.contradictions:
        run.uncertainty_notes.append("Источники расходятся по части утверждений; смотри contradictions в trace.")
    read_pages_ok = sum(1 for page in run.visited_pages for result in page.get("read_results", []) if result.get("status") == "ok")
    high_conf_findings = sum(1 for finding in run.findings if finding.get("confidence_local") == "high")
    strong_findings = sum(1 for finding in run.findings if finding.get("confidence_local") in {"high", "medium"})
    has_official_or_primary = any(str(item.get("authority") or "") in {"official", "primary"} for item in run.candidate_sources)
    run.confidence = "high" if read_pages_ok >= policy.min_sources_before_synthesis and high_conf_findings >= 1 else ("medium" if read_pages_ok >= 1 and strong_findings >= policy.min_sources_before_synthesis else "low")
    if freshness_unknown and run.confidence == "high": run.confidence = "medium"
    elif freshness_unknown and run.confidence == "medium" and policy.freshness_priority == "high": run.confidence = "low"
    if run.contradictions and run.confidence in {"high", "medium"}: run.confidence = "medium" if run.confidence == "high" else "low"
    if policy.require_official_source and not has_official_or_primary:
        run.confidence = "low"
        run.uncertainty_notes.append("Официальный или первичный источник не подтверждён; вывод опирается на вторичные пересказы.")
    mode_by_intent = {"fact_lookup": "short_factual", "product_docs_api_lookup": "short_factual", "comparison_evaluation": "comparison_brief", "breaking_news": "timeline", "people_company_ecosystem_tracking": "timeline", "background_explainer": "analyst_memo"}
    run.answer_mode = output_mode_override if output_mode_override in {"short_factual", "analyst_memo", "comparison_brief", "timeline"} else mode_by_intent.get(run.intent_type, "short_factual")
    _render_synthesis(run, policy)


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
        sources = clean_sources(sources)
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
    primary_sources: List[Dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in primary.get("sources") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen_urls:
            continue
        seen_urls.add(url)
        primary_sources.append({"title": str(item.get("title") or url).strip() or url, "url": url, "snippet": str(item.get("snippet") or item.get("content") or "").strip()})
        if len(primary_sources) >= MAX_RESULTS:
            break
    if primary_sources and str(primary.get("status") or "") == "ok":
        primary["sources"] = primary_sources
        return json.dumps(primary, ensure_ascii=False, indent=2)
    fallback = _search_openai(query)
    merged_sources: List[Dict[str, str]] = []
    seen_urls = set()
    for bucket in (primary.get("sources") or [], fallback.get("sources") or []):
        for item in bucket:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url.startswith(("http://", "https://")) or url in seen_urls:
                continue
            seen_urls.add(url)
            merged_sources.append({"title": str(item.get("title") or url).strip() or url, "url": url, "snippet": str(item.get("snippet") or item.get("content") or "").strip()})
            if len(merged_sources) >= MAX_RESULTS:
                break
        if len(merged_sources) >= MAX_RESULTS:
            break
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

def _research_run(ctx: ToolContext, query: str, budget_mode: str = "balanced", output_mode: str | None = None, freshness_bias: str | None = None) -> str:
    run = ResearchRun(user_query=str(query or "").strip())
    run.budget_mode = str(budget_mode or "balanced").strip().lower() or "balanced"
    budget = BUDGET_PROFILES.get(run.budget_mode, BUDGET_PROFILES["balanced"])
    if run.budget_mode not in BUDGET_PROFILES:
        run.budget_mode = "balanced"
    run.budget_limits = asdict(budget)
    run.budget_trace = {"subqueries_executed": 0, "pages_read": 0, "browse_depth_used": 0, "synthesis_rounds_used": 0, "early_stop_triggered": False, "early_stop_reason": "", "search_calls": 0, "selected_sources_considered": 0}
    lowered = run.user_query.lower()
    if not lowered:
        run.intent_type = DEFAULT_INTENT
    else:
        run.intent_type = next((intent_type for intent_type, keywords in INTENT_KEYWORDS if any(keyword in lowered for keyword in keywords)), ("fact_lookup" if any(ch.isdigit() for ch in lowered) else DEFAULT_INTENT))
    base_policy = INTENT_POLICIES.get(run.intent_type, INTENT_POLICIES[DEFAULT_INTENT])
    effective_freshness = str(freshness_bias or "").strip().lower() or base_policy.freshness_priority
    policy_obj = IntentPolicy(freshness_priority=effective_freshness if effective_freshness in {"low", "medium", "high"} else base_policy.freshness_priority, search_branches=base_policy.search_branches, min_sources_before_synthesis=base_policy.min_sources_before_synthesis, require_official_source=base_policy.require_official_source)
    policy = asdict(policy_obj)
    plan = _build_query_plan(run.user_query, run.intent_type, max_subqueries=budget.max_subqueries, freshness_priority_override=policy_obj.freshness_priority)
    run.subqueries = list(plan.subqueries)
    run.query_plan = asdict(plan)
    seen_urls: set[str] = set()
    ranked_sources: List[Dict[str, Any]] = []
    query_terms = {term for term in re.findall(r"[a-zA-Zа-яА-Я0-9_+-]{3,}", run.user_query.lower()) if len(term) >= 3}

    for subquery in run.subqueries:
        result = json.loads(_web_search(ctx, subquery))
        run.budget_trace["search_calls"] += 1
        run.budget_trace["subqueries_executed"] += 1
        sources: List[Dict[str, str]] = []
        source_urls: set[str] = set()
        for item in result.get("sources") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url.startswith(("http://", "https://")) or url in source_urls:
                continue
            source_urls.add(url)
            sources.append({"title": str(item.get("title") or url).strip() or url, "url": url, "snippet": str(item.get("snippet") or item.get("content") or "").strip()})
            if len(sources) >= 10:
                break
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
            authority, authority_score, authority_reasons = _source_authority(host, policy["require_official_source"])
            official = authority == "official"
            primary = authority in {"official", "primary"}
            if authority_score:
                score += authority_score
                reasons.extend(authority_reasons)
            if authority == "community":
                reasons.append("retelling-or-community")
            domain_bonus = 0.0
            for domain, value in DOMAIN_SCORES.items():
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
            aggregator_penalty = -1.7 if host in AGGREGATOR_DOMAINS else 0.0
            if aggregator_penalty:
                score += aggregator_penalty
                reasons.append(f"aggregator:{aggregator_penalty:.1f}")
            social_penalty = -1.3 if host in SOCIAL_DOMAINS else 0.0
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
            elif policy["require_official_source"] and not (official or primary) and score < 1.2:
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
                "authority": authority,
            }
            page_trace["ranked_sources"].append(entry)
            if decision == "selected":
                ranked_sources.append(entry)
                page_trace["selected_to_read"].append({"url": url, "score": entry["score"], "reasons": reasons})
            else:
                page_trace["rejected"].append({"url": url, "score": entry["score"], "reasons": reasons})
            seen_urls.add(url)
        page_trace["ranked_sources"].sort(key=lambda item: (READING_PRIORITY(item),), reverse=False)
        page_trace["selected_to_read"].sort(key=lambda item: READING_PRIORITY(next((row for row in page_trace["ranked_sources"] if row["url"] == item["url"]), item)))
        page_trace["rejected"].sort(key=itemgetter("score"), reverse=True)
        run.visited_pages.append(page_trace)
        read_budget_remaining = max(0, budget.max_pages_read - run.budget_trace["pages_read"])
        browse_depth = min(budget.max_browse_depth, read_budget_remaining)
        page_trace["read_results"] = []
        page_trace["budget"] = {"max_browse_depth": budget.max_browse_depth, "browse_depth_used": 0, "pages_read_before_branch": run.budget_trace["pages_read"], "pages_read_remaining": read_budget_remaining}
        run.budget_trace["selected_sources_considered"] += len(page_trace["selected_to_read"])
        for selected in page_trace["selected_to_read"][:browse_depth]:
            ranked_entry = next((item for item in page_trace["ranked_sources"] if item["url"] == selected["url"]), None)
            if not ranked_entry:
                continue
            read_result = _read_page_findings(run.user_query, ranked_entry)
            page_trace["read_results"].append(read_result)
            page_trace["budget"]["browse_depth_used"] += 1
            run.budget_trace["pages_read"] += 1
            run.budget_trace["browse_depth_used"] = max(run.budget_trace["browse_depth_used"], page_trace["budget"]["browse_depth_used"])
            run.findings.extend(read_result.get("findings") or [])
            read_pages_ok = sum(1 for page in run.visited_pages for result in page.get("read_results", []) if result.get("status") == "ok")
            strong_findings = sum(1 for finding in run.findings if finding.get("confidence_local") in {"high", "medium"})
            has_conflict = bool(getattr(run, "contradictions", []))
            has_official = any("official-source" in reason for source in run.candidate_sources for reason in source.get("reasons", []))
            should_stop = read_pages_ok >= budget.early_stop_min_read_pages and strong_findings >= budget.early_stop_min_findings and not has_conflict
            if policy.get("require_official_source"):
                should_stop = False
            if page_trace["query"] == run.query_plan.get("contradiction_check_query"):
                should_stop = False
            if should_stop or run.budget_trace["pages_read"] >= budget.max_pages_read:
                run.budget_trace["early_stop_triggered"] = True
                run.budget_trace["early_stop_reason"] = "enough-evidence" if should_stop else "page-budget-exhausted"
                break
    if not run.budget_trace["early_stop_reason"] and run.budget_trace["subqueries_executed"] >= budget.max_subqueries:
        run.budget_trace["early_stop_reason"] = "subquery-budget-exhausted"
    ranked_sources.sort(key=READING_PRIORITY)
    selected_limit = max(policy["min_sources_before_synthesis"], min(budget.max_pages_read, len(ranked_sources)))
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

    run.budget_trace["synthesis_rounds_used"] = min(1, budget.max_synthesis_rounds)
    output_map = {"brief": "short_factual", "memo": "analyst_memo", "timeline": "timeline", "comparison": "comparison_brief"}; _apply_research_quality(run, policy_obj, output_map.get(str(output_mode or "").strip().lower()))
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
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"}, "budget_mode": {"type": "string", "enum": ["cheap", "balanced", "deep"]},
                        "output_mode": {"type": "string", "enum": ["brief", "memo", "timeline", "comparison"]}, "freshness_bias": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                    "required": ["query"],
                },
            },
            lambda ctx, query, budget_mode="balanced", output_mode=None, freshness_bias=None: _research_run(ctx, query, budget_mode, output_mode, freshness_bias),
        ),
        ToolEntry(
            "deep_research",
            {
                "name": "deep_research",
                "description": "Run research in a dialogue-friendly mode and return a compact evidence-backed answer with configurable depth, output shape, and freshness bias.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"}, "depth": {"type": "string", "enum": ["cheap", "balanced", "deep"]},
                        "output": {"type": "string", "enum": ["brief", "memo", "timeline", "comparison"]}, "freshness_bias": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                    "required": ["query"],
                },
            },
            lambda ctx, query, depth="balanced", output="brief", freshness_bias="medium": (
                lambda payload, synthesis, caveats, sources: "\n".join(
                    [
                        f"Исследование: {payload.get('user_query', '')}", f"Глубина: {payload.get('budget_mode', 'balanced')} | Формат: {payload.get('answer_mode', 'short_factual')} | Уверенность: {payload.get('confidence', 'low')}", "", str(synthesis.get("short_answer") or payload.get("final_answer") or "Надёжный вывод пока не собран."),
                        *(["", "Оговорки:", *[f"- {note}" for note in caveats[:4]]] if caveats else []),
                        *(["", "Источники:", *[f"- {url}" for url in sources[:5]]] if sources else []),
                    ]
                )
            )(
                (payload := json.loads(_research_run(ctx, query, depth, output, freshness_bias))),
                (payload.get("synthesis") or {}),
                ((payload.get("synthesis") or {}).get("uncertainty_caveats") or []),
                [
                    str(item.get("url") or "").strip()
                    for item in ((payload.get("synthesis") or {}).get("sources") or [])
                    if str(item.get("url") or "").strip()
                ],
            ),
        ),
    ]
