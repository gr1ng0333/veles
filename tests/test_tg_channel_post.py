"""tests/test_tg_channel_post.py

Unit tests for tg_channel_post module.
All Telegram API calls are mocked — no real network requests.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure repo root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ── helper to create a fake successful Telegram API response ──────────────────

def _tg_ok(result: dict) -> bytes:
    return json.dumps({"ok": True, "result": result}).encode()


def _tg_error(code: int, desc: str) -> bytes:
    return json.dumps({"ok": False, "error_code": code, "description": desc}).encode()


# ── imports ───────────────────────────────────────────────────────────────────

from ouroboros.tools.tg_channel_post import (
    _normalize_chat_id,
    _get_token,
    _tg_post,
    _tg_post_photo,
    _tg_pin_message,
    get_tools,
)
from ouroboros.tools.registry import ToolContext


def _ctx() -> ToolContext:
    return ToolContext(repo_dir="/opt/veles", drive_root="/opt/veles-data", budget_remaining=100.0)


# ── _normalize_chat_id ────────────────────────────────────────────────────────

class TestNormalizeChatId(unittest.TestCase):
    def test_at_username(self):
        assert _normalize_chat_id("@myChannel") == "@myChannel"

    def test_numeric_string(self):
        assert _normalize_chat_id("-1001234567890") == -1001234567890

    def test_numeric_int(self):
        assert _normalize_chat_id(-1001234567890) == -1001234567890

    def test_non_numeric_string(self):
        assert _normalize_chat_id("myChannel") == "myChannel"

    def test_strips_whitespace(self):
        assert _normalize_chat_id("  @chan  ") == "@chan"


# ── _get_token ────────────────────────────────────────────────────────────────

class TestGetToken(unittest.TestCase):
    def test_raises_if_not_set(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}):
            with self.assertRaises(RuntimeError):
                _get_token()

    def test_returns_token(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:ABC"}):
            assert _get_token() == "123:ABC"


# ── _tg_post ──────────────────────────────────────────────────────────────────

class TestTgPost(unittest.TestCase):
    def _mock_urlopen(self, response_bytes: bytes):
        """Context manager that mocks urlopen to return given bytes."""
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = response_bytes
        return patch("urllib.request.urlopen", return_value=mock_resp)

    def test_success(self):
        payload = {"message_id": 42, "date": 1712000000}
        with self._mock_urlopen(_tg_ok(payload)):
            result = json.loads(_tg_post(_ctx(), chat_id="@testChan", text="Hello!"))
        assert result["ok"] is True
        assert result["message_id"] == 42
        assert result["chat_id"] == "@testChan"
        assert "Hello" in result["text_preview"]

    def test_empty_text_returns_error(self):
        result = json.loads(_tg_post(_ctx(), chat_id="@c", text=""))
        assert "error" in result

    def test_whitespace_text_returns_error(self):
        result = json.loads(_tg_post(_ctx(), chat_id="@c", text="   "))
        assert "error" in result

    def test_api_error_returns_ok_false(self):
        from urllib.error import HTTPError
        import io

        err_body = _tg_error(403, "Forbidden: bot is not a member")
        http_err = HTTPError(url="", code=403, msg="Forbidden", hdrs={}, fp=io.BytesIO(err_body))
        with patch("urllib.request.urlopen", side_effect=http_err):
            result = json.loads(_tg_post(_ctx(), chat_id="@c", text="Hi"))
        assert result["ok"] is False
        assert "403" in result["error"] or "Forbidden" in result["error"]

    def test_with_parse_mode(self):
        payload = {"message_id": 7, "date": 1712000001}
        captured_payload = {}

        def fake_urlopen(req, timeout=None):
            # Capture request data for inspection
            import json as _json
            captured_payload.update(_json.loads(req.data))
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.read.return_value = _tg_ok(payload)
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = json.loads(_tg_post(
                _ctx(),
                chat_id="@c",
                text="*bold*",
                parse_mode="Markdown",
            ))

        assert result["ok"] is True
        assert captured_payload.get("parse_mode") == "Markdown"

    def test_numeric_chat_id(self):
        payload = {"message_id": 1, "date": 1712000002}
        with self._mock_urlopen(_tg_ok(payload)):
            result = json.loads(_tg_post(_ctx(), chat_id="-1001234567890", text="Hi"))
        assert result["ok"] is True
        # Should be stored as int
        assert result["chat_id"] == -1001234567890


# ── _tg_post_photo ────────────────────────────────────────────────────────────

class TestTgPostPhoto(unittest.TestCase):
    def _mock_urlopen(self, response_bytes: bytes):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = response_bytes
        return patch("urllib.request.urlopen", return_value=mock_resp)

    def test_success(self):
        payload = {"message_id": 99, "date": 1712000003}
        with self._mock_urlopen(_tg_ok(payload)):
            result = json.loads(_tg_post_photo(
                _ctx(),
                chat_id="@chan",
                photo_url="https://example.com/img.png",
                caption="Test photo",
            ))
        assert result["ok"] is True
        assert result["message_id"] == 99
        assert result["caption_preview"] == "Test photo"

    def test_empty_url_returns_error(self):
        result = json.loads(_tg_post_photo(_ctx(), chat_id="@c", photo_url=""))
        assert "error" in result

    def test_no_caption(self):
        payload = {"message_id": 5, "date": 1712000004}
        with self._mock_urlopen(_tg_ok(payload)):
            result = json.loads(_tg_post_photo(
                _ctx(),
                chat_id="@chan",
                photo_url="https://example.com/img.png",
            ))
        assert result["ok"] is True
        assert result["caption_preview"] == ""


# ── _tg_pin_message ───────────────────────────────────────────────────────────

class TestTgPinMessage(unittest.TestCase):
    def _mock_urlopen(self, response_bytes: bytes):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = response_bytes
        return patch("urllib.request.urlopen", return_value=mock_resp)

    def test_success(self):
        with self._mock_urlopen(_tg_ok(True)):
            result = json.loads(_tg_pin_message(_ctx(), chat_id="@chan", message_id=42))
        assert result["ok"] is True
        assert result["pinned"] is True
        assert result["message_id"] == 42

    def test_api_error(self):
        from urllib.error import HTTPError
        import io

        err_body = _tg_error(400, "Bad Request: message to pin not found")
        http_err = HTTPError(url="", code=400, msg="Bad Request", hdrs={}, fp=io.BytesIO(err_body))
        with patch("urllib.request.urlopen", side_effect=http_err):
            result = json.loads(_tg_pin_message(_ctx(), chat_id="@chan", message_id=999))
        assert result["ok"] is False


# ── get_tools ─────────────────────────────────────────────────────────────────

class TestGetTools(unittest.TestCase):
    def test_returns_three_tools(self):
        tools = get_tools()
        assert len(tools) == 3

    def test_tool_names(self):
        tools = get_tools()
        names = {t.name for t in tools}
        assert "tg_post" in names
        assert "tg_post_photo" in names
        assert "tg_pin_message" in names

    def test_schemas_valid(self):
        tools = get_tools()
        for t in tools:
            assert "name" in t.schema
            assert "description" in t.schema
            assert "parameters" in t.schema
            assert t.schema["parameters"]["type"] == "object"
            assert "properties" in t.schema["parameters"]
            # required fields
            assert "required" in t.schema["parameters"]

    def test_required_params(self):
        tools = get_tools()
        tool_map = {t.name: t for t in tools}
        # tg_post requires chat_id and text
        assert set(tool_map["tg_post"].schema["parameters"]["required"]) >= {"chat_id", "text"}
        # tg_post_photo requires chat_id and photo_url
        assert set(tool_map["tg_post_photo"].schema["parameters"]["required"]) >= {"chat_id", "photo_url"}
        # tg_pin_message requires chat_id and message_id
        assert set(tool_map["tg_pin_message"].schema["parameters"]["required"]) >= {"chat_id", "message_id"}


if __name__ == "__main__":
    unittest.main()
