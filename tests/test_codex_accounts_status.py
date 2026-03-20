import json
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def test_get_accounts_status_reloads_state_file(monkeypatch, tmp_path):
    from ouroboros import codex_proxy_accounts as cpa

    state_path = tmp_path / "codex_accounts_state.json"
    monkeypatch.setattr(cpa, "ACCOUNTS_STATE_FILE", state_path)
    monkeypatch.setenv(
        "CODEX_ACCOUNTS",
        json.dumps([
            {"refresh": "r0"},
            {"refresh": "r1"},
        ]),
    )

    cpa._accounts = []
    cpa._active_idx = 0

    initial = {
        "active_idx": 0,
        "accounts": [
            {"access": "", "refresh": "r0", "expires": 0, "cooldown_until": 0, "dead": False, "last_429_at": 0, "request_timestamps": []},
            {"access": "", "refresh": "r1", "expires": 0, "cooldown_until": 0, "dead": False, "last_429_at": 0, "request_timestamps": []},
        ],
    }
    state_path.write_text(json.dumps(initial), encoding="utf-8")

    statuses = cpa.get_accounts_status(force_reload=True)
    assert statuses[1]["has_access"] is False
    assert statuses[1]["active"] is False

    recent = time.time() - 60

    updated = {
        "active_idx": 1,
        "accounts": [
            {"access": "", "refresh": "r0", "expires": 0, "cooldown_until": 0, "dead": False, "last_429_at": 0, "request_timestamps": []},
            {"access": "token-1", "refresh": "r1", "expires": 1234567890, "cooldown_until": 0, "dead": False, "last_429_at": 0, "request_timestamps": [recent, recent + 1, recent + 2]},
        ],
    }
    state_path.write_text(json.dumps(updated), encoding="utf-8")

    stale_statuses = cpa.get_accounts_status(force_reload=False)
    assert stale_statuses[1]["has_access"] is False
    assert stale_statuses[1]["active"] is False

    fresh_statuses = cpa.get_accounts_status(force_reload=True)
    assert fresh_statuses[1]["has_access"] is True
    assert fresh_statuses[1]["active"] is True
    assert fresh_statuses[1]["requests_7d"] == 3


def test_bootstrap_refresh_missing_access_tokens(monkeypatch, tmp_path):
    from ouroboros import codex_proxy_accounts as cpa

    state_path = tmp_path / "codex_accounts_state.json"
    monkeypatch.setattr(cpa, "ACCOUNTS_STATE_FILE", state_path)
    monkeypatch.setenv(
        "CODEX_ACCOUNTS",
        json.dumps([
            {"refresh": "r0"},
            {"access": "already", "refresh": "r1", "expires": time.time() + 7200},
            {"refresh": "r2"},
        ]),
    )

    cpa._accounts = []
    cpa._active_idx = 0

    initial = {
        "active_idx": 0,
        "accounts": [
            {"access": "", "refresh": "r0", "expires": 0, "cooldown_until": 0, "dead": False, "last_429_at": 0, "request_timestamps": []},
            {"access": "already", "refresh": "r1", "expires": time.time() + 7200, "cooldown_until": 0, "dead": False, "last_429_at": 0, "request_timestamps": []},
            {"access": "", "refresh": "r2", "expires": 0, "cooldown_until": 0, "dead": False, "last_429_at": 0, "request_timestamps": []},
        ],
    }
    state_path.write_text(json.dumps(initial), encoding="utf-8")

    calls = []

    def fake_refresh(acc, idx, auth_endpoint, urlopen):
        calls.append(idx)
        if idx == 0:
            acc["access"] = "fresh-0"
            acc["expires"] = time.time() + 7200
            cpa._save_accounts_state(cpa._accounts)
            return "fresh-0"
        return ""

    monkeypatch.setattr(cpa, "_refresh_account", fake_refresh)

    result = cpa.bootstrap_refresh_missing_access_tokens("https://auth.example/token", object())

    assert result["total"] == 3
    assert result["refreshed"] == [0]
    assert result["failed"] == [2]
    assert result["skipped"] == [1]
    assert calls == [0, 2]

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["accounts"][0]["access"] == "fresh-0"
    assert persisted["accounts"][2]["access"] == ""

    statuses = cpa.get_accounts_status(force_reload=True)
    assert statuses[0]["has_access"] is True
    assert statuses[1]["has_access"] is True
    assert statuses[2]["has_access"] is False



def test_classify_codex_http_failure_distinguishes_usage_limit_from_auth() -> None:
    from ouroboros import codex_proxy_accounts as cpa

    limited = cpa.classify_codex_http_failure(
        429,
        headers={
            "x-codex-secondary-used-percent": "100",
            "x-codex-primary-used-percent": "0",
        },
        body='{"error_code":"usage_limit_reached","message":"The usage limit has been reached"}',
    )
    assert limited["category"] == "rate_limit"
    assert limited["reason"] == "usage_limit_reached"
    assert limited["secondary_used_percent"] == "100"

    auth = cpa.classify_codex_http_failure(
        401,
        headers={},
        body='{"message":"Unauthorized"}',
    )
    assert auth["category"] == "auth"
    assert auth["reason"] == "unauthorized"



def test_get_accounts_status_exposes_last_error_and_compat_aliases(monkeypatch) -> None:
    from ouroboros import codex_proxy_accounts as cpa

    now = 1_700_000_000.0
    monkeypatch.setattr(cpa.time, "time", lambda: now)
    cpa._accounts = [{
        "access": "acc",
        "refresh": "ref",
        "expires": now + 3600,
        "cooldown_until": now + 120,
        "dead": False,
        "last_429_at": now - 10,
        "request_timestamps": [now - 100, now - 200],
        "quota": {"primary_used_percent": 12, "secondary_used_percent": 34},
        "last_error": {
            "status_code": 429,
            "category": "rate_limit",
            "reason": "usage_limit_reached",
        },
    }]
    cpa._active_idx = 0

    statuses = cpa.get_accounts_status(force_reload=False)
    assert len(statuses) == 1
    entry = statuses[0]
    assert entry["active"] is True
    assert entry["is_active"] is True
    assert entry["requests_5h"] == 2
    assert entry["usage_5h"] == 2
    assert entry["requests_7d"] == 2
    assert entry["usage_7d"] == 2
    assert entry["quota_5h_used_pct"] == 12
    assert entry["quota_7d_used_pct"] == 34
    assert entry["last_error_category"] == "rate_limit"
    assert entry["last_error_reason"] == "usage_limit_reached"
    assert entry["last_error_status_code"] == 429
