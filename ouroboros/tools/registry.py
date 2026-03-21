"""
Ouroboros — Tool registry (SSOT).

Plugin architecture: each module in tools/ exports get_tools().
ToolRegistry collects all tools, provides schemas() and execute().
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
import queue

from ouroboros.utils import safe_relpath


@dataclass
class BrowserState:
    """Per-task browser lifecycle state (Playwright). Isolated from generic ToolContext."""

    pw_instance: Any = None
    browser: Any = None
    context: Any = None
    page: Any = None
    last_screenshot_b64: Optional[str] = None
    last_failure_diagnostics: Optional[Dict[str, Any]] = None
    last_recovery_attempts: List[Dict[str, Any]] = field(default_factory=list)
    saved_sessions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    active_session_name: Optional[str] = None


@dataclass
class ToolContext:
    """Tool execution context — passed from the agent before each task."""

    repo_dir: pathlib.Path
    drive_root: pathlib.Path
    branch_dev: str = "ouroboros"
    pending_events: List[Dict[str, Any]] = field(default_factory=list)
    current_chat_id: Optional[int] = None
    current_task_type: Optional[str] = None
    last_push_succeeded: bool = False
    emit_progress_fn: Callable[[str], None] = field(default=lambda _: None)

    # LLM-driven model/effort switch (set by switch_model tool, read by loop.py)
    active_model_override: Optional[str] = None
    active_effort_override: Optional[str] = None

    # Per-task browser state
    browser_state: BrowserState = field(default_factory=BrowserState)

    # Budget tracking (set by loop.py for real-time usage events)
    event_queue: Optional[Any] = None
    task_id: Optional[str] = None

    # Owner interruptibility hooks for long-running tools
    incoming_messages: Optional[Any] = None
    interrupt_seen_ids: set[str] = field(default_factory=set)

    # Task depth for fork bomb protection
    task_depth: int = 0

    # True when running inside handle_chat_direct (not a queued worker task)
    is_direct_chat: bool = False

    # Current conversation messages (set by loop for safety checks)
    messages: Optional[List[Dict[str, Any]]] = None

    def repo_path(self, rel: str) -> pathlib.Path:
        return (self.repo_dir / safe_relpath(rel)).resolve()

    def drive_path(self, rel: str) -> pathlib.Path:
        return (self.drive_root / safe_relpath(rel)).resolve()

    def checkpoint(self, stage: str, *, payload: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Check whether the active task has been superseded or interrupted by a new owner message."""
        notes: List[str] = []
        pending: List[str] = []
        if self.incoming_messages is not None:
            while True:
                try:
                    msg = self.incoming_messages.get_nowait()
                except queue.Empty:
                    break
                except Exception:
                    break
                else:
                    text = str(msg or '').strip()
                    if text:
                        pending.append(text)
        if self.drive_root and self.task_id:
            try:
                from ouroboros.owner_inject import drain_owner_messages
                for text in drain_owner_messages(self.drive_root, task_id=self.task_id, seen_ids=self.interrupt_seen_ids):
                    if text:
                        pending.append(str(text).strip())
            except Exception:
                pass
        if not pending:
            return None
        lowered = ' '.join(pending).lower()
        reason = 'cancel_requested' if any(tok in lowered for tok in ('stop', 'cancel', 'стоп', 'отмена', 'прекрати', 'остановись')) else 'superseded_by_new_request'
        message = pending[-1]
        event = {
            'type': 'tool_interrupt_checkpoint',
            'task_id': self.task_id,
            'stage': stage,
            'reason': reason,
            'message': message[:500],
            'pending_count': len(pending),
            'payload': payload or {},
        }
        if self.pending_events is not None:
            self.pending_events.append(event)
        if self.event_queue is not None:
            try:
                self.event_queue.put_nowait(event)
            except Exception:
                pass
        return {'stage': stage, 'reason': reason, 'message': message, 'pending_messages': pending, 'payload': payload or {}}


@dataclass
class ToolEntry:
    """Single tool descriptor: name, schema, handler, metadata."""

    name: str
    schema: Dict[str, Any]
    handler: Callable  # fn(ctx: ToolContext, **args) -> str
    is_code_tool: bool = False
    timeout_sec: int = 120


