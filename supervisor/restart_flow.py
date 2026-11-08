from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import sys
import uuid
from typing import Any, Dict

from ouroboros.task_execution import atomic_json


def _acknowledge_restart_request(root: pathlib.Path, evt: Dict[str, Any]) -> None:
    """Consume the received request in the finished checkpoint before execv."""
    ts = evt.get("ts")
    if not isinstance(ts, str) or not ts:
        return
    task_id = evt.get("task_id")
    directory = root / "state" / "task_execution"
    if task_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(task_id)):
        return
    # Old workers sent no task ID, including direct chats outside the queue.
    candidates = [directory / f"{task_id}.json"] if task_id else directory.glob("*.json")
    matches = []
    for path in candidates:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if (row.get("schema_version") != 1 or row.get("task", {}).get("id") != path.stem
                    or row.get("phase") != "finished" or row.get("delivery_settled")):
                continue
            pending = row.get("pending_events", [])
            if not isinstance(pending, list) or not all(isinstance(event, dict) for event in pending):
                continue
            for index, event in enumerate(pending):
                if (event.get("type") == "restart_request" and event.get("ts") == ts
                        and event.get("reason") == evt.get("reason")):
                    matches.append((path, row, index))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    if len(matches) == 1:
        path, row, index = matches[0]
        del row["pending_events"][index]
        atomic_json(path, row)


def handle_restart_request(evt: Dict[str, Any], ctx: Any) -> None:
    st = ctx.load_state()
    reason = str(evt.get("reason") or "").strip()

    if st.get("owner_chat_id"):
        ctx.send_with_budget(
            int(st["owner_chat_id"]),
            f"♻️ Restart requested by agent: {reason or 'unspecified'}",
        )

    ctx.append_jsonl(
        ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
        {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "restart_flow_simple",
            "task_id": evt.get("task_id"),
            "reason": reason,
            "pending_count": len(ctx.PENDING),
            "running_count": len(ctx.RUNNING),
        },
    )

    ok, msg = ctx.safe_restart(
        reason="agent_restart_request", unsynced_policy="rescue_and_reset"
    )
    if not ok:
        if st.get("owner_chat_id"):
            ctx.send_with_budget(int(st["owner_chat_id"]), f"⚠️ Restart skipped: {msg}")
        return
    _acknowledge_restart_request(ctx.DRIVE_ROOT, evt)
    ctx.kill_workers()
    st2 = ctx.load_state()
    st2["session_id"] = uuid.uuid4().hex
    st2["tg_offset"] = int(st2.get("tg_offset") or st.get("tg_offset") or 0)
    st2["restart_notify_pending"] = True
    st2["restart_notify_reason"] = reason or "unspecified"
    st2["restart_notify_requested_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    st2["restart_notify_source"] = "agent_restart_request"
    st2["resume_needed"] = False
    st2["resume_reason"] = ""
    st2["resume_snapshot_pending_count"] = len(ctx.PENDING)
    st2["resume_snapshot_running_count"] = len(ctx.RUNNING)
    ctx.save_state(st2)
    ctx.persist_queue_snapshot(reason="pre_restart_exit")
    launcher = os.path.join(os.getcwd(), "colab_launcher.py")
    os.execv(sys.executable, [sys.executable, launcher])
