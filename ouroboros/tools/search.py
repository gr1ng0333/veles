"""Web search tool — structured search plus research-run skeleton."""

from __future__ import annotations

import html
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List
from urllib.parse import urlparse

from ouroboros.artifacts import save_artifact
from ouroboros.llm import LLMClient
from ouroboros.tools.search_ranking import DOC_QUERY_MARKERS, POLICY_QUERY_MARKERS, READING_PRIORITY, collect_research_sources
from ouroboros.tools.search_transport import READING_BACKEND, run_discovery_transport
from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)
SEARXNG_DEFAULT = "http://localhost:8888"
SERPER_DEFAULT_URL = "https://google.serper.dev/search"
MAX_RESULTS = 5
DEFAULT_INTENT = "background_explainer"
MAX_SUBQUERIES, MAX_PAGES_READ, MAX_BROWSE_DEPTH, MAX_SYNTHESIS_ROUNDS = 6, 6, 2, 1


_normalize_text_block = lambda text: re.sub(r"\s+", " ", html.unescape(str(text or "")).replace(" ", " ")).strip()

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
    status: str = "ok"
    interrupted: bool = False
    interrupt_reason: str = ""
    interrupt_stage: str = ""
    interrupt_message: str = ""


class ResearchInterrupted(RuntimeError):
    pass

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
    policy = INTENT_POLICIES.get(intent_type, INTENT_POLICIES[DEFAULT_INTENT]); comparison_benchmark = bool(intent_type == "comparison_evaluation" and re.search(r"\b(benchmark|latency|throughput|eval|evaluation|head[- ]?to[- ]?head|leaderboard|arena)\b", base.lower())); policy_sensitive = bool(re.search(r"\b(policy|privacy|retention|data usage|data retention|artifact retention|training)\b", base.lower())); docs_sensitive = bool(re.search(r"\b(docs|documentation|api|reference|sdk|guide|quickstart|manual|endpoint|rate limit)\b", base.lower()))
    freshness_priority = freshness_priority_override if freshness_priority_override in {"low", "medium", "high"} else policy.freshness_priority
    freshness_suffix = {
        "high": "latest updates",
        "medium": "recent",
        "low": "overview",
    }[freshness_priority]
    official_suffix = "official benchmark methodology maintainers" if comparison_benchmark else ("official policy data usage retention privacy docs" if policy_sensitive else ("official docs api reference vendor documentation" if docs_sensitive or policy.require_official_source else "official source"))
    alternative_suffix = {
        "comparison_evaluation": "tradeoffs benchmark methodology independent results",
        "breaking_news": "timeline and reactions",
        "product_docs_api_lookup": "reference guide vendor documentation api reference" if not policy_sensitive else "privacy policy data retention help center official guidance",
        "people_company_ecosystem_tracking": "ecosystem map",
        "fact_lookup": "exact value reference",
        "background_explainer": "overview",
    }.get(intent_type, "alternative wording")
    contradiction_suffix = {
        "breaking_news": "conflicting reports",
        "fact_lookup": "contradicting value",
        "product_docs_api_lookup": "limitations exceptions" if not policy_sensitive else "conflicting policy statement retention training opt out",
        "comparison_evaluation": "benchmark disagreement counterarguments",
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

def _web_search(ctx: ToolContext, query: str) -> str:
    del ctx
    def run_backend(name: str, q: str) -> Dict[str, Any]:
        if name == "searxng":
            if not SEARXNG_DEFAULT:
                return {"query": q, "status": "error", "backend": "searxng", "sources": [], "answer": "", "error": "SEARXNG_URL missing."}
            try:
                import urllib.parse
                import urllib.request

                params = urllib.parse.urlencode({"q": q, "format": "json", "language": "ru", "safesearch": 0})
                url = f"{SEARXNG_DEFAULT.rstrip('/')}/search?{params}"
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                return {"query": q, "status": "ok", "backend": "searxng", "sources": clean_sources(data.get("results", [])), "answer": "", "error": None}
            except Exception as exc:
                log.warning("SearXNG search failed: %s", exc)
                return {"query": q, "status": "error", "backend": "searxng", "sources": [], "answer": "", "error": repr(exc)}
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
            with urllib.request.urlopen(req, timeout=12) as resp:
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
            return {"query": q, "status": "error", "backend": "serper", "sources": [], "answer": "", "error": repr(exc)}

    result = run_discovery_transport(query, lambda q: run_backend("serper", q), (("searxng", lambda q: run_backend("searxng", q)), ("openai", lambda q: run_backend("openai", q))))
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
    run.transport = {"discovery_backend": "serper", "reading_backend": READING_BACKEND, "fallback_backend": None, "fallback_backends": [], "events": []}
    plan = _build_query_plan(run.user_query, run.intent_type, max_subqueries=budget.max_subqueries, freshness_priority_override=policy_obj.freshness_priority)
    run.subqueries = list(plan.subqueries)
    run.query_plan = asdict(plan)
    try:
        ranked_sources = collect_research_sources(
            run,
            lambda query: json.loads(_web_search(ctx, query)),
            _read_page_findings,
            _detect_contradictions,
            checkpoint_fn=lambda stage, **payload: (
                (lambda info: (
                    run.__setattr__("status", info.get("reason") or "interrupted"),
                    run.__setattr__("interrupted", True),
                    run.__setattr__("interrupt_reason", info.get("reason") or "interrupted"),
                    run.__setattr__("interrupt_stage", stage),
                    run.__setattr__("interrupt_message", str(info.get("message") or "").strip()),
                    (_ for _ in ()).throw(ResearchInterrupted(info.get("reason") or "research interrupted"))
                )[-1] if info else None)(
                    ctx.checkpoint(stage, payload=payload) if hasattr(ctx, "checkpoint") else None
                )
            ),
        )
        selected_limit = max(policy["min_sources_before_synthesis"], min(budget.max_pages_read, len(ranked_sources)))
        run.candidate_sources = [{"title": item.get("title") or item.get("url"), "url": item.get("url"), "snippet": item.get("snippet", ""), "score": item.get("score"), "authority": item.get("authority", "unknown"), "benchmark_primary_type": item.get("benchmark_primary_type", ""), "reasons": list(item.get("reasons") or []), "decision": item.get("decision", "selected"), "query": item.get("query", ""), "host": item.get("host", "")} for item in ranked_sources[:selected_limit] if item.get("url")]
        deduped_findings: List[Dict[str, Any]] = []
        seen_finding_keys: set[str] = set()
        for finding in run.findings:
            key = re.sub(r"\W+", " ", f"{finding.get('claim', '')} {finding.get('evidence_snippet', '')}".casefold()).strip()
            if not key or key in seen_finding_keys:
                continue
            seen_finding_keys.add(key)
            deduped_findings.append(finding)
        run.findings = deduped_findings
        if hasattr(ctx, "checkpoint"):
            info = ctx.checkpoint("pre_synthesis", payload={"findings": len(run.findings), "candidate_sources": len(run.candidate_sources), "pages_read": run.budget_trace.get("pages_read", 0)})
            if info:
                run.status = info.get("reason") or "interrupted"
                run.interrupted = True
                run.interrupt_reason = info.get("reason") or "interrupted"
                run.interrupt_stage = "pre_synthesis"
                run.interrupt_message = str(info.get("message") or "").strip()
                raise ResearchInterrupted(run.interrupt_reason or "research interrupted")
        run.budget_trace["synthesis_rounds_used"] = min(1, budget.max_synthesis_rounds)
        output_map = {"brief": "short_factual", "memo": "analyst_memo", "timeline": "timeline", "comparison": "comparison_brief"}; _apply_research_quality(run, policy_obj, output_map.get(str(output_mode or "").strip().lower()))
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
        ),
    ]
