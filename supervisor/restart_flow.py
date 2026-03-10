from __future__ import annotations

import datetime
import os
import sys
import uuid
from typing import Any, Dict


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
