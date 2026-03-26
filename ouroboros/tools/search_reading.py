from __future__ import annotations

import html
import re
import socket
from typing import Any, Dict, List
from urllib.parse import urlparse

from ouroboros.tools.search_transport import classify_timeout_error

_timeout_like = lambda exc: isinstance(exc, TimeoutError | socket.timeout) or exc.__class__.__name__ in {"TimeoutError", "ReadTimeout", "ConnectTimeout"}

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

def _apply_research_quality(
    run: ResearchRun,
    policy: IntentPolicy,
    output_mode_override: str | None = None,
    *,
    detect_contradictions_fn,
    render_synthesis_fn,
) -> None:
    freshness_known = sum(1 for finding in run.findings if str(finding.get("observed_at") or "").strip())
    freshness_unknown = max(0, len(run.findings) - freshness_known)
    run.freshness_summary = {"known_dated_findings": freshness_known, "undated_findings": freshness_unknown, "freshness_priority": policy.freshness_priority}
    if freshness_unknown and policy.freshness_priority in {"high", "medium"}:
        run.uncertainty_notes.append("Часть найденных утверждений без явной даты публикации или обновления.")
    run.contradictions = detect_contradictions_fn(run.findings)
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
    render_synthesis_fn(run, policy)
