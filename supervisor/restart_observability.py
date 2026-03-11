from __future__ import annotations

import datetime
from typing import Any, Dict, Optional, Tuple


def arm_manual_terminal_restart_handoff(
    state: Dict[str, Any],
    previous_pid: Optional[int],
    requested_at: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Infer a manual terminal restart from PID handoff when no explicit restart flag exists.

    This is intentionally conservative: it only arms the post-restart notification
    when a previous supervisor PID existed, the owner chat is already known, and
    no explicit restart handoff is pending.
    """
    st = dict(state or {})
    if not previous_pid or int(previous_pid) <= 0:
        return st, False
    if bool(st.get("restart_notify_pending")):
        return st, False
    if not st.get("owner_chat_id"):
        return st, False

    ts = (requested_at or '').strip() or datetime.datetime.now(datetime.timezone.utc).isoformat()
    st["restart_notify_pending"] = True
    st["restart_notify_reason"] = "manual_terminal_restart"
    st["restart_notify_requested_at"] = ts
    st["restart_notify_source"] = "manual_terminal_restart"
    return st, True
