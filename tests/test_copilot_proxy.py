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

    def fake_do_request(token, payload, endpoint="", initiator="user", interaction_id=None):
        captured.update(payload)
        captured["_endpoint"] = endpoint
        captured["_initiator"] = initiator
        captured["_interaction_id"] = interaction_id
        assert initiator in {"user", "agent"}
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
    monkeypatch.setattr(cpa, "track_copilot_usage", lambda idx, model: None)

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

    def fake_do_request(token, payload, endpoint="", initiator="user", interaction_id=None):
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
    monkeypatch.setattr(cpa, "track_copilot_usage", lambda idx, model: None)

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

    def fake_call_copilot(messages, tools=None, model="claude-sonnet-4-5", max_tokens=16384,
                          tool_choice=None, interaction_id=None, reasoning_effort=None, force_user_initiator=False):
        routed["model"] = model
        routed["messages"] = messages
        routed["tools"] = tools
        routed["interaction_id"] = interaction_id
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


def test_model_transport_resolution_contract():
    """Transport resolution should stay explicit and backward-compatible."""
    from ouroboros.llm import model_transport, transport_model_name

    assert model_transport("codex/gpt-5.4") == "codex"
    assert model_transport("codex-consciousness/gpt-5.1-codex-mini") == "codex-consciousness"
    assert model_transport("copilot/claude-sonnet-4.6") == "copilot"
    assert model_transport("anthropic/claude-sonnet-4.6") == "openrouter"
    assert model_transport("openai/o3") == "openrouter"

    assert transport_model_name("codex/gpt-5.4") == "gpt-5.4"
    assert transport_model_name("codex-consciousness/gpt-5.1-codex-mini") == "gpt-5.1-codex-mini"
    assert transport_model_name("copilot/claude-sonnet-4.6") == "claude-sonnet-4.6"
    assert transport_model_name("anthropic/claude-sonnet-4.6") == "anthropic/claude-sonnet-4.6"


def test_llm_openrouter_model_is_left_unprefixed(monkeypatch):
    """OpenRouter models should go through the default client unchanged."""
    from ouroboros.llm import LLMClient

    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)

            class FakeResp:
                def model_dump(self):
                    return {
                        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0},
                    }

            return FakeResp()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    client = LLMClient(api_key="test-key")
    monkeypatch.setattr(client, "_get_client", lambda: FakeClient())

    msg, usage = client.chat([{"role": "user", "content": "hi"}], model="anthropic/claude-sonnet-4.6")

    assert captured["model"] == "anthropic/claude-sonnet-4.6"
    assert msg["content"] == "ok"
    assert usage["cost"] == 0.0


# ---------------------------------------------------------------------------
# 8. Session tracking
# ---------------------------------------------------------------------------

def test_session_tracking_counts_rounds():
    """Session tracker must count rounds and tokens per interaction."""
    from ouroboros.copilot_proxy import _track_session, get_session_stats, _active_sessions

    # Clean state
    _active_sessions.clear()

    iid = "test-interaction-001"

    # Round 1 — user initiated
    stats = _track_session(iid, {"prompt_tokens": 1000, "completion_tokens": 200}, "user")
    assert stats["rounds"] == 1
    assert stats["premium_requests"] == 1
    assert stats["total_prompt_tokens"] == 1000

    # Round 2 — agent (free)
    stats = _track_session(iid, {"prompt_tokens": 2000, "completion_tokens": 300}, "agent")
    assert stats["rounds"] == 2
    assert stats["premium_requests"] == 1  # still 1!
    assert stats["total_prompt_tokens"] == 3000

    # Round 3 — agent (free)
    stats = _track_session(iid, {"prompt_tokens": 3000, "completion_tokens": 400}, "agent")
    assert stats["rounds"] == 3
    assert stats["premium_requests"] == 1  # still 1!

    # Verify get_session_stats
    retrieved = get_session_stats(iid)
    assert retrieved is not None
    assert retrieved["rounds"] == 3
    assert retrieved["total_completion_tokens"] == 900

    # Cleanup
    _active_sessions.clear()


def test_session_tracking_no_interaction_id():
    """Session tracker must handle None interaction_id gracefully."""
    from ouroboros.copilot_proxy import _track_session
    stats = _track_session(None, {"prompt_tokens": 100}, "user")
    assert stats == {}


def test_stale_session_cleanup():
    """Stale sessions must be cleaned up."""
    from ouroboros.copilot_proxy import _track_session, cleanup_stale_sessions, _active_sessions
    import time as _time

    _active_sessions.clear()

    _track_session("old-session", {"prompt_tokens": 100}, "user")
    # Manually age the session
    _active_sessions["old-session"]["last_activity"] = _time.time() - 7200  # 2 hours ago

    _track_session("new-session", {"prompt_tokens": 100}, "user")

    removed = cleanup_stale_sessions(max_age_seconds=3600)
    assert removed == 1
    assert "old-session" not in _active_sessions
    assert "new-session" in _active_sessions

    _active_sessions.clear()


