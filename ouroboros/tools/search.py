"""Web search tool — structured search plus research-run skeleton."""

from __future__ import annotations

import html
import json
import logging
import os
import time
import re
import socket
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List
from urllib.parse import urlparse

from ouroboros.artifacts import save_artifact
from ouroboros.circuit_breaker import CircuitBreaker
from ouroboros.llm import LLMClient
from ouroboros.tools.search_ranking import DOC_QUERY_MARKERS, POLICY_QUERY_MARKERS, READING_PRIORITY, collect_research_sources
from ouroboros.tools.search_transport import READING_BACKEND, classify_timeout_error, run_discovery_transport, timeout_profile
from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)
SEARXNG_DEFAULT = "http://localhost:8888"
SERPER_DEFAULT_URL = "https://google.serper.dev/search"
MAX_RESULTS = 5

# ---------------------------------------------------------------------------
# Circuit breakers — one per search backend
# ---------------------------------------------------------------------------
_searxng_breaker = CircuitBreaker("searxng", failure_threshold=3, recovery_timeout=60)
_serper_breaker = CircuitBreaker("serper", failure_threshold=5, recovery_timeout=120)
_ddg_breaker = CircuitBreaker("duckduckgo", failure_threshold=3, recovery_timeout=60)
_openai_breaker = CircuitBreaker("openai", failure_threshold=5, recovery_timeout=120)


_timeout_like = lambda exc: isinstance(exc, TimeoutError | socket.timeout) or exc.__class__.__name__ in {"TimeoutError", "ReadTimeout", "ConnectTimeout"}

DEFAULT_INTENT = "background_explainer"
MAX_SUBQUERIES, MAX_PAGES_READ, MAX_BROWSE_DEPTH, MAX_SYNTHESIS_ROUNDS = 6, 6, 2, 1


_normalize_text_block = lambda text: re.sub(r"\s+", " ", html.unescape(str(text or "")).replace(" ", " ")).strip()

