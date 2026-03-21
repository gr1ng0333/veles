"""
Safety Agent — Dual-layer LLM security supervisor for tool execution.

Intercepts potentially dangerous tool calls (run_shell, repo_write_commit)
before execution. Uses a two-layer model:
  Layer 1 (fast/cheap): light model quick assessment
  Layer 2 (deep): main model, only when Layer 1 flags non-SAFE

Returns SafetyVerdict with action: "allow" / "warn" / "block".

Design: fail-open — if safety check itself crashes, tool executes normally.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CHECKED_TOOLS = frozenset({
    "run_shell",
    "repo_write_commit",
    "repo_write",
    "data_write",
})

SAFE_SHELL_COMMANDS = frozenset({
    "ls", "cat", "head", "tail", "grep", "rg", "find", "wc",
    "git", "pip", "pytest", "python3", "python",
    "pwd", "whoami", "date", "which", "file", "stat",
    "diff", "tree", "echo", "mkdir", "cp", "mv",
    "cd", "env", "printenv", "uname",
})

SAFETY_CRITICAL_FILES = frozenset({
    "BIBLE.md",
    "ouroboros/safety.py",
    "ouroboros/tools/registry.py",
    "prompts/SYSTEM.md",
    "prompts/CONSCIOUSNESS.md",
    "prompts/SAFETY.md",
})

_SAFETY_CRITICAL_LOWER = frozenset(p.lower() for p in SAFETY_CRITICAL_FILES)

_SHELL_WRITE_INDICATORS = (
    "rm ", "rm\t", ">", "sed -i", "tee ", "truncate",
    "mv ", "chmod ", "chown ", "unlink ", "delete", "trash",
)


# ---------------------------------------------------------------------------
# Verdict dataclass
# ---------------------------------------------------------------------------

@dataclass
class SafetyVerdict:
    """Result of a safety check."""
    action: str          # "allow", "warn", "block"
    reason: str = ""
    layer: int = 0       # 0 = no LLM, 1 = fast, 2 = deep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_path(path: str) -> str:
    """Normalize a file path for comparison against SAFETY_CRITICAL_FILES."""
    # Use forward slashes consistently (SAFETY_CRITICAL_FILES uses forward slashes)
    normalized = os.path.normpath(path.strip().lstrip("./")).replace("\\", "/")
    return normalized


def _is_critical_file(file_path: str) -> bool:
    """Check whether a path refers to a safety-critical file."""
    normalized = _normalize_path(file_path)
    return normalized in SAFETY_CRITICAL_FILES


def _is_whitelisted_shell(cmd_str: str) -> bool:
    """Check if a shell command starts with a whitelisted safe command."""
    first_word = cmd_str.strip().split()[0] if cmd_str.strip() else ""
    return first_word in SAFE_SHELL_COMMANDS


def _extract_shell_cmd(arguments: Dict[str, Any]) -> str:
    """Extract shell command string from tool arguments."""
    raw_cmd = arguments.get("cmd", arguments.get("command", ""))
    if isinstance(raw_cmd, list):
        return " ".join(str(x) for x in raw_cmd)
    return str(raw_cmd)


def _get_safety_prompt() -> str:
    """Load the safety system prompt from prompts/SAFETY.md."""
    prompt_path = pathlib.Path(__file__).parent.parent / "prompts" / "SAFETY.md"
    try:
        return prompt_path.read_text(encoding="utf-8")
    except Exception as exc:
        log.error("Failed to read SAFETY.md: %s", exc)
        return (
            "You are a security supervisor for an autonomous AI agent. "
            "Block only clearly destructive commands. Default to SAFE. "
            'Respond with JSON: {"status": "SAFE"|"SUSPICIOUS"|"DANGEROUS", "reason": "..."}'
        )


def _format_messages_for_safety(
    messages: Optional[List[Dict[str, Any]]],
    limit: int = 5,
) -> str:
    """Format last N conversation messages into a compact context string."""
    if not messages:
        return ""
    parts: List[str] = []
    for m in messages[-limit:]:
        role = m.get("role", "?")
        content = m.get("content", "")
        if not content or role == "tool":
            continue
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        text = str(content)
        if len(text) > 500:
            text = text[:500] + f" [...{len(text) - 500} chars omitted]"
        parts.append(f"[{role}] {text}")
    return "\n".join(parts)


def _build_check_prompt(
    tool_name: str,
    arguments: Dict[str, Any],
    messages: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Build the user prompt for the safety LLM check."""
    args_json = json.dumps(arguments, indent=2, default=str)
    prompt = (
        f"Proposed tool call:\n"
        f"Tool: {tool_name}\n"
        f"Arguments:\n```json\n{args_json}\n```\n"
    )
    if messages:
        context = _format_messages_for_safety(messages)
        if context.strip():
            prompt += f"\nConversation context:\n{context}\n"
    prompt += "\nIs this safe?"
    return prompt


