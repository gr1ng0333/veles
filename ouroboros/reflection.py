"""
Ouroboros — Execution Reflection (Process Memory).

Generates LLM summaries of task execution when errors occurred.
Stored in task_reflections.jsonl and loaded into the next task's context,
giving the agent visibility into its own process across task boundaries.

Ported from Ouroboros Desktop v4.5.0 and adapted for Veles architecture.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any, Dict, List, Optional

from ouroboros.utils import utc_now_iso, append_jsonl

log = logging.getLogger(__name__)

_ERROR_MARKERS = frozenset({
    "REVIEW_BLOCKED",
    "TESTS_FAILED",
    "COMMIT_BLOCKED",
    "REVIEW_MAX_ITERATIONS",
    "TOOL_ERROR",
    "TOOL_TIMEOUT",
})

REFLECTIONS_FILENAME = "task_reflections.jsonl"

_REFLECTION_PROMPT = """\
You are reviewing a completed task execution trace for Ouroboros, a self-modifying AI agent.
The task had errors. Write a concise 150-250 word reflection covering:

1. What was the goal?
2. What specific errors/blocks occurred?
3. What was the root cause (if identifiable)?
4. What should be done differently next time?

Be concrete — cite specific file names, tool names, error messages. No platitudes.

## Task goal

{goal}

## Execution trace

{trace_summary}

## Error details

{error_details}