def clean_sources(rows: Any) -> List[Dict[str, str]]:
    cleaned: List[Dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen_urls:
            continue
        seen_urls.add(url)
        cleaned.append({
            "title": str(item.get("title") or url).strip() or url,
            "url": url,
            "snippet": _normalize_text_block(str(item.get("snippet") or item.get("content") or item.get("body") or item.get("text") or "").strip()),
        })
        if len(cleaned) >= MAX_RESULTS:
            break
    return cleaned

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
    transport: Dict[str, Any] = field(default_factory=dict)
    timeout_profile: Dict[str, int] = field(default_factory=dict)
    timeout_events: List[Dict[str, Any]] = field(default_factory=list)
    interruption_checks: List[Dict[str, Any]] = field(default_factory=list)
    owner_interrupt_seen: bool = False
    discovery_backend_used: str = ""
    reading_backend_used: str = ""
    fallback_chain: List[str] = field(default_factory=list)
    pages_attempted: int = 0
    pages_succeeded: int = 0
    pages_failed: int = 0
    degraded_mode: bool = False
    debug_summary: Dict[str, Any] = field(default_factory=dict)
    status: str = "ok"
    interrupted: bool = False
    interrupt_reason: str = ""
    interrupt_stage: str = ""
    interrupt_message: str = ""


class ResearchInterrupted(RuntimeError):
    pass


def _checkpoint_inline(ctx: ToolContext, run: ResearchRun, stage: str, payload: Dict[str, Any]) -> None:
    checkpoint = getattr(ctx, "checkpoint", None)
    event = checkpoint(stage, payload=payload) if callable(checkpoint) else None
    record = {"stage": stage, **(payload or {}), "owner_message_seen": bool(event)}
    if event:
        record.update({
            "reason": str(event.get("reason") or ""),
            "message": str(event.get("message") or ""),
            "pending_count": len(event.get("pending_messages") or []),
        })
        run.interrupted = True
        run.owner_interrupt_seen = True
        run.interrupt_reason = str(event.get("reason") or "superseded_by_new_request")
        run.interrupt_stage = stage
        run.interrupt_message = str(event.get("message") or "")
        run.status = run.interrupt_reason
    run.interruption_checks = [*(run.interruption_checks or []), record]
    if event:
        raise ResearchInterrupted(run.interrupt_reason)

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
        unique_source_rows.append({"url": source_url, "source_type": str(finding.get("source_type") or "page").strip() or "page", "observed_at": str(finding.get("observed_at") or "").strip(), "claim": _normalize_text_block(str(finding.get("claim") or "").strip()), "evidence_snippet": _normalize_text_block(str(finding.get("evidence_snippet") or "").strip()), "authority": source_authority_map.get(source_url, "secondary")})
    key_finding_rows = []
    for finding in ranked_findings[:4]:
        claim = _normalize_text_block(str(finding.get("claim") or "").strip())
        evidence = _normalize_text_block(str(finding.get("evidence_snippet") or "").strip())
        source_url = str(finding.get("source_url") or "").strip()
        if claim and evidence and source_url:
            key_finding_rows.append({"claim": claim, "evidence_snippet": evidence, "source_url": source_url, "source_type": str(finding.get("source_type") or "page").strip() or "page", "observed_at": str(finding.get("observed_at") or "").strip(), "confidence_local": str(finding.get("confidence_local") or "low"), "authority": source_authority_map.get(source_url, "secondary")})
    if not key_finding_rows:
        short_answer = (
            "Источники расходятся; уверенный вывод без дополнительной проверки делать нельзя." if run.contradictions else
            "Официальный первоисточник не подтверждён; надёжный ответ пока не собран." if policy.require_official_source and not any(item.get("authority") == "official" for item in run.candidate_sources) else
            "После чтения выбранных страниц надёжных утверждений пока недостаточно." if not run.findings else
            "Данных пока недостаточно для уверенного вывода."
        )
        run.synthesis = {"answer_mode": run.answer_mode, "short_answer": short_answer, "key_findings": [], "evidence_backed_explanation": "После чтения выбранных страниц не набралось утверждений с достаточной опорой на evidence.", "uncertainty_caveats": list(dict.fromkeys(run.uncertainty_notes)), "sources": unique_source_rows}
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
    lowered = base.lower()
    policy = INTENT_POLICIES.get(intent_type, INTENT_POLICIES[DEFAULT_INTENT]); comparison_benchmark = bool(intent_type == "comparison_evaluation" and re.search(r"\b(benchmark|latency|throughput|eval|evaluation|head[- ]?to[- ]?head|leaderboard|arena)\b", lowered)); comparison_ecosystem = bool(intent_type == "comparison_evaluation" and re.search(r"\b(ecosystem|tooling|workflow|integration|integrations|maintainer|plugin|extension|community)\b", lowered)); policy_sensitive = bool(re.search(r"\b(policy|privacy|retention|data usage|data retention|artifact retention|training|legal|terms)\b", lowered)); docs_sensitive = bool(re.search(r"\b(docs|documentation|api|reference|sdk|guide|quickstart|manual|endpoint|rate limit)\b", lowered))
    freshness_priority = freshness_priority_override if freshness_priority_override in {"low", "medium", "high"} else policy.freshness_priority
    freshness_suffix = {
        "high": "latest updates",
        "medium": "recent",
        "low": "overview",
    }[freshness_priority]
    vendor_hints = [hint for needle, hint in (("openai", "platform.openai.com openai"), ("anthropic", "docs.anthropic.com anthropic"), ("github actions", "docs.github.com github actions"), ("github", "docs.github.com github"), ("cursor", "docs.cursor.com cursor"), ("huggingface", "huggingface docs huggingface")) if needle in lowered]
    vendor_hint = " ".join(dict.fromkeys(vendor_hints))
    official_suffix = "official benchmark methodology maintainers" if comparison_benchmark else ("official policy data usage retention privacy docs" if policy_sensitive else ((f"{vendor_hint} official docs api reference vendor documentation".strip()) if docs_sensitive or policy.require_official_source else "official source"))
    if comparison_ecosystem:
        official_suffix = f"{vendor_hint} official docs integrations maintainer repo pricing".strip() or "official docs integrations maintainer repo pricing"
    elif intent_type == "comparison_evaluation" and not comparison_benchmark:
        official_suffix = f"{vendor_hint} official compare pricing feature matrix vendor docs".strip() or "official compare pricing feature matrix vendor docs"
    alternative_suffix = {
        "comparison_evaluation": "tradeoffs benchmark methodology independent results" if comparison_benchmark else ("integrations plugin maintainer repo workflow docs" if comparison_ecosystem else "tradeoffs official compare pricing feature matrix"),
        "breaking_news": "timeline and reactions",
        "product_docs_api_lookup": ((f"{vendor_hint} reference guide vendor documentation api reference".strip()) if not policy_sensitive else (f"{vendor_hint} privacy policy data retention help center official guidance".strip())),
        "people_company_ecosystem_tracking": "ecosystem map",
        "fact_lookup": "exact value reference",
        "background_explainer": "overview",
    }.get(intent_type, "alternative wording")
    contradiction_suffix = {
        "breaking_news": "conflicting reports",
        "fact_lookup": "contradicting value",
        "product_docs_api_lookup": "limitations exceptions" if not policy_sensitive else "conflicting policy statement retention training opt out",
        "comparison_evaluation": "benchmark disagreement counterarguments" if comparison_benchmark else "counterarguments maintainer disagreements",
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
    if policy.require_official_source or comparison_benchmark:
        candidates.append(official_docs_query)
    candidates.append(alternative_wording_query)
    if policy.search_branches >= 4 or freshness_priority == "low":
        candidates.append(contradiction_check_query)
    if policy.search_branches >= 5 and not policy.require_official_source and not comparison_benchmark:
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

def _read_page_findings(query: str, source: Dict[str, Any], timeout_sec: int = 12) -> Dict[str, Any]:
    url = str(source.get("url") or "")
    read_reasons = list(source.get("reasons") or [])
    browser_reason = "browser_not_used: default direct urllib reading path"
    try:
        import urllib.request

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; VelesResearch/1.0; +https://github.com/gr1ng0333/veles)",
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
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
            "read_reason": read_reasons,
            "browser_reason": browser_reason,
            "browser_used": False,
        }
    except Exception as exc:
        timeout_info = classify_timeout_error(exc, "page_read") if _timeout_like(exc) else None
        return {
            "url": url,
            "status": "timeout" if timeout_info else "error",
            "content_type": "",
            "text_preview": "",
            "relevant_sections": [],
            "findings": [],
            "error": (timeout_info or {}).get("type") or repr(exc),
            "timeout_detail": (timeout_info or {}).get("detail", ""),
            "timeout_limit": timeout_sec if timeout_info else None,
            "read_reason": read_reasons,
            "browser_reason": browser_reason,
            "browser_used": False,
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

# ---------------------------------------------------------------------------
# DuckDuckGo HTML scraper helpers (stdlib only)
# ---------------------------------------------------------------------------
_DDG_URL = "https://html.duckduckgo.com/html/"
_DDG_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_DDG_LINK_RE = re.compile(
    r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL,
)
_DDG_SNIPPET_RE = re.compile(
    r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL,
)


def _parse_ddg_html(raw_html: str, limit: int = MAX_RESULTS) -> list[dict[str, str]]:
    """Extract search results from DuckDuckGo HTML page."""
    import urllib.parse

    links = _DDG_LINK_RE.findall(raw_html)
    snippets = _DDG_SNIPPET_RE.findall(raw_html)
    results: list[dict[str, str]] = []
    for i, (href, title_html) in enumerate(links[:limit * 2]):
        title = re.sub(r"<[^>]+>", "", html.unescape(title_html)).strip()
        snippet = re.sub(r"<[^>]+>", "", html.unescape(snippets[i])).strip() if i < len(snippets) else ""
        url = href
        if "duckduckgo.com" in href:
            parsed = urllib.parse.urlparse(href)
            uddg = urllib.parse.parse_qs(parsed.query).get("uddg")
            if uddg:
                url = urllib.parse.unquote(uddg[0])
            else:
                continue
        if not url.startswith(("http://", "https://")):
            continue
        results.append({"title": title or url, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def _search_duckduckgo(q: str, timeout_sec: int = 10) -> dict[str, Any]:
    """DuckDuckGo HTML scrape search backend (stdlib only, zero deps)."""
    import urllib.parse
    import urllib.request

    encoded = urllib.parse.quote_plus(q)
    url = f"{_DDG_URL}?q={encoded}"
    req = urllib.request.Request(url, headers={"User-Agent": _DDG_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning("DuckDuckGo search failed: %s", exc)
        if _timeout_like(exc):
            timeout_info = classify_timeout_error(exc, "discovery")
            return {"query": q, "status": "timeout", "backend": "duckduckgo", "sources": [], "answer": "", "error": timeout_info["type"], "error_detail": timeout_info["detail"], "timeout_limit": timeout_sec}
        return {"query": q, "status": "error", "backend": "duckduckgo", "sources": [], "answer": "", "error": repr(exc)}
    sources = clean_sources(_parse_ddg_html(raw))
    if not sources:
        return {"query": q, "status": "no_results", "backend": "duckduckgo", "sources": [], "answer": "", "error": "DuckDuckGo returned no usable results."}
    return {"query": q, "status": "ok", "backend": "duckduckgo", "sources": sources, "answer": "", "error": None}


# ---------------------------------------------------------------------------
# Circuit-breaker-aware backend dispatcher
# ---------------------------------------------------------------------------
_BACKEND_BREAKERS: dict[str, CircuitBreaker] = {
    "searxng": _searxng_breaker,
    "serper": _serper_breaker,
    "duckduckgo": _ddg_breaker,
    "openai": _openai_breaker,
}


def _web_search(ctx: ToolContext, query: str, timeout_sec: int | None = None) -> str:
    del ctx
    timeout_sec = max(int(timeout_sec or 20), 1)
    def run_backend(name: str, q: str) -> Dict[str, Any]:
        breaker = _BACKEND_BREAKERS.get(name)
        if breaker and not breaker.allow_request():
            log.debug("Circuit breaker OPEN for %s, skipping", name)
            return {"query": q, "status": "error", "backend": name, "sources": [], "answer": "", "error": f"Circuit breaker open for {name}."}
        try:
            result = _run_backend_inner(name, q, timeout_sec)
        except Exception:
            if breaker:
                breaker.record_failure()
            raise
        if result.get("status") == "ok":
            if breaker:
                breaker.record_success()
        else:
            if breaker:
                breaker.record_failure()
        return result

    def _run_backend_inner(name: str, q: str, tmo: int) -> Dict[str, Any]:
        if name == "searxng":
            if not SEARXNG_DEFAULT:
                return {"query": q, "status": "error", "backend": "searxng", "sources": [], "answer": "", "error": "SEARXNG_URL missing."}
            try:
                import urllib.parse
                import urllib.request

                params = urllib.parse.urlencode({"q": q, "format": "json", "language": "ru", "safesearch": 0})
                url = f"{SEARXNG_DEFAULT.rstrip('/')}/search?{params}"
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=tmo) as resp:
                    data = json.loads(resp.read())
                return {"query": q, "status": "ok", "backend": "searxng", "sources": clean_sources(data.get("results", [])), "answer": "", "error": None}
            except Exception as exc:
                log.warning("SearXNG search failed: %s", exc)
                if _timeout_like(exc):
                    timeout_info = classify_timeout_error(exc, "discovery")
                    return {"query": q, "status": "timeout", "backend": "searxng", "sources": [], "answer": "", "error": timeout_info["type"], "error_detail": timeout_info["detail"], "timeout_limit": tmo}
                return {"query": q, "status": "error", "backend": "searxng", "sources": [], "answer": "", "error": repr(exc)}
        if name == "duckduckgo":
            return _search_duckduckgo(q, timeout_sec=tmo)
        if name == "openai":
            if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY")):
                return {"query": q, "status": "error", "backend": "openai", "sources": [], "answer": "", "error": "Web search backend unavailable: no OPENAI_API_KEY or OPENROUTER_API_KEY configured."}
            client = LLMClient()
            model = os.environ.get("WEB_SEARCH_MODEL", "openai/gpt-4.1-mini")
            prompt = (
                "Search the web and answer the user query. Return JSON with keys: answer, sources. "
                "sources must be a list of objects with title, url, snippet. Only include real URLs."
            )
            msg, _usage = client.chat(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": q},
                ],
                model=model,
                max_tokens=1200,
                tools=None,
                reasoning_effort="low",
            )
            payload: Dict[str, Any] = {}
            content = msg.get("content")
            if isinstance(content, str):
                try:
                    payload = json.loads(content)
                except Exception:
                    payload = {"answer": content.strip(), "sources": []}
            elif isinstance(content, list):
                text = "".join(part.get("text", "") for part in content if isinstance(part, dict))
                try:
                    payload = json.loads(text)
                except Exception:
                    payload = {"answer": text.strip(), "sources": []}
            return {"query": q, "status": "ok", "backend": "openai", "sources": clean_sources(payload.get("sources", [])), "answer": str(payload.get("answer") or "").strip(), "error": None}
        # Default: serper
        api_key = os.environ.get("SERPER_API_KEY", "").strip()
        if not api_key:
            return {"query": q, "status": "error", "backend": "serper", "sources": [], "answer": "", "error": "SERPER_API_KEY missing."}
        try:
            import urllib.request

            payload = json.dumps({"q": q, "num": MAX_RESULTS}).encode("utf-8")
            req = urllib.request.Request(
                os.environ.get("SERPER_URL", SERPER_DEFAULT_URL),
                data=payload,
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=tmo) as resp:
                data = json.loads(resp.read())
            organic = data.get("organic") or []
            answer_box = data.get("answerBox") or {}
            knowledge_graph = data.get("knowledgeGraph") or {}
            result = {
                "query": q,
                "status": "ok",
                "backend": "serper",
                "sources": clean_sources([
                    {"title": row.get("title", ""), "url": row.get("link", ""), "content": row.get("snippet", "")}
                    for row in organic[:MAX_RESULTS] if isinstance(row, dict)
                ]),
                "answer": "\n\n".join(
                    bit for bit in [
                        str(answer_box.get("answer") or "").strip(),
                        str(answer_box.get("snippet") or "").strip(),
                        str(knowledge_graph.get("description") or "").strip(),
                    ] if bit
                ),
                "error": None,
            }
            if not result["sources"] and not result["answer"]:
                result.update({"status": "no_results", "error": "Serper returned no usable results."})
            return result
        except Exception as exc:
            log.warning("Serper search failed: %s", exc)
            if _timeout_like(exc):
                timeout_info = classify_timeout_error(exc, "discovery")
                return {"query": q, "status": "timeout", "backend": "serper", "sources": [], "answer": "", "error": timeout_info["type"], "error_detail": timeout_info["detail"], "timeout_limit": tmo}
            return {"query": q, "status": "error", "backend": "serper", "sources": [], "answer": "", "error": repr(exc)}

    # Fallback chain: searxng → serper → duckduckgo → openai
    result = run_discovery_transport(
        query,
        lambda q: run_backend("searxng", q),
        (
            ("serper", lambda q: run_backend("serper", q)),
            ("duckduckgo", lambda q: run_backend("duckduckgo", q)),
            ("openai", lambda q: run_backend("openai", q)),
        ),
    )
    result["sources"] = clean_sources(result.get("sources", []))
    transport = dict(result.get("transport") or {})
    if transport:
        transport["reading_backend"] = transport.get("reading_backend")
        result["transport"] = transport
    return json.dumps(result, ensure_ascii=False, indent=2)

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
    official_sensitive_query = any(marker in lowered for marker in POLICY_QUERY_MARKERS) or any(marker in lowered for marker in ("docs", "documentation", "api", "reference", "guide", "sdk", "rate limit"))
    policy_obj = IntentPolicy(freshness_priority=effective_freshness if effective_freshness in {"low", "medium", "high"} else base_policy.freshness_priority, search_branches=base_policy.search_branches, min_sources_before_synthesis=base_policy.min_sources_before_synthesis, require_official_source=(base_policy.require_official_source or official_sensitive_query))
    policy = asdict(policy_obj)
    run.intent_policy = policy
    run.budget_profile = budget
    run.transport = {"discovery_backend": "searxng", "reading_backend": READING_BACKEND, "fallback_backend": None, "fallback_backends": [], "events": []}
    run.timeout_profile = timeout_profile(run.budget_mode)
    run.discovery_backend_used = "searxng"
    run.reading_backend_used = READING_BACKEND
    deadline = time.monotonic() + max(int(run.timeout_profile.get("overall_run_timeout_sec", 90)), 1)
    plan = _build_query_plan(run.user_query, run.intent_type, max_subqueries=budget.max_subqueries, freshness_priority_override=policy_obj.freshness_priority)
    run.subqueries = list(plan.subqueries)
    run.query_plan = asdict(plan)
    try:
        ranked_sources = collect_research_sources(
            run,
            lambda query: json.loads(_web_search(ctx, query, timeout_sec=run.timeout_profile.get("discovery_timeout_sec", 20))),
            lambda user_query, ranked_entry: _read_page_findings(user_query, ranked_entry, timeout_sec=int(run.timeout_profile.get("page_read_timeout_sec", 15))),
            _detect_contradictions,
            checkpoint_fn=(lambda stage, **payload: (_checkpoint_inline(ctx, run, stage, payload))),
        )
        for event in (run.transport.get("events") or []):
            if str(event.get("status") or "") == "timeout":
                info = {"stage": "discovery", "error_type": "discovery_timeout", "timeout_limit": int(run.timeout_profile.get("discovery_timeout_sec", 20))}
                if event.get("backend"):
                    info["backend"] = str(event.get("backend") or "")
                if event.get("trigger"):
                    info["trigger"] = str(event.get("trigger") or "")
                if event.get("reason"):
                    info["reason"] = str(event.get("reason") or "")
                run.timeout_events = [*(run.timeout_events or []), info]
                note = f"discovery_timeout at discovery" + (f" via {info['backend']}" if info.get("backend") else "")
                if note not in run.uncertainty_notes:
                    run.uncertainty_notes = [*(run.uncertainty_notes or []), note]
        for page in run.visited_pages:
            for item in page.get("read_results") or []:
                if str(item.get("status") or "") == "timeout":
                    info = {"stage": "page_read", "error_type": "page_read_timeout", "timeout_limit": int(run.timeout_profile.get("page_read_timeout_sec", 15)), "backend": READING_BACKEND}
                    if item.get("error"):
                        info["reason"] = str(item.get("error") or "")
                    if item.get("url"):
                        info["url"] = str(item.get("url") or "")
                    run.timeout_events = [*(run.timeout_events or []), info]
                    note = "page_read_timeout at page_read via urllib"
                    if note not in run.uncertainty_notes:
                        run.uncertainty_notes = [*(run.uncertainty_notes or []), note]
        if time.monotonic() > deadline:
            timeout_info = classify_timeout_error(TimeoutError("overall research budget exceeded"), "overall")
            info = {"stage": "overall", "error_type": timeout_info["type"], "timeout_limit": int(run.timeout_profile.get("overall_run_timeout_sec", 90))}
            if timeout_info.get("detail"):
                info["reason"] = timeout_info["detail"]
            run.timeout_events = [*(run.timeout_events or []), info]
            note = f"{timeout_info['type']} at overall"
            if note not in run.uncertainty_notes:
                run.uncertainty_notes = [*(run.uncertainty_notes or []), note]
            raise TimeoutError(timeout_info["type"])
        selected_limit = max(policy["min_sources_before_synthesis"], min(budget.max_pages_read, len(ranked_sources)))
        run.candidate_sources = [{"title": item.get("title") or item.get("url"), "url": item.get("url"), "snippet": item.get("snippet", ""), "score": item.get("score"), "authority": item.get("authority", "unknown"), "benchmark_primary_type": item.get("benchmark_primary_type", ""), "comparison_source_class": item.get("comparison_source_class", ""), "page_kind": item.get("page_kind", "generic"), "reasons": list(item.get("reasons") or []), "decision": item.get("decision", "selected"), "query": item.get("query", ""), "host": item.get("host", "")} for item in ranked_sources[:selected_limit] if item.get("url")]
        deduped_findings: List[Dict[str, Any]] = []
        seen_finding_keys: set[str] = set()
        for finding in run.findings:
            key = re.sub(r"\W+", " ", f"{finding.get('claim', '')} {finding.get('evidence_snippet', '')}".casefold()).strip()
            if not key or key in seen_finding_keys:
                continue
            seen_finding_keys.add(key)
            deduped_findings.append(finding)
        run.findings = deduped_findings
        _checkpoint_inline(ctx, run, "pre_synthesis", {"findings": len(run.findings), "candidate_sources": len(run.candidate_sources), "pages_read": run.budget_trace.get("pages_read", 0)})
        if time.monotonic() > deadline:
            timeout_info = classify_timeout_error(TimeoutError("overall research budget exceeded before synthesis"), "overall")
            info = {"stage": "overall", "error_type": timeout_info["type"], "timeout_limit": int(run.timeout_profile.get("overall_run_timeout_sec", 90))}
            if timeout_info.get("detail"):
                info["reason"] = timeout_info["detail"]
            run.timeout_events = [*(run.timeout_events or []), info]
            note = f"{timeout_info['type']} at overall"
            if note not in run.uncertainty_notes:
                run.uncertainty_notes = [*(run.uncertainty_notes or []), note]
            raise TimeoutError(timeout_info["type"])
        run.budget_trace["synthesis_rounds_used"] = min(1, budget.max_synthesis_rounds)
        output_map = {"brief": "short_factual", "memo": "analyst_memo", "timeline": "timeline", "comparison": "comparison_brief"}; _apply_research_quality(run, policy_obj, output_map.get(str(output_mode or "").strip().lower()))
    except TimeoutError as exc:
        timeout_info = classify_timeout_error(exc, "overall")
        if not run.timeout_events or run.timeout_events[-1].get("error_type") != timeout_info["type"]:
            info = {"stage": "overall", "error_type": timeout_info["type"], "timeout_limit": int(run.timeout_profile.get("overall_run_timeout_sec", 90))}
            if timeout_info.get("detail"):
                info["reason"] = timeout_info["detail"]
            run.timeout_events = [*(run.timeout_events or []), info]
            note = f"{timeout_info['type']} at overall"
            if note not in run.uncertainty_notes:
                run.uncertainty_notes = [*(run.uncertainty_notes or []), note]
        run.status = timeout_info["type"]
        run.confidence = "low"
        summary = "Исследование остановлено по лимиту времени; часть шагов могла не успеть завершиться."
        if not run.final_answer:
            run.final_answer = summary
        run.synthesis = run.synthesis or {"short_answer": run.final_answer, "key_findings": [], "evidence_backed_explanation": run.final_answer, "uncertainty_caveats": list(run.uncertainty_notes), "sources": []}
        payload = asdict(run)
    except ResearchInterrupted:
        run.confidence = "low"
        if run.interrupt_reason == "cancel_requested":
            note = "research run was cancelled by owner"
            summary = "Исследование остановлено по новому сообщению владельца."
        else:
            note = "research run was superseded by a newer owner request"
            summary = "Исследование прервано, потому что пришёл новый запрос владельца."
        run.uncertainty_notes = list(dict.fromkeys([*run.uncertainty_notes, note]))
        if not run.final_answer:
            run.final_answer = summary
        run.synthesis = run.synthesis or {"short_answer": run.final_answer, "key_findings": [], "evidence_backed_explanation": run.final_answer, "uncertainty_caveats": list(run.uncertainty_notes), "sources": []}
        payload = asdict(run)
    else:
        payload = asdict(run)
    payload["discovery_backend_used"] = payload.get("discovery_backend_used") or str((payload.get("transport") or {}).get("events", [{}])[-1].get("backend") or (payload.get("transport") or {}).get("discovery_backend") or "")
    payload["reading_backend_used"] = payload.get("reading_backend_used") or str((payload.get("transport") or {}).get("reading_backend") or READING_BACKEND)
    payload["fallback_chain"] = list(dict.fromkeys([event.get("backend") for event in (payload.get("transport") or {}).get("events", [])[1:] if event.get("backend")]))
    read_results = [result for page in payload.get("visited_pages", []) for result in page.get("read_results", [])]
    payload["pages_attempted"] = len(read_results)
    payload["pages_succeeded"] = sum(1 for item in read_results if item.get("status") == "ok")
    payload["pages_failed"] = sum(1 for item in read_results if item.get("status") != "ok")
    payload["owner_interrupt_seen"] = bool(payload.get("owner_interrupt_seen") or any(item.get("owner_message_seen") for item in payload.get("interruption_checks", [])))
    payload["degraded_mode"] = bool(payload.get("interrupted") or payload.get("timeout_events") or payload.get("pages_failed") or payload.get("fallback_chain"))
    payload["debug_summary"] = {
        "intent_type": payload.get("intent_type"),
        "status": payload.get("status"),
        "confidence": payload.get("confidence"),
        "discovery_backend_used": payload.get("discovery_backend_used"),
        "reading_backend_used": payload.get("reading_backend_used"),
        "fallback_chain": payload.get("fallback_chain", []),
        "pages_attempted": payload.get("pages_attempted", 0),
        "pages_succeeded": payload.get("pages_succeeded", 0),
        "pages_failed": payload.get("pages_failed", 0),
        "timeout_event_types": [item.get("error_type") for item in payload.get("timeout_events", []) if item.get("error_type")],
        "interruption_checks": len(payload.get("interruption_checks", [])),
        "owner_interrupt_seen": payload.get("owner_interrupt_seen", False),
        "degraded_mode": payload.get("degraded_mode", False),
    }
    artifact = save_artifact(ctx, filename=f"research-run-{re.sub(r'-+', '-', re.sub(r'[^a-z0-9._-]+', '-', run.user_query.lower())).strip('-._') or 'query'}.json", content=json.dumps(payload, ensure_ascii=False, indent=2), content_kind="json", source="research_run", mime_type="application/json", caption="Research run trace", metadata={"tool": "research_run", "intent_type": run.intent_type, "policy": policy, "query_plan": run.query_plan})
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
            timeout_sec=180,
        ),
        ToolEntry(
            "deep_research",
            {
                "name": "deep_research",
                "description": "Run research in a dialogue-friendly mode and return a compact evidence-backed answer with configurable depth, output shape, and freshness bias.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "depth": {"type": "string", "enum": ["cheap", "balanced", "deep"]}, "output": {"type": "string", "enum": ["brief", "memo", "timeline", "comparison"]}, "freshness_bias": {"type": "string", "enum": ["low", "medium", "high"]}},
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
            timeout_sec=180,
        ),
    ]