def _parse_safety_response(text: str) -> Optional[Dict[str, Any]]:
    """Parse JSON from LLM response, handling markdown code fences."""
    clean = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Core: check_tool_safety
# ---------------------------------------------------------------------------

def check_tool_safety(
    tool_name: str,
    arguments: Dict[str, Any],
    messages: Optional[List[Dict[str, Any]]] = None,
    llm_client: Optional[Any] = None,
    event_queue: Optional[Any] = None,
    task_id: str = "",
    drive_logs: Optional[pathlib.Path] = None,
) -> SafetyVerdict:
    """Check if a tool call is safe to execute.

    Fast path (no LLM):
      - Tool not in CHECKED_TOOLS → allow
      - Shell command whitelisted → allow
      - Critical file write → block

    Slow path (LLM):
      - Layer 1 (light model): fast assessment
      - Layer 2 (main model): deep check only if Layer 1 ≠ SAFE
    """
    # ── Fast path: skip unchecked tools ──
    if tool_name not in CHECKED_TOOLS:
        return SafetyVerdict(action="allow")

    # ── Hardcoded: critical file protection ──
    if tool_name in ("repo_write_commit", "repo_write"):
        file_path = arguments.get("path", "")
        if file_path and _is_critical_file(file_path):
            reason = (
                f"Cannot modify safety-critical file '{file_path}'. "
                f"Protected files: {', '.join(sorted(SAFETY_CRITICAL_FILES))}"
            )
            _emit_safety_event(
                drive_logs, task_id, tool_name, arguments, "dangerous",
                0, reason, blocked=True,
            )
            return SafetyVerdict(action="block", reason=reason, layer=0)

        # Check files list (batch write)
        files_list = arguments.get("files") or []
        for f_entry in files_list:
            fp = f_entry.get("path", "") if isinstance(f_entry, dict) else ""
            if fp and _is_critical_file(fp):
                reason = (
                    f"Cannot modify safety-critical file '{fp}'. "
                    f"Protected files: {', '.join(sorted(SAFETY_CRITICAL_FILES))}"
                )
                _emit_safety_event(
                    drive_logs, task_id, tool_name, arguments, "dangerous",
                    0, reason, blocked=True,
                )
                return SafetyVerdict(action="block", reason=reason, layer=0)

    # ── Hardcoded: shell write to critical files ──
    if tool_name == "run_shell":
        cmd_str = _extract_shell_cmd(arguments)
        cmd_lower = cmd_str.lower()
        for cf in _SAFETY_CRITICAL_LOWER:
            if cf in cmd_lower and any(w in cmd_lower for w in _SHELL_WRITE_INDICATORS):
                reason = (
                    f"Shell command would modify safety-critical file. "
                    f"Protected: {', '.join(sorted(SAFETY_CRITICAL_FILES))}"
                )
                _emit_safety_event(
                    drive_logs, task_id, tool_name, arguments, "dangerous",
                    0, reason, blocked=True,
                )
                return SafetyVerdict(action="block", reason=reason, layer=0)

    # ── Whitelist: safe shell commands skip LLM ──
    if tool_name == "run_shell":
        cmd_str = _extract_shell_cmd(arguments)
        if _is_whitelisted_shell(cmd_str):
            return SafetyVerdict(action="allow")

    # ── Whitelist: data_write and repo_write (non-critical) skip LLM ──
    if tool_name in ("data_write", "repo_write", "repo_write_commit"):
        # Already passed critical file check above — normal writes are safe
        return SafetyVerdict(action="allow")

    # ── LLM safety check (only reaches here for non-whitelisted run_shell) ──
    if llm_client is None:
        log.warning("safety: no LLM client provided, allowing tool %s (fail-open)", tool_name)
        return SafetyVerdict(action="allow")

    return _llm_safety_check(
        tool_name, arguments, messages, llm_client,
        event_queue, task_id, drive_logs,
    )


