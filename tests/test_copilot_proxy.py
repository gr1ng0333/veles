"""Tests for copilot_proxy and copilot_proxy_accounts."""

import json
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------------------
# 1. Payload formation — messages and tools passed as-is
# ---------------------------------------------------------------------------

def test_payload_passes_messages_and_tools_as_is(monkeypatch):
    """Messages and tools must be forwarded without conversion."""
    import ouroboros.copilot_proxy_accounts as cpa
    from ouroboros import copilot_proxy

    captured = {}

    def fake_do_request(token, payload, endpoint=""):
        captured.update(payload)
        captured["_endpoint"] = endpoint
        return {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    monkeypatch.setattr(copilot_proxy, "_do_request", fake_do_request)
    cpa._accounts = [{
        "github_token": "ghp_test", "copilot_token": "tok",
        "expires_at": int(time.time()) + 9999,
        "cooldown_until": 0, "dead": False, "last_429_at": 0,
        "request_timestamps": [],
    }]
    cpa._active_idx = 0
    monkeypatch.setattr(cpa, "_init_accounts", lambda force=False: None)
    monkeypatch.setattr(cpa, "_is_multi_account", lambda: False)
    monkeypatch.setattr(cpa, "_ensure_copilot_token", lambda acc, idx, urlopen: "tok")
    monkeypatch.setattr(cpa, "_record_successful_request", lambda idx: None)

    messages = [{"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "test_fn", "parameters": {}}}]

    msg, usage = copilot_proxy.call_copilot(messages, tools=tools, model="claude-sonnet-4-5")

    assert captured["messages"] is messages
    assert captured["tools"] is tools
    assert captured["stream"] is False
    assert captured["model"] == "claude-sonnet-4-5"
    assert msg["content"] == "ok"


# ---------------------------------------------------------------------------
# 2. Response parsing — standard Chat Completions
# ---------------------------------------------------------------------------

def test_parse_response_with_tool_calls(monkeypatch):
    """Tool calls in Chat Completions format should be returned intact."""
    import ouroboros.copilot_proxy_accounts as cpa
    from ouroboros import copilot_proxy

    def fake_do_request(token, payload, endpoint=""):
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "search", "arguments": '{"q":"test"}'},
                    }],
                },
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }

    monkeypatch.setattr(copilot_proxy, "_do_request", fake_do_request)
    cpa._accounts = [{
        "github_token": "ghp_t", "copilot_token": "tok",
        "expires_at": int(time.time()) + 9999,
        "cooldown_until": 0, "dead": False, "last_429_at": 0,
        "request_timestamps": [],
    }]
    cpa._active_idx = 0
    monkeypatch.setattr(cpa, "_init_accounts", lambda force=False: None)
    monkeypatch.setattr(cpa, "_is_multi_account", lambda: False)
    monkeypatch.setattr(cpa, "_ensure_copilot_token", lambda acc, idx, urlopen: "tok")
    monkeypatch.setattr(cpa, "_record_successful_request", lambda idx: None)

    msg, usage = copilot_proxy.call_copilot(
        [{"role": "user", "content": "test"}], model="gpt-4o",
    )

    assert msg["tool_calls"][0]["function"]["name"] == "search"
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 50
    assert usage["cost"] == 0.0


# ---------------------------------------------------------------------------
# 3. Token exchange (mock HTTP)
# ---------------------------------------------------------------------------

def test_token_exchange_mock():
    """Token exchange should GET from GitHub and return copilot token."""
    from ouroboros import copilot_proxy_accounts as cpa

    called_with = {}

    class FakeResponse:
        def __init__(self, data):
            self._data = json.dumps(data).encode()

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake_urlopen(req, timeout=30):
        called_with["url"] = req.full_url
        called_with["method"] = req.get_method()
        called_with["auth"] = req.get_header("Authorization")
        called_with["user_agent"] = req.get_header("User-agent")
        return FakeResponse({
            "token": "tid=abc123",
            "expires_at": 9999999999,
            "endpoints": {"api": "https://api.individual.githubcopilot.com"},
        })

    result = cpa._exchange_token("ghp_test123", fake_urlopen)

    assert result is not None
    assert result["copilot_token"] == "tid=abc123"
    assert result["expires_at"] == 9999999999
    assert result["copilot_api_base"] == "https://api.individual.githubcopilot.com"
    assert called_with["method"] == "GET"
    assert "token ghp_test123" in (called_with.get("auth") or "")
    assert called_with["user_agent"] == "GitHubCopilotChat/0.29.1"


