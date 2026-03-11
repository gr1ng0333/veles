import json

from ouroboros.tools.browser_session_actions import _browser_run_actions
from ouroboros.tools.registry import ToolContext


class DummyPage:
    def __init__(self):
        self.url = "https://example.com/login"
        self.calls = []
        self.visible = {"#ready"}

    def click(self, selector, timeout=0):
        self.calls.append(("click", selector, timeout))
        if selector == "#submit":
            self.url = "https://example.com/app"
            self.visible.add("#dashboard")

    def fill(self, selector, value, timeout=0):
        self.calls.append(("fill", selector, value, timeout))

    def select_option(self, selector, value, timeout=0):
        self.calls.append(("select", selector, value, timeout))

    def evaluate(self, js):
        self.calls.append(("evaluate", js))
        return {"ok": True, "script": js}

    def wait_for_selector(self, selector, timeout=0, state="visible"):
        self.calls.append(("wait_for_selector", selector, timeout, state))
        if selector not in self.visible:
            raise RuntimeError(f"missing selector: {selector}")

    def wait_for_timeout(self, timeout):
        self.calls.append(("wait_for_timeout", timeout))

    def goto(self, url, timeout=0, wait_until="load"):
        self.calls.append(("goto", url, timeout, wait_until))
        self.url = url
        if url.endswith("/dashboard"):
            self.visible.add("#dashboard")

    def wait_for_url(self, pattern, timeout=0):
        self.calls.append(("wait_for_url", pattern, timeout))
        if not self.url:
            raise RuntimeError("missing url")


class DummyBrowserState:
    def __init__(self, page):
        self.page = page
        self.browser = object()
        self.context = object()
        self.active_session_name = "example-session"


class DummyCtx(ToolContext):
    pass


def make_ctx(page):
    ctx = ToolContext(repo_dir='.', drive_root='.')
    ctx.browser_state = DummyBrowserState(page)
    return ctx


def test_browser_run_actions_executes_steps_and_verifies_progress(monkeypatch):
    page = DummyPage()
    ctx = make_ctx(page)

    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "fill", "selector": "#email", "value": "user@example.com"},
        {"action": "click", "selector": "#submit", "expect_selector": "#dashboard", "expect_url_substring": "/app"},
        {"action": "evaluate", "value": "() => document.title"},
    ]))

    assert payload["success"] is True
    assert payload["executed_steps"] == 3
    assert payload["current_url"].endswith('/app')
    assert payload["active_session_name"] == 'example-session'
    assert payload["results"][1]["checks"]["expect_selector"]["matched"] is True
    assert payload["results"][2]["evaluation_result"]["ok"] is True


def test_browser_run_actions_stops_early_on_failed_verification(monkeypatch):
    page = DummyPage()
    ctx = make_ctx(page)

    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "click", "selector": "#submit", "expect_selector": "#never-there"},
        {"action": "fill", "selector": "#after", "value": "x"},
    ]))

    assert payload["success"] is False
    assert payload["stopped_early"] is True
    assert payload["executed_steps"] == 1
    assert payload["results"][0]["success"] is False
    assert payload["results"][0]["checks"]["expect_selector"]["matched"] is False


def test_browser_run_actions_rejects_invalid_payload():
    ctx = make_ctx(DummyPage())
    result = _browser_run_actions(ctx, actions=[])
    assert result == 'Error: actions must be a non-empty array'


def test_browser_run_actions_supports_goto_and_navigation_wait(monkeypatch):
    page = DummyPage()
    ctx = make_ctx(page)

    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "goto", "value": "https://example.com/dashboard", "wait_until": "domcontentloaded", "wait_for_navigation": True, "expect_selector": "#dashboard", "expect_url_substring": "/dashboard"},
    ]))

    assert payload["success"] is True
    assert payload["current_url"].endswith('/dashboard')
    assert payload["results"][0]["navigated_to"].endswith('/dashboard')
    assert payload["results"][0]["checks"]["wait_for_navigation"]["matched"] is True
    assert ("goto", "https://example.com/dashboard", 5000, "domcontentloaded") in page.calls
    assert ("wait_for_url", "**", 5000) in page.calls
