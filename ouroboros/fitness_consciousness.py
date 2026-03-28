from __future__ import annotations

import json
import logging
import os
import pathlib
import queue
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from ouroboros.llm import LLMClient
from ouroboros.loop import run_llm_loop
from ouroboros.model_modes import (
    get_background_model,
    get_background_reasoning_effort,
)
from ouroboros.tools import ToolRegistry
from ouroboros.tools.registry import ToolEntry, ToolContext
from ouroboros.utils import append_jsonl, clip_text, read_text, utc_now_iso
from supervisor.state import load_state, save_state

log = logging.getLogger(__name__)

FITNESS_TZ = timezone(timedelta(hours=3))
FITNESS_WAKE_HOURS = (9, 13, 20)
QUIET_WINDOW = timedelta(minutes=15)
QUIET_DELAY = timedelta(minutes=20)
QUIET_MAX_RETRIES = 3

_DEFAULT_MONITOR_STATE = {
    "wakeup_count": 0,
    "quiet_retry_count": 0,
    "last_run_at": "1970-01-01T00:00:00Z",
    "last_sent_at": "1970-01-01T00:00:00Z",
    "pending_wakeup_at": "",
    "fitness_spent_usd": 0.0,
}


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _normalize_monitor_state(raw: Any) -> Dict[str, Any]:
    base = dict(_DEFAULT_MONITOR_STATE)
    if isinstance(raw, dict):
        base.update(raw)
    try:
        base["wakeup_count"] = max(0, int(base.get("wakeup_count", 0)))
    except Exception:
        base["wakeup_count"] = 0
    try:
        base["quiet_retry_count"] = max(0, int(base.get("quiet_retry_count", 0)))
    except Exception:
        base["quiet_retry_count"] = 0
    try:
        base["fitness_spent_usd"] = max(0.0, float(base.get("fitness_spent_usd", 0.0) or 0.0))
    except Exception:
        base["fitness_spent_usd"] = 0.0
    return base


def _seconds_until_next_slot(now_utc: Optional[datetime] = None) -> float:
    now_utc = now_utc or datetime.now(timezone.utc)
    now_local = now_utc.astimezone(FITNESS_TZ)
    candidates = []
    for days_ahead in (0, 1):
        base_date = (now_local + timedelta(days=days_ahead)).date()
        for hour in FITNESS_WAKE_HOURS:
            candidate_local = datetime.combine(
                base_date,
                datetime.min.time(),
                tzinfo=FITNESS_TZ,
            ).replace(hour=hour)
            candidate_utc = candidate_local.astimezone(timezone.utc)
            if candidate_utc > now_utc:
                candidates.append(candidate_utc)
    if not candidates:
        return 24 * 3600.0
    return max(1.0, (min(candidates) - now_utc).total_seconds())


