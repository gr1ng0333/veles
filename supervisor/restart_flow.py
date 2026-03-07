from __future__ import annotations

import datetime
import os
import sys
import uuid
from typing import Any, Dict


def handle_restart_request(evt: Dict[str, Any], ctx: Any) -> None:
    st = ctx.load_state()
    reason = str(evt.get("reason") or "").strip()

    advisor_result = None
    policy_decision = None
    try:
        from supervisor.restart_advisor import advise_restart, evaluate_restart_policy
        advisor_result = advise_restart(
            reason=reason,
            state=st or {},
            pending_count=len(ctx.PENDING),
            running_count=len(ctx.RUNNING),
        )
        policy_decision = evaluate_restart_policy(
            reason=reason,
            state=st or {},
            pending_count=len(ctx.PENDING),
            running_count=len(ctx.RUNNING),
            advisor_result=advisor_result,
        )
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "restart_advisor_verdict",
                "reason": reason,
                "advisor": advisor_result,
                "policy": policy_decision,
            },
        )
        if policy_decision.get("requested_verdict") != policy_decision.get("supervisor_action"):
            ctx.append_jsonl(
                ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "restart_advisor_policy_decision",
                    "reason": reason,
                    "requested_verdict": policy_decision.get("requested_verdict"),
                    "supervisor_action": policy_decision.get("supervisor_action"),
                    "policy": policy_decision.get("policy"),
                    "signals": policy_decision.get("signals") or {},
                },
            )
    except Exception as e:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "restart_advisor_error",
                "reason": reason,
                "error": repr(e),
            },
        )

    if st.get("owner_chat_id"):
        verdict_suffix = ""
        if isinstance(advisor_result, dict) and advisor_result.get("verdict"):
            verdict_suffix = f" [advisor: {advisor_result.get('verdict')}]"
        ctx.send_with_budget(
            int(st["owner_chat_id"]),
            f"♻️ Restart requested by agent: {reason or 'unspecified'}{verdict_suffix}",
        )

    if isinstance(policy_decision, dict) and policy_decision.get("supervisor_action") == "skip_restart":
        if st.get("owner_chat_id"):
            ctx.send_with_budget(
                int(st["owner_chat_id"]),
                f"⏸️ Restart suppressed by policy: {policy_decision.get('policy')}",
            )
        return

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
    ctx.save_state(st2)
    ctx.persist_queue_snapshot(reason="pre_restart_exit")
    launcher = os.path.join(os.getcwd(), "colab_launcher.py")
    os.execv(sys.executable, [sys.executable, launcher])
