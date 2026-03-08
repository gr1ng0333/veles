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
