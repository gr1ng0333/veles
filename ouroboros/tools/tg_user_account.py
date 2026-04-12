"""tg_user_account — Veles's personal Telegram user account (@gpldgg / @veles_agi).

FIREWALL POLICY (mandatory, do not remove):
    - This module NEVER auto-listens, polls, or forwards incoming messages to LLM.
    - Incoming messages reach the LLM only when the agent explicitly calls tg_inbox_read().
    - There is NO event handler, NO background thread, NO supervisor trigger for this account.
    - This is the ONLY safe way to operate a user account inside an LLM-driven agent:
      on-demand reads with explicit intent, never automatic injection.

Why this matters:
    If incoming messages were auto-forwarded to the LLM context, any person who knows
    the account exists could attempt prompt injection by sending a crafted message.
    On-demand polling with explicit tool calls is the firewall against this attack surface.

Tools:
    tg_inbox_read(limit?, peer?)       — read recent incoming messages (on-demand)
    tg_send_as_me(to, text)            — send a message from the user account
    tg_user_account_status()           — check account status, session health

Requirements:
    TG_API_ID, TG_API_HASH, TG_PHONE env vars + session string at
    /opt/veles-data/telegram/veles_session.string

    pip install telethon (already in venv)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_SESSION_FILE = Path("/opt/veles-data/telegram/veles_session.string")
_DEFAULT_TIMEOUT = 30


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ensure_event_loop() -> None:
    """Ensure current thread has an event loop (Python 3.10+ fix for ThreadPoolExecutor)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)


def _get_credentials() -> tuple[str, str, str]:
    """Return (api_id, api_hash, session_string). Raises if missing."""
    api_id = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    if not api_id or not api_hash:
        raise RuntimeError("TG_API_ID and TG_API_HASH env vars required")
    if not _SESSION_FILE.exists():
        raise RuntimeError(f"Session file not found: {_SESSION_FILE}")
    session_string = _SESSION_FILE.read_text(encoding="utf-8").strip()
    if not session_string:
        raise RuntimeError("Session file is empty — re-authenticate first")
    return api_id, api_hash, session_string


def _make_client(api_id: str, api_hash: str, session_string: str):
    """Create a Telethon TelegramClient with StringSession (sync-friendly)."""
    try:
        from telethon.sync import TelegramClient  # type: ignore
        from telethon.sessions import StringSession  # type: ignore
    except ImportError as exc:
        raise RuntimeError("telethon not installed — run: pip install telethon") from exc

    return TelegramClient(
        StringSession(session_string),
        int(api_id),
        api_hash,
    )


def _format_message(msg: Any, me_id: int) -> Dict[str, Any]:
    """Convert Telethon Message to a safe, serializable dict."""
    peer_id: Optional[int] = None
    peer_name: Optional[str] = None

    try:
        sender = msg.sender
        if sender is not None:
            peer_id = getattr(sender, "id", None)
            first = getattr(sender, "first_name", "") or ""
            last = getattr(sender, "last_name", "") or ""
            username = getattr(sender, "username", None)
            peer_name = (f"{first} {last}".strip() or username or str(peer_id))
    except Exception:
        pass

    direction = "incoming" if (peer_id and peer_id != me_id) else "outgoing"
    date_str = msg.date.isoformat() if msg.date else ""

    return {
        "id": msg.id,
        "date": date_str,
        "direction": direction,
        "from_id": peer_id,
        "from_name": peer_name,
        "text": msg.text or "",
        "out": msg.out,
    }


# ── Tool: tg_inbox_read ───────────────────────────────────────────────────────

def _tg_inbox_read(
    ctx: ToolContext,
    limit: int = 10,
    peer: Optional[str] = None,
) -> str:
    """Read recent incoming messages from the Veles user account (@gpldgg).

    FIREWALL NOTE: This tool is the ONLY way incoming messages reach the agent.
    It must be called explicitly — never triggered automatically.

    Args:
        limit: max messages to return (1–50, default 10)
        peer:  optional username/phone/id to read only messages from that peer.
               If None, returns messages from the most active recent dialogs.
    """
    # Enforce limit bounds
    limit = max(1, min(limit, 50))

    try:
        api_id, api_hash, session_string = _get_credentials()
    except RuntimeError as exc:
        return json.dumps({"ok": False, "error": str(exc)})

    try:
        _ensure_event_loop()
        client = _make_client(api_id, api_hash, session_string)
        with client:
            me = client.get_me()
            me_id: int = me.id

            messages: List[Dict[str, Any]] = []

            if peer:
                # Read messages from specific peer
                entity = client.get_entity(peer)
                for msg in client.iter_messages(entity, limit=limit):
                    messages.append(_format_message(msg, me_id))
            else:
                # Read from top recent dialogs — but only incoming messages
                for dialog in client.iter_dialogs(limit=20):
                    if len(messages) >= limit:
                        break
                    # Skip channels/groups for personal inbox — only private chats
                    if not dialog.is_user:
                        continue
                    for msg in client.iter_messages(dialog.entity, limit=5):
                        if len(messages) >= limit:
                            break
                        formatted = _format_message(msg, me_id)
                        if formatted["direction"] == "incoming":
                            messages.append(formatted)

            # Sort by date descending (newest first)
            messages.sort(key=lambda m: m.get("date", ""), reverse=True)

            return json.dumps({
                "ok": True,
                "account": f"@{me.username}" if me.username else str(me_id),
                "messages_count": len(messages),
                "peer_filter": peer,
                "messages": messages,
                "firewall": "on-demand-only",  # reminder that this is not automatic
            }, ensure_ascii=False)

    except Exception as exc:
        log.exception("tg_inbox_read error")
        return json.dumps({"ok": False, "error": str(exc)})


