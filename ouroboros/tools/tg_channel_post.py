"""tg_channel_post — post messages to Telegram channels/chats via Bot API.

Requires the bot to be an administrator in the target channel/group.
The bot token is taken from TELEGRAM_BOT_TOKEN env var.

Tools:
    tg_post(chat_id, text, parse_mode?, disable_preview?)
        — send a text message to any channel/chat

    tg_post_photo(chat_id, photo_url, caption?, parse_mode?)
        — send a photo with optional caption

    tg_pin_message(chat_id, message_id, disable_notification?)
        — pin a message in a channel/group

Usage examples:
    # Post to a public channel (bot must be admin):
    tg_post(chat_id="@myChannel", text="Hello from Veles!")

    # Post to a channel by numeric ID:
    tg_post(chat_id="-1001234567890", text="Update: v7.2.0 released")

    # Markdown formatting:
    tg_post(chat_id="@myChannel", text="*Bold* and _italic_", parse_mode="Markdown")

    # HTML formatting:
    tg_post(chat_id="@myChannel", text="<b>Bold</b>", parse_mode="HTML")

    # Photo post:
    tg_post_photo(chat_id="@myChannel", photo_url="https://example.com/img.jpg",
                  caption="New release screenshot")

Notes:
    - For channels: bot must be added as admin with "Post Messages" permission.
    - For groups: bot must be a member.
    - chat_id can be "@username" or numeric ID (as string or int).
    - Numeric IDs for channels are typically -100XXXXXXXXXX.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Union

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 20
_TG_API_BASE = "https://api.telegram.org/bot{token}/{method}"


# ── Bot API helpers ────────────────────────────────────────────────────────────

def _get_token() -> str:
    """Get bot token from env. Raises if not set."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN env var is not set")
    return token


