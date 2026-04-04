"""tg_summarize — LLM-powered summary of Telegram channel posts.

Fetches recent posts from one or more public Telegram channels and produces
a structured LLM summary: key topics, main ideas, notable links.

Designed for research channel monitoring — e.g. tracking ML papers,
AI news, or technical blogs without reading every post manually.

Tools:
    tg_summarize(channel, limit, since_post_id)
        — summarize one channel
    tg_summarize_watchlist(limit_per_channel)
        — summarize ALL watchlist channels since their last-seen watermarks

Usage:
    tg_summarize(channel="abstractDL")
    tg_summarize(channel="abstractDL", limit=50)
    tg_summarize(channel="abstractDL", since_post_id=358)
    tg_summarize_watchlist()                   # needs tg_watchlist subscriptions
    tg_summarize_watchlist(limit_per_channel=30)
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")
_SUMMARY_MODEL_DEFAULT = "codex/gpt-4.1-mini"
_SUMMARY_MODEL_FALLBACK = "anthropic/claude-haiku-4.5"

_SUMMARY_PROMPT_TEMPLATE = """\
You are summarizing Telegram posts from the channel @{channel}.
Posts are sorted oldest-first. Total: {post_count} posts.

Your task:
1. Identify 3–7 KEY TOPICS covered in these posts.
2. For each topic, write 1–3 sentences summarizing the main ideas.
3. List any notable external links (URLs) mentioned.
4. Note the date range of posts.

Be concrete. Cite specific methods, papers, tools, or claims when mentioned.
If posts contain ML/AI research — be precise about what was found or proposed.
Write in the same language as the posts (auto-detect).

## Posts

{posts_text}

## Output format (JSON)

{{
  "channel": "@{channel}",
  "date_range": "YYYY-MM-DD to YYYY-MM-DD",
  "post_count": {post_count},
  "summary": "2–4 sentence overall summary of what this channel covers",
  "topics": [
    {{"topic": "...", "details": "..."}},
    ...
  ],
  "notable_links": ["url1", "url2", ...],
  "model_note": "optional note if content was unclear"
}}

