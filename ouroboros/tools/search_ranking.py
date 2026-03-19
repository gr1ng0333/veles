from __future__ import annotations

import re
from operator import itemgetter
from typing import Any, Callable, Dict, List

DOMAIN_SCORES: Dict[str, float] = {
    "docs.python.org": 2.6, "platform.openai.com": 2.8, "openai.com": 2.4, "docs.anthropic.com": 2.8,
    "anthropic.com": 2.4, "developer.mozilla.org": 2.5, "developers.google.com": 2.6, "github.com": 1.8,
    "techcrunch.com": 1.2, "theverge.com": 1.1,
}
AGGREGATOR_DOMAINS = {"www.reddit.com", "reddit.com", "news.ycombinator.com", "hn.algolia.com", "medium.com", "towardsdatascience.com", "www.linkedin.com", "linkedin.com"}
SOCIAL_DOMAINS = {"x.com", "twitter.com", "www.x.com", "www.twitter.com", "facebook.com", "www.facebook.com"}
OFFICIAL_HOST_MARKERS = ("docs.", "developer.", "developers.", "platform.", "api.")
OFFICIAL_DOC_HOSTS = ("docs.python.org", "docs.github.com", "pkg.go.dev", "go.dev", "core.telegram.org", "git-scm.com", "freedesktop.org", "man7.org", "docs.docker.com", "developers.cloudflare.com", "platform.openai.com", "docs.anthropic.com")
OFFICIAL_POLICY_HOSTS = ("openai.com", "help.openai.com", "anthropic.com")
OFFICIAL_POLICY_PATH_HINTS = ("policy", "policies", "privacy", "security", "trust", "data-usage", "data-usage-policies", "data-retention", "retention", "training", "enterprise-privacy", "usage-data", "legal")
OFFICIAL_DOC_PATH_HINTS = ("/docs", "/doc", "/reference", "/api", "/guides", "/manual", "/learn", "/sdk", "/quickstart", "/help", "platform/docs", "platform/reference")
OFFICIAL_PRICING_PATH_HINTS = ("/pricing", "/plans", "/billing", "/enterprise")
MARKETING_PATH_HINTS = ("/blog", "/index/", "/news", "/customers", "/case-studies", "/solutions", "/lp/", "/landing", "/announcements", "/features")
DOC_QUERY_MARKERS = ("docs", "documentation", "api", "reference", "sdk", "guide", "quickstart", "manual", "rate limit", "endpoint")
POLICY_QUERY_MARKERS = ("policy", "data usage", "data retention", "privacy", "retention", "training", "artifact retention", "rate limits official source")
PRIMARY_HOST_MARKERS = ("openai.com", "anthropic.com", "github.com", "python.org", "mozilla.org", "google.com", "huggingface.co", "arxiv.org", "cursor.com")
BENCHMARK_VENDOR_HOSTS = ("platform.openai.com", "openai.com", "docs.anthropic.com", "anthropic.com")
BENCHMARK_LEADERBOARD_HOSTS = ("huggingface.co", "paperswithcode.com", "lmarena.ai", "chat.lmsys.org")
BENCHMARK_PAPER_HOSTS = ("arxiv.org", "huggingface.co")
BENCHMARK_REPO_HOSTS = ("github.com",)
COMPARISON_BENCHMARK_MARKERS = ("benchmark", "latency", "throughput", "eval", "evaluation", "leaderboard", "arena", "head-to-head")
COMPARISON_ECOSYSTEM_MARKERS = ("ecosystem", "tooling", "workflow", "integration", "maintainer", "community", "extension", "plugin")
READING_PRIORITY = lambda item: (0 if item.get("authority") == "official" else 1, 0 if item.get("benchmark_primary_type") else 1, -(item.get("score") or 0.0))