Write the reflection now. Plain text, no markdown headers.
"""


def _truncate_with_notice(text: Any, limit: int) -> str:
    raw = str(text or "")
    if len(raw) <= limit:
        return raw
    marker = f"... [+{len(raw) - limit} chars]"
    available = max(0, limit - len(marker))
    marker = f"... [+{len(raw) - available} chars]"
    available = max(0, limit - len(marker))
    return raw[:available] + marker


# ------------------------------------------------------------------
# Detection
# ------------------------------------------------------------------

def should_generate_reflection(
    task_eval: Dict[str, Any],
    response_text: str,
    llm_trace: Dict[str, Any],
    rounds: int = 0,
    max_rounds: int = 0,
) -> bool:
    """Decide whether a task warrants an execution reflection.

    Returns True when ANY of the following hold:
    - task_eval["ok"] is False
    - task_eval["tool_errors"] > 0
    - response_text contains known error markers
    - rounds > 80% of max_rounds (agent nearly hit the limit)
    - tool calls in llm_trace have is_error or marker strings
    """
    # Failed task
    if not task_eval.get("ok", True):
        return True

    # Tool errors recorded in eval
    if int(task_eval.get("tool_errors", 0)) > 0:
        return True

    # Near max-rounds (>80 %)
    if max_rounds > 0 and rounds > 0 and rounds >= max_rounds * 0.8:
        return True

    # Error markers in response text
    response_upper = (response_text or "").upper()
    for marker in _ERROR_MARKERS:
        if marker in response_upper:
            return True

    # Error markers / is_error in tool call trace
    for tc in (llm_trace.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        if tc.get("is_error"):
            return True
        result_str = str(tc.get("result", ""))
        for marker in _ERROR_MARKERS:
            if marker in result_str:
                return True

    return False


# ------------------------------------------------------------------
# Trace helpers
# ------------------------------------------------------------------

def _collect_error_details(llm_trace: Dict[str, Any], cap: int = 3000) -> str:
    """Extract error tool results from the trace, up to *cap* chars."""
    parts: List[str] = []
    total = 0
    tool_calls = llm_trace.get("tool_calls") or []

    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        result_str = str(tc.get("result", ""))
        is_relevant = tc.get("is_error") or any(m in result_str for m in _ERROR_MARKERS)
        if not is_relevant:
            continue
        tool_name = tc.get("tool", "unknown")
        snippet = f"[{tool_name}]: {result_str}"
        if total + len(snippet) > cap:
            remaining = cap - total
            if remaining > 50:
                parts.append(_truncate_with_notice(snippet, remaining))
            break
        parts.append(snippet)
        total += len(snippet)

    return "\n\n".join(parts) if parts else "(no error details captured)"


def _detect_markers(llm_trace: Dict[str, Any], response_text: str = "") -> List[str]:
    """Return list of error marker strings found in the trace and response."""
    found: set = set()

    # From tool-call results
    for tc in (llm_trace.get("tool_calls") or []):
        result_str = str(tc.get("result", "") if isinstance(tc, dict) else "")
        for marker in _ERROR_MARKERS:
            if marker in result_str:
                found.add(marker)

    # From response text
    response_upper = (response_text or "").upper()
    for marker in _ERROR_MARKERS:
        if marker in response_upper:
            found.add(marker)

    return sorted(found)


def _build_trace_summary(llm_trace: Dict[str, Any], cap: int = 2000) -> str:
    """Build a compact textual summary of the LLM trace for the reflection prompt."""
    tool_calls = llm_trace.get("tool_calls") or []
    if not tool_calls:
        return "(no tool calls)"

    parts: List[str] = []
    total = 0
    for i, tc in enumerate(tool_calls):
        if not isinstance(tc, dict):
            continue
        tool_name = tc.get("tool", "unknown")
        status = "ERROR" if tc.get("is_error") else "ok"
        result_preview = _truncate_with_notice(tc.get("result", ""), 200)
        line = f"{i + 1}. {tool_name} [{status}]: {result_preview}"
        if total + len(line) > cap:
            parts.append(f"... (+{len(tool_calls) - i} more tool calls)")
            break
        parts.append(line)
        total += len(line)

    return "\n".join(parts)


# ------------------------------------------------------------------
# Generation
# ------------------------------------------------------------------

def generate_reflection(
    task_id: str,
    task_text: str,
    task_eval: Dict[str, Any],
    response_text: str,
    llm_trace: Dict[str, Any],
    rounds: int,
    max_rounds: int,
    llm_client: Any,
) -> Dict[str, Any]:
    """Call the light LLM to produce an execution reflection.

    Returns a structured dict ready for appending to the reflections JSONL.
    """
    from ouroboros.model_modes import get_reflection_model

    goal = _truncate_with_notice(task_text, 200)
    error_details = _collect_error_details(llm_trace)
    markers = _detect_markers(llm_trace, response_text)
    error_count = int(task_eval.get("tool_errors", 0))
    trace_summary = _build_trace_summary(llm_trace)

    prompt = _REFLECTION_PROMPT.format(
        goal=goal or "(no goal text)",
        trace_summary=trace_summary,
        error_details=error_details,
    )

    light_model = get_reflection_model()
    try:
        resp_msg, _usage = llm_client.chat(
            messages=[{"role": "user", "content": prompt}],
            model=light_model,
            reasoning_effort="low",
            max_tokens=512,
        )
        reflection_text = (resp_msg.get("content") or "").strip()
    except Exception as e:
        log.warning("Reflection LLM call failed: %s", e)
        reflection_text = f"(reflection generation failed: {e})"

    return {
        "ts": utc_now_iso(),
        "task_id": task_id,
        "goal": goal,
        "rounds": rounds,
        "max_rounds": max_rounds,
        "error_count": error_count,
        "key_markers": markers,
        "reflection": reflection_text,
    }


def generate_reflection_template(
    task_id: str,
    task_text: str,
    task_eval: Dict[str, Any],
    response_text: str,
    llm_trace: Dict[str, Any],
    rounds: int,
    max_rounds: int,
) -> Dict[str, Any]:
    """Create a template-based reflection without LLM call (fallback)."""
    goal = _truncate_with_notice(task_text, 200)
    markers = _detect_markers(llm_trace, response_text)
    error_count = int(task_eval.get("tool_errors", 0))

    # Identify error tools
    error_tools: List[str] = []
    for tc in (llm_trace.get("tool_calls") or []):
        if isinstance(tc, dict) and tc.get("is_error"):
            error_tools.append(tc.get("tool", "unknown"))

    summary_parts = []
    if not task_eval.get("ok", True):
        summary_parts.append("Task failed.")
    if error_count:
        summary_parts.append(f"{error_count} tool errors ({', '.join(error_tools[:5])}).")
    if markers:
        summary_parts.append(f"Markers: {', '.join(markers)}.")
    if max_rounds > 0 and rounds >= max_rounds * 0.8:
        summary_parts.append(f"Used {rounds}/{max_rounds} rounds (near limit).")

    return {
        "ts": utc_now_iso(),
        "task_id": task_id,
        "goal": goal,
        "rounds": rounds,
        "max_rounds": max_rounds,
        "error_count": error_count,
        "key_markers": markers,
        "error_tools": error_tools[:10],
        "reflection": " ".join(summary_parts) or "Task had issues.",
    }


# ------------------------------------------------------------------
# Persistence
# ------------------------------------------------------------------

def append_reflection(drive_root: pathlib.Path, entry: Dict[str, Any]) -> None:
    """Persist a reflection entry to the JSONL file."""
    reflections_path = drive_root / "logs" / REFLECTIONS_FILENAME
    try:
        append_jsonl(reflections_path, entry)
        log.info(
            "Execution reflection saved (task=%s, markers=%s)",
            entry.get("task_id", "?"),
            entry.get("key_markers", []),
        )
    except Exception:
        log.warning("Failed to save execution reflection", exc_info=True)

    if entry.get("key_markers"):
        try:
            _update_patterns(drive_root, entry)
        except Exception:
            log.debug("Pattern register update failed (non-critical)", exc_info=True)


# ------------------------------------------------------------------
# Pattern Register
# ------------------------------------------------------------------

_PATTERNS_PROMPT = """\
You maintain a Pattern Register for Ouroboros, a self-modifying AI agent.
Below is the current register and a new error reflection. Update the register.