class FitnessConsciousness:
    """Isolated fitness daemon with its own prompt, logs, schedule and budget."""

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

        self._fitness_root = self._drive_root / "fitness"
        self._fitness_logs = self._fitness_root / "logs"
        self._fitness_state_dir = self._fitness_root / "state"
        self._fitness_logs.mkdir(parents=True, exist_ok=True)
        self._fitness_state_dir.mkdir(parents=True, exist_ok=True)

        self._llm = LLMClient()
        self._running = False
        self._paused = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._wakeup_event = threading.Event()
        self._next_wakeup_sec = _seconds_until_next_slot()
        self._fitness_budget_usd = float(os.environ.get("OUROBOROS_FITNESS_BUDGET_USD", "50"))
        self._monitor_state = _normalize_monitor_state(self._load_monitor_state())

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    @property
    def _model(self) -> str:
        return get_background_model()

    def start(self) -> str:
        if self.is_running:
            return "Fitness consciousness is already running."
        self._running = True
        self._paused = False
        self._stop_event.clear()
        self._wakeup_event.clear()
        self._schedule_next_slot()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="fitness-consciousness")
        self._thread.start()
        return "Fitness consciousness started."

    def stop(self) -> str:
        if not self._running:
            return "Fitness consciousness is not running."
        self._running = False
        self._stop_event.set()
        self._wakeup_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None
        return "Fitness consciousness stopped."

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False
        self._wakeup_event.set()

    def inject_observation(self, text: str) -> None:
        append_jsonl(self._fitness_logs / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "fitness_observation",
            "text": text[:500],
        })

    def _monitor_state_path(self) -> pathlib.Path:
        return self._fitness_state_dir / "fitness_consciousness.json"

    def _load_monitor_state(self) -> Dict[str, Any]:
        path = self._monitor_state_path()
        if not path.exists():
            return dict(_DEFAULT_MONITOR_STATE)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return dict(_DEFAULT_MONITOR_STATE)

    def _save_monitor_state(self) -> None:
        path = self._monitor_state_path()
        path.write_text(
            json.dumps(self._monitor_state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _load_prompt(self) -> str:
        prompt_path = self._repo_dir / "prompts" / "FITNESS.md"
        if not prompt_path.exists():
            return "Ты — изолированный фитнес-контур. Работай коротко и по делу."
        return read_text(prompt_path)

    def _build_context(self) -> str:
        parts = [
            "Это отдельный фитнес-контур. Основной рабочий контекст сюда не попадает.",
            f"UTC now: {utc_now_iso()}",
            f"Следующие разрешённые локальные wakeup-слоты (UTC+3): {', '.join(str(h) + ':00' for h in FITNESS_WAKE_HOURS)}",
            "Если владельца не нужно дёргать прямо сейчас — не отправляй сообщение и ответь SILENT.",
            "Если нужно задать вопрос или дать короткий пинок/план — используй send_owner_message ровно один раз.",
            "drive_read / drive_write работают только внутри fitness/.",
        ]

        profile_path = self._fitness_root / "profile.json"
        if profile_path.exists():
            parts.append("## Profile\n\n" + clip_text(profile_path.read_text(encoding="utf-8"), 5000))

        week_files = sorted(self._fitness_root.glob("*-W*.json"))
        if week_files:
            latest_week = week_files[-1]
            parts.append(
                f"## Current week ({latest_week.name})\n\n"
                + clip_text(latest_week.read_text(encoding="utf-8"), 6000)
            )

        summary_files = sorted(self._fitness_root.glob("*_summary.json"))
        if summary_files:
            latest_summary = summary_files[-1]
            parts.append(
                f"## Latest summary ({latest_summary.name})\n\n"
                + clip_text(latest_summary.read_text(encoding="utf-8"), 4000)
            )

        state = load_state()
        public_state = {
            "last_owner_message_at": state.get("last_owner_message_at", ""),
            "last_outgoing_at": state.get("last_outgoing_at", ""),
            "fitness_awaiting_reply": state.get("fitness_awaiting_reply", False),
        }
        parts.append("## Shared state\n\n" + json.dumps(public_state, ensure_ascii=False, indent=2))
        parts.append("## Monitor state\n\n" + json.dumps(self._monitor_state, ensure_ascii=False, indent=2))
        return "\n\n".join(parts)

    def _normalize_fitness_path(self, path: str) -> pathlib.Path:
        raw = (path or "").strip().replace("\\", "/").lstrip("/")
        parts = [part for part in raw.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise ValueError("Path traversal is not allowed")
        return self._fitness_root.joinpath(*parts)

    def _tool_drive_read(self, ctx: ToolContext, path: str) -> str:
        target = self._normalize_fitness_path(path)
        if not target.exists() or not target.is_file():
            return f"⚠️ File not found: {path}"
        return target.read_text(encoding="utf-8")

    def _tool_drive_write(self, ctx: ToolContext, path: str, content: str, mode: str = "overwrite") -> str:
        target = self._normalize_fitness_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append" and target.exists():
            with target.open("a", encoding="utf-8") as fh:
                fh.write(content)
        else:
            target.write_text(content, encoding="utf-8")
        append_jsonl(self._fitness_logs / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "fitness_drive_write",
            "path": str(target.relative_to(self._fitness_root)),
            "mode": mode,
            "content_len": len(content),
        })
        return f"OK: wrote {len(content)} chars to fitness/{target.relative_to(self._fitness_root)}"

    def _message_requests_reply(self, text: str) -> bool:
        return "?" in (text or "")

    def _queue_owner_message(self, text: str, reason: str = "") -> str:
        chat_id = self._owner_chat_id_fn()
        if not chat_id:
            return "⚠️ No active chat — cannot send proactive message."
        message = (text or "").strip()
        if not message:
            return "⚠️ Empty message."

        if self._event_queue is not None:
            try:
                self._event_queue.put_nowait({
                    "type": "send_message",
                    "chat_id": chat_id,
                    "text": message,
                    "format": "markdown",
                    "is_progress": False,
                    "ts": utc_now_iso(),
                    "chat_scope": "fitness",
                })
            except Exception:
                log.exception("Failed to enqueue fitness owner message")

        append_jsonl(self._fitness_logs / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "fitness_proactive_message",
            "reason": reason,
            "text_preview": message[:200],
        })

        if self._message_requests_reply(message):
            state = load_state()
            state["fitness_awaiting_reply"] = True
            state["fitness_last_question_at"] = utc_now_iso()
            save_state(state)
        self._monitor_state["last_sent_at"] = utc_now_iso()
        self._save_monitor_state()
        return "OK: fitness message queued for delivery."

    def _tool_send_owner_message(self, ctx: ToolContext, text: str, reason: str = "") -> str:
        return self._queue_owner_message(text=text, reason=reason)

    def _append_chat_log(self, direction: str, text: str) -> None:
        state = load_state()
        append_jsonl(self._fitness_logs / "chat.jsonl", {
            "ts": utc_now_iso(),
            "session_id": state.get("session_id"),
            "direction": direction,
            "chat_id": self._owner_chat_id_fn(),
            "user_id": state.get("owner_id"),
            "text": text,
        })

    def handle_owner_message(self, text: str) -> str:
        message = (text or "").strip()
        if not message:
            return "⚠️ Empty fitness message."

        self._append_chat_log("in", message)
        state = load_state()
        state["fitness_awaiting_reply"] = False
        state["fitness_next_message"] = False
        state["fitness_last_owner_message_at"] = utc_now_iso()
        save_state(state)

        if not self._check_budget():
            append_jsonl(self._fitness_logs / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "fitness_budget_exhausted",
                "source": "owner_message",
                "fitness_spent_usd": self._monitor_state.get("fitness_spent_usd", 0.0),
                "budget_cap_usd": self._fitness_budget_usd,
            })
            return "⚠️ Fitness budget exhausted."

        tools = self._build_tools()
        system_prompt = self._load_prompt() + "\n\nОперационные правила:\n- Это прямое входящее сообщение владельца в fitness-контур.\n- Если нужен видимый ответ владельцу, используй send_owner_message и заверши ответом SILENT.\n- Не трогай основной контекст и не упоминай внутреннюю архитектуру без нужды."
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self._build_context()},
            {"role": "user", "content": f"Новое сообщение владельца для fitness-контура:\n{message}"},
        ]
        incoming = queue.Queue()
        last_sent_before = self._monitor_state.get("last_sent_at")
        final_text, usage, _ = run_llm_loop(
            messages=messages,
            tools=tools,
            llm=self._llm,
            drive_logs=self._fitness_logs,
            emit_progress=lambda _text: None,
            incoming_messages=incoming,
            task_type="fitness_owner_message",
            task_id=f"fitness-owner-{int(time.time())}",
            budget_remaining_usd=self._fitness_budget_usd,
            event_queue=self._event_queue,
            initial_effort=get_background_reasoning_effort(),
            drive_root=self._drive_root,
        )
        try:
            self._monitor_state["fitness_spent_usd"] = float(self._monitor_state.get("fitness_spent_usd", 0.0) or 0.0) + float(usage.get("cost_usd", 0.0) or 0.0)
        except Exception:
            pass
        self._monitor_state["last_run_at"] = utc_now_iso()
        self._save_monitor_state()
        append_jsonl(self._fitness_logs / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "fitness_owner_message_handled",
            "text_preview": message[:200],
            "cost_usd": float(usage.get("cost_usd", 0.0) or 0.0),
        })
        sent_during_run = self._monitor_state.get("last_sent_at") != last_sent_before
        reply = (final_text or "").strip()
        if reply and reply.upper() != "SILENT" and not sent_during_run:
            self._queue_owner_message(reply, reason="owner_message_reply_fallback")
        return "OK: fitness owner message handled."

    def _build_tools(self) -> ToolRegistry:
        registry = ToolRegistry(self._repo_dir, self._drive_root)
        allowed = {
            name
            for name in registry.available_tools()
            if name.startswith("fitness_") or name.startswith("fatsecret_")
        }
        allowed.update({"drive_read", "drive_write", "send_owner_message"})
        registry._entries = {name: entry for name, entry in registry._entries.items() if name in allowed}

        base_read = registry._entries["drive_read"]
        base_write = registry._entries["drive_write"]
        base_send = registry._entries["send_owner_message"]
        registry.register(ToolEntry(
            name="drive_read",
            schema=base_read.schema,
            handler=self._tool_drive_read,
            is_code_tool=base_read.is_code_tool,
            timeout_sec=base_read.timeout_sec,
        ))
        registry.register(ToolEntry(
            name="drive_write",
            schema=base_write.schema,
            handler=self._tool_drive_write,
            is_code_tool=base_write.is_code_tool,
            timeout_sec=base_write.timeout_sec,
        ))
        registry.register(ToolEntry(
            name="send_owner_message",
            schema=base_send.schema,
            handler=self._tool_send_owner_message,
            is_code_tool=base_send.is_code_tool,
            timeout_sec=base_send.timeout_sec,
        ))

        ctx = ToolContext(
            repo_dir=self._repo_dir,
            drive_root=self._drive_root,
            current_chat_id=self._owner_chat_id_fn(),
            current_task_type="fitness_consciousness",
            event_queue=self._event_queue,
            task_id=f"fitness-{int(time.time())}",
        )
        registry.set_context(ctx)
        return registry

    def _schedule_next_slot(self, now_utc: Optional[datetime] = None) -> float:
        self._next_wakeup_sec = _seconds_until_next_slot(now_utc)
        next_wakeup = (now_utc or datetime.now(timezone.utc)) + timedelta(seconds=self._next_wakeup_sec)
        self._monitor_state["pending_wakeup_at"] = next_wakeup.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        self._monitor_state["quiet_retry_count"] = 0
        self._save_monitor_state()
        return self._next_wakeup_sec

    def _delay_for_quiet(self, now_utc: datetime) -> Dict[str, Any]:
        state = load_state()
        recent_ts = []
        for key in ("last_owner_message_at", "last_outgoing_at"):
            parsed = _parse_iso(state.get(key))
            if parsed is not None:
                recent_ts.append(parsed)
        last_active_at = max(recent_ts) if recent_ts else None
        if last_active_at is None or (now_utc - last_active_at) >= QUIET_WINDOW:
            return {"delayed": False, "reason": "quiet"}

        retries = int(self._monitor_state.get("quiet_retry_count", 0) or 0)
        if retries < QUIET_MAX_RETRIES:
            self._monitor_state["quiet_retry_count"] = retries + 1
            delayed_until = now_utc + QUIET_DELAY
            self._next_wakeup_sec = QUIET_DELAY.total_seconds()
            self._monitor_state["pending_wakeup_at"] = delayed_until.isoformat().replace("+00:00", "Z")
            self._save_monitor_state()
            append_jsonl(self._fitness_logs / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "fitness_quiet_delay",
                "retry": retries + 1,
                "delayed_until": delayed_until.isoformat().replace("+00:00", "Z"),
            })
            return {"delayed": True, "reason": "recent_activity", "retry": retries + 1}

        self._monitor_state["quiet_retry_count"] = 0
        self._schedule_next_slot(now_utc)
        append_jsonl(self._fitness_logs / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "fitness_quiet_cancelled",
            "reason": "recent_activity_max_retries",
        })
        return {"delayed": True, "reason": "cancelled_after_retries", "retry": retries}

    def _check_budget(self) -> bool:
        return float(self._monitor_state.get("fitness_spent_usd", 0.0) or 0.0) < self._fitness_budget_usd

    def _think(self) -> None:
        if not self._check_budget():
            append_jsonl(self._fitness_logs / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "fitness_budget_exhausted",
                "fitness_spent_usd": self._monitor_state.get("fitness_spent_usd", 0.0),
                "budget_cap_usd": self._fitness_budget_usd,
            })
            self._next_wakeup_sec = 6 * 3600.0
            return

        tools = self._build_tools()
        system_prompt = self._load_prompt() + "\n\nОперационные правила:\n- Если не нужно писать владельцу, ответь SILENT.\n- Если нужно написать владельцу, используй send_owner_message.\n- Не трогай основной контекст и не упоминай внутреннюю архитектуру без нужды."
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self._build_context()},
        ]
        incoming = queue.Queue()
        final_text, usage, _ = run_llm_loop(
            messages=messages,
            tools=tools,
            llm=self._llm,
            drive_logs=self._fitness_logs,
            emit_progress=lambda _text: None,
            incoming_messages=incoming,
            task_type="fitness_consciousness",
            task_id=f"fitness-{int(time.time())}",
            budget_remaining_usd=self._fitness_budget_usd,
            event_queue=self._event_queue,
            initial_effort=get_background_reasoning_effort(),
            drive_root=self._drive_root,
        )
        try:
            self._monitor_state["fitness_spent_usd"] = float(self._monitor_state.get("fitness_spent_usd", 0.0) or 0.0) + float(usage.get("cost_usd", 0.0) or 0.0)
        except Exception:
            pass
        self._monitor_state["wakeup_count"] = int(self._monitor_state.get("wakeup_count", 0) or 0) + 1
        self._monitor_state["last_run_at"] = utc_now_iso()
        append_jsonl(self._fitness_logs / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "fitness_cycle_done",
            "final_text": (final_text or "")[:200],
            "cost_usd": usage.get("cost_usd", 0.0),
        })
        self._save_monitor_state()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            timeout = 1.0 if self._paused else max(1.0, float(self._next_wakeup_sec))
            fired = self._wakeup_event.wait(timeout=timeout)
            self._wakeup_event.clear()
            if self._stop_event.is_set():
                break
            if self._paused and not fired:
                continue
            try:
                now_utc = datetime.now(timezone.utc)
                quiet = self._delay_for_quiet(now_utc)
                if not quiet.get("delayed"):
                    self._think()
                    self._schedule_next_slot(datetime.now(timezone.utc))
            except Exception:
                append_jsonl(self._fitness_logs / "events.jsonl", {
                    "ts": utc_now_iso(),
                    "type": "fitness_loop_error",
                    "traceback": traceback.format_exc()[-4000:],
                })
                log.exception("Fitness consciousness loop failed")
                self._schedule_next_slot(datetime.now(timezone.utc))
