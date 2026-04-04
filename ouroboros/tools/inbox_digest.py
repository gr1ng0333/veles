"""inbox_digest — cross-source intelligence briefing from all monitoring feeds.

Collects new items from ALL inbox sources (Telegram channels, RSS feeds,
web monitors), groups them thematically across sources, and generates a
single coherent LLM digest — not per-channel summaries, but a unified
intelligence briefing.

Why this is different from tg_summarize_watchlist:
  - tg_summarize_watchlist: one LLM call per channel → N per-channel summaries
  - inbox_digest: one LLM call total → one thematic brief across all sources

This is the final piece closing the intelligence pipeline:
  subscribe → monitor → digest → (optional) notify owner

Tools:
    inbox_digest(limit_per_source, sources, notify_owner, since_hours, model)
        — unified cross-source digest since last check

Usage:
    inbox_digest()                                # quick daily brief
    inbox_digest(notify_owner=True)               # brief + send to Andrey
    inbox_digest(sources=["telegram"])            # telegram only
    inbox_digest(since_hours=48, limit_per_source=50)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")
_DIGEST_MODEL_DEFAULT = "codex/gpt-4.1-mini"
_DIGEST_MODEL_FALLBACK = "anthropic/claude-haiku-4.5"

_DIGEST_PROMPT = """\
You are creating an intelligence briefing from monitoring feeds.
Items below come from multiple sources: Telegram channels, RSS feeds, web monitors.
Total: {item_count} new items across {source_count} sources.
Period: {period}.

Your task:
1. Identify 3–8 KEY THEMES that emerge across all items (not per-source, but cross-source).
2. For each theme: 1–3 concrete sentences. Cite specific findings, tools, papers, claims.
3. List up to 10 notable links worth following.
4. If any single item stands out as unusually important — flag it in "highlight".

Group by theme, not by source. If the same topic appears in Telegram + RSS — merge them.
Write in Russian if most content is in Russian, otherwise English.

## Items (sorted oldest-first)

{items_text}

## Output format (JSON only, no markdown)

{{
  "period": "{period}",
  "total_items": {item_count},
  "sources_active": {source_count},
  "themes": [
    {{
      "theme": "short theme name",
      "details": "1-3 sentences with concrete facts",
      "source_types": ["telegram", "rss"]
    }}
  ],
  "highlight": "optional — one sentence about the single most important item, or null",
  "notable_links": ["url1", "url2"]
}}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_iso(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _format_item(item: Dict[str, Any], idx: int) -> str:
    """Format one inbox item for the LLM prompt."""
    source = item.get("source_type", "?")
    name = item.get("source_name", "")
    date = (item.get("date") or "")[:10]
    title = (item.get("title") or "").strip()
    text = (item.get("text") or "").strip()
    links = item.get("links") or []

    # Compact representation
    header = f"[{idx}] [{source}/{name}] {date}"
    body = title if title else ""
    if text and text != title:
        body = (body + " — " + text) if body else text
    body = body[:400]  # cap per item

    line = header + "\n" + body
    if links:
        line += f"\n  → {', '.join(str(l) for l in links[:2])}"
    return line


def _format_items_for_prompt(items: List[Dict[str, Any]], max_chars: int = 14000) -> str:
    lines: List[str] = []
    total = 0
    for i, item in enumerate(items, 1):
        line = _format_item(item, i)
        if total + len(line) > max_chars:
            lines.append(f"... ({len(items) - i + 1} more items truncated)")
            break
        lines.append(line)
        total += len(line)
    return "\n\n".join(lines)


def _call_llm_digest(prompt: str, model: str = _DIGEST_MODEL_DEFAULT) -> tuple[str, Dict[str, Any]]:
    """Call LLM with fallback."""
    from ouroboros.llm import LLMClient

    llm = LLMClient()
    models = [model]
    if _DIGEST_MODEL_FALLBACK not in models:
        models.append(_DIGEST_MODEL_FALLBACK)

    last_exc: Optional[Exception] = None
    for m in models:
        try:
            msg, usage = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                model=m,
                tools=None,
                reasoning_effort="low",
                max_tokens=2000,
            )
            return (msg.get("content") or "").strip(), usage
        except Exception as exc:
            last_exc = exc
            if any(w in str(exc).lower() for w in ("401", "403", "unauthorized", "user not found")):
                log.warning("inbox_digest: auth error on %s, trying fallback: %s", m, exc)
                continue
            raise

    raise last_exc or RuntimeError("All digest models failed")