Rules:
- If this is a NEW error class: add a row.
- If this is a RECURRING class: increment count, update root cause/fix if you have better info.
- Keep the markdown table format.
- Be concrete: cite file names, tool names, error types.
- Max 20 rows. If full, merge least-important entries.

## Current register

{current_patterns}

## New reflection

Task: {goal}
Markers: {markers}
Reflection: {reflection}

Output ONLY the updated markdown table (with header). No extra text.
"""

_PATTERNS_HEADER = (
    "# Pattern Register\n\n"
    "| Error class | Count | Root cause | Structural fix | Status |\n"
    "|-------------|-------|------------|----------------|--------|\n"
)


def _update_patterns(drive_root: pathlib.Path, entry: Dict[str, Any]) -> None:
    """Update patterns.md knowledge base topic via LLM (Pattern Register)."""
    from ouroboros.llm import LLMClient
    from ouroboros.model_modes import get_reflection_model

    patterns_path = drive_root / "memory" / "knowledge" / "patterns.md"
    patterns_path.parent.mkdir(parents=True, exist_ok=True)

    if patterns_path.exists():
        current = patterns_path.read_text(encoding="utf-8")
    else:
        current = _PATTERNS_HEADER

    prompt = _PATTERNS_PROMPT.format(
        current_patterns=(
            _truncate_with_notice(current, 3000)
            + (
                "\n\n[IMPORTANT: The current register was compacted for prompt size. "
                "Preserve existing rows unless you are intentionally merging or updating them.]"
                if len(current) > 3000
                else ""
            )
        ),
        goal=_truncate_with_notice(entry.get("goal", "?"), 200),
        markers=", ".join(entry.get("key_markers", [])),
        reflection=_truncate_with_notice(entry.get("reflection", ""), 500),
    )

    light_model = get_reflection_model()
    client = LLMClient()
    resp_msg, _usage = client.chat(
        messages=[{"role": "user", "content": prompt}],
        model=light_model,
        reasoning_effort="low",
        max_tokens=1024,
    )
    updated = (resp_msg.get("content") or "").strip()
    if not updated or "|" not in updated:
        log.warning("Pattern register LLM returned invalid output, skipping update")
        return

    if not updated.startswith("#"):
        updated = "# Pattern Register\n\n" + updated

    patterns_path.write_text(updated + "\n", encoding="utf-8")
    log.info("Pattern register updated (%d chars)", len(updated))


# ------------------------------------------------------------------
# Public API — single entry point for agent pipeline
# ------------------------------------------------------------------

def maybe_create_reflection(
    task_id: str,
    task_text: str,
    task_eval: Dict[str, Any],
    response_text: str,
    llm_trace: Dict[str, Any],
    rounds: int,
    max_rounds: int,
    drive_root: pathlib.Path,
    llm_client: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Create and persist an execution reflection if the task warrants one.

    Returns the reflection dict if created, None otherwise.
    This function is designed to never raise — all errors are caught.
    """
    try:
        if not should_generate_reflection(
            task_eval=task_eval,
            response_text=response_text,
            llm_trace=llm_trace,
            rounds=rounds,
            max_rounds=max_rounds,
        ):
            return None

        # Try LLM-based reflection if client available
        if llm_client is not None:
            try:
                entry = generate_reflection(
                    task_id=task_id,
                    task_text=task_text,
                    task_eval=task_eval,
                    response_text=response_text,
                    llm_trace=llm_trace,
                    rounds=rounds,
                    max_rounds=max_rounds,
                    llm_client=llm_client,
                )
            except Exception:
                log.warning("LLM reflection failed, falling back to template", exc_info=True)
                entry = generate_reflection_template(
                    task_id=task_id,
                    task_text=task_text,
                    task_eval=task_eval,
                    response_text=response_text,
                    llm_trace=llm_trace,
                    rounds=rounds,
                    max_rounds=max_rounds,
                )
        else:
            entry = generate_reflection_template(
                task_id=task_id,
                task_text=task_text,
                task_eval=task_eval,
                response_text=response_text,
                llm_trace=llm_trace,
                rounds=rounds,
                max_rounds=max_rounds,
            )

        append_reflection(drive_root, entry)
        return entry

    except Exception:
        log.warning("Execution reflection failed (non-critical)", exc_info=True)
        return None