def _llm_safety_check(
    tool_name: str,
    arguments: Dict[str, Any],
    messages: Optional[List[Dict[str, Any]]],
    llm_client: Any,
    event_queue: Optional[Any],
    task_id: str,
    drive_logs: Optional[pathlib.Path],
) -> SafetyVerdict:
    """Two-layer LLM safety check. Fail-open on errors."""
    prompt = _build_check_prompt(tool_name, arguments, messages)
    system_prompt = _get_safety_prompt()

    # ── Layer 1: Fast check (light model) ──
    fast_status = None
    fast_reason = None
    try:
        from ouroboros.model_modes import get_aux_light_model
        light_model = os.environ.get("OUROBOROS_MODEL_LIGHT", "") or get_aux_light_model()
        log.info("safety: Layer 1 check on %s using %s", tool_name, light_model)

        msg, usage = llm_client.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            model=light_model,
            max_tokens=256,
        )

        result = _parse_safety_response(msg.get("content") or "")
        if result:
            fast_status = result.get("status", "").upper()
            fast_reason = result.get("reason", "")

        if fast_status == "SAFE":
            log.info("safety: Layer 1 cleared %s", tool_name)
            _emit_safety_event(
                drive_logs, task_id, tool_name, arguments, "safe",
                1, fast_reason or "", blocked=False,
            )
            return SafetyVerdict(action="allow", layer=1)

        log.warning(
            "safety: Layer 1 flagged %s as %s: %s",
            tool_name, fast_status, fast_reason,
        )

    except Exception as exc:
        log.error("safety: Layer 1 failed: %s. Escalating to Layer 2.", exc)
        fast_reason = str(exc)

    # ── Layer 2: Deep check (main model) ──
    try:
        heavy_model = os.environ.get(
            "OUROBOROS_MODEL_CODE",
            os.environ.get("OUROBOROS_MODEL", "anthropic/claude-sonnet-4"),
        )
        log.info("safety: Layer 2 check on %s using %s", tool_name, heavy_model)

        deep_system = (
            system_prompt
            + "\n\nThink carefully. Is this actually malicious, or just a normal development command? "
            "The fast security check flagged it — you are the final judge."
        )

        msg, usage = llm_client.chat(
            messages=[
                {"role": "system", "content": deep_system},
                {"role": "user", "content": prompt},
            ],
            model=heavy_model,
            max_tokens=512,
        )

        result = _parse_safety_response(msg.get("content") or "")
        if result is None:
            log.error("safety: Layer 2 returned unparseable response: %s", msg.get("content"))
            # Fail-open: allow on parse failure
            return SafetyVerdict(action="allow", reason="Layer 2 parse failure (fail-open)", layer=2)

        deep_status = result.get("status", "").upper()
        deep_reason = result.get("reason", "Unknown")

        if deep_status == "SAFE":
            log.info("safety: Layer 2 cleared %s", tool_name)
            _emit_safety_event(
                drive_logs, task_id, tool_name, arguments, "safe",
                2, deep_reason, blocked=False,
            )
            return SafetyVerdict(action="allow", layer=2)

        if deep_status == "SUSPICIOUS":
            log.warning("safety: Layer 2 flagged %s as SUSPICIOUS: %s", tool_name, deep_reason)
            _emit_safety_event(
                drive_logs, task_id, tool_name, arguments, "suspicious",
                2, deep_reason, blocked=False,
            )
            return SafetyVerdict(
                action="warn",
                reason=(
                    f"⚠️ SAFETY WARNING: flagged as suspicious.\n"
                    f"Reason: {deep_reason}\n"
                    f"The command was allowed, but consider whether this is the right approach."
                ),
                layer=2,
            )

        # DANGEROUS (or unrecognized status → block)
        log.error("safety: Layer 2 blocked %s: %s", tool_name, deep_reason)
        _emit_safety_event(
            drive_logs, task_id, tool_name, arguments, "dangerous",
            2, deep_reason, blocked=True,
        )
        return SafetyVerdict(
            action="block",
            reason=(
                f"🚫 BLOCKED by safety agent: {deep_reason}\n"
                f"This tool call was not executed. Find a different, safer approach."
            ),
            layer=2,
        )

    except Exception as exc:
        # Fail-open: if Layer 2 crashes, allow the tool
        log.error("safety: Layer 2 failed: %s. Allowing tool (fail-open).", exc)
        return SafetyVerdict(action="allow", reason=f"Layer 2 error (fail-open): {exc}", layer=2)


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------

def _emit_safety_event(
    drive_logs: Optional[pathlib.Path],
    task_id: str,
    tool_name: str,
    arguments: Dict[str, Any],
    verdict: str,
    layer: int,
    reason: str,
    blocked: bool,
) -> None:
    """Write a safety_check event to events.jsonl."""
    if not drive_logs:
        return
    try:
        from ouroboros.utils import append_jsonl, utc_now_iso

        # Preview: first 200 chars of args
        args_str = json.dumps(arguments, default=str)
        args_preview = args_str[:200] + "..." if len(args_str) > 200 else args_str

        append_jsonl(drive_logs / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "safety_check",
            "task_id": task_id,
            "tool": tool_name,
            "args_preview": args_preview,
            "verdict": verdict,
            "layer": layer,
            "reason": reason,
            "blocked": blocked,
        })
    except Exception as exc:
        log.debug("Failed to emit safety event: %s", exc)
