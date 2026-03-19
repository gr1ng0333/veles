from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Tuple

READING_BACKEND = "urllib"

TIMEOUT_PROFILES = {
    "cheap": {
        "overall_run_timeout_sec": 45,
        "discovery_timeout_sec": 12,
        "page_read_timeout_sec": 10,
        "browser_timeout_ms": 15000,
        "synthesis_timeout_sec": 8,
        "eval_timeout_sec": 10,
        "shell_timeout_sec": 60,
    },
    "balanced": {
        "overall_run_timeout_sec": 90,
        "discovery_timeout_sec": 20,
        "page_read_timeout_sec": 15,
        "browser_timeout_ms": 25000,
        "synthesis_timeout_sec": 15,
        "eval_timeout_sec": 15,
        "shell_timeout_sec": 90,
    },
    "deep": {
        "overall_run_timeout_sec": 150,
        "discovery_timeout_sec": 30,
        "page_read_timeout_sec": 25,
        "browser_timeout_ms": 40000,
        "synthesis_timeout_sec": 25,
        "eval_timeout_sec": 20,
        "shell_timeout_sec": 120,
    },
}


TIMEOUT_ERROR_TYPES = {
    "discovery": "discovery_timeout",
    "page_read": "page_read_timeout",
    "browser": "browser_timeout",
    "synthesis": "synthesis_timeout",
    "eval": "eval_timeout",
    "overall": "overall_run_timeout",
}

timeout_profile = lambda mode: dict(TIMEOUT_PROFILES.get(str(mode or "balanced").strip().lower() or "balanced", TIMEOUT_PROFILES["balanced"]))
classify_timeout_error = lambda exc, stage: {"type": TIMEOUT_ERROR_TYPES.get(stage, "tool_timeout"), "stage": stage, "detail": str(exc).strip() or type(exc).__name__}


def run_discovery_transport(
    query: str,
    primary_search_fn: Callable[[str], Dict[str, Any] | None],
    fallback_search_fns: Iterable[Tuple[str, Callable[[str], Dict[str, Any] | None]]],
) -> Dict[str, Any]:
    events: list[Dict[str, Any]] = []
    primary = dict(primary_search_fn(query) or {})
    primary.setdefault("query", query)
    primary["backend"] = "serper"
    primary.setdefault("status", "error")
    primary.setdefault("sources", [])
    primary.setdefault("answer", "")
    primary.setdefault("error", "serper returned no result.")
    cleaned: list[Dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in primary.get("sources") or []:
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
        if len(cleaned) >= 5:
            break
    primary["sources"] = cleaned
    if primary["status"] == "ok" and not primary["sources"] and not str(primary.get("answer") or "").strip():
        primary["status"] = "no_results"
        primary["error"] = primary.get("error") or "serper returned no usable results."
    events.append({
        "stage": "discovery",
        "backend": "serper",
        "status": primary.get("status"),
        "used": True,
        "trigger": "primary",
        "reason": primary.get("error") if primary.get("status") != "ok" else None,
        "timeout_limit": primary.get("timeout_limit"),
    })
    if primary.get("status") == "ok" and (primary.get("sources") or str(primary.get("answer") or "").strip()):
        primary["transport"] = {
            "discovery_backend": "serper",
            "used_backend": "serper",
            "reading_backend": None,
            "fallback_backend": None,
            "events": events,
        }
        return primary
    fallback_reason = "serper_no_results" if primary.get("status") == "no_results" else ("serper_timeout" if primary.get("status") == "timeout" else "serper_error")
    last = primary
    for backend_name, fn in fallback_search_fns:
        fallback = dict(fn(query) or {})
        fallback.setdefault("query", query)
        fallback["backend"] = backend_name
        fallback.setdefault("status", "error")
        fallback.setdefault("sources", [])
        fallback.setdefault("answer", "")
        fallback.setdefault("error", f"{backend_name} returned no result.")
        cleaned = []
        seen_urls = set()
        for item in fallback.get("sources") or []:
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
            if len(cleaned) >= 5:
                break
        fallback["sources"] = cleaned
        if fallback["status"] == "ok" and not fallback["sources"] and not str(fallback.get("answer") or "").strip():
            fallback["status"] = "no_results"
            fallback["error"] = fallback.get("error") or f"{backend_name} returned no usable results."
        events.append({
            "stage": "fallback_discovery",
            "backend": backend_name,
            "status": fallback.get("status"),
            "used": True,
            "trigger": fallback_reason,
            "reason": fallback.get("error") if fallback.get("status") != "ok" else None,
            "timeout_limit": fallback.get("timeout_limit"),
        })
        if fallback.get("status") == "ok" and (fallback.get("sources") or str(fallback.get("answer") or "").strip()):
            fallback["transport"] = {
                "discovery_backend": "serper",
                "used_backend": backend_name,
                "reading_backend": None,
                "fallback_backend": backend_name,
                "events": events,
            }
            return fallback
        fallback_reason = f"{backend_name}_{fallback.get('status') or 'error'}"
        last = fallback
    status = "no_results" if all(event.get("status") == "no_results" for event in events) else "error"
    return {
        "query": query,
        "status": status,
        "backend": last.get("backend") or "serper",
        "sources": [],
        "answer": "",
        "error": " | ".join(str(event.get("reason") or "").strip() for event in events if event.get("reason")) or last.get("error"),
        "transport": {
            "discovery_backend": "serper",
            "used_backend": last.get("backend") or "serper",
            "reading_backend": None,
            "fallback_backend": last.get("backend") if last.get("backend") != "serper" else None,
            "events": events,
        },
    }
