"""
Supervisor — child_watch: wakes Veles when an ocbot job it delegated ends.

Polled from the main loop without any model call: one non-blocking read of the
ocbot event feed every POLL_INTERVAL_SEC. Terminal events of Veles's jobs become
a `task` with the supervision protocol; everything else is only logged. The
cursor lives in state/child_watch.json, so restarts neither lose nor replay
events (it is committed only after the tasks were enqueued).
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from ouroboros import child_api

log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 10
OUTAGE_NOTICE_SEC = 600
WAKE_TYPES = {"job.done", "job.needs_attention", "job.stopped", "job.failed"}
CHILD_TOOLS = ("child_report,child_diff,child_accept,child_followup,child_reject,"
               "child_delegate,child_status,child_jobs,child_stop")

PROTOCOL = """Протокол надзора за младшим:
1. skill_load("child-ocbot") и enable_tools(tools='{tools}').
2. child_report — прочитай отчёт, проверку ocbot, коммиты и diff-stat; child_diff — ключевые файлы, а не всё подряд.
3. Проверь сам: child_accept(job_id, verify_only=true).
4. Реши:
   - принять → child_accept(job_id) (слияние в базовую ветку без push);
   - поправить → child_followup с конкретными замечаниями (не больше 2 поправок на задачу);
   - отклонить → child_reject с причиной, затем переформулируй или разбей задачу.
5. Если parent_ref указывает на шаг плана — обнови план и делегируй следующий шаг через child_delegate.
Ограничения: задачу, остановленную владельцем, не перезапускай — сообщи ему. Если причина «Модель недоступна»/лимит — не повторяй сразу, сообщи владельцу. Не больше 3 отклонений подряд по одному плану — остановись и спроси владельца."""


class ChildWatch:
    def __init__(self, drive_root: pathlib.Path, *, clock: Callable[[], float] = time.time,
                 request: Callable[..., Dict[str, Any]] = child_api.request):
        self.path = pathlib.Path(drive_root) / "state" / "child_watch.json"
        self.clock = clock
        self.request = request
        self.last_poll = 0.0
        self.failing_since: Optional[float] = None
        self.outage_notified = False

    # -- cursor -------------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _store(self, state: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(self.path)

    def commit(self, cursor: int) -> None:
        state = self._load()
        state["after"] = int(cursor)
        self._store(state)

    # -- polling ------------------------------------------------------------
    def _initial_cursor(self) -> int:
        """First run: start from the end of the feed instead of replaying history."""
        after = 0
        while True:
            data = self.request("GET", "/events", query={"after": after, "limit": 500, "wait": 0}, timeout=5)
            if not data.get("events"):
                return after
            after = int(data["next"])

    def poll(self, owner_chat_id: Optional[int]) -> tuple[List[Dict[str, Any]], Optional[int], Optional[str]]:
        """Return (tasks to enqueue, cursor to commit after enqueueing, owner notice)."""
        now = self.clock()
        if now - self.last_poll < POLL_INTERVAL_SEC:
            return [], None, None
        self.last_poll = now
        state = self._load()
        try:
            if "after" not in state:
                self.commit(self._initial_cursor())
                state = self._load()
            data = self.request("GET", "/events", query={"after": state["after"], "limit": 100, "wait": 0}, timeout=5)
        except child_api.ChildApiError as exc:
            if exc.code == "token_unavailable":
                return [], None, None  # ocbot integration is not installed here
            if self.failing_since is None:
                self.failing_since = now
            if not self.outage_notified and now - self.failing_since >= OUTAGE_NOTICE_SEC:
                self.outage_notified = True
                return [], None, f"⚠️ Младший (ocbot) не отвечает больше {OUTAGE_NOTICE_SEC // 60} мин: {exc}"
            return [], None, None
        notice = None
        if self.outage_notified:
            notice = "✅ Младший (ocbot) снова на связи."
        self.failing_since = None
        self.outage_notified = False
        tasks = []
        for event in data.get("events") or []:
            if event.get("origin") != "veles" or event.get("type") not in WAKE_TYPES or not owner_chat_id:
                continue
            tasks.append(self._task(event, int(owner_chat_id)))
        cursor = int(data.get("next") or state["after"])
        return tasks, (cursor if cursor != state["after"] else None), notice

    def _task(self, event: Dict[str, Any], chat_id: int) -> Dict[str, Any]:
        data = event.get("data") or {}
        state = event["type"].split(".", 1)[1]
        lines = [
            f"[child_report] Младший (ocbot) закончил задачу J{event.get('num')}: состояние {state}.",
            f"job_id: {event['job_id']}",
        ]
        if event.get("parent_ref"):
            lines.append(f"parent_ref: {event['parent_ref']}")
        if data.get("reason"):
            lines.append("Причина: " + str(data["reason"])[:800])
        if data.get("by") and state == "stopped":
            lines.append("Остановил: " + ("владелец" if data["by"] == "owner" else "Велес"))
        if data.get("published"):
            lines.append("Ветка опубликована: " + str(data["published"])[:12])
        lines += ["", PROTOCOL.format(tools=CHILD_TOOLS)]
        return {
            "id": uuid.uuid4().hex[:8],
            "type": "task",
            "priority": 0,
            "chat_id": chat_id,
            "text": "\n".join(lines),
            "child_job_id": event["job_id"],
            "child_event_seq": event["seq"],
        }
