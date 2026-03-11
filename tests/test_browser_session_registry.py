import json
from pathlib import Path
from types import SimpleNamespace

from ouroboros.tools import browser as browser_mod
from ouroboros.tools import browser_persisted_sessions as persisted_mod
from ouroboros.tools.browser_session_registry import (
    OWNER_ONLY_SCOPE,
    SESSION_STATUS_FRESH,
    SESSION_STATUS_STALE,
    build_persisted_session_record,
    build_session_registry_key,
    get_persisted_session,
    mark_persisted_session_checked,
    upsert_persisted_session,
    validate_owner_authorized_scope,
)


class DummyPage:
    def __init__(self, url: str = "https://example.test/dashboard"):
        self.url = url
        self.goto_calls = []

    def goto(self, url, timeout=0, wait_until=""):
        self.url = url
        self.goto_calls.append({"url": url, "timeout": timeout, "wait_until": wait_until})


class DummyContext:
    def __init__(self, state):
        self._state = state

    def storage_state(self):
        return self._state


class DummyBrowserState:
    def __init__(self, state):
        self.context = DummyContext(state)
        self.page = DummyPage()
        self.saved_sessions = {}
        self.active_session_name = None
        self.browser = object()
        self.last_screenshot_b64 = None


class DummyToolContext:
    def __init__(self, tmp_path: Path, state=None):
        self._tmp_path = tmp_path
        self.browser_state = DummyBrowserState(state or {"cookies": [{"name": "sid"}], "origins": [{"origin": "https://example.test"}]})

    def drive_path(self, rel: str) -> Path:
        return self._tmp_path / rel



def test_registry_roundtrip_and_key_derivation(tmp_path):
    ctx = DummyToolContext(tmp_path)
    key = build_session_registry_key(site_profile={"domain": "labs.example.edu"}, account_label="Main Student")
    record = build_persisted_session_record(
        key=key,
        storage_state={"cookies": [{"name": "sid"}], "origins": []},
        cookies_count=1,
        origins_count=0,
        current_url="https://labs.example.edu/app",
        site_profile={"domain": "labs.example.edu", "site_name": "Labs"},
        account_label="Main Student",
        owner_authorized=True,
        account_scope=OWNER_ONLY_SCOPE,
    )
    upsert_persisted_session(ctx, key=key, record=record)

    loaded = get_persisted_session(ctx, key=key)
    assert loaded is not None
    assert loaded["site_key"] == "labs-example-edu"
    assert loaded["account_key"] == "main-student"
    assert loaded["session_status"] == SESSION_STATUS_FRESH



def test_validate_owner_authorized_scope_rejects_non_owner_scope():
    assert validate_owner_authorized_scope(owner_authorized=False, account_scope=OWNER_ONLY_SCOPE)
    assert validate_owner_authorized_scope(owner_authorized=True, account_scope="shared")
    assert validate_owner_authorized_scope(owner_authorized=True, account_scope=OWNER_ONLY_SCOPE) is None



def test_mark_persisted_session_checked_updates_status(tmp_path):
    ctx = DummyToolContext(tmp_path)
    key = build_session_registry_key(site_profile={"domain": "labs.example.edu"}, account_label="main")
    record = build_persisted_session_record(
        key=key,
        storage_state={"cookies": [{"name": "sid"}], "origins": []},
        cookies_count=1,
        origins_count=0,
        current_url="https://labs.example.edu/app",
        site_profile={"domain": "labs.example.edu"},
        account_label="main",
        owner_authorized=True,
        account_scope=OWNER_ONLY_SCOPE,
    )
    upsert_persisted_session(ctx, key=key, record=record)
    updated = mark_persisted_session_checked(ctx, key=key, alive=False, check_details={"checked": True, "alive": False})
    assert updated is not None
    assert updated["session_status"] == SESSION_STATUS_STALE
    assert updated["check_details"]["alive"] is False



def test_browser_persisted_session_handlers(tmp_path, monkeypatch):
    ctx = DummyToolContext(tmp_path)

    monkeypatch.setattr(persisted_mod, "_ensure_browser", lambda c: c.browser_state.page)
    monkeypatch.setattr(persisted_mod, "_replace_browser_context", lambda c, storage_state=None: DummyPage("https://labs.example.edu/home"))
    monkeypatch.setattr(persisted_mod, "_check_session_alive_via_protected_url", lambda c, protected_url, timeout=5000: {"checked": True, "alive": True, "url": protected_url, "status": 200})

    saved = json.loads(persisted_mod._browser_persist_session(
        ctx,
        account_label="student-main",
        site_profile={"domain": "labs.example.edu", "site_name": "Labs"},
        owner_authorized=True,
        account_scope=OWNER_ONLY_SCOPE,
    ))
    assert saved["saved"] is True
    assert saved["site_key"] == "labs-example-edu"

    meta = json.loads(persisted_mod._browser_get_persisted_session(
        ctx,
        account_label="student-main",
        site_profile={"domain": "labs.example.edu", "site_name": "Labs"},
    ))
    assert meta["found"] is True
    assert "storage_state" not in meta

    restored = json.loads(persisted_mod._browser_restore_persisted_session(
        ctx,
        account_label="student-main",
        site_profile={"domain": "labs.example.edu", "site_name": "Labs"},
        url="https://labs.example.edu/dashboard",
        protected_url="https://labs.example.edu/dashboard",
    ))
    assert restored["restored"] is True
    assert restored["session_status"] == SESSION_STATUS_FRESH
    assert restored["should_relogin"] is False



def test_browser_persist_session_requires_owner_scope(tmp_path, monkeypatch):
    ctx = DummyToolContext(tmp_path)
    monkeypatch.setattr(persisted_mod, "_ensure_browser", lambda c: c.browser_state.page)
    result = persisted_mod._browser_persist_session(
        ctx,
        account_label="student-main",
        site_profile={"domain": "labs.example.edu"},
        owner_authorized=False,
        account_scope=OWNER_ONLY_SCOPE,
    )
    assert result.startswith("Error:")
