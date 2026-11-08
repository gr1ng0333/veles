"""Ouroboros context builder. Assembles LLM context from prompts, memory, logs, and runtime state."""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.utils import (
    utc_now_iso, read_text, estimate_tokens, get_git_info,
)
from ouroboros.memory import Memory

log = logging.getLogger(__name__)

STARTUP_TARGET_TOKENS = 35_000
RECENT_CHAT_MAX_CHARS = 24_000
RECENT_CHAT_FULL_MESSAGES = 32


def _build_user_content(task: Dict[str, Any]) -> Any:
    """Build user message content. Images are auto-described via Codex VLM.

    All images (from owner Telegram messages) are routed through
    vlm_preprocess.describe_image() BEFORE entering the main LLM context.
    This ensures images always go through Codex VLM (free, vision-capable)
    regardless of the active main model.
    """
    text = task.get("text", "")
    image_b64 = task.get("image_base64")
    image_mime = task.get("image_mime", "image/jpeg")
    image_caption = task.get("image_caption", "")

    if not image_b64:
        if not text:
            return "(empty message)"
        return text

    if not str(image_mime or '').lower().startswith('image/'):
        combined_text = (
            ((image_caption + '\n' + text).strip())
            if image_caption and text and text != image_caption
            else (image_caption or text or '(attachment received)')
        )
        return combined_text or '(attachment received)'

    # Route image through Codex VLM for text description
    try:
        from ouroboros.vlm_preprocess import describe_image
        vlm_description = describe_image(
            image_b64=image_b64,
            image_mime=image_mime,
            caption=image_caption or text,
        )
    except Exception as e:
        log.warning("VLM image preprocess import/call failed: %s", e)
        vlm_description = None

    if vlm_description:
        # Build text-only content with VLM description
        parts = []
        if image_caption:
            parts.append(f"[User sent an image with caption: {image_caption}]")
        elif text:
            parts.append(f"[User sent an image with message: {text}]")
        else:
            parts.append("[User sent an image]")
        parts.append(f"[Image description (via Codex VLM):]\n{vlm_description}")
        if text and text != image_caption:
            parts.append(text)
        return "\n\n".join(parts)
    else:
        # Fallback: include raw image as multipart (original behavior)
        log.warning("VLM preprocess failed, falling back to raw multipart image")
        parts = []
        combined_text = ""
        if image_caption:
            combined_text = image_caption
        if text and text != image_caption:
            combined_text = (combined_text + "\n" + text).strip() if combined_text else text
        if not combined_text:
            combined_text = "Analyze the screenshot"
        parts.append({"type": "text", "text": combined_text})
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{image_b64}"}
        })
        return parts

def _build_runtime_section(env: Any, task: Dict[str, Any]) -> str:
    """Build the runtime context section (utc_now, repo_dir, drive_root, git_head, git_branch, task info, budget info)."""
    # --- Git context ---
    try:
        git_branch, git_sha = get_git_info(env.repo_dir)
    except Exception:
        log.debug("Failed to get git info for context", exc_info=True)
        git_branch, git_sha = "unknown", "unknown"

    # --- Budget calculation ---
    budget_info = None
    try:
        state_json = _safe_read(env.drive_path("state/state.json"), fallback="{}")
        state_data = json.loads(state_json)
        spent_usd = float(state_data.get("spent_usd", 0))
        total_usd = float(os.environ.get("TOTAL_BUDGET", "1"))
        remaining_usd = total_usd - spent_usd
        budget_info = {"total_usd": total_usd, "spent_usd": spent_usd, "remaining_usd": remaining_usd}
    except Exception:
        log.debug("Failed to calculate budget info for context", exc_info=True)
        pass

    # --- Runtime context JSON ---
    runtime_data = {
        "utc_now": utc_now_iso(),
        "repo_dir": str(env.repo_dir),
        "drive_root": str(env.drive_root),
        "git_head": git_sha,
        "git_branch": git_branch,
        "task": {"id": task.get("id"), "type": task.get("type")},
    }
    if budget_info:
        runtime_data["budget"] = budget_info
    runtime_ctx = json.dumps(runtime_data, ensure_ascii=False, indent=2)
    return "## Runtime context\n\n" + runtime_ctx

