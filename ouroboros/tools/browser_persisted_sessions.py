from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ouroboros.tools.browser import _ensure_browser, _replace_browser_context, _session_snapshot
from ouroboros.tools.browser_runtime import _check_session_alive_via_protected_url
from ouroboros.tools.browser_session_registry import (
    OWNER_ONLY_SCOPE,
    build_persisted_session_record,
    build_session_registry_key,
    get_persisted_session,
    mark_persisted_session_checked,
    upsert_persisted_session,
    validate_owner_authorized_scope,
)
from ouroboros.tools.registry import ToolContext, ToolEntry



def _browser_persist_session(
    ctx: ToolContext,
    account_label: str,
    site_profile: Optional[Dict[str, Any]] = None,
    site_url: str = "",
    owner_authorized: bool = False,
    account_scope: str = OWNER_ONLY_SCOPE,
) -> str:
    account_label = (account_label or "").strip()
    if not account_label:
        return "Error: account_label is required"
    guard_error = validate_owner_authorized_scope(owner_authorized=owner_authorized, account_scope=account_scope)
    if guard_error:
        return f"Error: {guard_error}"

    page = _ensure_browser(ctx)
    snapshot = _session_snapshot(ctx.browser_state.context)
    key = build_session_registry_key(site_profile=site_profile, site_url=site_url, account_label=account_label)
    record = build_persisted_session_record(
        key=key,
        storage_state=snapshot["storage_state"],
        cookies_count=snapshot["cookies_count"],
        origins_count=snapshot["origins_count"],
        current_url=page.url,
        site_profile=site_profile,
        account_label=account_label,
        owner_authorized=owner_authorized,
        account_scope=account_scope,
    )
    upsert_persisted_session(ctx, key=key, record=record)
    result = {
        "saved": True,
        "registry_key": key.compound,
        "account_label": account_label,
        "site_key": key.site_key,
        "session_status": record.get("session_status"),
        "cookies_count": snapshot["cookies_count"],
        "origins_count": snapshot["origins_count"],
        "current_url": page.url,
        "registry_path": str(ctx.drive_path("state/browser_session_registry.json")),
    }
    return json.dumps(result, ensure_ascii=False)



def _browser_get_persisted_session(
    ctx: ToolContext,
    account_label: str,
    site_profile: Optional[Dict[str, Any]] = None,
    site_url: str = "",
) -> str:
    account_label = (account_label or "").strip()
    if not account_label:
        return "Error: account_label is required"
    key = build_session_registry_key(site_profile=site_profile, site_url=site_url, account_label=account_label)
    record = get_persisted_session(ctx, key=key)
    if not record:
        return json.dumps({"found": False, "registry_key": key.compound}, ensure_ascii=False)
    result = dict(record)
    result.pop("storage_state", None)
    result["found"] = True
    result["registry_key"] = key.compound
    return json.dumps(result, ensure_ascii=False)



def _browser_restore_persisted_session(
    ctx: ToolContext,
    account_label: str,
    site_profile: Optional[Dict[str, Any]] = None,
    site_url: str = "",
    url: str = "",
    protected_url: str = "",
) -> str:
    account_label = (account_label or "").strip()
    if not account_label:
        return "Error: account_label is required"
    key = build_session_registry_key(site_profile=site_profile, site_url=site_url, account_label=account_label)
    record = get_persisted_session(ctx, key=key)
    if not record:
        return json.dumps({"restored": False, "found": False, "registry_key": key.compound}, ensure_ascii=False)

    storage_state = record.get("storage_state") or {}
    _ensure_browser(ctx)
    page = _replace_browser_context(ctx, storage_state=storage_state)
    target_url = (url or "").strip()
    if target_url:
        page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
    probe = _check_session_alive_via_protected_url(ctx, (protected_url or "").strip())
    alive = True if not probe.get("checked") else bool(probe.get("alive"))
    updated = mark_persisted_session_checked(ctx, key=key, alive=alive, check_details=probe) or record
    result = {
        "restored": True,
        "found": True,
        "registry_key": key.compound,
        "account_label": account_label,
        "current_url": page.url,
        "navigated": bool(target_url),
        "session_status": updated.get("session_status"),
        "probe": probe,
        "should_relogin": bool(probe.get("checked")) and not bool(probe.get("alive")),
    }
    return json.dumps(result, ensure_ascii=False)



def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="browser_persist_session",
            schema={
                "name": "browser_persist_session",
                "description": "Persist the current browser storage state to the owner-authorized site session registry for reuse across tasks and restarts.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "account_label": {"type": "string", "description": "Owner account label for this site session"},
                        "site_profile": {"type": "object", "description": "Structured site profile with domain/site_name/auth hints"},
                        "site_url": {"type": "string", "description": "Optional site URL fallback for deriving the registry key"},
                        "owner_authorized": {"type": "boolean", "description": "Must be true; persisted sessions are restricted to owner-authorized accounts"},
                        "account_scope": {"type": "string", "description": "Allowed value: owner_only"},
                    },
                    "required": ["account_label"],
                },
            },
            handler=_browser_persist_session,
            timeout_sec=60,
        ),
        ToolEntry(
            name="browser_restore_persisted_session",
            schema={
                "name": "browser_restore_persisted_session",
                "description": "Restore a persisted owner-authorized site session, optionally probe whether it is still alive, and reopen a URL.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "account_label": {"type": "string", "description": "Owner account label for this site session"},
                        "site_profile": {"type": "object", "description": "Structured site profile with domain/site_name/auth hints"},
                        "site_url": {"type": "string", "description": "Optional site URL fallback for deriving the registry key"},
                        "url": {"type": "string", "description": "Optional URL to open after restoring session"},
                        "protected_url": {"type": "string", "description": "Optional authenticated URL for session-alive probing"},
                    },
                    "required": ["account_label"],
                },
            },
            handler=_browser_restore_persisted_session,
            timeout_sec=60,
        ),
        ToolEntry(
            name="browser_get_persisted_session",
            schema={
                "name": "browser_get_persisted_session",
                "description": "Read persisted owner-authorized site session metadata from the browser session registry without touching the live browser.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "account_label": {"type": "string", "description": "Owner account label for this site session"},
                        "site_profile": {"type": "object", "description": "Structured site profile with domain/site_name/auth hints"},
                        "site_url": {"type": "string", "description": "Optional site URL fallback for deriving the registry key"},
                    },
                    "required": ["account_label"],
                },
            },
            handler=_browser_get_persisted_session,
            timeout_sec=60,
        ),
    ]
