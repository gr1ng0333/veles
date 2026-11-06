"""P0 execution outcomes and durable task checkpoints; no automatic action replay."""
from __future__ import annotations

import json
import os
import pathlib
import re
import tempfile
import shutil
from typing import Any

from ouroboros.utils import utc_now_iso


def set_outcome(usage: dict, status: str, reason: str = "") -> None:
    usage["execution_status"] = status
    usage["execution_reason"] = reason


def outcome(usage: dict) -> dict:
    status = usage.get("execution_status", "completed")
    return {"status": status, "ok": status == "completed",
            "reason": str(usage.get("execution_reason") or "")}


def validate_completion(usage: dict, trace: dict) -> None:
    """A final model paragraph does not override a recorded execution failure."""
    if outcome(usage)["ok"] and any(t.get("is_error") for t in trace.get("tool_calls", [])
                                    if isinstance(t, dict)):
        set_outcome(usage, "failed", "tool_error")


def completion_text(text: str, usage: dict) -> str:
    execution = outcome(usage)
    if not execution["ok"] and not text.startswith("⚠️"):
        return f"⚠️ Задача {execution['status']} ({execution['reason']}); результат требует проверки.\n\n{text}"
    return text


def atomic_json(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".execution-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class TaskCheckpoint:
    """Publish before effects; only ready/response_ready may resume automatically.

    An in-flight tool or provider call has an unknown outcome after process loss.
    Those tasks are reported as interrupted, never started over. Final responses
    are cached before delivery so replaying a delivery cannot rerun tools.
    """
    def __init__(self, root: pathlib.Path, task: dict):
        task_id = str(task.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
            raise ValueError("Invalid task checkpoint id")
        self.path = pathlib.Path(root) / "state" / "task_execution" / (task_id + ".json")
        self.data: dict[str, Any] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                if self.data.get("schema_version") != 1 or self.data.get("task", {}).get("id") != task_id:
                    raise ValueError("Invalid task checkpoint schema or identity")
            except (ValueError, TypeError, AttributeError):
                shutil.copyfile(self.path, self.path.with_suffix(".invalid"))
                self.data = {"schema_version": 1, "task": dict(task), "phase": "invalid_checkpoint"}
        self.task = dict(task)
        self.existing = bool(self.data)

    def begin(self) -> None:
        if not self.data:
            self.data = {"schema_version": 1, "task": self.task, "phase": "preparing"}
            self.write()

    def write(self) -> None:
        self.data["updated_at"] = utc_now_iso()
        atomic_json(self.path, self.data)

    def save(self, phase: str, state: dict, messages: list, ctx: Any, seen: set,
             response: dict | None = None) -> None:
        # anti is immutable runtime policy, rebuilt on resume; executor/queues
        # are process resources, never serialized. Everything else is JSON.
        self.data.update(phase=phase, loop={k: v for k, v in state.items() if k != "anti"},
                         messages=messages, pending_events=list(ctx.pending_events),
                         owner_msg_seen=list(seen), response=response)
        self.data["tool_context"] = {k: getattr(ctx, k, None) for k in
                                     ("_pending_compaction", "active_model_override", "active_effort_override")}
        self.write()

    def finish(self, text: str, usage: dict, trace: dict, events: list) -> None:
        self.data.update(phase="finished", result={"text": text, "usage": usage, "trace": trace},
                         pending_events=list(events), delivery_settled=False)
        self.write()

    def recovery_problem(self) -> str:
        if self.task.get("_recovery_interrupted"):
            return str(self.task["_recovery_interrupted"])
        phase = self.data.get("phase")
        if phase == "finished":
            # Deferred control events may have reached the supervisor before it
            # crashed. Replaying them can repeat scheduling/restarts/actions.
            if not self.data.get("delivery_settled") and any(
                e.get("type") not in ("send_message", "tool_interrupt_checkpoint")
                for e in self.data.get("pending_events", [])
            ):
                return "recovery_unknown_outcome:event_delivery"
            return ""
        if phase in ("ready", "response_ready") and isinstance(self.data.get("loop"), dict):
            return ""
        if self.task.get("_resume_execution") or self.existing:
            return f"recovery_unknown_outcome:{phase or 'missing_checkpoint'}"
        return ""


def recoverable_tasks(root: pathlib.Path) -> list[dict]:
    """Include direct-chat tasks, which never entered the supervisor queue."""
    tasks = []
    for path in sorted((pathlib.Path(root) / "state" / "task_execution").glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            task = row.get("task")
            if not row.get("delivery_settled") and isinstance(task, dict) and task.get("id") and task.get("chat_id"):
                tasks.append({**task, "_resume_execution": True})
        except (OSError, ValueError, TypeError, AttributeError):
            # A corrupt checkpoint is still addressable via its queued task;
            # the agent refuses to execute it rather than erasing the evidence.
            continue
    return tasks


def stop_execution(root: pathlib.Path, task: dict, status: str, reason: str) -> None:
    checkpoint = TaskCheckpoint(root, task)
    usage = dict(checkpoint.data.get("loop", {}).get("accumulated_usage", {}))
    set_outcome(usage, status, reason)
    text = f"Задача {task['id']}: {status} ({reason}). Автоматический повтор отключён."
    checkpoint.begin()
    checkpoint.finish(text, usage, checkpoint.data.get("loop", {}).get("llm_trace", {}), [])
    checkpoint.data["delivery_settled"] = True
    checkpoint.write()
    atomic_json(pathlib.Path(root) / "task_results" / (str(task["id"]) + ".json"),
                {"task_id": task["id"], "status": status, "reason": reason, "result": text,
                 "ts": utc_now_iso()})


def settle_execution(root: pathlib.Path, task_id: str) -> None:
    path = pathlib.Path(root) / "state" / "task_execution" / (task_id + ".json")
    if path.exists():
        checkpoint = TaskCheckpoint(root, {"id": task_id})
        if checkpoint.data.get("phase") == "finished":
            checkpoint.data["delivery_settled"] = True
            checkpoint.write()