# Per-tool timeout overrides (seconds).
# Applies when a ToolEntry uses the default 120s.
# Tools that set explicit timeout_sec in their ToolEntry keep their value.
TOOL_TIMEOUT_OVERRIDES: Dict[str, int] = {
    # Fast read tools — 15s
    "repo_read": 15, "repo_list": 15,
    "drive_read": 15, "drive_list": 15,
    "knowledge_read": 15, "knowledge_list": 15,
    "git_status": 15, "git_diff": 15,
    "chat_history": 15, "list_available_tools": 15,
    "plan_status": 15, "get_task_result": 15,
    "codebase_digest": 15,
    # Medium write/search tools — 30s
    "repo_write_commit": 30, "repo_commit_push": 30,
    "drive_write": 30, "knowledge_write": 30,
    "run_shell": 30, "web_search": 30, "academic_search": 30,
    "update_scratchpad": 30, "update_identity": 30,
    "plan_create": 30, "plan_step_done": 30, "plan_update": 30,
    "plan_approve": 30, "plan_reject": 30, "plan_complete": 30,
    "send_owner_message": 30, "send_document": 30, "send_local_file": 30,
    "schedule_task": 30, "cancel_task": 30,
    "toggle_evolution": 30, "toggle_consciousness": 30,
    "switch_model": 30, "switch_codex_account": 30,
    "enable_tools": 30, "save_artifact": 30,
    # Slow browser/research tools — 60s
    "browse_page": 60, "browser_action": 60,
    "browser_fill_login_form": 60, "browser_check_login_state": 60,
    "browser_solve_captcha": 60, "send_browser_screenshot": 60,
    "analyze_screenshot": 60, "solve_simple_captcha": 60,
    "vlm_query": 60,
    "compact_context": 60, "request_review": 60,
    "codebase_health": 60, "vps_health_check": 60,
    "monitor_snapshot": 60, "doctor": 60,
    # Very slow tools — 120s+
    "multi_model_review": 120, "wait_for_task": 120,
    # research_run and deep_research have 180s in their ToolEntry
    # browser_run_actions has 120s in its ToolEntry
}


CORE_TOOL_NAMES = {
    "repo_read", "repo_list", "repo_write_commit", "repo_commit_push",
    "drive_read", "drive_list", "drive_write",
    "run_shell",
    "git_status", "git_diff",
    "schedule_task", "wait_for_task", "get_task_result",
    "update_scratchpad", "update_identity",
    "chat_history", "web_search", "academic_search",
    "send_owner_message", "send_document", "send_local_file", "switch_model",
    "request_restart", "promote_to_stable",
    "knowledge_read", "knowledge_write",
    "browse_page", "browser_action", "analyze_screenshot", "solve_simple_captcha",
    "send_browser_screenshot",
    # Plan management
    "plan_create", "plan_approve", "plan_reject", "plan_step_done",
    "plan_update", "plan_complete", "plan_status",
}