# ── Tool: tg_send_as_me ───────────────────────────────────────────────────────

def _tg_send_as_me(
    ctx: ToolContext,
    to: str,
    text: str,
) -> str:
    """Send a message from the Veles user account (@gpldgg / @veles_agi).

    Args:
        to:   recipient username (e.g. '@someone'), phone '+79...', or numeric user_id
        text: message text (1–4096 chars)
    """
    if not text or not text.strip():
        return json.dumps({"ok": False, "error": "text must not be empty"})
    if len(text) > 4096:
        return json.dumps({"ok": False, "error": f"text too long ({len(text)} chars, max 4096)"})
    if not to or not to.strip():
        return json.dumps({"ok": False, "error": "recipient 'to' must not be empty"})

    try:
        api_id, api_hash, session_string = _get_credentials()
    except RuntimeError as exc:
        return json.dumps({"ok": False, "error": str(exc)})

    try:
        _ensure_event_loop()
        client = _make_client(api_id, api_hash, session_string)
        with client:
            me = client.get_me()
            entity = client.get_entity(to.strip())
            msg = client.send_message(entity, text.strip())

            return json.dumps({
                "ok": True,
                "from": f"@{me.username}" if me.username else str(me.id),
                "to": to.strip(),
                "message_id": msg.id,
                "date": msg.date.isoformat() if msg.date else "",
                "text_preview": text[:80] + ("..." if len(text) > 80 else ""),
            }, ensure_ascii=False)

    except Exception as exc:
        log.exception("tg_send_as_me error (to=%s)", to)
        return json.dumps({"ok": False, "error": str(exc)})


# ── Tool: tg_user_account_status ─────────────────────────────────────────────

def _tg_user_account_status(ctx: ToolContext) -> str:
    """Check health of the Veles user account session.

    Returns account info (username, id, name) and session validity.
    Does NOT read any messages.
    """
    try:
        api_id, api_hash, session_string = _get_credentials()
    except RuntimeError as exc:
        return json.dumps({"ok": False, "error": str(exc)})

    try:
        _ensure_event_loop()
        client = _make_client(api_id, api_hash, session_string)
        with client:
            me = client.get_me()
            return json.dumps({
                "ok": True,
                "session_valid": True,
                "user_id": me.id,
                "username": me.username,
                "first_name": me.first_name,
                "last_name": me.last_name,
                "phone": me.phone,
                "is_bot": me.bot,
                "session_file": str(_SESSION_FILE),
                "firewall_policy": "no-auto-listener",
            }, ensure_ascii=False)

    except Exception as exc:
        log.exception("tg_user_account_status error")
        return json.dumps({"ok": False, "session_valid": False, "error": str(exc)})


# ── Tool registry ─────────────────────────────────────────────────────────────

_INBOX_SCHEMA: Dict[str, Any] = {
    "name": "tg_inbox_read",
    "description": (
        "Read recent incoming messages from Veles's personal Telegram account (@gpldgg / @veles_agi). "
        "FIREWALL: this tool is the ONLY entry point for user account messages into the LLM. "
        "It must always be called explicitly — never triggered automatically. "
        "Use when the owner asks to check what someone wrote to the account."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max messages to return (1–50, default 10)",
                "default": 10,
            },
            "peer": {
                "type": "string",
                "description": (
                    "Optional: username (@someone), phone (+79...), or user_id to filter by. "
                    "If omitted, returns recent incoming from private chats."
                ),
            },
        },
    },
}

_SEND_SCHEMA: Dict[str, Any] = {
    "name": "tg_send_as_me",
    "description": (
        "Send a message from the Veles personal Telegram account (@gpldgg / @veles_agi). "
        "Use this when you want to write to someone as Veles (not as the bot). "
        "The message appears as coming from the user account directly."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Recipient: username (@someone), phone (+79...), or numeric user_id",
            },
            "text": {
                "type": "string",
                "description": "Message text (1–4096 chars)",
            },
        },
        "required": ["to", "text"],
    },
}

_STATUS_SCHEMA: Dict[str, Any] = {
    "name": "tg_user_account_status",
    "description": (
        "Check health of the Veles personal Telegram user account session. "
        "Returns account info (username, id, name) and whether the session is valid. "
        "Does NOT read any messages."
    ),
    "parameters": {"type": "object", "properties": {}},
}


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="tg_inbox_read",
            schema=_INBOX_SCHEMA,
            handler=lambda ctx, **kw: _tg_inbox_read(ctx, **kw),
        ),
        ToolEntry(
            name="tg_send_as_me",
            schema=_SEND_SCHEMA,
            handler=lambda ctx, **kw: _tg_send_as_me(ctx, **kw),
        ),
        ToolEntry(
            name="tg_user_account_status",
            schema=_STATUS_SCHEMA,
            handler=lambda ctx, **kw: _tg_user_account_status(ctx, **kw),
        ),
    ]