Output ONLY valid JSON. No markdown fences, no extra text.
"""


def _format_posts_for_prompt(posts: List[Dict[str, Any]], max_chars: int = 12000) -> str:
    """Format posts list into a compact text for the LLM prompt."""
    lines: List[str] = []
    total = 0
    for p in posts:
        post_id = p.get("id", "?")
        date = (p.get("date") or "")[:10]
        text = (p.get("text") or "").strip()
        links = p.get("links") or []

        if not text:
            continue

        line = f"[{date} #{post_id}] {text}"
        if links:
            line += f"\n  links: {', '.join(links[:3])}"
        line += "\n"

        if total + len(line) > max_chars:
            lines.append(f"... ({len(posts) - len(lines)} more posts truncated)")
            break
        lines.append(line)
        total += len(line)

    return "\n".join(lines)


def _call_llm_with_fallback(
    prompt: str,
    primary_model: str = _SUMMARY_MODEL_DEFAULT,
    max_tokens: int = 1500,
) -> tuple[str, Dict[str, Any]]:
    """Call LLM with automatic fallback to haiku on auth error."""
    from ouroboros.llm import LLMClient

    llm = LLMClient()
    models = [primary_model]
    if _SUMMARY_MODEL_FALLBACK not in models:
        models.append(_SUMMARY_MODEL_FALLBACK)

    last_exc: Optional[Exception] = None
    for model in models:
        try:
            msg, usage = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                tools=None,
                reasoning_effort="low",
                max_tokens=max_tokens,
            )
            return (msg.get("content") or "").strip(), usage
        except Exception as exc:
            last_exc = exc
            msg_lower = str(exc).lower()
            if any(w in msg_lower for w in ("401", "403", "unauthorized", "user not found", "authentication")):
                log.warning("tg_summarize: auth error on %s, trying fallback: %s", model, exc)
                continue
            raise

    raise last_exc or RuntimeError("All summary models failed")


def _emit_usage(ctx: ToolContext, usage: Dict[str, Any], model: str) -> None:
    """Emit LLM usage event for budget tracking."""
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
                "source": "tg_summarize",
                "model": model,
                "task_id": ctx.task_id,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass


def _parse_llm_json(raw: str) -> Dict[str, Any]:
    """Parse JSON from LLM output, tolerating minor formatting issues."""
    raw = raw.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(
            l for l in lines if not l.strip().startswith("```")
        ).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Attempt to extract first {...} block
        import re
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {"raw": raw, "parse_error": "LLM did not return valid JSON"}


# ── Tool: tg_summarize ────────────────────────────────────────────────────────

def _tg_summarize(
    ctx: ToolContext,
    channel: str,
    limit: int = 30,
    since_post_id: int = 0,
    model: str = _SUMMARY_MODEL_DEFAULT,
) -> str:
    """Fetch and summarize one public Telegram channel."""
    from ouroboros.tools.tg_channel_read import _fetch_channel_posts

    channel = channel.lstrip("@").strip()
    if not channel:
        return json.dumps({"error": "channel must not be empty"})

    limit = max(1, min(limit, 200))

    # Fetch posts
    result = _fetch_channel_posts(
        channel=channel,
        limit=limit,
        since_post_id=since_post_id,
    )
    if result.get("error"):
        return json.dumps({"error": result["error"], "channel": channel})

    posts = result.get("posts", [])
    if not posts:
        return json.dumps({
            "channel": channel,
            "post_count": 0,
            "summary": "No posts found.",
            "topics": [],
            "notable_links": [],
        })

    # Format for LLM
    posts_text = _format_posts_for_prompt(posts)
    prompt = _SUMMARY_PROMPT_TEMPLATE.format(
        channel=channel,
        post_count=len(posts),
        posts_text=posts_text,
    )

    try:
        raw_response, usage = _call_llm_with_fallback(prompt, primary_model=model)
        _emit_usage(ctx, usage, model)
        parsed = _parse_llm_json(raw_response)
    except Exception as exc:
        log.error("tg_summarize: LLM call failed for %s: %s", channel, exc)
        return json.dumps({
            "error": f"LLM summarization failed: {exc}",
            "channel": channel,
            "post_count": len(posts),
        })

    # Ensure required fields
    parsed.setdefault("channel", f"@{channel}")
    parsed.setdefault("post_count", len(posts))
    return json.dumps(parsed, ensure_ascii=False, indent=2)


# ── Tool: tg_summarize_watchlist ──────────────────────────────────────────────

def _tg_summarize_watchlist(
    ctx: ToolContext,
    limit_per_channel: int = 30,
    model: str = _SUMMARY_MODEL_DEFAULT,
) -> str:
    """Summarize all watchlist channels since their last-seen watermarks.

    Fetches new posts only (since last_id watermark per channel),
    then generates an LLM summary for each channel with new content.
    Channels with no new posts are skipped.
    """
    from ouroboros.tools.tg_watchlist import _load_watchlist, _normalize_channel
    from ouroboros.tools.tg_channel_read import _fetch_channel_posts

    watchlist = _load_watchlist()
    if not watchlist:
        return json.dumps({
            "channels_processed": 0,
            "message": "Watchlist is empty. Use tg_watchlist_add() to subscribe.",
            "results": [],
        })

    limit_per_channel = max(1, min(limit_per_channel, 100))
    results: List[Dict[str, Any]] = []

    for ch, meta in sorted(watchlist.items()):
        last_id = meta.get("last_id", 0)
        since = last_id + 1 if last_id > 0 else 0

        fetch_result = _fetch_channel_posts(
            channel=ch,
            limit=limit_per_channel,
            since_post_id=since,
        )

        if fetch_result.get("error"):
            results.append({
                "channel": ch,
                "error": fetch_result["error"],
                "new_posts": 0,
            })
            continue

        posts = fetch_result.get("posts", [])
        # Extra guard: only posts strictly newer than last_id
        if last_id > 0:
            posts = [p for p in posts if p["id"] > last_id]

        if not posts:
            results.append({
                "channel": ch,
                "new_posts": 0,
                "summary": "No new posts since last check.",
                "topics": [],
                "notable_links": [],
            })
            continue

        # Summarize new posts
        posts_text = _format_posts_for_prompt(posts)
        prompt = _SUMMARY_PROMPT_TEMPLATE.format(
            channel=ch,
            post_count=len(posts),
            posts_text=posts_text,
        )

        try:
            raw_response, usage = _call_llm_with_fallback(prompt, primary_model=model)
            _emit_usage(ctx, usage, model)
            parsed = _parse_llm_json(raw_response)
        except Exception as exc:
            log.error("tg_summarize_watchlist: LLM failed for %s: %s", ch, exc)
            parsed = {
                "error": f"LLM summarization failed: {exc}",
            }

        parsed["channel"] = f"@{ch}"
        parsed["new_posts"] = len(posts)
        results.append(parsed)

    channels_with_content = sum(1 for r in results if r.get("new_posts", 0) > 0)

    return json.dumps({
        "channels_processed": len(results),
        "channels_with_new_content": channels_with_content,
        "results": results,
    }, ensure_ascii=False, indent=2)


# ── Tool registration ─────────────────────────────────────────────────────────

_SUMMARIZE_SCHEMA = {
    "name": "tg_summarize",
    "description": (
        "Fetch posts from a public Telegram channel and generate an LLM summary.\n"
        "Returns: key topics, main ideas, notable links, date range.\n"
        "Great for quickly understanding what a research/news channel covers.\n\n"
        "Parameters:\n"
        "- channel: username without @, e.g. 'abstractDL'\n"
        "- limit: max posts to fetch and summarize (1–200, default 30)\n"
        "- since_post_id: summarize only posts with id >= this value\n"
        "- model: LLM model to use (default: codex/gpt-4.1-mini)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "channel": {
                "type": "string",
                "description": "Channel username (without @), e.g. 'abstractDL'",
            },
            "limit": {
                "type": "integer",
                "description": "Max posts to fetch and summarize (1–200, default 30)",
                "default": 30,
            },
            "since_post_id": {
                "type": "integer",
                "description": "Summarize only posts with id >= this value (0 = latest)",
                "default": 0,
            },
            "model": {
                "type": "string",
                "description": "LLM model for summarization (default: codex/gpt-4.1-mini)",
                "default": _SUMMARY_MODEL_DEFAULT,
            },
        },
        "required": ["channel"],
    },
}

_SUMMARIZE_WATCHLIST_SCHEMA = {
    "name": "tg_summarize_watchlist",
    "description": (
        "Summarize all subscribed Telegram channels (from watchlist) since their last-seen watermarks.\n"
        "For each channel with new posts: generates an LLM summary of what's new.\n"
        "Channels with no new posts are reported as 'no new content'.\n"
        "Requires at least one channel in watchlist (use tg_watchlist_add first).\n\n"
        "Parameters:\n"
        "- limit_per_channel: max new posts to summarize per channel (default 30)\n"
        "- model: LLM model to use (default: codex/gpt-4.1-mini)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit_per_channel": {
                "type": "integer",
                "description": "Max new posts to summarize per channel (1–100, default 30)",
                "default": 30,
            },
            "model": {
                "type": "string",
                "description": "LLM model for summarization (default: codex/gpt-4.1-mini)",
                "default": _SUMMARY_MODEL_DEFAULT,
            },
        },
        "required": [],
    },
}


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="tg_summarize",
            schema=_SUMMARIZE_SCHEMA,
            handler=lambda ctx, **kw: _tg_summarize(ctx, **kw),
        ),
        ToolEntry(
            name="tg_summarize_watchlist",
            schema=_SUMMARIZE_WATCHLIST_SCHEMA,
            handler=lambda ctx, **kw: _tg_summarize_watchlist(ctx, **kw),
        ),
    ]