def collect_research_sources(run: Any, web_search_fn: Any, read_page_fn: Any, detect_contradictions_fn: Any, checkpoint_fn: Callable[..., None] | None = None) -> List[Dict[str, Any]]:
    policy, budget = run.intent_policy, run.budget_profile
    query_terms = {term for term in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_-]+", run.user_query.lower()) if len(term) > 2}
    ranked_sources: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()
    for subquery in run.subqueries[: budget.max_subqueries]:
        run.budget_trace["subqueries_executed"] += 1
        result = web_search_fn(subquery)
        sources = list(result.get("sources") or [])[:5]
        run.budget_trace["search_calls"] += 1
        transport = dict(result.get("transport") or {})
        if not transport:
            event = {"backend": result.get("backend"), "status": result.get("status")}
            if result.get("error"):
                event["reason"] = result.get("error")
            if result.get("status") == "timeout":
                event["trigger"] = "backend_timeout"
                event["timeout_limit"] = result.get("timeout_limit")
            transport = {
                "discovery_backend": result.get("backend"),
                "fallback_backend": None,
                "events": [event],
            }
        page_trace = {"query": subquery, "status": result.get("status"), "backend": result.get("backend"), "source_count": len(sources), "intent_type": run.intent_type, "policy": policy, "transport": transport, "ranked_sources": [], "selected_to_read": [], "rejected": []}
        run.transport.setdefault("events", []).extend([event for event in transport.get("events", []) if event not in run.transport.get("events", [])])
        if checkpoint_fn:
            checkpoint_fn("post_discovery", query=subquery, backend=result.get("backend"), source_count=len(sources))
        if transport.get("fallback_backend") and transport.get("fallback_backend") not in (run.transport.get("fallback_backends") or []):
            run.transport.setdefault("fallback_backends", []).append(transport.get("fallback_backend"))
            run.transport["fallback_backend"] = transport.get("fallback_backend")
        for index, source in enumerate(sources):
            url = str(source.get("url") or "").strip(); lowered_url = url.lower(); host_match = re.match(r"https?://([^/]+)", lowered_url)
            host = (host_match.group(1) if host_match else "").lstrip("www."); title = str(source.get("title") or "").strip(); snippet = str(source.get("snippet") or "").strip(); haystack = f"{title} {snippet} {url}".lower(); score = 0.0; reasons: List[str] = []
            lowered_query = run.user_query.lower(); authority, authority_score, authority_reasons = "secondary", 0.0, []
            official_host = any(marker in host for marker in OFFICIAL_HOST_MARKERS) or any(host == domain or host.endswith(f".{domain}") for domain in OFFICIAL_DOC_HOSTS)
            official_doc_host = any(host == domain or host.endswith(f".{domain}") for domain in OFFICIAL_DOC_HOSTS)
            official_doc_path = any(hint in lowered_url for hint in OFFICIAL_DOC_PATH_HINTS)
            official_pricing_path = any(hint in lowered_url for hint in OFFICIAL_PRICING_PATH_HINTS)
            policy_sensitive = any(marker in lowered_query for marker in POLICY_QUERY_MARKERS)
            docs_sensitive = any(marker in lowered_query for marker in DOC_QUERY_MARKERS)
            official_policy_host = any(host == domain or host.endswith(f".{domain}") for domain in OFFICIAL_POLICY_HOSTS)
            official_policy_path = any(hint in lowered_url for hint in OFFICIAL_POLICY_PATH_HINTS)
            if policy["require_official_source"] and policy_sensitive and official_policy_host and official_policy_path:
                authority, authority_score, authority_reasons = "official", 3.6, ["official-source", "official-policy-path"]
            elif policy["require_official_source"] and docs_sensitive and (official_doc_host or (official_host and official_doc_path)) and official_doc_path:
                authority, authority_score, authority_reasons = "official", 3.4, ["official-source", "official-doc-path"]
            elif policy["require_official_source"] and official_host and official_doc_path:
                authority, authority_score, authority_reasons = "official", 3.0, ["official-source", "official-doc-path"]
            elif policy["require_official_source"] and official_policy_host and official_pricing_path and "pricing" in lowered_query:
                authority, authority_score, authority_reasons = "official", 3.1, ["official-source", "official-pricing-path"]
            elif any(token in host for token in PRIMARY_HOST_MARKERS):
                authority, authority_score, authority_reasons = "primary", 2.0, ["primary-source"]
            elif host in SOCIAL_DOMAINS:
                authority, authority_score, authority_reasons = "community", -0.6, ["community-source"]
            official = authority == "official"; primary = authority in {"official", "primary"}
            if authority_score: score += authority_score; reasons.extend(authority_reasons)
            if authority == "community": reasons.append("retelling-or-community")
            domain_bonus = next((value / 10.0 for domain, value in DOMAIN_SCORES.items() if host == domain or host.endswith(f".{domain}")), 0.0)
            if domain_bonus: score += domain_bonus; reasons.append(f"domain-trust:{domain_bonus:+.1f}")
            freshness_hits = len(re.findall(r"\b(2024|2025|2026|today|latest|recent|updated|новост|сегодня|обновл)\b", haystack)); freshness_weight = {"high": 0.8, "medium": 0.5, "low": 0.2}[policy["freshness_priority"]]
            if freshness_hits: freshness_score = min(1.5, freshness_hits * freshness_weight); score += freshness_score; reasons.append(f"freshness:{freshness_score:+.1f}")
            overlap = sum(1 for term in query_terms if term in haystack)
            if overlap: topical_score = min(3.0, overlap * 0.6); score += topical_score; reasons.append(f"topical:{topical_score:+.1f}")
            is_duplicate = url in seen_urls
            if is_duplicate: score += -2.5; reasons.append("duplicate:-2.5")
            if host in AGGREGATOR_DOMAINS: score += -1.7; reasons.append("aggregator:-1.7")
            if host in SOCIAL_DOMAINS: score += -1.3; reasons.append("forum-social:-1.3")
            if index == 0: score += 0.4; reasons.append("serp-position:+0.4")
            benchmark_hits = len(re.findall(r"\b(benchmark|benchmarks|methodology|throughput|latency|eval|evaluation|head[- ]to[- ]head|comparison|leaderboard|arena)\b", haystack))
            policy_hits = len(re.findall(r"\b(policy|privacy|retention|training|data usage|data retention|artifact retention|usage data|legal)\b", haystack))
            docs_hits = len(re.findall(r"\b(docs|documentation|api|reference|sdk|guide|manual|endpoint|quickstart)\b", haystack))
            pricing_hits = len(re.findall(r"\b(pricing|price|plan|billing|seat|seats)\b", haystack))
            page_kind = "policy_legal" if any(hint in lowered_url for hint in OFFICIAL_POLICY_PATH_HINTS) else ("docs" if any(hint in lowered_url for hint in OFFICIAL_DOC_PATH_HINTS) else ("pricing" if any(hint in lowered_url for hint in OFFICIAL_PRICING_PATH_HINTS) else ("marketing" if any(hint in lowered_url for hint in MARKETING_PATH_HINTS) else "generic")))
            reasons.append(f"page-kind:{page_kind}")
            if benchmark_hits: benchmark_score = min(1.8, benchmark_hits * 0.45); score += benchmark_score; reasons.append(f"benchmark-signal:{benchmark_score:+.1f}")
            policy_branch = any(token in subquery.lower() for token in POLICY_QUERY_MARKERS) or any(token in run.user_query.lower() for token in POLICY_QUERY_MARKERS)
            docs_branch = bool(policy["require_official_source"] and (any(token in subquery.lower() for token in DOC_QUERY_MARKERS) or any(token in run.user_query.lower() for token in DOC_QUERY_MARKERS))) and not policy_branch
            official_policy_candidate = policy_hits and any(hint in lowered_url for hint in OFFICIAL_POLICY_PATH_HINTS) and any(host == domain or host.endswith(f".{domain}") for domain in OFFICIAL_POLICY_HOSTS)
            official_doc_candidate = any(hint in lowered_url for hint in OFFICIAL_DOC_PATH_HINTS) and (any(host == domain or host.endswith(f".{domain}") for domain in OFFICIAL_DOC_HOSTS) or any(marker in host for marker in OFFICIAL_HOST_MARKERS))
            official_pricing_candidate = pricing_hits and any(hint in lowered_url for hint in OFFICIAL_PRICING_PATH_HINTS) and authority in {"official", "primary"}
            if policy_branch and official_policy_candidate: score += 1.8; reasons.append("policy-primary-path:+1.8")
            elif policy_branch and page_kind == "marketing" and authority in {"official", "primary"}: score -= 1.0; reasons.append("policy-marketing-penalty:-1.0")
            elif policy_branch and not official_policy_candidate and authority in {"official", "primary"} and page_kind != "policy_legal": score -= 0.8; reasons.append("policy-wrong-surface:-0.8")
            elif policy_branch and not official and host not in AGGREGATOR_DOMAINS and host not in SOCIAL_DOMAINS: score -= 0.7; reasons.append("policy-nonofficial:-0.7")
            if docs_branch and official_doc_candidate: score += 1.8; reasons.append("docs-primary-path:+1.8")
            elif docs_branch and page_kind == "pricing" and authority in {"official", "primary"}: score -= 0.9; reasons.append("docs-pricing-mismatch:-0.9")
            elif docs_branch and page_kind == "marketing" and authority in {"official", "primary"}: score -= 1.1; reasons.append("docs-marketing-penalty:-1.1")
            elif docs_branch and authority in {"official", "primary"} and docs_hits == 0: score -= 0.7; reasons.append("docs-nondoc-surface:-0.7")
            benchmark_branch = any(token in subquery.lower() for token in ("benchmark", "methodology", "maintainers", "official benchmark"))
            benchmark_primary_type = ""
            if benchmark_branch or benchmark_hits:
                if any(host == domain or host.endswith(f".{domain}") for domain in BENCHMARK_VENDOR_HOSTS) and ("docs" in lowered_url or "/guides/" in lowered_url or "/reference" in lowered_url or "benchmark" in lowered_url or "eval" in lowered_url): benchmark_primary_type = "vendor_docs"
                elif any(host == domain or host.endswith(f".{domain}") for domain in BENCHMARK_LEADERBOARD_HOSTS) and ("leaderboard" in lowered_url or "arena" in lowered_url or "leaderboard" in haystack): benchmark_primary_type = "leaderboard"
                elif any(host == domain or host.endswith(f".{domain}") for domain in BENCHMARK_PAPER_HOSTS) and ("/papers/" in lowered_url or host == "arxiv.org" or "/abs/" in lowered_url or "paper" in haystack): benchmark_primary_type = "paper"
                elif any(host == domain or host.endswith(f".{domain}") for domain in BENCHMARK_REPO_HOSTS): benchmark_primary_type = "repo_methodology"
            benchmark_primary_bonus = {"vendor_docs": 1.3, "leaderboard": 1.0, "paper": 0.9, "repo_methodology": 0.8}.get(benchmark_primary_type, 0.0)
            if benchmark_primary_bonus: score += benchmark_primary_bonus; reasons.append(f"benchmark-primary:{benchmark_primary_type}:{benchmark_primary_bonus:+.1f}")
            if official and ("official docs" in subquery.lower() or "reference guide" in subquery.lower() or "official policy" in subquery.lower() or benchmark_branch): score += 1.0; reasons.append("official-branch:+1.0")
            comparison_branch = run.intent_type == "comparison_evaluation"
            comparison_kind = "benchmark" if any(token in run.user_query.lower() for token in COMPARISON_BENCHMARK_MARKERS) else ("ecosystem" if any(token in run.user_query.lower() for token in COMPARISON_ECOSYSTEM_MARKERS) else "feature")
            comparison_source_class = ""
            if comparison_branch and authority in {"official", "primary"} and any(token in lowered_url for token in ("/compare", "/comparisons", "/versus", "/vs")): comparison_source_class = "official_compare_page"
            elif comparison_branch and authority in {"official", "primary"} and (official_doc_candidate or official_pricing_candidate or any(token in lowered_url for token in ("/feature", "/features", "/integrations", "/pricing"))): comparison_source_class = "vendor_docs_pricing_matrix"
            elif comparison_branch and benchmark_primary_type in {"vendor_docs", "leaderboard", "paper"}: comparison_source_class = "benchmark_primary"
            elif comparison_branch and benchmark_primary_type == "repo_methodology": comparison_source_class = "maintainer_primary_repo"
            comparison_bonus = {("feature", "official_compare_page"): 1.2, ("feature", "vendor_docs_pricing_matrix"): 1.0, ("feature", "maintainer_primary_repo"): 0.7, ("benchmark", "benchmark_primary"): 1.2, ("benchmark", "vendor_docs_pricing_matrix"): 0.8, ("benchmark", "maintainer_primary_repo"): 0.9, ("ecosystem", "maintainer_primary_repo"): 1.1, ("ecosystem", "vendor_docs_pricing_matrix"): 0.9, ("ecosystem", "official_compare_page"): 0.8, ("ecosystem", "benchmark_primary"): 0.5}.get((comparison_kind, comparison_source_class), 0.0)
            if comparison_bonus: score += comparison_bonus; reasons.append(f"comparison-preferred-source:{comparison_source_class}:{comparison_bonus:+.1f}")
            elif comparison_branch and (host in AGGREGATOR_DOMAINS or "review" in haystack or "roundup" in haystack or "opinion" in haystack): score -= 0.9; reasons.append("comparison-roundup-noise:-0.9")
            if benchmark_branch and primary: score += 0.8; reasons.append("primary-benchmark-branch:+0.8")
            elif benchmark_branch and not primary and benchmark_hits == 0 and not benchmark_primary_type: score -= 0.6; reasons.append("benchmark-branch-without-signal:-0.6")
            if page_kind == "marketing" and authority in {"official", "primary"} and (policy_branch or docs_branch or comparison_branch): score -= 0.7; reasons.append("vendor-marketing-penalty:-0.7")
            strict_official_query = bool(policy["require_official_source"] and re.search(r"\b(official|documentation|docs|reference|policy|privacy|retention)\b", run.user_query.lower()))
            decision = "reject" if is_duplicate else ("reject" if strict_official_query and not official else ("reject" if score < 0.4 else "selected"))
            if is_duplicate: reasons.append("selection-policy:duplicate-url")
            elif strict_official_query and not official: reasons.append("selection-policy:official-needed")
            elif score < 0.4: reasons.append("selection-policy:low-score")
            entry = {"title": title or url, "url": url, "snippet": snippet, "score": round(score, 3), "reasons": reasons, "decision": decision, "host": host, "query": subquery, "authority": authority, "benchmark_primary_type": benchmark_primary_type}
            page_trace["ranked_sources"].append(entry)
            if entry["decision"] == "selected":
                ranked_sources.append(entry)
                page_trace["selected_to_read"].append({"url": url, "score": entry["score"], "reasons": entry["reasons"], "read_reason": [*entry["reasons"], f"selected-for-reading:score={entry['score']}"]})
            else:
                page_trace["rejected"].append({"url": url, "score": entry["score"], "reasons": entry["reasons"], "decision_reason": list(entry["reasons"])})
            seen_urls.add(url)
        page_trace["ranked_sources"].sort(key=lambda item: (READING_PRIORITY(item),), reverse=False)
        page_trace["selected_to_read"].sort(key=lambda item: READING_PRIORITY(next((row for row in page_trace["ranked_sources"] if row["url"] == item["url"]), item)))
        page_trace["rejected"].sort(key=itemgetter("score"), reverse=True)
        if checkpoint_fn:
            checkpoint_fn("post_ranking", query=subquery, ranked_count=len(page_trace["ranked_sources"]), selected_count=len(page_trace["selected_to_read"]))
        existing_candidate_urls = {str(item.get("url") or "") for item in run.candidate_sources}
        for item in [{"title": item.get("title") or item.get("url"), "url": item.get("url"), "snippet": item.get("snippet", ""), "score": item.get("score"), "authority": item.get("authority", "unknown"), "benchmark_primary_type": item.get("benchmark_primary_type", "")} for item in page_trace["ranked_sources"] if item.get("url")]:
            if item["url"] not in existing_candidate_urls:
                run.candidate_sources.append(item)
                existing_candidate_urls.add(item["url"])
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
            read_result = read_page_fn(run.user_query, ranked_entry)
            read_result.setdefault("transport", {"reading_backend": run.transport.get("reading_backend", "urllib"), "discovery_backend": page_trace.get("transport", {}).get("used_backend") or page_trace.get("backend")})
            read_result.setdefault("read_reason", list(selected.get("read_reason") or selected.get("reasons") or ranked_entry.get("reasons") or []))
            read_result.setdefault("selection_reason", list(ranked_entry.get("reasons") or []))
            read_result.setdefault("browser_reason", "browser_not_used: default direct urllib reading path")
            read_result.setdefault("browser_used", False)
            page_trace["read_results"].append(read_result)
            page_trace["budget"]["browse_depth_used"] += 1
            run.budget_trace["pages_read"] += 1
            run.budget_trace["browse_depth_used"] = max(run.budget_trace["browse_depth_used"], page_trace["budget"]["browse_depth_used"])
            run.findings.extend(read_result.get("findings") or [])
            if checkpoint_fn:
                checkpoint_fn("page_read_complete", query=subquery, url=ranked_entry.get("url"), findings_added=len(read_result.get("findings") or []), pages_read=run.budget_trace["pages_read"])
            read_pages_ok = sum(1 for page in run.visited_pages for result in page.get("read_results", []) if result.get("status") == "ok")
            strong_findings = sum(1 for finding in run.findings if finding.get("confidence_local") in {"high", "medium"})
            has_conflict = bool(detect_contradictions_fn(run.findings))
            should_stop = read_pages_ok >= budget.early_stop_min_read_pages and strong_findings >= budget.early_stop_min_findings and not has_conflict
            if policy.get("require_official_source") and not any(str(item.get("authority") or "") == "official" for item in ranked_sources[: max(1, policy.get("min_sources_before_synthesis", 1))]):
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
    return ranked_sources