class ToolRegistry:
    """Ouroboros tool registry (SSOT).

    To add a tool: create a module in ouroboros/tools/,
    export get_tools() -> List[ToolEntry].
    """

    def __init__(self, repo_dir: pathlib.Path, drive_root: pathlib.Path):
        self._entries: Dict[str, ToolEntry] = {}
        self._ctx = ToolContext(repo_dir=repo_dir, drive_root=drive_root)
        self._load_modules()

    def _load_modules(self) -> None:
        """Auto-discover tool modules in ouroboros/tools/ that export get_tools()."""
        import importlib
        import pkgutil
        import ouroboros.tools as tools_pkg
        for _importer, modname, _ispkg in pkgutil.iter_modules(tools_pkg.__path__):
            if modname.startswith("_") or modname == "registry":
                continue
            try:
                mod = importlib.import_module(f"ouroboros.tools.{modname}")
                if hasattr(mod, "get_tools"):
                    for entry in mod.get_tools():
                        self._entries[entry.name] = entry
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "Failed to load tool module %s", modname, exc_info=True)

    def set_context(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    def _get_llm_client(self) -> Optional[Any]:
        """Lazy-load LLM client for safety checks."""
        try:
            from ouroboros.llm import LLMClient
            return LLMClient()
        except Exception:
            return None

    def register(self, entry: ToolEntry) -> None:
        """Register a new tool (for extension by Ouroboros)."""
        self._entries[entry.name] = entry

    # --- Contract ---

    def available_tools(self) -> List[str]:
        return [e.name for e in self._entries.values()]

    def schemas(self, core_only: bool = False) -> List[Dict[str, Any]]:
        if not core_only:
            return [
                {"type": "function", "function": e.schema, "name": e.name}
                for e in self._entries.values()
            ]
        # Core tools + meta-tools for discovering/enabling extended tools
        result = []
        for e in self._entries.values():
            if e.name in CORE_TOOL_NAMES or e.name in ("list_available_tools", "enable_tools"):
                result.append({"type": "function", "function": e.schema, "name": e.name})
        return result

    def list_non_core_tools(self) -> List[Dict[str, str]]:
        """Return name+description of all non-core tools."""
        result = []
        for e in self._entries.values():
            if e.name not in CORE_TOOL_NAMES:
                desc = e.schema.get("description", "No description")
                result.append({"name": e.name, "description": desc})
        return result

    def get_schema_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Return the full schema for a specific tool."""
        entry = self._entries.get(name)
        if entry:
            return {"type": "function", "function": entry.schema}
        return None

    def get_timeout(self, name: str) -> int:
        """Return timeout_sec for the named tool.

        Priority: explicit ToolEntry.timeout_sec (if not default 120)
        → TOOL_TIMEOUT_OVERRIDES → fallback 30s.
        """
        entry = self._entries.get(name)
        if entry is not None and entry.timeout_sec != 120:
            # Tool module set an explicit non-default timeout
            return entry.timeout_sec
        if name in TOOL_TIMEOUT_OVERRIDES:
            return TOOL_TIMEOUT_OVERRIDES[name]
        if entry is not None:
            return entry.timeout_sec  # default 120
        return 30  # unknown tool default

    def execute(self, name: str, args: Dict[str, Any]) -> str:
        entry = self._entries.get(name)
        if entry is None:
            return f"⚠️ Unknown tool: {name}. Available: {', '.join(sorted(self._entries.keys()))}"

        # --- Safety Agent: pre-execution check ---
        try:
            from ouroboros.safety import check_tool_safety

            drive_logs = self._ctx.drive_root / "logs" if self._ctx.drive_root else None
            verdict = check_tool_safety(
                tool_name=name,
                arguments=args,
                messages=self._ctx.messages,
                llm_client=self._get_llm_client(),
                event_queue=self._ctx.event_queue,
                task_id=self._ctx.task_id or "",
                drive_logs=drive_logs,
            )
            if verdict.action == "block":
                return f"🚫 BLOCKED by safety agent: {verdict.reason}"
        except Exception as exc:
            # Fail-open: safety crash → tool executes normally
            import logging
            logging.getLogger(__name__).warning(
                "Safety check failed for %s, allowing (fail-open): %s", name, exc,
            )

        try:
            result = entry.handler(self._ctx, **args)
        except TypeError as e:
            return f"⚠️ TOOL_ARG_ERROR ({name}): {e}"
        except Exception as e:
            return f"⚠️ TOOL_ERROR ({name}): {e}"

        # Append safety warning if verdict was "warn"
        try:
            if verdict.action == "warn" and verdict.reason:
                return f"{verdict.reason}\n\n---\n{result}"
        except (NameError, AttributeError):
            pass

        return result

    def override_handler(self, name: str, handler) -> None:
        """Override the handler for a registered tool (used for closure injection)."""
        entry = self._entries.get(name)
        if entry:
            self._entries[name] = ToolEntry(
                name=entry.name,
                schema=entry.schema,
                handler=handler,
                timeout_sec=entry.timeout_sec,
            )

    @property
    def CODE_TOOLS(self) -> frozenset:
        return frozenset(e.name for e in self._entries.values() if e.is_code_tool)