def _build_memory_sections(memory: Memory) -> List[Tuple[str, str]]:
    sections = [
        ("Identity", "## Identity\n\n" + memory.load_identity()),
        ("Scratchpad", "## Scratchpad\n\n" + memory.load_scratchpad()),
    ]
    # Keep an unfinished owner plan in view across tasks and model changes.
    try:
        from ouroboros.plans import get_active_plan, get_draft_plan, format_plan_for_context
        plan = get_active_plan(memory.drive_root) or get_draft_plan(memory.drive_root)
        if plan:
            sections.append(("Current plan", format_plan_for_context(plan)))
    except Exception:
        log.debug("Could not load current plan", exc_info=True)
    return sections


def _build_recent_chat(memory: Memory, chat_id: int | str | None) -> str:
    entries = memory.read_jsonl_tail("chat.jsonl", 100, chat_id=chat_id)
    history = memory.summarize_chat(
        entries, max_chars=RECENT_CHAT_MAX_CHARS, full_messages=RECENT_CHAT_FULL_MESSAGES,
    )
    if not history:
        return ""
    return ("## Recent chat\n\n"
            "Historical messages. ← owner, → Veles, ? unknown speaker, 📋 system event. "
            "The current request follows separately. Older messages are available via chat_history.\n\n"
            + history)


def _build_health_invariants(env: Any) -> str:
    """Build health invariants section for LLM-first self-detection.
    Surfaces anomalies as informational text. The LLM (not code) decides what action to take. (Bible P0+P3)"""
    checks = []

    # 1. Version sync: VERSION file vs pyproject.toml
    try:
        ver_file = read_text(env.repo_path("VERSION")).strip()
        pyproject = read_text(env.repo_path("pyproject.toml"))
        pyproject_ver = ""
        for line in pyproject.splitlines():
            if line.strip().startswith("version"):
                pyproject_ver = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
        if ver_file and pyproject_ver and ver_file != pyproject_ver:
            checks.append(f"CRITICAL: VERSION DESYNC — VERSION={ver_file}, pyproject.toml={pyproject_ver}")
        elif ver_file:
            checks.append(f"OK: version sync ({ver_file})")
    except Exception:
        pass

    # 2. Budget drift
    try:
        state_json = read_text(env.drive_path("state/state.json"))
        state_data = json.loads(state_json)
        drift_pct = float(state_data.get("budget_drift_pct") or 0.0)
        our = float(state_data.get("spent_usd") or 0.0)
        theirs = float(state_data.get("openrouter_total_usd") or 0.0)
        if drift_pct > 20.0:
            checks.append(
                f"WARNING: BUDGET DRIFT {drift_pct:.1f}% — tracked=${our:.2f} vs OpenRouter=${theirs:.2f}"
            )
        else:
            checks.append("OK: budget drift within tolerance")
    except Exception:
        pass

    # 3. Per-task cost anomalies
    try:
        from supervisor.state import per_task_cost_summary
        costly = [t for t in per_task_cost_summary(5) if t["cost"] > 5.0]
        for t in costly:
            checks.append(
                f"WARNING: HIGH-COST TASK — task_id={t['task_id']} "
                f"cost=${t['cost']:.2f} rounds={t['rounds']}"
            )
        if not costly:
            checks.append("OK: no high-cost tasks (>$5)")
    except Exception:
        pass

    # 4. Stale identity.md
    try:
        import time as _time
        identity_path = env.drive_path("memory/identity.md")
        if identity_path.exists():
            age_hours = (_time.time() - identity_path.stat().st_mtime) / 3600
            if age_hours > 4:
                checks.append(f"WARNING: STALE IDENTITY — identity.md last updated {age_hours:.0f}h ago")
            else:
                checks.append("OK: identity.md recent")
    except Exception:
        pass

    # 5. Duplicate processing detection: same owner message text appearing in multiple tasks
    try:
        import hashlib
        msg_hash_to_tasks: Dict[str, set] = {}
        tail_bytes = 256_000

        def _scan_file_for_injected(path, type_field="type", type_value="owner_message_injected"):
            if not path.exists():
                return
            file_size = path.stat().st_size
            with path.open("r", encoding="utf-8") as f:
                if file_size > tail_bytes:
                    f.seek(file_size - tail_bytes)
                    f.readline()
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        if ev.get(type_field) != type_value:
                            continue
                        text = ev.get("text", "")
                        if not text and "event_repr" in ev:
                            # Historical entries in supervisor.jsonl lack "text";
                            # try to extract task_id at least for presence detection
                            text = ev.get("event_repr", "")[:200]
                        if not text:
                            continue
                        text_hash = hashlib.md5(text.encode()).hexdigest()[:12]
                        tid = ev.get("task_id") or "unknown"
                        if text_hash not in msg_hash_to_tasks:
                            msg_hash_to_tasks[text_hash] = set()
                        msg_hash_to_tasks[text_hash].add(tid)
                    except (json.JSONDecodeError, ValueError):
                        continue

        _scan_file_for_injected(env.drive_path("logs/events.jsonl"))
        # Also check supervisor.jsonl for historically unhandled events
        _scan_file_for_injected(
            env.drive_path("logs/supervisor.jsonl"),
            type_field="event_type",
            type_value="owner_message_injected",
        )

        dupes = {h: tids for h, tids in msg_hash_to_tasks.items() if len(tids) > 1}
        if dupes:
            checks.append(
                f"CRITICAL: DUPLICATE PROCESSING — {len(dupes)} message(s) "
                f"appeared in multiple tasks: {', '.join(str(sorted(tids)) for tids in dupes.values())}"
            )
        else:
            checks.append("OK: no duplicate message processing detected")
    except Exception:
        pass

    # 6. Cache hit rate monitoring
    try:
        cache_rate = _compute_cache_hit_rate(env)
        if cache_rate is not None:
            if cache_rate < 0.30:
                checks.append(
                    f"WARNING: LOW CACHE HIT RATE — {cache_rate:.0%} cached. "
                    "Context structure may be degrading prompt caching efficiency."
                )
            elif cache_rate >= 0.50:
                checks.append(f"OK: cache hit rate ({cache_rate:.0%})")
            else:
                checks.append(f"INFO: cache hit rate moderate ({cache_rate:.0%})")
    except Exception:
        pass

    if not checks:
        return ""
    return "## Health Invariants\n\n" + "\n".join(f"- {c}" for c in checks)

