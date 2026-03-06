from __future__ import annotations

import datetime
import json
import logging
import os
from typing import Any, Dict, Optional

from ouroboros.llm import LLMClient

log = logging.getLogger(__name__)

_ALLOWED_VERDICTS = {
    "no_restart",
    "soft_restart_recommended",
    "hard_restart_recommended",
    "escalate_to_main_model",
}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _recent_restart_count(state: Dict[str, Any]) -> int:
    history = state.get("recent_restart_history")
    if isinstance(history, list):
        return len(history)
    return _safe_int(state.get("recent_restart_count"), 0)


def _build_restart_signals(
    *,
    reason: str,
    state: Dict[str, Any],
    pending_count: int,
    running_count: int,
) -> Dict[str, Any]:
    interrupted_work = bool(state.get("resume_needed")) or _safe_int(state.get("resume_snapshot_pending_count"), 0) > 0 or _safe_int(state.get("resume_snapshot_running_count"), 0) > 0
    no_progress = _safe_int(state.get("no_commit_streak"), 0) > 0 or _safe_int(state.get("evolution_consecutive_failures"), 0) > 0
    queue_backlog = int(pending_count) + int(running_count)
    return {
        "reason": str(reason or "").strip(),
        "interrupted_work": interrupted_work,
        "pending_count": int(pending_count),
        "running_count": int(running_count),
        "queue_backlog": queue_backlog,
        "resume_snapshot_pending_count": _safe_int(state.get("resume_snapshot_pending_count"), 0),
        "resume_snapshot_running_count": _safe_int(state.get("resume_snapshot_running_count"), 0),
        "recent_restart_count": _recent_restart_count(state),
        "evolution_mode_enabled": bool(state.get("evolution_mode_enabled")),
        "evolution_consecutive_failures": _safe_int(state.get("evolution_consecutive_failures"), 0),
        "no_commit_streak": _safe_int(state.get("no_commit_streak"), 0),
        "no_progress": no_progress,
        "suppress_auto_resume_until_owner_message": bool(state.get("suppress_auto_resume_until_owner_message")),
        "launcher_session_id": str(state.get("launcher_session_id") or ""),
        "last_owner_message_at": str(state.get("last_owner_message_at") or ""),
        "last_evolution_task_at": str(state.get("last_evolution_task_at") or ""),
    }


def build_restart_advisor_payload(
    *,
    reason: str,
    state: Dict[str, Any],
    pending_count: int,
    running_count: int,
) -> Dict[str, Any]:
    signals = _build_restart_signals(
        reason=reason,
        state=state,
        pending_count=pending_count,
        running_count=running_count,
    )
    return {
        "reason": signals["reason"],
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "contract_version": 2,
        "signals": signals,
    }


def evaluate_restart_policy(
    *,
    reason: str,
    state: Dict[str, Any],
    pending_count: int,
    running_count: int,
    advisor_result: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    signals = _build_restart_signals(
        reason=reason,
        state=state,
        pending_count=pending_count,
        running_count=running_count,
    )
    requested_verdict = str((advisor_result or {}).get("verdict") or "escalate_to_main_model").strip().lower()
    confidence = advisor_result.get("confidence") if isinstance(advisor_result, dict) else 0.0
    try:
        confidence = float(confidence)
    except Exception:
        confidence = 0.0

    hard_restart_allowed = signals["interrupted_work"] or signals["recent_restart_count"] >= 2 or (signals["running_count"] > 0 and signals["no_progress"])
    blocked_by_active_work = signals["running_count"] > 0 and not signals["interrupted_work"] and not signals["no_progress"]

    if requested_verdict == "no_restart":
        supervisor_action = "skip_restart"
        policy = "advisor_veto"
    elif blocked_by_active_work:
        supervisor_action = "skip_restart"
        policy = "active_work_guard"
    elif requested_verdict == "hard_restart_recommended":
        if hard_restart_allowed:
            supervisor_action = "restart_now"
            policy = "hard_restart_guard_pass"
        else:
            supervisor_action = "restart_now"
            policy = "downgraded_to_soft_restart"
    elif requested_verdict in {"soft_restart_recommended", "escalate_to_main_model"}:
        supervisor_action = "restart_now"
        policy = requested_verdict
    else:
        supervisor_action = "restart_now"
        policy = "fail_open_unknown_verdict"

    return {
        "requested_verdict": requested_verdict,
        "advisor_confidence": max(0.0, min(1.0, confidence)),
        "supervisor_action": supervisor_action,
        "policy": policy,
        "hard_restart_allowed": hard_restart_allowed,
        "blocked_by_active_work": blocked_by_active_work,
        "signals": signals,
    }


def _extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    txt = str(raw or "").strip()
    if not txt:
        return None
    try:
        obj = json.loads(txt)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    start = txt.find("{")
    end = txt.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(txt[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _normalize_verdict(data: Dict[str, Any], *, model: str) -> Dict[str, Any]:
    verdict = str(data.get("verdict") or "escalate_to_main_model").strip().lower()
    if verdict not in _ALLOWED_VERDICTS:
        verdict = "escalate_to_main_model"

    try:
        confidence = float(data.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    signals = data.get("signals") if isinstance(data.get("signals"), list) else []
    risks = data.get("risks") if isinstance(data.get("risks"), list) else []

    return {
        "ok": True,
        "model": model,
        "verdict": verdict,
        "confidence": confidence,
        "summary": str(data.get("summary") or "").strip(),
        "signals": [str(x)[:200] for x in signals[:8]],
        "risks": [str(x)[:200] for x in risks[:8]],
        "raw": data,
    }


def advise_restart(
    *,
    reason: str,
    state: Dict[str, Any],
    pending_count: int,
    running_count: int,
) -> Dict[str, Any]:
    payload = build_restart_advisor_payload(
        reason=reason,
        state=state,
        pending_count=pending_count,
        running_count=running_count,
    )
    model = os.environ.get("OUROBOROS_RESTART_ADVISOR_MODEL", "codex/gpt-5.4").strip() or "codex/gpt-5.4"
    prompt = (
        "You are a narrow restart advisor for a self-modifying agent supervisor. "
        "Decide only whether a restart request looks justified from the provided restart signals. "
        "Do not invent missing facts and do not assume authority over the supervisor. "
        "Return JSON only with fields: verdict, confidence, summary, signals, risks. "
        "Allowed verdict values: no_restart, soft_restart_recommended, hard_restart_recommended, escalate_to_main_model.\n\n"
        f"Input:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )

    try:
        client = LLMClient()
        resp_msg, usage = client.chat(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            reasoning_effort="low",
            max_tokens=300,
            tools=None,
        )
        data = _extract_json_object(resp_msg.get("content") or "")
        if not isinstance(data, dict):
            raise ValueError("restart advisor returned non-JSON response")
        result = _normalize_verdict(data, model=model)
        result["usage"] = usage
        result["payload"] = payload
        return result
    except Exception as e:
        log.warning("Restart advisor failed; supervisor will fail-open", exc_info=True)
        return {
            "ok": False,
            "model": model,
            "verdict": "escalate_to_main_model",
            "confidence": 0.0,
            "summary": f"advisor_unavailable: {type(e).__name__}",
            "signals": [],
            "risks": [repr(e)],
            "payload": payload,
        }
