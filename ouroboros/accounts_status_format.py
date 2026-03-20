from __future__ import annotations

from typing import Any, Dict, List


_REASON_LABELS = {
    "usage_limit_reached": "usage_limit_reached",
    "quota_exhausted": "usage_limit_reached",
    "auth_failure": "auth_failure",
    "unauthorized": "auth_failure",
    "forbidden": "auth_failure",
    "temporary_429": "temporary 429",
    "rate_limited": "temporary 429",
    "rate_limit": "temporary 429",
}


def _fmt_pct(value: Any) -> str:
    try:
        return f"{int(float(value))}%"
    except Exception:
        return "—"


def _classify_display_reason(status: Dict[str, Any]) -> str:
    reason = str(status.get("last_error_reason") or "").strip().lower()
    category = str(status.get("last_error_category") or "").strip().lower()
    code = status.get("last_error_status_code")

    if reason in ("usage_limit_reached", "quota_exhausted"):
        return "usage_limit_reached"
    if category == "auth" or code in (401, 403) or reason in ("auth_failure", "unauthorized", "forbidden"):
        return "auth_failure"
    if code == 429 or category == "rate_limit" or reason in ("temporary_429", "rate_limited", "rate_limit"):
        return "temporary 429"
    if reason:
        return _REASON_LABELS.get(reason, reason)
    return ""


def _quota_hints(status: Dict[str, Any]) -> str:
    q5 = status.get("quota_5h_used_pct")
    q7 = status.get("quota_7d_used_pct")
    plan = str(status.get("quota_plan") or "").strip()
    hints: List[str] = []
    if q5 is not None:
        hints.append(f"5h used {_fmt_pct(q5)}")
    if q7 is not None:
        hints.append(f"7d used {_fmt_pct(q7)}")
    if plan:
        hints.append(f"plan {plan}")
    return " | ".join(hints)


def _quota_free_str(status: Dict[str, Any]) -> str:
    q5 = status.get("quota_5h_used_pct")
    q7 = status.get("quota_7d_used_pct")
    if q5 is None or q7 is None:
        return "5h: — | 7d: —"
    try:
        free_5h = max(0, 100 - int(float(q5)))
    except Exception:
        free_5h = None
    try:
        free_7d = max(0, 100 - int(float(q7)))
    except Exception:
        free_7d = None
    if free_5h is None or free_7d is None:
        return "5h: — | 7d: —"
    return f"5h: {free_5h}% free | 7d: {free_7d}% free"


def _status_line(status: Dict[str, Any]) -> str:
    idx = status["index"]
    if status.get("dead"):
        return f"💀 #{idx}: dead"
    if status.get("in_cooldown"):
        mins = int((status.get("cooldown_remaining") or 0) // 60)
        icon = "⏳"
        prefix = f"cooldown {mins}m"
    elif status.get("has_access"):
        icon = "✅"
        prefix = "ok"
    else:
        icon = "⚠️"
        prefix = "no token"
    active_marker = " ← active" if status.get("active") else ""
    return f"{icon} #{idx}: {prefix} | {_quota_free_str(status)}{active_marker}"


def _diag_line(status: Dict[str, Any]) -> str:
    reason = _classify_display_reason(status)
    category = str(status.get("last_error_category") or "").strip()
    code = status.get("last_error_status_code")
    pieces: List[str] = []
    if reason:
        pieces.append(f"reason={reason}")
    elif status.get("has_access"):
        pieces.append("reason=ok")
    if category:
        pieces.append(f"category={category}")
    if code:
        pieces.append(f"http={code}")
    quota = _quota_hints(status)
    if quota:
        pieces.append(f"quota hints: {quota}")
    return "    ↳ " + (" | ".join(pieces) if pieces else "diagnostics: none")


def format_codex_accounts_status(statuses: List[Dict[str, Any]]) -> str:
    lines = [f"📊 Codex Accounts: {len(statuses)} шт.", ""]
    sum_5h = 0.0
    sum_7d = 0.0
    quota_count = 0

    for status in statuses:
        lines.append(_status_line(status))
        lines.append(_diag_line(status))
        q5 = status.get("quota_5h_used_pct")
        q7 = status.get("quota_7d_used_pct")
        if q5 is not None and q7 is not None:
            try:
                sum_5h += max(0, 100 - int(float(q5)))
                sum_7d += max(0, 100 - int(float(q7)))
                quota_count += 1
            except Exception:
                pass

    if quota_count > 0:
        avg_5h = sum_5h / quota_count
        avg_7d = sum_7d / quota_count
        lines.append("")
        lines.append(f"Σ Средняя квота: 5h: {avg_5h:.0f}% free | 7d: {avg_7d:.0f}% free")

    return "\n".join(lines)
