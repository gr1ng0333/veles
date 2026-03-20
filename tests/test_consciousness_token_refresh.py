"""Tests for consciousness Codex token auto-refresh."""

import json
import pathlib
import sys
import time
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_urlopen_mock(access="new_access", refresh="new_refresh", expires_in=864000):
    """Return a mock urlopen that returns a successful OAuth refresh response."""
    response_data = json.dumps({
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": expires_in,
    }).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = response_data
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen = MagicMock(return_value=mock_resp)
    return mock_urlopen


def _make_urlopen_failure():
    """Return a mock urlopen that raises an exception."""
    mock_urlopen = MagicMock(side_effect=Exception("network error"))
    return mock_urlopen


# ---------------------------------------------------------------------------
# Tests: refresh_token_if_needed with CODEX_CONSCIOUSNESS prefix
# ---------------------------------------------------------------------------

class TestConsciousnessTokenRefreshUsesEnv:
    """Consciousness refresh must read from CODEX_CONSCIOUSNESS_* env vars."""

    def test_reads_consciousness_env_vars(self, monkeypatch):
        from ouroboros import codex_proxy_accounts as cpa

        monkeypatch.setenv("CODEX_CONSCIOUSNESS_ACCESS", "old_access")
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_REFRESH", "my_refresh_token")
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_EXPIRES", str(int(time.time() + 999999)))

        tokens = cpa._load_tokens("CODEX_CONSCIOUSNESS")

        assert tokens["access_token"] == "old_access"
        assert tokens["refresh_token"] == "my_refresh_token"

    def test_refresh_not_triggered_when_token_fresh(self, monkeypatch):
        from ouroboros import codex_proxy_accounts as cpa

        future = str(int(time.time() + 999999))
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_ACCESS", "still_valid")
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_REFRESH", "rt")
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_EXPIRES", future)

        mock_urlopen = _make_urlopen_mock()
        result = cpa.refresh_token_if_needed(
            "https://auth.openai.com/oauth/token", mock_urlopen,
            prefix="CODEX_CONSCIOUSNESS",
        )

        assert result == "still_valid"
        mock_urlopen.assert_not_called()


class TestConsciousnessRefreshUpdatesEnv:
    """After refresh, new tokens must be written back to os.environ."""

    def test_updates_env_after_refresh(self, monkeypatch):
        from ouroboros import codex_proxy_accounts as cpa

        # Token is expired — should trigger refresh
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_ACCESS", "old")
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_REFRESH", "rt_valid")
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_EXPIRES", "0")

        mock_urlopen = _make_urlopen_mock(
            access="fresh_access", refresh="fresh_refresh", expires_in=86400,
        )

        result = cpa.refresh_token_if_needed(
            "https://auth.openai.com/oauth/token", mock_urlopen,
            prefix="CODEX_CONSCIOUSNESS",
        )

        assert result == "fresh_access"
        import os
        assert os.environ["CODEX_CONSCIOUSNESS_ACCESS"] == "fresh_access"
        assert os.environ["CODEX_CONSCIOUSNESS_REFRESH"] == "fresh_refresh"
        assert int(os.environ["CODEX_CONSCIOUSNESS_EXPIRES"]) > time.time()

    def test_refresh_failure_returns_old_token(self, monkeypatch):
        from ouroboros import codex_proxy_accounts as cpa

        monkeypatch.setenv("CODEX_CONSCIOUSNESS_ACCESS", "old_token")
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_REFRESH", "rt")
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_EXPIRES", "0")

        mock_urlopen = _make_urlopen_failure()

        result = cpa.refresh_token_if_needed(
            "https://auth.openai.com/oauth/token", mock_urlopen,
            prefix="CODEX_CONSCIOUSNESS",
        )

        # Should return old token on failure, not crash
        assert result == "old_token"