# ---------------------------------------------------------------------------
# 9. Quota tracking — model cost, monthly reset, persisted state
# ---------------------------------------------------------------------------

def test_model_cost_known_models():
    """Known model cost multipliers should match spec."""
    from ouroboros.copilot_proxy_accounts import _model_cost
    assert _model_cost("claude-haiku-4.5") == 0.33
    assert _model_cost("claude-sonnet-4.6") == 1.0
    assert _model_cost("claude-opus-4.6") == 3.0


def test_model_cost_fuzzy_match():
    """Model names with dash/dot variations should match."""
    from ouroboros.copilot_proxy_accounts import _model_cost
    # claude-sonnet-4-5 should fuzzy-match claude-sonnet-4.5
    cost = _model_cost("claude-sonnet-4-5")
    assert cost == 1.0


def test_model_cost_unknown_defaults_to_1():
    """Unknown model names default to 1.0."""
    from ouroboros.copilot_proxy_accounts import _model_cost
    assert _model_cost("unknown-model-xyz") == 1.0


def test_track_copilot_usage_adds_units(monkeypatch, tmp_path):
    """track_copilot_usage should accumulate usage_units."""
    from ouroboros import copilot_proxy_accounts as cpa

    state_path = tmp_path / "copilot_accounts_state.json"
    monkeypatch.setattr(cpa, "ACCOUNTS_STATE_FILE", state_path)
    monkeypatch.setenv("COPILOT_ACCOUNTS", json.dumps([
        {"github_token": "ghp_0"},
    ]))

    cpa._accounts = []
    cpa._active_idx = 0
    cpa._init_accounts(force=True)

    assert cpa._accounts[0]["usage_units"] == 0.0

    cpa.track_copilot_usage(0, "claude-opus-4.6")
    assert cpa._accounts[0]["usage_units"] == 3.0

    cpa.track_copilot_usage(0, "claude-haiku-4.5")
    assert abs(cpa._accounts[0]["usage_units"] - 3.33) < 0.01

    cpa.track_copilot_usage(0, "claude-sonnet-4.6")
    assert abs(cpa._accounts[0]["usage_units"] - 4.33) < 0.01

    # Check history
    history = cpa._accounts[0]["usage_history"]
    assert len(history) == 3
    assert history[0]["model"] == "claude-opus-4.6"
    assert history[0]["cost"] == 3.0
    assert history[1]["cost"] == 0.33


def test_track_copilot_usage_persists(monkeypatch, tmp_path):
    """Usage data should be persisted to state file."""
    from ouroboros import copilot_proxy_accounts as cpa

    state_path = tmp_path / "copilot_accounts_state.json"
    monkeypatch.setattr(cpa, "ACCOUNTS_STATE_FILE", state_path)
    monkeypatch.setenv("COPILOT_ACCOUNTS", json.dumps([
        {"github_token": "ghp_0"},
    ]))

    cpa._accounts = []
    cpa._active_idx = 0
    cpa._init_accounts(force=True)

    cpa.track_copilot_usage(0, "claude-opus-4.6")

    # Verify state file written
    assert state_path.exists()
    saved = json.loads(state_path.read_text())
    assert saved["accounts"][0]["usage_units"] == 3.0
    assert len(saved["accounts"][0]["usage_history"]) == 1


def test_monthly_reset(monkeypatch, tmp_path):
    """Usage should reset when month changes."""
    import datetime as dt
    from ouroboros import copilot_proxy_accounts as cpa

    state_path = tmp_path / "copilot_accounts_state.json"
    monkeypatch.setattr(cpa, "ACCOUNTS_STATE_FILE", state_path)
    monkeypatch.setenv("COPILOT_ACCOUNTS", json.dumps([
        {"github_token": "ghp_0"},
    ]))

    cpa._accounts = []
    cpa._active_idx = 0
    cpa._init_accounts(force=True)

    # Simulate previous month usage
    cpa._accounts[0]["usage_units"] = 150.0
    cpa._accounts[0]["last_reset"] = "2025-01-01"
    cpa._accounts[0]["usage_history"] = [{"ts": "old", "model": "x", "cost": 1}]

    # track_copilot_usage should trigger reset since last_reset is old
    cpa.track_copilot_usage(0, "claude-sonnet-4.6")

    # After reset + 1 sonnet request = 1.0
    assert cpa._accounts[0]["usage_units"] == 1.0
    assert cpa._accounts[0]["last_reset"] == dt.date.today().strftime("%Y-%m-01")


