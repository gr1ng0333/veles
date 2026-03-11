from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ouroboros.tools.browser_auth_flow import normalize_site_profile
from ouroboros.tools.registry import ToolContext


OWNER_ONLY_SCOPE = "owner_only"
SESSION_STATUS_FRESH = "fresh"
SESSION_STATUS_STALE = "stale"
SESSION_STATUS_UNKNOWN = "unknown"


@dataclass(frozen=True)
class SessionRegistryKey:
    site_key: str
    account_key: str

    @property
    def compound(self) -> str:
        return f"{self.site_key}::{self.account_key}"



def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()



def _normalize_key_part(value: str, fallback: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in (value or "").strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned or fallback



def build_session_registry_key(*, site_profile: Optional[Dict[str, Any]], site_url: str = "", account_label: str) -> SessionRegistryKey:
    profile = normalize_site_profile(site_profile)
    domain = (profile.get("domain") or "").strip()
    site_name = (profile.get("site_name") or "").strip()
    site_source = domain or site_name or site_url or "site"
    return SessionRegistryKey(
        site_key=_normalize_key_part(site_source, "site"),
        account_key=_normalize_key_part(account_label, "account"),
    )



def _registry_path(ctx: ToolContext) -> str:
    return str(ctx.drive_path("state/browser_session_registry.json"))



def load_browser_session_registry(ctx: ToolContext) -> Dict[str, Any]:
    path = ctx.drive_path("state/browser_session_registry.json")
    if not path.exists():
        return {"sessions": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"sessions": {}}
    if not isinstance(data, dict):
        return {"sessions": {}}
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        data["sessions"] = {}
    return data



def save_browser_session_registry(ctx: ToolContext, registry: Dict[str, Any]) -> None:
    path = ctx.drive_path("state/browser_session_registry.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")



def build_persisted_session_record(
    *,
    key: SessionRegistryKey,
    storage_state: Dict[str, Any],
    cookies_count: int,
    origins_count: int,
    current_url: str,
    site_profile: Optional[Dict[str, Any]],
    account_label: str,
    owner_authorized: bool,
    account_scope: str,
    session_status: str = SESSION_STATUS_FRESH,
) -> Dict[str, Any]:
    profile = normalize_site_profile(site_profile)
    return {
        "site_key": key.site_key,
        "account_key": key.account_key,
        "site_profile": profile,
        "account_label": account_label,
        "owner_authorized": bool(owner_authorized),
        "account_scope": account_scope or OWNER_ONLY_SCOPE,
        "session_status": session_status,
        "last_saved_at": _utc_now(),
        "last_checked_at": "",
        "last_reused_at": "",
        "current_url": current_url,
        "cookies_count": int(cookies_count),
        "origins_count": int(origins_count),
        "storage_state": storage_state,
    }



def get_persisted_session(ctx: ToolContext, *, key: SessionRegistryKey) -> Optional[Dict[str, Any]]:
    registry = load_browser_session_registry(ctx)
    record = registry.get("sessions", {}).get(key.compound)
    return record if isinstance(record, dict) else None



def upsert_persisted_session(ctx: ToolContext, *, key: SessionRegistryKey, record: Dict[str, Any]) -> Dict[str, Any]:
    registry = load_browser_session_registry(ctx)
    sessions = registry.setdefault("sessions", {})
    sessions[key.compound] = record
    save_browser_session_registry(ctx, registry)
    return record



def mark_persisted_session_checked(
    ctx: ToolContext,
    *,
    key: SessionRegistryKey,
    alive: bool,
    check_details: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    record = get_persisted_session(ctx, key=key)
    if not record:
        return None
    record["session_status"] = SESSION_STATUS_FRESH if alive else SESSION_STATUS_STALE
    record["last_checked_at"] = _utc_now()
    record["check_details"] = check_details or {}
    if alive:
        record["last_reused_at"] = _utc_now()
    upsert_persisted_session(ctx, key=key, record=record)
    return record



def validate_owner_authorized_scope(*, owner_authorized: bool, account_scope: str) -> Optional[str]:
    if not owner_authorized:
        return "persisted browser sessions require owner_authorized=true"
    if (account_scope or OWNER_ONLY_SCOPE) != OWNER_ONLY_SCOPE:
        return "only owner_only account_scope is allowed for persisted browser sessions"
    return None
