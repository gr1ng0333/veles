from __future__ import annotations

import html
import json
import time
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext
from ouroboros.tools.search_planning import DEFAULT_INTENT, IntentPolicy

_normalize_text_block = lambda text: re.sub(r"\s+", " ", html.unescape(str(text or "")).replace(" ", " ")).strip()

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

def _render_synthesis(run: ResearchRun, policy: IntentPolicy, *, save_artifact_fn) -> None:
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