class TestConsciousnessProactiveRefreshThreshold:
    """Proactive refresh must trigger when expires < REFRESH_THRESHOLD_SEC."""

    def test_refresh_triggered_when_near_expiry(self, monkeypatch):
        from ouroboros import codex_proxy_accounts as cpa

        # Expires in 60 seconds — well under REFRESH_THRESHOLD_SEC (3600)
        near_expiry = str(int(time.time() + 60))
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_ACCESS", "about_to_expire")
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_REFRESH", "rt_alive")
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_EXPIRES", near_expiry)

        mock_urlopen = _make_urlopen_mock(
            access="refreshed_token", refresh="new_rt", expires_in=86400,
        )

        result = cpa.refresh_token_if_needed(
            "https://auth.openai.com/oauth/token", mock_urlopen,
            prefix="CODEX_CONSCIOUSNESS",
        )

        assert result == "refreshed_token"
        mock_urlopen.assert_called_once()

    def test_refresh_triggered_when_already_expired(self, monkeypatch):
        from ouroboros import codex_proxy_accounts as cpa

        # Already expired
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_ACCESS", "dead_token")
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_REFRESH", "rt_alive")
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_EXPIRES", str(int(time.time() - 3600)))

        mock_urlopen = _make_urlopen_mock(access="revived")

        result = cpa.refresh_token_if_needed(
            "https://auth.openai.com/oauth/token", mock_urlopen,
            prefix="CODEX_CONSCIOUSNESS",
        )

        assert result == "revived"
        mock_urlopen.assert_called_once()

    def test_no_refresh_when_no_refresh_token(self, monkeypatch):
        from ouroboros import codex_proxy_accounts as cpa

        monkeypatch.setenv("CODEX_CONSCIOUSNESS_ACCESS", "expired_token")
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_REFRESH", "")
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_EXPIRES", "0")

        mock_urlopen = _make_urlopen_mock()

        result = cpa.refresh_token_if_needed(
            "https://auth.openai.com/oauth/token", mock_urlopen,
            prefix="CODEX_CONSCIOUSNESS",
        )

        assert result == "expired_token"
        mock_urlopen.assert_not_called()


class TestCallCodexConsciousnessRouting:
    """call_codex with CODEX_CONSCIOUSNESS prefix must NOT use multi-account rotation."""

    def test_consciousness_bypasses_multi_account(self, monkeypatch):
        from ouroboros import codex_proxy as cp

        # Simulate multi-account being enabled
        monkeypatch.setattr(cp, "_is_multi_account", lambda: True)

        # Set consciousness tokens
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_ACCESS", "cs_token")
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_REFRESH", "cs_rt")
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_EXPIRES", str(int(time.time() + 999999)))

        # Mock _do_request to return valid response
        fake_response = {
            "response": {
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }
        }
        do_request_mock = MagicMock(return_value=fake_response)
        monkeypatch.setattr(cp, "_do_request", do_request_mock)

        # Mock rotation to track if it gets called
        rotation_mock = MagicMock()
        monkeypatch.setattr(cp, "_call_with_rotation", rotation_mock)

        msg, usage = cp.call_codex(
            [{"role": "user", "content": "think"}],
            model="gpt-5.3-codex",
            token_prefix="CODEX_CONSCIOUSNESS",
        )

        # Rotation must NOT be called for consciousness
        rotation_mock.assert_not_called()
        # _do_request must be called with consciousness access token
        do_request_mock.assert_called_once()
        assert do_request_mock.call_args[0][0] == "cs_token"

    def test_401_retry_resets_correct_expires_key(self, monkeypatch):
        import urllib.error
        from ouroboros import codex_proxy as cp

        monkeypatch.setattr(cp, "_is_multi_account", lambda: True)
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_ACCESS", "cs_token")
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_REFRESH", "cs_rt")
        monkeypatch.setenv("CODEX_CONSCIOUSNESS_EXPIRES", str(int(time.time() + 999999)))

        call_count = {"n": 0}
        fake_response = {
            "response": {
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }
        }

        def side_effect(token, payload):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise urllib.error.HTTPError(
                    "url", 401, "Unauthorized", {}, BytesIO(b"expired"),
                )
            return fake_response

        monkeypatch.setattr(cp, "_do_request", side_effect)

        import os
        msg, usage = cp.call_codex(
            [{"role": "user", "content": "think"}],
            model="gpt-5.3-codex",
            token_prefix="CODEX_CONSCIOUSNESS",
        )

        # After 401, CODEX_CONSCIOUSNESS_EXPIRES must be reset (not CODEX_TOKEN_EXPIRES)
        assert os.environ.get("CODEX_CONSCIOUSNESS_EXPIRES") == "0"
        assert call_count["n"] == 2