def _tg_api_call(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Make a Telegram Bot API call. Returns parsed JSON response.

    Raises RuntimeError with a human-readable message on API/network errors.
    """
    token = _get_token()
    url = _TG_API_BASE.format(token=token, method=method)

    data = json.dumps({k: v for k, v in payload.items() if v is not None}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            result = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"Telegram API HTTP {exc.code}: {body[:500]}") from exc
    except Exception as exc:
        raise RuntimeError(f"Telegram API request failed: {exc}") from exc

    if not result.get("ok"):
        desc = result.get("description", "unknown error")
        code = result.get("error_code", "?")
        raise RuntimeError(f"Telegram API error {code}: {desc}")

    return result


def _normalize_chat_id(chat_id: Union[str, int]) -> Union[str, int]:
    """Accept '@username', numeric int, or numeric string."""
    if isinstance(chat_id, int):
        return chat_id
    s = str(chat_id).strip()
    if s.startswith("@"):
        return s
    # Try numeric
    try:
        return int(s)
    except ValueError:
        return s  # Let Telegram validate


# ── Tool implementations ───────────────────────────────────────────────────────

def _tg_post(
    ctx: ToolContext,
    chat_id: Union[str, int],
    text: str,
    parse_mode: Optional[str] = None,
    disable_web_page_preview: bool = False,
    disable_notification: bool = False,
) -> str:
    """Send a text message to a Telegram channel or group."""
    if not text or not text.strip():
        return json.dumps({"error": "text must not be empty"})

    text = text.strip()
    cid = _normalize_chat_id(chat_id)

    payload: Dict[str, Any] = {
        "chat_id": cid,
        "text": text,
        "disable_web_page_preview": disable_web_page_preview or False,
        "disable_notification": disable_notification or False,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        result = _tg_api_call("sendMessage", payload)
    except RuntimeError as exc:
        return json.dumps({
            "ok": False,
            "error": str(exc),
        })

    msg = result.get("result", {})
    return json.dumps({
        "ok": True,
        "message_id": msg.get("message_id"),
        "chat_id": cid,
        "date": msg.get("date"),
        "text_preview": text[:100] + ("..." if len(text) > 100 else ""),
    })


def _tg_post_photo(
    ctx: ToolContext,
    chat_id: Union[str, int],
    photo_url: str,
    caption: Optional[str] = None,
    parse_mode: Optional[str] = None,
    disable_notification: bool = False,
) -> str:
    """Send a photo to a Telegram channel or group."""
    if not photo_url or not photo_url.strip():
        return json.dumps({"error": "photo_url must not be empty"})

    cid = _normalize_chat_id(chat_id)

    payload: Dict[str, Any] = {
        "chat_id": cid,
        "photo": photo_url.strip(),
        "disable_notification": disable_notification or False,
    }
    if caption:
        payload["caption"] = caption.strip()
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        result = _tg_api_call("sendPhoto", payload)
    except RuntimeError as exc:
        return json.dumps({
            "ok": False,
            "error": str(exc),
        })

    msg = result.get("result", {})
    return json.dumps({
        "ok": True,
        "message_id": msg.get("message_id"),
        "chat_id": cid,
        "date": msg.get("date"),
        "caption_preview": (caption or "")[:100],
    })


def _tg_pin_message(
    ctx: ToolContext,
    chat_id: Union[str, int],
    message_id: int,
    disable_notification: bool = False,
) -> str:
    """Pin a message in a Telegram channel or group."""
    cid = _normalize_chat_id(chat_id)

    payload: Dict[str, Any] = {
        "chat_id": cid,
        "message_id": int(message_id),
        "disable_notification": disable_notification or False,
    }

    try:
        result = _tg_api_call("pinChatMessage", payload)
    except RuntimeError as exc:
        return json.dumps({
            "ok": False,
            "error": str(exc),
        })

    return json.dumps({
        "ok": True,
        "pinned": True,
        "chat_id": cid,
        "message_id": int(message_id),
    })


# ── Tool registration ──────────────────────────────────────────────────────────

_POST_SCHEMA = {
    "name": "tg_post",
    "description": (
        "Send a text message to a Telegram channel or group via Bot API. "
        "The bot must be an administrator with 'Post Messages' permission. "
        "Supports plain text, Markdown (v1), and HTML formatting.\n\n"
        "Examples:\n"
        "  tg_post(chat_id='@myChannel', text='Hello!')\n"
        "  tg_post(chat_id='-1001234567890', text='*Bold text*', parse_mode='Markdown')\n"
        "  tg_post(chat_id='@myChannel', text='<b>HTML</b>', parse_mode='HTML')\n\n"
        "Returns message_id on success (useful for tg_pin_message)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "description": "Channel username (@myChannel) or numeric chat ID (-1001234567890)",
                "oneOf": [{"type": "string"}, {"type": "integer"}],
            },
            "text": {
                "type": "string",
                "description": "Message text. Max 4096 chars.",
            },
            "parse_mode": {
                "type": "string",
                "enum": ["Markdown", "MarkdownV2", "HTML"],
                "description": "Optional: formatting mode (default: plain text)",
            },
            "disable_web_page_preview": {
                "type": "boolean",
                "description": "Disable link previews (default: false)",
            },
            "disable_notification": {
                "type": "boolean",
                "description": "Send silently without notification (default: false)",
            },
        },
        "required": ["chat_id", "text"],
    },
}

_POST_PHOTO_SCHEMA = {
    "name": "tg_post_photo",
    "description": (
        "Send a photo to a Telegram channel or group via Bot API. "
        "The bot must be an administrator with 'Post Messages' permission.\n\n"
        "Examples:\n"
        "  tg_post_photo(chat_id='@myChannel', photo_url='https://example.com/img.jpg')\n"
        "  tg_post_photo(chat_id='@myChannel', photo_url='https://...', "
        "caption='New release!', parse_mode='Markdown')\n\n"
        "Returns message_id on success."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "description": "Channel username (@myChannel) or numeric chat ID",
                "oneOf": [{"type": "string"}, {"type": "integer"}],
            },
            "photo_url": {
                "type": "string",
                "description": "Public URL of the photo to send",
            },
            "caption": {
                "type": "string",
                "description": "Optional caption (max 1024 chars)",
            },
            "parse_mode": {
                "type": "string",
                "enum": ["Markdown", "MarkdownV2", "HTML"],
                "description": "Optional: formatting mode for caption",
            },
            "disable_notification": {
                "type": "boolean",
                "description": "Send silently (default: false)",
            },
        },
        "required": ["chat_id", "photo_url"],
    },
}

_PIN_SCHEMA = {
    "name": "tg_pin_message",
    "description": (
        "Pin a message in a Telegram channel or group. "
        "The bot must have 'Pin Messages' permission.\n\n"
        "Example:\n"
        "  result = tg_post(chat_id='@myChannel', text='Important!')\n"
        "  tg_pin_message(chat_id='@myChannel', message_id=result['message_id'])"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "description": "Channel username or numeric chat ID",
                "oneOf": [{"type": "string"}, {"type": "integer"}],
            },
            "message_id": {
                "type": "integer",
                "description": "ID of the message to pin",
            },
            "disable_notification": {
                "type": "boolean",
                "description": "Pin silently without notification (default: false)",
            },
        },
        "required": ["chat_id", "message_id"],
    },
}


def get_tools() -> list:
    return [
        ToolEntry("tg_post", _POST_SCHEMA, lambda ctx, **kw: _tg_post(ctx, **kw)),
        ToolEntry("tg_post_photo", _POST_PHOTO_SCHEMA, lambda ctx, **kw: _tg_post_photo(ctx, **kw)),
        ToolEntry("tg_pin_message", _PIN_SCHEMA, lambda ctx, **kw: _tg_pin_message(ctx, **kw)),
    ]
