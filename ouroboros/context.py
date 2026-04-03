"""Ouroboros context builder. Assembles LLM context from prompts, memory, logs, and runtime state."""

from __future__ import annotations

import copy
import json
import logging
import os
import pathlib
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.utils import (
    utc_now_iso, read_text, clip_text, estimate_tokens, get_git_info,
)
from ouroboros.memory import Memory
from ouroboros.llm import model_transport

log = logging.getLogger(__name__)

def _build_copilot_round_policy_section() -> str:
    return (
        "## Copilot Round Policy\n\n"
        "Если активная модель идёт через `copilot/*`, это жёсткий протокол, а не рекомендация.\n\n"
        "- Абсолютный потолок: **30 раундов на весь Copilot-loop**.\n"
        "- Не создавай новый premium-thread / interaction_id после 30-го раунда.\n"
        "- Один loop-раунд должен по возможности включать **несколько независимых tool calls**, а не один микрошаг.\n"
        "- Не трать Copilot-раунд на `repo_read` одного файла, если можно за тот же раунд собрать весь минимально нужный срез.\n\n"
        "### Фазовая политика\n\n"
        "**Раунды 1–10 — разведка и сужение гипотезы**\n"
        "- собирай контекст батчами;\n"
        "- предпочитай несколько чтений/проверок за раунд;\n"
        "- не дроби задачу на one-file-per-round без причины.\n\n"
        "**Раунды 11–20 — изменение системы**\n"
        "- переходи от чтения к патчу;\n"
        "- запускай узкие проверки;\n"
        "- не зависай в read-only цикле.\n\n"
        "**Раунды 21–30 — финализация**\n"
        "- задача должна выйти в commit / push / ясный handoff;\n"
        "- запрещены рыхлые микрошаги и декоративные уточнения;\n"
        "- если всё ещё нет результата, сворачивайся в конкретное состояние, а не продолжай бесконечный loop.\n\n"
        "### Антипаттерны для Copilot\n\n"
        "- два и более подряд read-only раунда без сужения гипотезы;\n"
        "- один trivial tool на жирном контексте;\n"
        "- дорогой раунд с минимальным сдвигом по задаче;\n"
        "- попытка дожить до 31+ раунда вместо того, чтобы завершить работу раньше.\n"
    )

def _build_user_content(task: Dict[str, Any]) -> Any:
    """Build user message content. Supports text + optional image."""
    text = task.get("text", "")
    image_b64 = task.get("image_base64")
    image_mime = task.get("image_mime", "image/jpeg")
    image_caption = task.get("image_caption", "")

    if not image_b64:
        # Return fallback text if both text and image are empty
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

    # Multipart content with text + image
    parts = []
    # Combine caption and text for the text part
    combined_text = ""
    if image_caption:
        combined_text = image_caption
    if text and text != image_caption:
        combined_text = (combined_text + "\n" + text).strip() if combined_text else text

    # Always include a text part when there's an image
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

def _build_memory_sections(memory: Memory) -> List[str]:
    """Build scratchpad, identity, active plan, dialogue summary sections."""
    sections = []

    scratchpad_raw = memory.load_scratchpad()
    sections.append("## Scratchpad\n\n" + clip_text(scratchpad_raw, 90000))

    identity_raw = memory.load_identity()
    sections.append("## Identity\n\n" + clip_text(identity_raw, 80000))

    # Active plan (if any)
    try:
        from ouroboros.plans import get_active_plan, format_plan_for_context
        active_plan = get_active_plan(memory.drive_root)
        if active_plan:
            sections.append(format_plan_for_context(active_plan))
    except Exception:
        pass

    # Dialogue history (block-based consolidator replaces legacy dialogue_summary.md)
    try:
        from ouroboros.consolidator import DialogueConsolidator
        consolidator = DialogueConsolidator(drive_root=memory.drive_root, llm_client=None)
        blocks_text = consolidator.render_for_context()
        if blocks_text.strip():
            sections.append("## Dialogue History\n\n" + clip_text(blocks_text, 20000))
        else:
            # Fallback to legacy dialogue_summary.md during migration
            summary_path = memory.drive_root / "memory" / "dialogue_summary.md"
            if summary_path.exists():
                summary_text = read_text(summary_path)
                if summary_text.strip():
                    sections.append("## Dialogue Summary\n\n" + clip_text(summary_text, 20000))
    except Exception:
        # Ultimate fallback if consolidator import fails
        summary_path = memory.drive_root / "memory" / "dialogue_summary.md"
        if summary_path.exists():
            summary_text = read_text(summary_path)
            if summary_text.strip():
                sections.append("## Dialogue Summary\n\n" + clip_text(summary_text, 20000))

    return sections