def test_copilot_accounts_status_text(monkeypatch, tmp_path):
    """Status text should format correctly."""
    from ouroboros import copilot_proxy_accounts as cpa

    state_path = tmp_path / "copilot_accounts_state.json"
    monkeypatch.setattr(cpa, "ACCOUNTS_STATE_FILE", state_path)
    monkeypatch.setenv("COPILOT_ACCOUNTS", json.dumps([
        {"github_token": "ghp_0"},
        {"github_token": "ghp_1"},
    ]))

    cpa._accounts = []
    cpa._active_idx = 0
    cpa._init_accounts(force=True)

    # Add some usage to account 0
    cpa.track_copilot_usage(0, "claude-opus-4.6")
    cpa.track_copilot_usage(0, "claude-opus-4.6")

    text = cpa.copilot_accounts_status_text()
    assert "🤖 Copilot Accounts: 2 шт." in text
    assert "6.0/300 units" in text
    assert "#0:" in text
    assert "#1:" in text
    assert "0.0/300 units" in text


def test_track_usage_invalid_idx(monkeypatch, tmp_path):
    """Tracking with out-of-range index should not crash."""
    from ouroboros import copilot_proxy_accounts as cpa

    state_path = tmp_path / "copilot_accounts_state.json"
    monkeypatch.setattr(cpa, "ACCOUNTS_STATE_FILE", state_path)
    monkeypatch.setenv("COPILOT_ACCOUNTS", json.dumps([
        {"github_token": "ghp_0"},
    ]))

    cpa._accounts = []
    cpa._active_idx = 0
    cpa._init_accounts(force=True)

    # Should not raise
    cpa.track_copilot_usage(99, "claude-sonnet-4.6")
    assert cpa._accounts[0]["usage_units"] == 0.0


def test_load_accounts_bash_stripped_json(monkeypatch, tmp_path):
    """Accounts should load even when bash 'source .env' strips inner JSON quotes."""
    from ouroboros import copilot_proxy_accounts as cpa

    state_path = tmp_path / "copilot_accounts_state.json"
    monkeypatch.setattr(cpa, "ACCOUNTS_STATE_FILE", state_path)
    # Simulate bash-stripped JSON: source .env removes double quotes from unquoted value
    monkeypatch.setenv(
        "COPILOT_ACCOUNTS",
        "[{github_token:ghu_asg58igXXXXXXXX},{github_token:ghu_WnB25Q6YYYYYYYY}]",
    )

    cpa._accounts = []
    cpa._active_idx = 0
    cpa._init_accounts(force=True)

    assert len(cpa._accounts) == 2
    assert cpa._accounts[0]["github_token"] == "ghu_asg58igXXXXXXXX"
    assert cpa._accounts[1]["github_token"] == "ghu_WnB25Q6YYYYYYYY"


def test_load_accounts_outer_quotes_stripped(monkeypatch, tmp_path):
    """Surrounding single/double quotes on COPILOT_ACCOUNTS should be stripped."""
    from ouroboros import copilot_proxy_accounts as cpa

    state_path = tmp_path / "copilot_accounts_state.json"
    monkeypatch.setattr(cpa, "ACCOUNTS_STATE_FILE", state_path)
    # Value wrapped in single quotes (some .env loaders pass them through)
    monkeypatch.setenv(
        "COPILOT_ACCOUNTS",
        '\'[{"github_token":"ghu_testToken1"},{"github_token":"ghu_testToken2"}]\'',
    )

    cpa._accounts = []
    cpa._active_idx = 0
    cpa._init_accounts(force=True)

    assert len(cpa._accounts) == 2
    assert cpa._accounts[0]["github_token"] == "ghu_testToken1"
    assert cpa._accounts[1]["github_token"] == "ghu_testToken2"


def test_llm_chat_forwards_force_user_initiator_to_copilot(monkeypatch):
    from ouroboros.llm import LLMClient

    routed = {}

    def fake_call_copilot(messages, tools=None, model=None, max_tokens=None,
                          tool_choice=None, interaction_id=None, reasoning_effort=None, force_user_initiator=False):
        routed["force_user_initiator"] = force_user_initiator
        return {"role": "assistant", "content": "ok"}, {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr("ouroboros.copilot_proxy.call_copilot", fake_call_copilot)

    client = LLMClient()
    msg, usage = client.chat(
        messages=[{"role": "assistant", "content": "continue"}],
        model="copilot/claude-sonnet-4.6",
        force_user_initiator=True,
    )

    assert msg["content"] == "ok"
    assert usage["prompt_tokens"] == 1
    assert routed["force_user_initiator"] is True
