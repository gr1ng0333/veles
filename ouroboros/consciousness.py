"""Ouroboros \u2014 Background System Auditor.

A background daemon that wakes periodically to audit one module of the
codebase at a time. Writes findings to healthcheck.md. Never sends
messages to the owner chat. Never modifies code.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import random
import subprocess
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ouroboros.utils import utc_now_iso, read_text, append_jsonl, clip_text
from ouroboros.llm import LLMClient, model_transport, transport_model_name
from ouroboros.model_modes import get_background_model, get_background_reasoning_effort

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Audit state helpers
# ---------------------------------------------------------------------------

def _discover_modules(repo_dir: pathlib.Path) -> List[str]:
    """Discover all Python modules under ouroboros/ and supervisor/."""
    modules = []
    for pkg in ("ouroboros", "supervisor"):
        pkg_dir = repo_dir / pkg
        if not pkg_dir.is_dir():
            continue
        for py in sorted(pkg_dir.rglob("*.py")):
            rel = py.relative_to(repo_dir)
            # Convert path to dotted module name
            parts = list(rel.parts)
            if parts[-1] == "__init__.py":
                parts = parts[:-1]
            else:
                parts[-1] = parts[-1].removesuffix(".py")
            if parts:
                modules.append(".".join(parts))
    return modules


def _load_audit_state(path: pathlib.Path) -> Dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"checked": {}, "queue_index": 0}


def _save_audit_state(path: pathlib.Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _check_import(module_name: str, repo_dir: pathlib.Path) -> str:
    """Try `python -c 'import <module>'` and return result."""
    try:
        result = subprocess.run(
            ["python", "-c", f"import {module_name}"],
            capture_output=True, text=True, timeout=15,
            cwd=str(repo_dir),
        )
        if result.returncode == 0:
            return "OK"
        return (result.stderr or result.stdout or "unknown error")[:500]
    except subprocess.TimeoutExpired:
        return "TIMEOUT (15s)"
    except Exception as e:
        return f"ERROR: {e}"


def _grep_recent_errors(module_name: str, drive_root: pathlib.Path) -> str:
    """Get last 5 error-like events mentioning this module from events.jsonl."""
    short_name = module_name.split(".")[-1]
    events_path = drive_root / "logs" / "events.jsonl"
    if not events_path.exists():
        return "(no events.jsonl)"
    try:
        # Tail last 500 lines, search for module name in error events
        lines = events_path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
        matches = []
        for line in lines:
            if short_name not in line:
                continue
            try:
                ev = json.loads(line)
                etype = ev.get("type", "")
                if "error" in etype.lower() or "traceback" in str(ev.get("error", "")).lower():
                    matches.append(line[:300])
            except Exception:
                continue
        if not matches:
            return "(no recent errors for this module)"
        return "\n".join(matches[-5:])
    except Exception as e:
        return f"(error reading logs: {e})"


def _read_module_source(module_name: str, repo_dir: pathlib.Path) -> str:
    """Read module source code."""
    parts = module_name.split(".")
    # Try as file first
    file_path = repo_dir / "/".join(parts[:-1]) / f"{parts[-1]}.py" if len(parts) > 1 else repo_dir / f"{parts[0]}.py"
    if not file_path.exists():
        # Try as package __init__.py
        file_path = repo_dir / "/".join(parts) / "__init__.py"
    if not file_path.exists():
        return f"(cannot find source for {module_name})"
    try:
        return file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"(error reading {file_path}: {e})"


def _write_healthcheck(
    drive_root: pathlib.Path,
    module_name: str,
    verdict: str,
    details: str,
    import_result: str,
) -> None:
    """Append or update module entry in healthcheck.md."""
    hc_path = drive_root / "memory" / "healthcheck.md"
    hc_path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing content
    existing = ""
    if hc_path.exists():
        try:
            existing = hc_path.read_text(encoding="utf-8")
        except Exception:
            existing = ""

    # Remove old entry for this module if exists
    lines = existing.splitlines(keepends=True)
    new_lines: list = []
    skip_until_next_module = False
    for line in lines:
        if line.startswith(f"### {module_name}"):
            skip_until_next_module = True
            continue
        if skip_until_next_module and line.startswith("### "):
            skip_until_next_module = False
        if not skip_until_next_module:
            new_lines.append(line)

    existing_clean = "".join(new_lines).rstrip()

    # Add header if missing
    if not existing_clean.startswith("# Healthcheck"):
        existing_clean = f"# Healthcheck Report\n\n_Auto-generated by background auditor. Request via `/healthcheck`._\n\n_Last updated: {utc_now_iso()}_\n" + existing_clean
    else:
        # Update timestamp
        import re
        existing_clean = re.sub(
            r"_Last updated:.*?_",
            f"_Last updated: {utc_now_iso()}_",
            existing_clean,
            count=1,
        )

    # Format new entry
    icon = "\u2705" if verdict == "OK" else "\u26a0\ufe0f"
    entry = f"\n\n### {module_name}\n{icon} **{verdict}** | import: {import_result}\n"
    if verdict != "OK" and details.strip():
        entry += f"\n{details.strip()}\n"

    hc_path.write_text(existing_clean + entry, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class BackgroundConsciousness:
    """Background system auditor for Ouroboros."""

    def __init__(
        self,
        drive_root: pathlib.Path,
        repo_dir: pathlib.Path,
        event_queue: Any,
        owner_chat_id_fn: Callable[[], Optional[int]],
    ):
        self._drive_root = drive_root
        self._repo_dir = repo_dir
        self._event_queue = event_queue
        self._owner_chat_id_fn = owner_chat_id_fn

        self._llm = LLMClient()
        self._running = False
        self._paused = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._wakeup_event = threading.Event()
        self._next_wakeup_sec: float = 900.0

        # Budget tracking
        self._bg_spent_usd: float = 0.0
        self._bg_budget_pct: float = float(
            os.environ.get("OUROBOROS_BG_BUDGET_PCT", "10")
        )

        # Audit state
        self._audit_state_path = drive_root / "memory" / "audit_state.json"
        self._audit_state = _load_audit_state(self._audit_state_path)

    # -------------------------------------------------------------------
    # Lifecycle (unchanged API)
    # -------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    @property
    def _model(self) -> str:
        return get_background_model()

    def start(self) -> str:
        if self.is_running:
            return "Background consciousness is already running."
        self._running = True
        self._paused = False
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return "Background consciousness started."

    def stop(self) -> str:
        if not self.is_running:
            return "Background consciousness is not running."
        self._running = False
        self._stop_event.set()
        self._wakeup_event.set()
        return "Background consciousness stopping."

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False
        self._wakeup_event.set()

    def inject_observation(self, text: str) -> None:
        """Legacy API compat \u2014 no-op in auditor mode."""
        pass

    # -------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._next_wakeup_sec = 900.0 + random.uniform(-180, 180)
            self._wakeup_event.clear()
            self._wakeup_event.wait(timeout=self._next_wakeup_sec)

            if self._stop_event.is_set():
                break
            if self._paused:
                continue
            if not self._check_budget():
                continue

            try:
                self._audit_one_module()
            except Exception as e:
                append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                    "ts": utc_now_iso(),
                    "type": "consciousness_error",
                    "error": repr(e),
                    "traceback": traceback.format_exc()[:1500],
                })

    def _check_budget(self) -> bool:
        try:
            total_budget = float(os.environ.get("TOTAL_BUDGET", "1"))
            if total_budget <= 0:
                return True
            max_bg = total_budget * (self._bg_budget_pct / 100.0)
            return self._bg_spent_usd < max_bg
        except Exception:
            return True

    # -------------------------------------------------------------------
    # Core: audit one module per wakeup
    # -------------------------------------------------------------------

    def _audit_one_module(self) -> None:
        """Pick next unchecked module, audit it, write result."""
        modules = _discover_modules(self._repo_dir)
        if not modules:
            return

        # Find next unchecked module
        checked = self._audit_state.get("checked", {})
        unchecked = [m for m in modules if m not in checked]

        if not unchecked:
            # All checked \u2014 start new cycle
            self._audit_state["checked"] = {}
            self._audit_state["cycle_completed_at"] = utc_now_iso()
            _save_audit_state(self._audit_state_path, self._audit_state)
            unchecked = modules

        module_name = unchecked[0]

        # 1. Check import
        import_result = _check_import(module_name, self._repo_dir)

        # 2. Read source (cap at 4000 chars for context budget)
        source = _read_module_source(module_name, self._repo_dir)
        source_clipped = clip_text(source, 4000)

        # 3. Grep recent errors
        recent_errors = _grep_recent_errors(module_name, self._drive_root)

        # 4. Build LLM context and ask
        prompt = self._load_bg_prompt()
        system_msg = f"{prompt}\n\n## \u041c\u043e\u0434\u0443\u043b\u044c: {module_name}\n\n### \u0418\u043c\u043f\u043e\u0440\u0442-\u0447\u0435\u043a\n{import_result}\n\n### \u041a\u043e\u0434\n```python\n{source_clipped}\n```\n\n### \u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0435 \u043e\u0448\u0438\u0431\u043a\u0438 \u0438\u0437 \u043b\u043e\u0433\u043e\u0432\n{recent_errors}"

        model = self._model
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": f"\u041f\u0440\u043e\u0432\u0435\u0440\u044c \u043c\u043e\u0434\u0443\u043b\u044c {module_name}."},
        ]

        try:
            msg, usage = self._llm.chat(
                messages=messages,
                model=model,
                tools=None,
                reasoning_effort=get_background_reasoning_effort(),
                max_tokens=2048,
            )
            cost = float(usage.get("cost") or 0)
            self._bg_spent_usd += cost

            # Update global budget
            try:
                from supervisor.state import update_budget_from_usage
                update_budget_from_usage({
                    "cost": cost, "rounds": 1,
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "cached_tokens": usage.get("cached_tokens", 0),
                })
            except Exception:
                pass

            if self._event_queue is not None:
                self._event_queue.put({
                    "type": "llm_usage",
                    "provider": "openrouter",
                    "usage": usage,
                    "source": "consciousness",
                    "ts": utc_now_iso(),
                    "category": "consciousness",
                })

            content = (msg.get("content") or "").strip()

            # Parse verdict
            verdict = "OK"
            if "ISSUES_FOUND" in content:
                verdict = "ISSUES_FOUND"
            elif "VERDICT: OK" in content:
                verdict = "OK"

            # Write to healthcheck.md
            _write_healthcheck(
                self._drive_root,
                module_name,
                verdict,
                content,
                "OK" if import_result == "OK" else "FAIL",
            )

            # Mark as checked
            self._audit_state.setdefault("checked", {})[module_name] = {
                "at": utc_now_iso(),
                "verdict": verdict,
            }
            _save_audit_state(self._audit_state_path, self._audit_state)

            # Log
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_audit",
                "module": module_name,
                "verdict": verdict,
                "import_ok": import_result == "OK",
                "cost_usd": cost,
                "model": model,
            })

        except Exception as e:
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_llm_error",
                "module": module_name,
                "error": repr(e),
            })
            # Mark as checked even on error (retry next cycle)
            self._audit_state.setdefault("checked", {})[module_name] = {
                "at": utc_now_iso(),
                "verdict": f"ERROR: {repr(e)[:100]}",
            }
            _save_audit_state(self._audit_state_path, self._audit_state)

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    def _load_bg_prompt(self) -> str:
        prompt_path = self._repo_dir / "prompts" / "CONSCIOUSNESS.md"
        if prompt_path.exists():
            return read_text(prompt_path)
        return "You are a system auditor. Check the module for issues."