def _compute_cache_hit_rate(env: Any) -> Optional[float]:
    """Compute prompt cache hit rate from recent llm_round events."""
    events_path = env.drive_path("logs/events.jsonl")
    if not events_path.exists():
        return None
    total_prompt = total_cached = count = 0
    try:
        file_size = events_path.stat().st_size
        with events_path.open("r", encoding="utf-8") as f:
            if file_size > 256_000:
                f.seek(file_size - 256_000)
                f.readline()
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    if ev.get("type") != "llm_round":
                        continue
                    usage = ev.get("usage", ev)
                    pt = int(usage.get("prompt_tokens", 0))
                    if pt > 0:
                        total_prompt += pt
                        total_cached += int(usage.get("cached_tokens", 0))
                        count += 1
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
    except Exception:
        return None
    if count < 5 or total_prompt == 0:
        return None
    return total_cached / total_prompt

def build_startup_sections(env: Any, memory: Memory, task: Dict[str, Any]) -> List[List[Tuple[str, str]]]:
    """One initial view for ordinary tasks. The model opens extra sources as needed."""
    static = [
        ("Language rule", "Respond in Russian unless the owner explicitly writes in English."),
        ("SYSTEM.md", _safe_read(env.repo_path("prompts/SYSTEM.md"), "You are Veles.")),
        ("BIBLE.md", "## BIBLE.md\n\n" + _safe_read(env.repo_path("BIBLE.md"))),
        ("Skill map", _safe_read(env.repo_path("prompts/skills/_map.md"))),
    ]
    dynamic = [
        ("Runtime", _build_runtime_section(env, task)),
        ("Health invariants", _build_health_invariants(env)),
        ("Recent chat", _build_recent_chat(memory, task.get("chat_id"))),
    ]
    return [static, _build_memory_sections(memory), dynamic]