# ------------------------------------------------------------------
# Context formatting
# ------------------------------------------------------------------

def format_recent_reflections(
    entries: List[Dict[str, Any]],
    limit: int = 10,
    max_chars: int = 5000,
) -> str:
    """Format recent execution reflections for dynamic context block."""
    if not entries:
        return ""

    blocks: List[str] = []
    total_chars = 0

    for entry in entries[-limit:]:
        ts_full = str(entry.get("ts", ""))
        ts = ts_full[:16] if len(ts_full) >= 16 else ts_full
        task_id_short = str(entry.get("task_id", ""))[:8]

        header_bits = [bit for bit in [ts, task_id_short] if bit]
        header = " | ".join(header_bits) or "unknown"

        lines = [f"### {header}"]

        goal = str(entry.get("goal", "")).strip()
        if goal:
            lines.append(f"- Goal: {goal}")

        markers = [str(m).strip() for m in (entry.get("key_markers") or []) if str(m).strip()]
        if markers:
            lines.append(f"- Markers: {', '.join(markers)}")

        rounds_val = entry.get("rounds")
        max_rounds_val = entry.get("max_rounds")
        if rounds_val not in (None, ""):
            rounds_str = str(rounds_val)
            if max_rounds_val not in (None, "", 0):
                rounds_str += f"/{max_rounds_val}"
            lines.append(f"- Rounds: {rounds_str}")

        error_count = entry.get("error_count")
        if error_count not in (None, "", 0):
            lines.append(f"- Errors: {error_count}")

        reflection = str(entry.get("reflection", "")).strip()
        if reflection:
            lines.append("")
            lines.append(reflection)

        block = "\n".join(lines).strip()

        if total_chars + len(block) > max_chars:
            break
        blocks.append(block)
        total_chars += len(block)

    return "\n\n".join(blocks)
