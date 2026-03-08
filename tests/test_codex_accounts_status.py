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
