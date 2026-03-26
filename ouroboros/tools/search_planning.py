"""Planning helpers for the structured search tool."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List

from ouroboros.search_utils import detect_query_type, extract_core_subject, shorten_query

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
    query_type: str = "general"
    core_subject: str = ""


BUDGET_PROFILES: Dict[str, ResearchBudgetProfile] = {
    "cheap": ResearchBudgetProfile(3, 2, 1, 1, 1, 2),
    "balanced": ResearchBudgetProfile(4, 4, 2, 1, 2, 2),
    "deep": ResearchBudgetProfile(MAX_SUBQUERIES, MAX_PAGES_READ, MAX_BROWSE_DEPTH, MAX_SYNTHESIS_ROUNDS, 3, 4),
}

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

_VENDOR_HINT_PATTERNS = (
    ("openai", "platform.openai.com openai"),
    ("anthropic", "docs.anthropic.com anthropic"),
    ("github actions", "docs.github.com github actions"),
    ("github", "docs.github.com github"),
    ("cursor", "docs.cursor.com cursor"),
    ("huggingface", "huggingface docs huggingface"),
)
_COMPARISON_BENCHMARK_RE = re.compile(r"\b(benchmark|latency|throughput|eval|evaluation|head[- ]?to[- ]?head|leaderboard|arena)\b")
_COMPARISON_ECOSYSTEM_RE = re.compile(r"\b(ecosystem|tooling|workflow|integration|integrations|maintainer|plugin|extension|community)\b")
_POLICY_SENSITIVE_RE = re.compile(r"\b(policy|privacy|retention|data usage|data retention|artifact retention|training|legal|terms)\b")
_DOCS_SENSITIVE_RE = re.compile(r"\b(docs|documentation|api|reference|sdk|guide|quickstart|manual|endpoint|rate limit)\b")


def detect_intent_type(query: str) -> str:
    lowered = str(query or "").strip().lower()
    if not lowered:
        return DEFAULT_INTENT
    matched = next(
        (intent_type for intent_type, keywords in INTENT_KEYWORDS if any(keyword in lowered for keyword in keywords)),
        None,
    )
    if matched:
        return matched
    return "fact_lookup" if any(ch.isdigit() for ch in lowered) else DEFAULT_INTENT




def _planning_flags(intent_type: str, lowered: str) -> dict[str, bool]:
    return {
        "comparison_benchmark": bool(intent_type == "comparison_evaluation" and _COMPARISON_BENCHMARK_RE.search(lowered)),
        "comparison_ecosystem": bool(intent_type == "comparison_evaluation" and _COMPARISON_ECOSYSTEM_RE.search(lowered)),
        "policy_sensitive": bool(_POLICY_SENSITIVE_RE.search(lowered)),
        "docs_sensitive": bool(_DOCS_SENSITIVE_RE.search(lowered)),
    }



def _official_suffix(intent_type: str, policy: IntentPolicy, vendor_hint: str, flags: dict[str, bool]) -> str:
    if flags["comparison_benchmark"]:
        return "official benchmark methodology maintainers"
    if flags["policy_sensitive"]:
        return "official policy data usage retention privacy docs"
    if flags["comparison_ecosystem"]:
        return f"{vendor_hint} official docs integrations maintainer repo pricing".strip() or "official docs integrations maintainer repo pricing"
    if intent_type == "comparison_evaluation":
        return f"{vendor_hint} official compare pricing feature matrix vendor docs".strip() or "official compare pricing feature matrix vendor docs"
    if flags["docs_sensitive"] or policy.require_official_source:
        return f"{vendor_hint} official docs api reference vendor documentation".strip()
    return "official source"



def _alternative_suffix(intent_type: str, vendor_hint: str, flags: dict[str, bool]) -> str:
    if intent_type == "comparison_evaluation":
        if flags["comparison_benchmark"]:
            return "tradeoffs benchmark methodology independent results"
        if flags["comparison_ecosystem"]:
            return "integrations plugin maintainer repo workflow docs"
        return "tradeoffs official compare pricing feature matrix"
    if intent_type == "breaking_news":
        return "timeline and reactions"
    if intent_type == "product_docs_api_lookup":
        if flags["policy_sensitive"]:
            return f"{vendor_hint} privacy policy data retention help center official guidance".strip()
        return f"{vendor_hint} reference guide vendor documentation api reference".strip()
    if intent_type == "people_company_ecosystem_tracking":
        return "ecosystem map"
    if intent_type == "fact_lookup":
        return "exact value reference"
    if intent_type == "background_explainer":
        return "overview"
    return "alternative wording"



def _contradiction_suffix(intent_type: str, flags: dict[str, bool]) -> str:
    if intent_type == "breaking_news":
        return "conflicting reports"
    if intent_type == "fact_lookup":
        return "contradicting value"
    if intent_type == "product_docs_api_lookup":
        return "conflicting policy statement retention training opt out" if flags["policy_sensitive"] else "limitations exceptions"
    if intent_type == "comparison_evaluation":
        return "benchmark disagreement counterarguments" if flags["comparison_benchmark"] else "counterarguments maintainer disagreements"
    if intent_type == "people_company_ecosystem_tracking":
        return "controversy changes"
    if intent_type == "background_explainer":
        return "common misconceptions"
    return "contradictions"



def _compose_query_variants(base: str, intent_type: str, policy: IntentPolicy, freshness_priority: str) -> dict[str, str]:
    freshness_suffix = {
        "high": "latest updates",
        "medium": "recent",
        "low": "overview",
    }[freshness_priority]
    lowered = base.lower()
    flags = _planning_flags(intent_type, lowered)
    vendor_hint = " ".join(dict.fromkeys(hint for needle, hint in _VENDOR_HINT_PATTERNS if needle in lowered))
    official_suffix = _official_suffix(intent_type, policy, vendor_hint, flags)
    alternative_suffix = _alternative_suffix(intent_type, vendor_hint, flags)
    contradiction_suffix = _contradiction_suffix(intent_type, flags)
    return {
        "primary_query": base,
        "freshness_query": f"{base} {freshness_suffix}" if base else freshness_suffix,
        "official_docs_query": f"{base} {official_suffix}" if base else official_suffix,
        "alternative_wording_query": f"{base} {alternative_suffix}" if base else alternative_suffix,
        "contradiction_check_query": f"{base} {contradiction_suffix}" if base else contradiction_suffix,
        "comparison_benchmark": flags["comparison_benchmark"],
    }



def _build_query_plan(query: str, intent_type: str, max_subqueries: int = MAX_SUBQUERIES, freshness_priority_override: str | None = None) -> QueryPlan:
    base = re.sub(r"\s+", " ", str(query or "").strip())
    compact_base = shorten_query(base, max_len=96)
    query_type = detect_query_type(base)
    core_subject = extract_core_subject(base)
    policy = INTENT_POLICIES.get(intent_type, INTENT_POLICIES[DEFAULT_INTENT])
    override = str(freshness_priority_override or "").strip().lower()
    freshness_priority = override if override in {"low", "medium", "high"} else policy.freshness_priority
    variants = _compose_query_variants(compact_base or base, intent_type, policy, freshness_priority)
    comparison_benchmark = bool(variants.get("comparison_benchmark"))
    candidates = [variants["primary_query"]]
    if freshness_priority in {"high", "medium"}:
        candidates.append(variants["freshness_query"])
    if policy.require_official_source or comparison_benchmark:
        candidates.append(variants["official_docs_query"])
    candidates.append(variants["alternative_wording_query"])
    if policy.search_branches >= 4 or freshness_priority == "low":
        candidates.append(variants["contradiction_check_query"])
    if policy.search_branches >= 5 and not policy.require_official_source and not comparison_benchmark:
        candidates.append(variants["official_docs_query"])
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
    return QueryPlan(
        primary_query=variants["primary_query"],
        freshness_query=variants["freshness_query"],
        official_docs_query=variants["official_docs_query"],
        alternative_wording_query=variants["alternative_wording_query"],
        contradiction_check_query=variants["contradiction_check_query"],
        subqueries=subqueries,
        branch_budget=len(subqueries),
        query_type=query_type,
        core_subject=core_subject,
    )