def _build_recent_sections(memory: Memory, env: Any, task_id: str = "") -> List[str]:
    """Build recent chat, recent progress, recent tools, recent events sections."""
    sections = []

    chat_summary = memory.summarize_chat(
        memory.read_jsonl_tail("chat.jsonl", 200))
    if chat_summary:
        sections.append("## Recent chat\n\n" + chat_summary)

    progress_entries = memory.read_jsonl_tail("progress.jsonl", 200)
    if task_id:
        filtered = [e for e in progress_entries if str(e.get("task_id", "")).strip() == task_id]
        progress_entries = filtered if filtered else progress_entries[-5:]
    progress_summary = memory.summarize_progress(progress_entries, limit=15)
    if progress_summary:
        sections.append("## Recent progress\n\n" + progress_summary)

    tools_entries = memory.read_jsonl_tail("tools.jsonl", 200)
    if task_id:
        filtered = [e for e in tools_entries if str(e.get("task_id", "")).strip() == task_id]
        tools_entries = filtered if filtered else tools_entries[-5:]
    tools_summary = memory.summarize_tools(tools_entries)
    if tools_summary:
        sections.append("## Recent tools\n\n" + tools_summary)

    events_entries = memory.read_jsonl_tail("events.jsonl", 200)
    if task_id:
        filtered = [e for e in events_entries if str(e.get("task_id", "")).strip() == task_id]
        events_entries = filtered if filtered else events_entries[-5:]
    events_summary = memory.summarize_events(events_entries)
    if events_summary:
        sections.append("## Recent events\n\n" + events_summary)

    supervisor_summary = memory.summarize_supervisor(
        memory.read_jsonl_tail("supervisor.jsonl", 200))
    if supervisor_summary:
        sections.append("## Supervisor\n\n" + supervisor_summary)

    # Execution reflections — process memory from previous tasks
    try:
        from ouroboros.reflection import format_recent_reflections
        reflections_entries = memory.read_jsonl_tail("task_reflections.jsonl", 20)
        reflections_text = format_recent_reflections(reflections_entries, limit=10)
        if reflections_text:
            sections.append("## Execution reflections\n\n" + reflections_text)
    except Exception:
        pass

    return sections

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

def _build_active_skills_sections(env: Any) -> list:
    """Load active skill files from prompts/skills/ based on state active_skills."""
    sections = []
    try:
        state_path = env.drive_path('state/state.json')
        state_data = json.loads(state_path.read_text(encoding='utf-8'))
        active_skills = state_data.get('active_skills') or []
        if not active_skills:
            return sections
        skills_dir = pathlib.Path(env.repo_path('prompts/skills'))
        for skill_name in active_skills:
            skill_file = skills_dir / f'{skill_name}.md'
            if skill_file.exists():
                content = skill_file.read_text(encoding='utf-8').strip()
                if content:
                    sections.append(f'## Skill: {skill_name}' + '\n\n' + content)
            else:
                log.debug('Skill file not found: %s', skill_file)
    except Exception:
        log.debug('Failed to load active skills for context', exc_info=True)
    return sections

