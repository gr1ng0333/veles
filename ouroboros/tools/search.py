"""Web search tool — structured search plus research-run skeleton."""

from __future__ import annotations

import html
import json
import logging
import os
import time
import re
import socket
from dataclasses import asdict
from typing import Any, Dict, List

from ouroboros.artifacts import save_artifact
from ouroboros.circuit_breaker import CircuitBreaker
from ouroboros.llm import LLMClient
from ouroboros.search_utils import shorten_query, expand_search_queries
from ouroboros.tools.search_planning import (
    BUDGET_PROFILES,
    DEFAULT_INTENT,
    INTENT_KEYWORDS,
    INTENT_POLICIES,
    MAX_BROWSE_DEPTH,
    MAX_PAGES_READ,
    MAX_SUBQUERIES,
    MAX_SYNTHESIS_ROUNDS,
    IntentPolicy,
    QueryPlan,
    ResearchBudgetProfile,
    _build_query_plan,
    detect_intent_type,
)
from ouroboros.tools.search_ranking import DOC_QUERY_MARKERS, POLICY_QUERY_MARKERS, READING_PRIORITY, collect_research_sources
from ouroboros.tools import search_reading as _search_reading
from ouroboros.tools import search_synthesis as _search_synthesis
from ouroboros.tools.search_transport import READING_BACKEND, classify_timeout_error, run_discovery_transport, timeout_profile
from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)
ResearchInterrupted = _search_synthesis.ResearchInterrupted
ResearchRun = _search_synthesis.ResearchRun
_checkpoint_inline = _search_synthesis._checkpoint_inline
_detect_contradictions = _search_synthesis._detect_contradictions


def _render_synthesis(run: ResearchRun, policy: IntentPolicy) -> None:
    return _search_synthesis._render_synthesis(run, policy, save_artifact_fn=save_artifact)


def _read_page_findings(query: str, source: Dict[str, Any], timeout_sec: int = 12) -> Dict[str, Any]:
    return _search_reading._read_page_findings(query, source, timeout_sec=timeout_sec)


def _apply_research_quality(run: ResearchRun, policy: IntentPolicy, output_mode_override: str | None = None) -> None:
    return _search_reading._apply_research_quality(
        run,
        policy,
        output_mode_override,
        detect_contradictions_fn=_detect_contradictions,
        render_synthesis_fn=_render_synthesis,
    )

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
    # Shorten overly long queries for better search results
    original_query = str(query or "").strip()
    query = shorten_query(original_query)
    if query != original_query:
        log.debug("Query shortened: %r → %r", original_query, query)
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
            model = os.environ.get("WEB_SEARCH_MODEL", "codex/gpt-4.1-mini")
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

    # Fallback chain: serper (if API key) → searxng → duckduckgo → openai
    _has_serper_key = bool(os.environ.get("SERPER_API_KEY", "").strip())
    if _has_serper_key:
        _primary_fn = lambda q: run_backend("serper", q)
        _fallbacks = (
            ("searxng", lambda q: run_backend("searxng", q)),
            ("duckduckgo", lambda q: run_backend("duckduckgo", q)),
            ("openai", lambda q: run_backend("openai", q)),
        )
    else:
        _primary_fn = lambda q: run_backend("searxng", q)
        _fallbacks = (
            ("duckduckgo", lambda q: run_backend("duckduckgo", q)),
            ("openai", lambda q: run_backend("openai", q)),
        )
    result = run_discovery_transport(
        query,
        _primary_fn,
        _fallbacks,
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
    run.intent_type = detect_intent_type(run.user_query)
    base_policy = INTENT_POLICIES.get(run.intent_type, INTENT_POLICIES[DEFAULT_INTENT])
    effective_freshness = str(freshness_bias or "").strip().lower() or base_policy.freshness_priority
    official_sensitive_query = any(marker in lowered for marker in POLICY_QUERY_MARKERS) or any(marker in lowered for marker in ("docs", "documentation", "api", "reference", "guide", "sdk", "rate limit"))
    policy_obj = IntentPolicy(freshness_priority=effective_freshness if effective_freshness in {"low", "medium", "high"} else base_policy.freshness_priority, search_branches=base_policy.search_branches, min_sources_before_synthesis=base_policy.min_sources_before_synthesis, require_official_source=(base_policy.require_official_source or official_sensitive_query))
    policy = asdict(policy_obj)
    run.intent_policy = policy
    run.budget_profile = budget
    _research_primary = "serper" if bool(os.environ.get("SERPER_API_KEY", "").strip()) else "searxng"
    run.transport = {"discovery_backend": _research_primary, "reading_backend": READING_BACKEND, "fallback_backend": None, "fallback_backends": [], "events": []}
    run.timeout_profile = timeout_profile(run.budget_mode)
    run.discovery_backend_used = _research_primary
    run.reading_backend_used = READING_BACKEND
    deadline = time.monotonic() + max(int(run.timeout_profile.get("overall_run_timeout_sec", 90)), 1)
    plan = _build_query_plan(run.user_query, run.intent_type, max_subqueries=budget.max_subqueries, freshness_priority_override=policy_obj.freshness_priority)
    # Expand queries for broader research coverage
    merged_subqueries = list(plan.subqueries)
    seen_sq = {sq.casefold() for sq in merged_subqueries}
    for eq in expand_search_queries(run.user_query):
        if eq.casefold() not in seen_sq:
            merged_subqueries.append(eq)
            seen_sq.add(eq.casefold())
    run.subqueries = merged_subqueries
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
        run.candidate_sources = [{"title": item.get("title") or item.get("url"), "url": item.get("url"), "snippet": item.get("snippet", ""), "score": item.get("score"), "authority": item.get("authority", "unknown"), "benchmark_primary_type": item.get("benchmark_primary_type", ""), "comparison_source_class": item.get("comparison_source_class", ""), "page_kind": item.get("page_kind", "generic"), "reasons": list(item.get("reasons") or []), "decision": item.get("decision", "selected"), "query": item.get("query", ""), "host": item.get("host", ""), "query_type": item.get("query_type", "general"), "core_subject": item.get("core_subject", ""), "dedupe_signature": item.get("dedupe_signature", ""), "signal_trace": dict(item.get("signal_trace") or {})} for item in ranked_sources[:selected_limit] if item.get("url")]
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
