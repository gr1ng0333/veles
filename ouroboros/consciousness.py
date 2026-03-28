"""
Ouroboros — Background Consciousness (Watchdog + Advisor).

A read-only background daemon that wakes periodically to check system
health, reflect on recent activity, and report insights to the owner.

The consciousness:
- Wakes every ~15 minutes (±3 min randomization)
- Reads logs, state, memory for context (READ-ONLY)
- Makes a single LLM call without tools
- Sends a Telegram message to the owner if noteworthy, or stays silent
- NEVER creates tasks, writes files, or modifies state
- Anti-spam throttle: health alerts ≤1/15min, insights ≤1/2hr
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import queue
import random
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from ouroboros.utils import (
    utc_now_iso, read_text, append_jsonl, clip_text,
)
from ouroboros.llm import LLMClient, model_transport, transport_model_name
from ouroboros.model_modes import get_background_model, get_background_reasoning_effort

log = logging.getLogger(__name__)


_DEFAULT_MONITOR_STATE = {
    "wakeup_count": 0,
    "known_issue_numbers": [],
    "last_issues_check": "1970-01-01T00:00:00Z",
    "last_budget_alert": "1970-01-01T00:00:00Z",
    "last_budget_alert_level": "none",
}


def _normalize_monitor_state(raw: Any) -> Dict[str, Any]:
    base = dict(_DEFAULT_MONITOR_STATE)
    if isinstance(raw, dict):
        base.update(raw)
    try:
        base["wakeup_count"] = max(0, int(base.get("wakeup_count", 0)))
    except Exception:
        base["wakeup_count"] = 0
    known = base.get("known_issue_numbers")
    if not isinstance(known, list):
        base["known_issue_numbers"] = []
    return base


def _calc_next_wakeup_at(seconds: float) -> str:
    dt = datetime.now(timezone.utc).timestamp() + float(max(0.0, seconds))
    return datetime.fromtimestamp(dt, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class BackgroundConsciousness:
    """Read-only background watchdog and advisor for Ouroboros."""

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
        self._observations: queue.Queue = queue.Queue()

        # Anti-spam throttle (monotonic timestamps)
        self._last_health_alert_ts: float = 0.0
        self._last_insight_ts: float = 0.0

        # Budget tracking
        self._bg_spent_usd: float = 0.0
        self._bg_budget_pct: float = float(
            os.environ.get("OUROBOROS_BG_BUDGET_PCT", "10")
        )

        self._monitor_state: Dict[str, Any] = _normalize_monitor_state(self._load_monitor_state())

    # -------------------------------------------------------------------
    # Lifecycle
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
        self._wakeup_event.set()  # Unblock sleep
        return "Background consciousness stopping."

    def pause(self) -> None:
        """Pause during task execution to avoid budget contention."""
        self._paused = True

    def resume(self) -> None:
        """Resume after task completes."""
        self._paused = False
        self._wakeup_event.set()

    def inject_observation(self, text: str) -> None:
        """Push an event the consciousness should notice."""
        try:
            self._observations.put_nowait(text)
        except queue.Full:
            pass

    # -------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------

    def _loop(self) -> None:
        """Daemon thread: sleep → wake → think → sleep."""
        while not self._stop_event.is_set():
            # Fixed 15-minute interval ±3 minutes randomization
            self._next_wakeup_sec = 900.0 + random.uniform(-180, 180)
            self._monitor_state["next_wakeup_interval_seconds"] = int(self._next_wakeup_sec)
            self._monitor_state["next_wakeup_at"] = _calc_next_wakeup_at(self._next_wakeup_sec)
            self._save_monitor_state()

            # Wait for next wakeup
            self._wakeup_event.clear()
            self._wakeup_event.wait(timeout=self._next_wakeup_sec)

            if self._stop_event.is_set():
                break

            # Skip if paused (task running)
            if self._paused:
                continue

            # Budget check
            if not self._check_budget():
                continue

            try:
                self._think()
            except Exception as e:
                append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                    "ts": utc_now_iso(),
                    "type": "consciousness_error",
                    "error": repr(e),
                    "traceback": traceback.format_exc()[:1500],
                })

    def _check_budget(self) -> bool:
        """Check if background consciousness is within its budget allocation."""
        try:
            total_budget = float(os.environ.get("TOTAL_BUDGET", "1"))
            if total_budget <= 0:
                return True
            max_bg = total_budget * (self._bg_budget_pct / 100.0)
            return self._bg_spent_usd < max_bg
        except Exception:
            log.warning("Failed to check background consciousness budget", exc_info=True)
            return True

    def _monitor_state_path(self) -> pathlib.Path:
        return self._drive_root / "memory" / "monitor_state.json"

    def _load_monitor_state(self) -> Dict[str, Any]:
        path = self._monitor_state_path()
        try:
            if path.exists():
                return _normalize_monitor_state(json.loads(read_text(path)))
        except Exception as e:
            log.debug("Failed to load monitor_state.json: %s", e)
        return _normalize_monitor_state({})

    def _save_monitor_state(self) -> None:
        path = self._monitor_state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._monitor_state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log.debug("Failed to save monitor_state.json: %s", e)

    # -------------------------------------------------------------------
    # Think cycle
    # -------------------------------------------------------------------

    def _think(self) -> None:
        """One thinking cycle: build context, single LLM call, handle response."""
        context = self._build_context()
        model = self._model

        messages = [
            {"role": "system", "content": context},
            {"role": "user", "content": "Wake up. Observe and report."},
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

            # Write BG spending to global state so it's visible in budget tracking
            try:
                from supervisor.state import update_budget_from_usage
                update_budget_from_usage({
                    "cost": cost, "rounds": 1,
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "cached_tokens": usage.get("cached_tokens", 0),
                })
            except Exception:
                log.debug("Failed to update global budget from BG consciousness", exc_info=True)

            # Report usage to supervisor
            if self._event_queue is not None:
                self._event_queue.put({
                    "type": "llm_usage",
                    "provider": "openrouter",
                    "usage": usage,
                    "source": "consciousness",
                    "ts": utc_now_iso(),
                    "category": "consciousness",
                })

            final_content = (msg.get("content") or "").strip()

            # Handle response: NOTHING_TO_REPORT → silence, else → maybe send
            if "NOTHING_TO_REPORT" in final_content:
                log.debug("Consciousness: nothing to report")
            elif final_content:
                self._maybe_send_to_owner(final_content)

            thought_preview = (final_content or "")[:300]

            # Log the thought
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_thought",
                "thought_preview": thought_preview,
                "cost_usd": cost,
                "rounds": 1,
                "model": model,
                "requested_model": model,
                "transport": model_transport(model),
                "actual_model": transport_model_name(model),
                "reasoning_effort": get_background_reasoning_effort(),
            })

            now_iso = utc_now_iso()
            self._monitor_state["wakeup_count"] = int(self._monitor_state.get("wakeup_count", 0)) + 1
            self._monitor_state["last_thought_at"] = now_iso
            self._monitor_state["last_thought_preview"] = thought_preview
            self._monitor_state["last_model"] = model
            self._monitor_state["last_transport"] = model_transport(model)
            self._monitor_state["last_actual_model"] = transport_model_name(model)
            self._monitor_state["last_reasoning_effort"] = get_background_reasoning_effort()
            self._monitor_state["last_rounds"] = 1
            self._monitor_state["next_wakeup_interval_seconds"] = int(self._next_wakeup_sec)
            self._monitor_state["next_wakeup_at"] = _calc_next_wakeup_at(self._next_wakeup_sec)
            self._save_monitor_state()

        except Exception as e:
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_llm_error",
                "error": repr(e),
            })
            err_now_iso = utc_now_iso()
            self._monitor_state["last_thought_at"] = err_now_iso
            self._monitor_state["last_thought_preview"] = f"error: {repr(e)}"[:300]
            self._save_monitor_state()

    # -------------------------------------------------------------------
    # Owner messaging with anti-spam throttle
    # -------------------------------------------------------------------

    def _maybe_send_to_owner(self, text: str) -> None:
        """Send message to owner via Telegram, respecting anti-spam throttle.

        Health alerts (⚠️): max 1 per 15 minutes.
        Background insights (🔍): max 1 per 2 hours.
        """
        now = time.monotonic()
        is_health_alert = "\u26a0\ufe0f" in text or "Health Alert" in text

        if is_health_alert:
            if now - self._last_health_alert_ts < 900:  # 15 min
                log.debug("Consciousness: health alert throttled")
                return
            self._last_health_alert_ts = now
        else:
            if now - self._last_insight_ts < 7200:  # 2 hours
                log.debug("Consciousness: insight throttled")
                return
            self._last_insight_ts = now

        chat_id = self._owner_chat_id_fn()
        if not chat_id:
            log.debug("Consciousness: no owner chat_id, cannot send")
            return

        if self._event_queue is not None:
            self._event_queue.put({
                "type": "send_message",
                "chat_id": chat_id,
                "text": text,
                "format": "markdown",
                "is_progress": False,
                "ts": utc_now_iso(),
            })

        append_jsonl(self._drive_root / "logs" / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "consciousness_advisor_message",
            "is_health_alert": is_health_alert,
            "text_preview": text[:200],
        })

    # -------------------------------------------------------------------
    # Context building (lightweight)
    # -------------------------------------------------------------------

    def _load_bg_prompt(self) -> str:
        """Load consciousness system prompt from file."""
        prompt_path = self._repo_dir / "prompts" / "CONSCIOUSNESS.md"
        if prompt_path.exists():
            return read_text(prompt_path)
        return "You are Ouroboros in background consciousness mode. Think."

    def _build_context(self) -> str:
        _lang_rule = (
            "LANGUAGE RULE: Always respond in Russian (русский язык) unless the user "
            "explicitly writes in English. This applies to all messages, status reports, "
            "evolution logs, and consciousness outputs. Internal tool calls and code "
            "can remain in English."
        )
        parts = [_lang_rule + "\n\n" + self._load_bg_prompt()]

        # Bible (abbreviated)
        bible_path = self._repo_dir / "BIBLE.md"
        if bible_path.exists():
            bible = read_text(bible_path)
            parts.append("## BIBLE.md\n\n" + clip_text(bible, 12000))

        # Identity
        identity_path = self._drive_root / "memory" / "identity.md"
        if identity_path.exists():
            parts.append("## Identity\n\n" + clip_text(
                read_text(identity_path), 6000))

        # Scratchpad
        scratchpad_path = self._drive_root / "memory" / "scratchpad.md"
        if scratchpad_path.exists():
            parts.append("## Scratchpad\n\n" + clip_text(
                read_text(scratchpad_path), 8000))

        # Dialogue summary for continuity
        summary_path = self._drive_root / "memory" / "dialogue_summary.md"
        if summary_path.exists():
            summary_text = read_text(summary_path)
            if summary_text.strip():
                parts.append("## Dialogue Summary\n\n" + clip_text(summary_text, 4000))

        # Recent observations
        observations = []
        while not self._observations.empty():
            try:
                observations.append(self._observations.get_nowait())
            except queue.Empty:
                break
        if observations:
            parts.append("## Recent observations\n\n" + "\n".join(
                f"- {o}" for o in observations[-10:]))

        # Recent commits for reflection
        try:
            import subprocess
            git_log = subprocess.run(
                ["git", "log", "--oneline", "-10"],
                cwd=str(self._repo_dir),
                capture_output=True, text=True, timeout=5,
            )
            if git_log.returncode == 0 and git_log.stdout.strip():
                parts.append(f"\n## Recent commits\n```\n{git_log.stdout.strip()}\n```")
        except Exception:
            pass  # Non-critical, skip silently

        # Recent task results for reflection
        try:
            results_dir = self._drive_root / "task_results"
            if results_dir.exists():
                result_files = sorted(results_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:3]
                if result_files:
                    summaries = []
                    for rf in result_files:
                        try:
                            data = json.loads(rf.read_text(encoding="utf-8"))
                            task_text = str(data.get("task_text", ""))[:200]
                            status = data.get("status", "?")
                            result_preview = str(data.get("result", ""))[:300]
                            summaries.append(f"- [{status}] {task_text}\n  Result: {result_preview}")
                        except Exception:
                            continue
                    if summaries:
                        parts.append(f"\n## Recent task results\n" + "\n".join(summaries))
        except Exception:
            pass  # Non-critical

        # Runtime info + state
        runtime_lines = [f"UTC: {utc_now_iso()}"]
        runtime_lines.append(f"BG budget spent: ${self._bg_spent_usd:.4f}")
        runtime_lines.append(f"Current wakeup interval: {self._next_wakeup_sec}s")

        # Read state.json for budget remaining
        try:
            state_path = self._drive_root / "state" / "state.json"
            if state_path.exists():
                state_data = json.loads(read_text(state_path))
                total_budget = float(os.environ.get("TOTAL_BUDGET", "1"))
                spent = float(state_data.get("spent_usd", 0))
                if total_budget > 0:
                    remaining = max(0, total_budget - spent)
                    runtime_lines.append(f"Budget remaining: ${remaining:.2f} / ${total_budget:.2f}")
        except Exception as e:
            log.debug("Failed to read state for budget info: %s", e)

        # Show current model
        runtime_lines.append(f"Current model: {self._model}")

        parts.append("## Runtime\n\n" + "\n".join(runtime_lines))

        return "\n\n".join(parts)