def build_llm_messages(
    env: Any,
    memory: Memory,
    task: Dict[str, Any],
    review_context_builder: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Build the full LLM message context for a task.

    Args:
        env: Env instance with repo_path/drive_path helpers
        memory: Memory instance for scratchpad/identity/logs
        task: Task dict with id, type, text, etc.
        review_context_builder: Optional callable for review tasks (signature: () -> str)

    Returns:
        (messages, cap_info) tuple:
            - messages: List of message dicts ready for LLM
            - cap_info: Dict with token trimming metadata
    """
    # --- Extract task type for adaptive context ---
    task_type = str(task.get("type") or "user")

    # --- Read base prompts and state ---
    base_prompt = _safe_read(
        env.repo_path("prompts/SYSTEM.md"),
        fallback="You are Ouroboros. Your base prompt could not be loaded."
    )
    bible_md = _safe_read(env.repo_path("BIBLE.md"))
    readme_md = _safe_read(env.repo_path("README.md"))
    state_json = _safe_read(env.drive_path("state/state.json"), fallback="{}")

    # --- Load memory ---
    memory.ensure_files()

    # --- Assemble messages with 3-block prompt caching ---
    # Block 1: Static content (SYSTEM.md + BIBLE.md + README) — cached
    # Block 2: Semi-stable content (identity + scratchpad + knowledge) — cached
    # Block 3: Dynamic content (state + runtime + recent logs) — uncached

    # BIBLE.md always included (Constitution requires it for every decision)
    # README.md only for evolution/review (architecture context)
    needs_full_context = task_type in ("evolution", "review", "scheduled")

    # Language rule — must come before all other instructions
    _lang_rule = (
        "LANGUAGE RULE: Always respond in Russian (русский язык) unless the user "
        "explicitly writes in English. This applies to all messages, status reports, "
        "evolution logs, and consciousness outputs. Internal tool calls and code "
        "can remain in English.\n\n"
    )

    static_text = (
        _lang_rule
        + base_prompt + "\n\n"
        + "## BIBLE.md\n\n" + clip_text(bible_md, 180000)
    )

    # Architecture map — agent needs to know the system before safety rules
    arch_path = env.repo_path("prompts/ARCHITECTURE.md")
    arch_text = _safe_read(arch_path, fallback="")
    if arch_text.strip():
        static_text += "\n\n## ARCHITECTURE.md\n\n" + clip_text(arch_text, 20000)

    # Pre-commit review checklist — always visible so agent self-checks commits
    checklists_md = _safe_read(env.repo_path("prompts/CHECKLISTS.md"), fallback="")
    if checklists_md.strip():
        static_text += "\n\n" + clip_text(checklists_md, 2500)

    # Skills map — always visible so agent knows what to load on demand
    skills_map_md = _safe_read(env.repo_path("prompts/skills/_map.md"), fallback="")
    if skills_map_md.strip():
        static_text += "\n\n" + clip_text(skills_map_md, 3000)

    active_model = str(os.environ.get("OUROBOROS_MODEL") or "")
    if model_transport(active_model) == "copilot":
        static_text += "\n\n" + _build_copilot_round_policy_section()

    if needs_full_context:
        readme_limit = 2000 if task_type == "evolution" else 180000
        static_text += "\n\n## README.md\n\n" + clip_text(readme_md, readme_limit)

    # Semi-stable content: identity, scratchpad, knowledge
    # These change ~once per task, not per round
    semi_stable_parts = []
    semi_stable_parts.extend(_build_memory_sections(memory))
    semi_stable_parts.extend(_build_active_skills_sections(env))

    kb_index_path = env.drive_path("memory/knowledge/_index.md")
    if kb_index_path.exists():
        kb_index = kb_index_path.read_text(encoding="utf-8")
        if kb_index.strip():
            semi_stable_parts.append("## Knowledge base\n\n" + clip_text(kb_index, 50000))

    # Pattern Register — recurring error patterns from execution reflections
    try:
        patterns_path = env.drive_path("memory/knowledge/patterns.md")
        if patterns_path.exists():
            patterns_text = patterns_path.read_text(encoding="utf-8")
            if patterns_text.strip():
                semi_stable_parts.append(
                    "## Known error patterns (Pattern Register)\n\n"
                    + clip_text(patterns_text, 30000)
                )
    except Exception:
        pass

    semi_stable_text = "\n\n".join(semi_stable_parts)

    # Dynamic content: changes every round
    dynamic_parts = [
        "## Drive state\n\n" + clip_text(state_json, 90000),
        _build_runtime_section(env, task),
    ]

    # Health invariants — surfaces anomalies for LLM-first self-detection (Bible P0+P3)
    health_section = _build_health_invariants(env)
    if health_section:
        dynamic_parts.append(health_section)

    dynamic_parts.extend(_build_recent_sections(memory, env, task_id=task.get("id", "")))

    if str(task.get("type") or "") == "review" and review_context_builder is not None:
        try:
            review_ctx = review_context_builder()
            if review_ctx:
                dynamic_parts.append(review_ctx)
        except Exception:
            log.debug("Failed to build review context", exc_info=True)
            pass

    dynamic_text = "\n\n".join(dynamic_parts)

    # Language reminder at end of prompt (recency bias)
    _lang_rule_reminder = (
        "\n\n---\nНАПОМИНАНИЕ: отвечай на русском языке. "
        "Код и tool calls — на английском, всё остальное — русский.\n"
    )
    dynamic_text += _lang_rule_reminder

    # System message with 3 content blocks for optimal caching
    messages: List[Dict[str, Any]] = [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": static_text,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                },
                {
                    "type": "text",
                    "text": semi_stable_text,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": dynamic_text,
                },
            ],
        },
        {"role": "user", "content": _build_user_content(task)},
    ]

    # --- Soft-cap token trimming ---
    messages, cap_info = apply_message_token_soft_cap(messages, 200000)

    return messages, cap_info

def apply_message_token_soft_cap(
    messages: List[Dict[str, Any]],
    soft_cap_tokens: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Trim prunable context sections if estimated tokens exceed soft cap.

    Returns (pruned_messages, cap_info_dict).
    """
    def _estimate_message_tokens(msg: Dict[str, Any]) -> int:
        """Estimate tokens for a message, handling multipart content."""
        content = msg.get("content", "")
        if isinstance(content, list):
            # Multipart content: sum tokens from all text blocks
            total = 0
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    total += estimate_tokens(str(block.get("text", "")))
            return total + 6
        return estimate_tokens(str(content)) + 6

    estimated = sum(_estimate_message_tokens(m) for m in messages)
    info: Dict[str, Any] = {
        "estimated_tokens_before": estimated,
        "estimated_tokens_after": estimated,
        "soft_cap_tokens": soft_cap_tokens,
        "trimmed_sections": [],
    }

    if soft_cap_tokens <= 0 or estimated <= soft_cap_tokens:
        return messages, info

    # Prune log summaries from the dynamic text block in multipart system messages
    prunable = ["## Recent chat", "## Recent progress", "## Recent tools", "## Recent events", "## Supervisor"]
    pruned = copy.deepcopy(messages)
    for prefix in prunable:
        if estimated <= soft_cap_tokens:
            break
        for i, msg in enumerate(pruned):
            content = msg.get("content")

            # Handle multipart content (trim from dynamic text block)
            if isinstance(content, list) and msg.get("role") == "system":
                # Find the dynamic text block (the block without cache_control)
                for j, block in enumerate(content):
                    if (isinstance(block, dict) and
                        block.get("type") == "text" and
                        "cache_control" not in block):
                        text = block.get("text", "")
                        if prefix in text:
                            # Remove this section from the dynamic text
                            lines = text.split("\n\n")
                            new_lines = []
                            skip_section = False
                            for line in lines:
                                if line.startswith(prefix):
                                    skip_section = True
                                    info["trimmed_sections"].append(prefix)
                                    continue
                                if line.startswith("##"):
                                    skip_section = False
                                if not skip_section:
                                    new_lines.append(line)

                            block["text"] = "\n\n".join(new_lines)
                            estimated = sum(_estimate_message_tokens(m) for m in pruned)
                            break
                break

            # Handle legacy string content (for backwards compatibility)
            elif isinstance(content, str) and content.startswith(prefix):
                pruned.pop(i)
                info["trimmed_sections"].append(prefix)
                estimated = sum(_estimate_message_tokens(m) for m in pruned)
                break

    info["estimated_tokens_after"] = estimated
    return pruned, info

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

    return {**msg, "content": summary}

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
            result.append({**msg, "content": idx_to_summary[i]})
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