def test_token_exchange_failure_returns_none():
    """Failed token exchange should return None, not raise."""
    from ouroboros import copilot_proxy_accounts as cpa

    def failing_urlopen(req, timeout=30):
        raise OSError("connection refused")

    result = cpa._exchange_token("ghp_bad", failing_urlopen)
    assert result is None


# ---------------------------------------------------------------------------
# 4. Account rotation — cooldown, dead, switching
# ---------------------------------------------------------------------------

def test_account_rotation_cooldown_and_dead(monkeypatch, tmp_path):
    """Accounts on cooldown or dead should be skipped during rotation."""
    from ouroboros import copilot_proxy_accounts as cpa

    state_path = tmp_path / "copilot_accounts_state.json"
    monkeypatch.setattr(cpa, "ACCOUNTS_STATE_FILE", state_path)
    monkeypatch.setenv("COPILOT_ACCOUNTS", json.dumps([
        {"github_token": "ghp_0"},
        {"github_token": "ghp_1"},
        {"github_token": "ghp_2"},
    ]))

    cpa._accounts = []
    cpa._active_idx = 0
    cpa._init_accounts(force=True)

    assert len(cpa._accounts) == 3

    # Put account 0 on cooldown
    cpa._on_rate_limit(0)
    assert cpa._accounts[0]["cooldown_until"] > time.time()

    # Get active — should skip 0, return 1
    result = cpa._get_active_account()
    assert result is not None
    _, idx = result
    assert idx == 1

    # Kill account 1
    cpa._on_dead_account(1)
    assert cpa._accounts[1]["dead"] is True

    # Get active — should return 2
    result = cpa._get_active_account()
    assert result is not None
    _, idx = result
    assert idx == 2

    # Kill account 2 — all exhausted (0 on cooldown, 1+2 dead)
    cpa._on_dead_account(2)
    result = cpa._get_active_account()
    assert result is None


def test_single_account_from_env(monkeypatch, tmp_path):
    """COPILOT_GITHUB_TOKEN fallback should create one account."""
    from ouroboros import copilot_proxy_accounts as cpa

    state_path = tmp_path / "copilot_accounts_state.json"
    monkeypatch.setattr(cpa, "ACCOUNTS_STATE_FILE", state_path)
    monkeypatch.delenv("COPILOT_ACCOUNTS", raising=False)
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghp_single")

    cpa._accounts = []
    cpa._active_idx = 0
    cpa._init_accounts(force=True)

    assert len(cpa._accounts) == 1
    assert cpa._accounts[0]["github_token"] == "ghp_single"


# ---------------------------------------------------------------------------
# 5. Routing in llm.py — "copilot/claude-sonnet-4-5" → call_copilot
# ---------------------------------------------------------------------------

def test_llm_routes_copilot_model(monkeypatch):
    """Model 'copilot/claude-sonnet-4-5' should route to call_copilot."""
    from ouroboros.llm import LLMClient

    routed = {}

    def fake_call_copilot(messages, tools=None, model="claude-sonnet-4-5", max_tokens=16384):
        routed["model"] = model
        routed["messages"] = messages
        routed["tools"] = tools
        return (
            {"role": "assistant", "content": "routed"},
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0},
        )

    monkeypatch.setattr("ouroboros.copilot_proxy.call_copilot", fake_call_copilot)

    client = LLMClient()
    msgs = [{"role": "user", "content": "hi"}]
    msg, usage = client.chat(msgs, model="copilot/claude-sonnet-4-5")

    assert routed["model"] == "claude-sonnet-4-5"
    assert msg["content"] == "routed"


def test_llm_rejects_non_claude_copilot_model():
    """GPT/Codex models must not route through Copilot."""
    from ouroboros.llm import LLMClient

    client = LLMClient()

    try:
        client.chat([{"role": "user", "content": "hi"}], model="copilot/gpt-4o")
        raise AssertionError("Expected ValueError for non-Claude Copilot model")
    except ValueError as e:
        assert "Copilot routing is reserved for Claude-family models only" in str(e)