def _emit_usage(ctx: ToolContext, usage: Dict[str, Any], model: str) -> None:
    if not usage:
        return
    try:
        from supervisor.state import update_budget_from_usage
        update_budget_from_usage({
            "cost": float(usage.get("cost") or 0),
            "rounds": 1,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "cached_tokens": usage.get("cached_tokens", 0),
        })
    except Exception:
        pass

    if ctx.event_queue is not None:
        try:
            ctx.event_queue.put({
                "type": "llm_usage",
                "provider": "openrouter",
                "usage": usage,
                "source": "inbox_digest",
                "model": model,
                "task_id": ctx.task_id,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass


def _parse_llm_json(raw: str) -> Dict[str, Any]:
    """Parse JSON from LLM output, tolerating minor formatting issues."""
    import re

    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(l for l in lines if not l.strip().startswith("```")).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {"raw": raw, "parse_error": "LLM did not return valid JSON"}


def _filter_by_age(items: List[Dict[str, Any]], since_hours: float) -> List[Dict[str, Any]]:
    """Filter items to those published within since_hours. If date is missing, keep."""
    if since_hours <= 0:
        return items
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    result: List[Dict[str, Any]] = []
    for item in items:
        dt = _parse_iso(item.get("date") or item.get("published") or "")
        if dt is None or dt >= cutoff:
            result.append(item)
    return result


# ---------------------------------------------------------------------------
# Main tool
# ---------------------------------------------------------------------------

def _inbox_digest(
    ctx: ToolContext,
    limit_per_source: int = 20,
    sources: Optional[List[str]] = None,
    notify_owner: bool = False,
    since_hours: float = 0,
    model: str = _DIGEST_MODEL_DEFAULT,
) -> str:
    """Fetch all new items from all monitoring sources and generate a unified digest."""
    limit_per_source = max(1, min(limit_per_source, 100))
    enabled = set(sources) if sources else {"telegram", "rss", "web"}

    # --- Collect items from inbox ---
    all_items: List[Dict[str, Any]] = []
    source_summary: Dict[str, int] = {}

    if "telegram" in enabled:
        try:
            from ouroboros.tools.inbox import _collect_telegram
            items = _collect_telegram(ctx, limit_per_source)
            all_items.extend(items)
            source_summary["telegram"] = len(items)
        except Exception as exc:
            log.warning("inbox_digest: telegram collect failed: %s", exc)
            source_summary["telegram"] = 0

    if "rss" in enabled:
        try:
            from ouroboros.tools.inbox import _collect_rss
            items = _collect_rss(ctx, limit_per_source)
            all_items.extend(items)
            source_summary["rss"] = len(items)
        except Exception as exc:
            log.warning("inbox_digest: rss collect failed: %s", exc)
            source_summary["rss"] = 0

    if "web" in enabled:
        try:
            from ouroboros.tools.inbox import _collect_web
            items = _collect_web(ctx)
            all_items.extend(items)
            source_summary["web"] = len(items)
        except Exception as exc:
            log.warning("inbox_digest: web collect failed: %s", exc)
            source_summary["web"] = 0

    # Apply time filter
    if since_hours > 0:
        before = len(all_items)
        all_items = _filter_by_age(all_items, since_hours)
        log.debug("inbox_digest: time filter %sh removed %d items", since_hours, before - len(all_items))

    # Sort oldest-first for coherent narrative
    def _sort_key(item: Dict[str, Any]):
        dt = _parse_iso(item.get("date") or item.get("published") or "")
        return dt or datetime.min.replace(tzinfo=timezone.utc)

    all_items.sort(key=_sort_key)

    total = len(all_items)
    active_sources = [k for k, v in source_summary.items() if v > 0]

    if total == 0:
        result = {
            "total_items": 0,
            "sources": source_summary,
            "message": "No new items in monitoring feeds.",
            "themes": [],
            "notable_links": [],
            "highlight": None,
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    # Determine period string
    dates = [_parse_iso(i.get("date") or i.get("published") or "") for i in all_items]
    dates = [d for d in dates if d is not None]
    if dates:
        date_min = min(dates).strftime("%Y-%m-%d")
        date_max = max(dates).strftime("%Y-%m-%d")
        period = f"{date_min} to {date_max}"
    else:
        period = "unknown period"

    # Build prompt
    items_text = _format_items_for_prompt(all_items)
    prompt = _DIGEST_PROMPT.format(
        item_count=total,
        source_count=len(active_sources),
        period=period,
        items_text=items_text,
    )

    # Call LLM
    try:
        raw_response, usage = _call_llm_digest(prompt, model=model)
        _emit_usage(ctx, usage, model)
        parsed = _parse_llm_json(raw_response)
    except Exception as exc:
        log.error("inbox_digest: LLM call failed: %s", exc)
        # Return raw feed without summary on LLM failure
        return json.dumps({
            "error": f"LLM digest failed: {exc}",
            "total_items": total,
            "sources": source_summary,
            "items": all_items,
        }, ensure_ascii=False, indent=2)

    # Merge metadata
    parsed.setdefault("total_items", total)
    parsed.setdefault("sources_active", len(active_sources))
    parsed["source_breakdown"] = source_summary
    parsed.setdefault("period", period)

    # Optional: notify owner
    if notify_owner:
        try:
            _send_digest_to_owner(ctx, parsed)
        except Exception as exc:
            log.warning("inbox_digest: owner notify failed: %s", exc)
            parsed["notify_error"] = str(exc)

    return json.dumps(parsed, ensure_ascii=False, indent=2)


def _send_digest_to_owner(ctx: ToolContext, digest: Dict[str, Any]) -> None:
    """Format digest as a Telegram message and send to owner via send_owner_message."""
    from ouroboros.tools.core import _send_owner_message

    total = digest.get("total_items", 0)
    period = digest.get("period", "")
    themes = digest.get("themes") or []
    highlight = digest.get("highlight")
    links = digest.get("notable_links") or []
    sources = digest.get("source_breakdown") or {}

    # Format as readable text
    parts: List[str] = []
    parts.append(f"📬 **Inbox Digest** — {period}")
    parts.append(f"Всего новых: {total} | " + ", ".join(f"{k}: {v}" for k, v in sources.items() if v > 0))

    if highlight:
        parts.append(f"\n⭐ **Главное:** {highlight}")

    if themes:
        parts.append("\n**Темы:**")
        for t in themes[:6]:
            name = t.get("theme", "")
            details = t.get("details", "")
            src = t.get("source_types") or []
            src_tag = f" [{', '.join(src)}]" if src else ""
            parts.append(f"• **{name}**{src_tag}: {details}")

    if links:
        parts.append("\n**Ссылки:**")
        for l in links[:5]:
            parts.append(f"  {l}")

    text = "\n".join(parts)

    # Use send_owner_message tool
    _send_owner_message(ctx, text=text, reason="inbox_digest notify_owner=True")


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

_SCHEMA = {
    "name": "inbox_digest",
    "description": (
        "Fetch all new items from all monitoring sources (Telegram watchlist, RSS feeds, "
        "web monitors) and generate a single unified LLM intelligence briefing.\n\n"
        "Unlike tg_summarize_watchlist (per-channel summaries), inbox_digest groups "
        "content THEMATICALLY across all sources — one coherent brief instead of N separate summaries.\n\n"
        "Parameters:\n"
        "- limit_per_source: max items per source (1–100, default 20)\n"
        "- sources: which sources to include ('telegram', 'rss', 'web') — default all\n"
        "- notify_owner: if True, also sends the digest to owner via Telegram message\n"
        "- since_hours: only include items from last N hours (0 = no filter)\n"
        "- model: LLM model for summarization (default: codex/gpt-4.1-mini)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit_per_source": {
                "type": "integer",
                "description": "Max new items per source (1–100, default 20)",
                "default": 20,
            },
            "sources": {
                "type": "array",
                "items": {"type": "string", "enum": ["telegram", "rss", "web"]},
                "description": "Source types to include (default: all). E.g. ['telegram', 'rss']",
            },
            "notify_owner": {
                "type": "boolean",
                "description": "If True, also sends digest to owner via Telegram (default false)",
                "default": False,
            },
            "since_hours": {
                "type": "number",
                "description": "Only include items from last N hours. 0 = no time filter (default 0).",
                "default": 0,
            },
            "model": {
                "type": "string",
                "description": "LLM model for summarization (default: codex/gpt-4.1-mini)",
                "default": _DIGEST_MODEL_DEFAULT,
            },
        },
        "required": [],
    },
}


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="inbox_digest",
            schema=_SCHEMA,
            handler=lambda ctx, **kw: _inbox_digest(ctx, **kw),
        ),
    ]
