"""context_inspect — structured breakdown of current LLM context by section.

Growth tool: answers "what exactly is consuming my token budget?" in one call.
Returns per-section token estimates for the 3-block context structure:

  Block 0 (Static/cached): SYSTEM.md, BIBLE.md, ARCHITECTURE.md, CHECKLISTS.md, README.md
  Block 1 (Semi-stable): Scratchpad, Identity, Active plan, Dialogue history,
                          Knowledge index, Pattern register
  Block 2 (Dynamic): Drive state, Runtime context, Health invariants,
                      Recent chat, Recent progress, Recent tools, Recent events,
                      Supervisor, Execution reflections

Also shows total, soft cap usage, and which block dominates.
Useful for: understanding tokenizer differences between models, debugging
large context, identifying which section to trim first.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any, Dict, List, Optional

from ouroboros.utils import estimate_tokens, clip_text
from ouroboros.tools.registry import ToolContext, ToolEntry

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")
_REPO_DIR = os.environ.get("OUROBOROS_REPO_DIR", "/opt/veles")
_SOFT_CAP = 200_000


def _safe_read(path: pathlib.Path, fallback: str = "") -> str:
    try:
        if path.exists():
            return path.read_text(encoding="utf-8")
    except Exception:
        pass
    return fallback


def _tok(text: str) -> int:
    return estimate_tokens(text)


def _section(name: str, text: str, include_text: bool = False) -> Dict[str, Any]:
    chars = len(text)
    tokens = _tok(text)
    entry: Dict[str, Any] = {
        "section": name,
        "chars": chars,
        "tokens_est": tokens,
    }
    if include_text:
        entry["preview"] = text[:200] + "..." if len(text) > 200 else text
    return entry


def _build_context_inspect(
    ctx: ToolContext,
    include_preview: bool = False,
    task_type: str = "user",
) -> str:
    repo = pathlib.Path(_REPO_DIR)
    drive = pathlib.Path(_DRIVE_ROOT)

    # ── Block 0: Static ──────────────────────────────────────────────────────
    block0: List[Dict[str, Any]] = []

    lang_rule = (
        "LANGUAGE RULE: Always respond in Russian (русский язык) unless the user "
        "explicitly writes in English. This applies to all messages, status reports, "
        "evolution logs, and consciousness outputs. Internal tool calls and code "
        "can remain in English.\n\n"
    )
    block0.append(_section("language_rule", lang_rule, include_preview))

    system_md = _safe_read(repo / "prompts" / "SYSTEM.md")
    block0.append(_section("SYSTEM.md", system_md, include_preview))

    bible_md = _safe_read(repo / "BIBLE.md")
    block0.append(_section("BIBLE.md", clip_text(bible_md, 180_000), include_preview))

    arch_md = _safe_read(repo / "prompts" / "ARCHITECTURE.md")
    if arch_md.strip():
        block0.append(_section("ARCHITECTURE.md", clip_text(arch_md, 20_000), include_preview))

    checklists_md = _safe_read(repo / "prompts" / "CHECKLISTS.md")
    if checklists_md.strip():
        block0.append(_section("CHECKLISTS.md", clip_text(checklists_md, 2_500), include_preview))

    needs_full_context = task_type in ("evolution", "review", "scheduled")
    if needs_full_context:
        readme_md = _safe_read(repo / "README.md")
        readme_limit = 2000 if task_type == "evolution" else 180_000
        block0.append(_section("README.md", clip_text(readme_md, readme_limit), include_preview))

    # ── Block 1: Semi-stable ─────────────────────────────────────────────────
    block1: List[Dict[str, Any]] = []

    scratchpad = _safe_read(drive / "memory" / "scratchpad.md")
    block1.append(_section("Scratchpad", clip_text(scratchpad, 90_000), include_preview))

    identity = _safe_read(drive / "memory" / "identity.md")
    block1.append(_section("Identity", clip_text(identity, 80_000), include_preview))

    # Active plan
    try:
        from ouroboros.plans import get_active_plan, format_plan_for_context
        from ouroboros.memory import Memory
        mem = Memory(drive_root=drive)
        active_plan = get_active_plan(drive)
        if active_plan:
            plan_text = format_plan_for_context(active_plan)
            block1.append(_section("Active plan", plan_text, include_preview))
    except Exception:
        pass

    # Dialogue history
    try:
        from ouroboros.consolidator import DialogueConsolidator
        consolidator = DialogueConsolidator(drive_root=drive, llm_client=None)
        blocks_text = consolidator.render_for_context()
        if blocks_text.strip():
            block1.append(_section("Dialogue history", clip_text(blocks_text, 20_000), include_preview))
    except Exception:
        # fallback
        summary_path = drive / "memory" / "dialogue_summary.md"
        summary_text = _safe_read(summary_path)
        if summary_text.strip():
            block1.append(_section("Dialogue summary (legacy)", clip_text(summary_text, 20_000), include_preview))

    kb_index_path = drive / "memory" / "knowledge" / "_index.md"
    kb_index = _safe_read(kb_index_path)
    if kb_index.strip():
        block1.append(_section("Knowledge base index", clip_text(kb_index, 50_000), include_preview))

    patterns_path = drive / "memory" / "knowledge" / "patterns.md"
    patterns_text = _safe_read(patterns_path)
    if patterns_text.strip():
        block1.append(_section("Pattern register", clip_text(patterns_text, 30_000), include_preview))

    # ── Block 2: Dynamic ─────────────────────────────────────────────────────
    block2: List[Dict[str, Any]] = []

    state_json = _safe_read(drive / "state" / "state.json", fallback="{}")
    block2.append(_section("Drive state", clip_text(state_json, 90_000), include_preview))

    # Runtime context (approximation — actual build_runtime_section uses live git)
    runtime_approx = json.dumps({
        "utc_now": "...", "repo_dir": str(repo), "drive_root": str(drive),
        "git_head": "...", "git_branch": "...",
        "task": {"id": "...", "type": task_type},
    }, indent=2)
    block2.append(_section("Runtime context", runtime_approx, include_preview))

    # Health invariants (approximation — reads same files)
    health_lines = []
    try:
        ver = (repo / "VERSION").read_text().strip()
        health_lines.append(f"OK: version sync ({ver})")
    except Exception:
        pass
    if health_lines:
        health_text = "## Health Invariants\n\n" + "\n".join(f"- {c}" for c in health_lines)
        block2.append(_section("Health invariants", health_text, include_preview))

    # Recent logs — read tails
    def _tail_text(log_name: str, tail_bytes: int = 32_000) -> str:
        p = drive / "logs" / log_name
        if not p.exists():
            return ""
        size = p.stat().st_size
        try:
            with p.open("rb") as f:
                if size > tail_bytes:
                    f.seek(-tail_bytes, 2)
                    f.readline()
                return f.read().decode("utf-8", errors="replace")
        except Exception:
            return ""

    chat_raw = _tail_text("chat.jsonl")
    block2.append(_section("Recent chat", chat_raw, include_preview))

    progress_raw = _tail_text("progress.jsonl")
    block2.append(_section("Recent progress", progress_raw, include_preview))

    tools_raw = _tail_text("tools.jsonl")
    block2.append(_section("Recent tools", tools_raw, include_preview))

    events_raw = _tail_text("events.jsonl")
    block2.append(_section("Recent events", events_raw, include_preview))

    supervisor_raw = _tail_text("supervisor.jsonl")
    block2.append(_section("Supervisor", supervisor_raw, include_preview))

    reflections_raw = _tail_text("task_reflections.jsonl", tail_bytes=16_000)
    block2.append(_section("Execution reflections", reflections_raw, include_preview))

    lang_reminder = (
        "\n\n---\nНАПОМИНАНИЕ: отвечай на русском языке. "
        "Код и tool calls — на английском, всё остальное — русский.\n"
    )
    block2.append(_section("Language reminder", lang_reminder, include_preview))

    # ── Summaries ────────────────────────────────────────────────────────────
    def _block_summary(sections: List[Dict[str, Any]], name: str) -> Dict[str, Any]:
        total_tokens = sum(s["tokens_est"] for s in sections)
        total_chars = sum(s["chars"] for s in sections)
        top = sorted(sections, key=lambda s: -s["tokens_est"])[:3]
        return {
            "block": name,
            "total_tokens_est": total_tokens,
            "total_chars": total_chars,
            "sections": sections,
            "top_3_by_tokens": [
                {"section": s["section"], "tokens_est": s["tokens_est"]} for s in top
            ],
        }

    b0 = _block_summary(block0, "Block0_Static_cached_1h")
    b1 = _block_summary(block1, "Block1_SemiStable_cached")
    b2 = _block_summary(block2, "Block2_Dynamic_uncached")

    total_tokens = b0["total_tokens_est"] + b1["total_tokens_est"] + b2["total_tokens_est"]

    # User message stub (1 token + overhead)
    user_msg_tokens = 10
    grand_total = total_tokens + user_msg_tokens

    dominant = max([b0, b1, b2], key=lambda b: b["total_tokens_est"])

    result = {
        "task_type": task_type,
        "soft_cap_tokens": _SOFT_CAP,
        "grand_total_est": grand_total,
        "soft_cap_usage_pct": round(grand_total / _SOFT_CAP * 100, 1),
        "dominant_block": dominant["block"],
        "blocks": {
            "Block0_Static_cached_1h": {
                "tokens_est": b0["total_tokens_est"],
                "chars": b0["total_chars"],
                "top_sections": b0["top_3_by_tokens"],
                "sections": b0["sections"],
            },
            "Block1_SemiStable_cached": {
                "tokens_est": b1["total_tokens_est"],
                "chars": b1["total_chars"],
                "top_sections": b1["top_3_by_tokens"],
                "sections": b1["sections"],
            },
            "Block2_Dynamic_uncached": {
                "tokens_est": b2["total_tokens_est"],
                "chars": b2["total_chars"],
                "top_sections": b2["top_3_by_tokens"],
                "sections": b2["sections"],
            },
        },
        "note": (
            "Token estimates use chars/4 heuristic — actual tokenizer counts vary by model "
            "(Claude BPE ~1.15x, GPT-5 ~1.0x). "
            "Dynamic block values are approximate — actual context built live by context.py."
        ),
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="context_inspect",
            schema={
                "name": "context_inspect",
                "description": (
                    "Structured breakdown of the current LLM context window by section and block. "
                    "Returns token estimates for each section in the 3-block context structure:\n"
                    "- Block 0 (Static/cached 1h): SYSTEM.md, BIBLE.md, ARCHITECTURE.md, CHECKLISTS.md, README.md\n"
                    "- Block 1 (Semi-stable/cached): Scratchpad, Identity, Active plan, Dialogue history, "
                    "Knowledge index, Pattern register\n"
                    "- Block 2 (Dynamic/uncached): Drive state, Runtime context, Health invariants, "
                    "Recent chat/progress/tools/events/supervisor, Execution reflections\n\n"
                    "Use to:\n"
                    "- Understand which section dominates the token budget\n"
                    "- Diagnose why token counts differ between models\n"
                    "- Identify which section to prune before hitting soft cap\n"
                    "- Audit context size before heavy multi-round tasks\n\n"
                    "Parameters:\n"
                    "- include_preview: if true, include 200-char preview of each section (default false)\n"
                    "- task_type: task type context to simulate (user/evolution/review/scheduled, "
                    "affects whether README.md is included)"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "include_preview": {
                            "type": "boolean",
                            "description": "Include 200-char text preview for each section. Default: false.",
                        },
                        "task_type": {
                            "type": "string",
                            "description": "Simulate context for this task type. Default: 'user'. "
                                           "Use 'evolution' to see README.md included.",
                            "enum": ["user", "evolution", "review", "scheduled"],
                        },
                    },
                    "required": [],
                },
            },
            handler=_build_context_inspect,
            timeout_sec=30,
        )
    ]