def build_llm_messages(
    env: Any, memory: Memory, task: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    memory.ensure_files()
    blocks = [
        {"type": "text", "text": "\n\n".join(text for _, text in sections if text)}
        for sections in build_startup_sections(env, memory, task)
    ]
    blocks[0]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    blocks[1]["cache_control"] = {"type": "ephemeral"}
    user_content = _build_user_content(task)
    user_parts = user_content if isinstance(user_content, list) else [{"type": "text", "text": user_content}]
    estimated = 12 + sum(estimate_tokens(part["text"]) for part in [*blocks, *user_parts]
                         if part.get("type") == "text")
    return [
        {"role": "system", "content": blocks},
        {"role": "user", "content": user_content},
    ], {"estimated_input_text_tokens": estimated, "startup_target_tokens": STARTUP_TARGET_TOKENS}


# --- Protected compaction tools ---

_COMPACTION_PROTECTED_TOOLS = frozenset({
    "repo_commit_push",
    "repo_write_commit",
    "knowledge_read",
    "knowledge_write",
    "knowledge_list",
    "plan_step_done",
    "plan_create",
    "plan_complete",
})

def _find_tool_name_for_result(tool_msg: dict, messages: list) -> str:
    """Find the tool name that produced a given tool result by matching tool_call_id."""
    tid = tool_msg.get("tool_call_id", "")
    if not tid:
        return ""
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if tc.get("id") == tid:
                return tc.get("function", {}).get("name", "")
    return ""

def _with_saved_output_ref(content: str, summary: str) -> str:
    for line in content.split("\n", 4)[:4]:
        if line.startswith('Saved output: drive_read(path="logs/shell-output/'):
            return summary if line in summary else f"{summary}\n{line}"
    return summary


def _compact_tool_result(msg: dict, content: str) -> dict:
    """
    Compact a single tool result message.

    Args:
        msg: Original tool result message dict
        content: Content string to compact

    Returns:
        Compacted message dict
    """
    is_error = content.startswith("⚠️")
    # Create a short summary
    if is_error:
        summary = content[:200]  # Keep error details
    else:
        # Keep first line or first 80 chars
        first_line = content.split('\n')[0][:80]
        char_count = len(content)
        summary = f"{first_line}... ({char_count} chars)" if char_count > 80 else content[:200]

    return {**msg, "content": _with_saved_output_ref(content, summary)}

def _compact_assistant_msg(msg: dict) -> dict:
    """
    Compact assistant message content and tool_call arguments.

    Args:
        msg: Original assistant message dict

    Returns:
        Compacted message dict
    """
    compacted_msg = dict(msg)

    # Trim content (progress notes)
    content = msg.get("content") or ""
    if len(content) > 150:
        content = content[:150] + "... (compacted)"
    compacted_msg["content"] = content

    # Compact tool_call arguments
    if msg.get("tool_calls"):
        compacted_tool_calls = []
        for tc in msg["tool_calls"]:
            compacted_tc = dict(tc)

            # Always preserve id and function name
            if "function" in compacted_tc:
                func = dict(compacted_tc["function"])
                args_str = func.get("arguments", "")

                if args_str:
                    compacted_tc["function"] = _compact_tool_call_arguments(
                        func["name"], args_str
                    )
                else:
                    compacted_tc["function"] = func

            compacted_tool_calls.append(compacted_tc)

        compacted_msg["tool_calls"] = compacted_tool_calls

    return compacted_msg

def compact_tool_history(messages: list, keep_recent: int = 6) -> list:
    """
    Compress old tool call/result message pairs into compact summaries.

    Keeps the last `keep_recent` tool-call rounds intact (they may be
    referenced by the LLM). Older rounds get their tool results truncated
    to a short summary line, and tool_call arguments are compacted.

    This dramatically reduces prompt tokens in long tool-use conversations
    without losing important context (the tool names and whether they succeeded
    are preserved).
    """
    # Find all indices that are tool-call assistant messages
    # (messages with tool_calls field)
    tool_round_starts = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tool_round_starts.append(i)

    if len(tool_round_starts) <= keep_recent:
        return messages  # Nothing to compact

    # Rounds to compact: all except the last keep_recent
    rounds_to_compact = set(tool_round_starts[:-keep_recent])

    # Build compacted message list
    result = []
    for i, msg in enumerate(messages):
        # Skip system messages with multipart content (prompt caching format)
        if msg.get("role") == "system" and isinstance(msg.get("content"), list):
            result.append(msg)
            continue

        if msg.get("role") == "tool" and i > 0:
            # Check if the preceding assistant message (with tool_calls)
            # is one we want to compact
            # Find which round this tool result belongs to
            parent_round = None
            for rs in reversed(tool_round_starts):
                if rs < i:
                    parent_round = rs
                    break

            if parent_round is not None and parent_round in rounds_to_compact:
                # Protected tools: keep result intact
                tool_name = _find_tool_name_for_result(msg, messages)
                if tool_name not in _COMPACTION_PROTECTED_TOOLS:
                    # Compact this tool result
                    content = str(msg.get("content") or "")
                    result.append(_compact_tool_result(msg, content))
                    continue

        # For compacted assistant messages, also trim the content (progress notes)
        # AND compact tool_call arguments — but skip if any tool_call is protected
        if i in rounds_to_compact and msg.get("role") == "assistant":
            has_protected = any(
                tc.get("function", {}).get("name", "") in _COMPACTION_PROTECTED_TOOLS
                for tc in (msg.get("tool_calls") or [])
            )
            if not has_protected:
                result.append(_compact_assistant_msg(msg))
                continue

        result.append(msg)

    return result

def compact_tool_history_llm(messages: list, keep_recent: int = 6) -> list:
    """LLM-driven compaction: summarize old tool results via a light model.

    Falls back to simple truncation (compact_tool_history) on any error.
    Called when the agent explicitly invokes the compact_context tool.
    """
    tool_round_starts = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tool_round_starts.append(i)

    if len(tool_round_starts) <= keep_recent:
        return messages

    rounds_to_compact = set(tool_round_starts[:-keep_recent])

    old_results = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "tool" or i == 0:
            continue
        parent_round = None
        for rs in reversed(tool_round_starts):
            if rs < i:
                parent_round = rs
                break
        if parent_round is not None and parent_round in rounds_to_compact:
            # Skip protected tools from LLM compaction
            tool_name = _find_tool_name_for_result(msg, messages)
            if tool_name in _COMPACTION_PROTECTED_TOOLS:
                continue
            content = str(msg.get("content") or "")
            if len(content) > 120:
                tool_call_id = msg.get("tool_call_id", "")
                old_results.append({"idx": i, "tool_call_id": tool_call_id, "content": content[:1500]})

    if not old_results:
        return compact_tool_history(messages, keep_recent=keep_recent)

    batch_text = "\n---\n".join(
        f"[{r['tool_call_id']}]\n{r['content']}" for r in old_results[:20]
    )
    prompt = (
        "Summarize each tool result below into 1-2 lines of key facts. "
        "Preserve errors, file paths, and important values. "
        "Output one summary per [id] block, same order.\n\n" + batch_text
    )

    try:
        from ouroboros.llm import LLMClient
        from ouroboros.model_modes import get_aux_light_model
        light_model = get_aux_light_model()
        client = LLMClient()
        resp_msg, _usage = client.chat(
            messages=[{"role": "user", "content": prompt}],
            model=light_model,
            reasoning_effort="low",
            max_tokens=1024,
        )
        summary_text = resp_msg.get("content") or ""
        if not summary_text.strip():
            raise ValueError("empty summary response")
    except Exception:
        log.warning("LLM compaction failed, falling back to truncation", exc_info=True)
        return compact_tool_history(messages, keep_recent=keep_recent)

    summary_lines = summary_text.strip().split("\n")
    summary_map: Dict[str, str] = {}
    current_id = None
    current_lines: list = []
    for line in summary_lines:
        stripped = line.strip()
        if stripped.startswith("[") and "]" in stripped:
            if current_id is not None:
                summary_map[current_id] = " ".join(current_lines).strip()
            bracket_end = stripped.index("]")
            current_id = stripped[1:bracket_end]
            rest = stripped[bracket_end + 1:].strip()
            current_lines = [rest] if rest else []
        elif current_id is not None:
            current_lines.append(stripped)
    if current_id is not None:
        summary_map[current_id] = " ".join(current_lines).strip()

    idx_to_summary = {}
    for r in old_results:
        s = summary_map.get(r["tool_call_id"])
        if s:
            idx_to_summary[r["idx"]] = s

    result = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "system" and isinstance(msg.get("content"), list):
            result.append(msg)
            continue
        if i in idx_to_summary:
            summary = _with_saved_output_ref(str(msg.get("content") or ""), idx_to_summary[i])
            result.append({**msg, "content": summary})
            continue
        if msg.get("role") == "tool" and i > 0:
            parent_round = None
            for rs in reversed(tool_round_starts):
                if rs < i:
                    parent_round = rs
                    break
            if parent_round is not None and parent_round in rounds_to_compact:
                # Protected tools: keep result intact
                tool_name = _find_tool_name_for_result(msg, messages)
                if tool_name not in _COMPACTION_PROTECTED_TOOLS:
                    content = str(msg.get("content") or "")
                    result.append(_compact_tool_result(msg, content))
                    continue
        if i in rounds_to_compact and msg.get("role") == "assistant":
            has_protected = any(
                tc.get("function", {}).get("name", "") in _COMPACTION_PROTECTED_TOOLS
                for tc in (msg.get("tool_calls") or [])
            )
            if not has_protected:
                result.append(_compact_assistant_msg(msg))
                continue
        result.append(msg)

    return result

def _compact_tool_call_arguments(tool_name: str, args_json: str) -> Dict[str, Any]:
    """
    Compact tool call arguments for old rounds.

    For tools with large content payloads, remove the large field and add _truncated marker.
    For other tools, truncate arguments if > 500 chars.

    Args:
        tool_name: Name of the tool
        args_json: JSON string of tool arguments

    Returns:
        Dict with 'name' and 'arguments' (JSON string, possibly compacted)
    """
    # Tools with large content fields that should be stripped
    LARGE_CONTENT_TOOLS = {
        "repo_write_commit": "content",
        "drive_write": "content",
        "update_scratchpad": "content",
    }

    try:
        args = json.loads(args_json)

        # Check if this tool has a large content field to remove
        if tool_name in LARGE_CONTENT_TOOLS:
            large_field = LARGE_CONTENT_TOOLS[tool_name]
            if large_field in args and args[large_field]:
                args[large_field] = {"_truncated": True}
                return {"name": tool_name, "arguments": json.dumps(args, ensure_ascii=False)}

        # For other tools, if args JSON is > 500 chars, compact to valid JSON
        if len(args_json) > 500:
            compacted_args = {}
            for k, v in args.items():
                if isinstance(v, str) and len(v) > 150:
                    compacted_args[k] = v[:150] + "..."
                else:
                    compacted_args[k] = v
            return {"name": tool_name, "arguments": json.dumps(compacted_args, ensure_ascii=False)}

        # Otherwise return unchanged
        return {"name": tool_name, "arguments": args_json}

    except (json.JSONDecodeError, Exception):
        # If we can't parse JSON, produce valid JSON fallback
        if len(args_json) > 500:
            return {"name": tool_name, "arguments": json.dumps({"_compacted": args_json[:200]})}
        return {"name": tool_name, "arguments": args_json}

def _safe_read(path: pathlib.Path, fallback: str = "") -> str:
    """Read a file, returning fallback if it doesn't exist or errors."""
    try:
        if path.exists():
            return read_text(path)
    except Exception:
        log.debug(f"Failed to read file {path} in _safe_read", exc_info=True)
        pass
    return fallback
