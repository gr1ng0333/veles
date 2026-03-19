import json

from ouroboros.tools.browser_session_actions import _browser_run_actions, get_tools
from ouroboros.tools.browser_tool_defs import _browser_run_actions_schema
from ouroboros.tools.registry import ToolContext


class DummyLocator:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    @property
    def first(self):
        return self

    def inner_text(self, timeout=0):
        self._page.calls.append(("inner_text", self._selector, timeout))
        if self._selector not in self._page.visible:
            raise RuntimeError(f"missing selector for text: {self._selector}")
        return self._page.texts.get(self._selector, "")


class DummyPage:
    def __init__(self):
        self.url = "https://example.com/login"
        self.calls = []
        self.visible = {"#ready"}
        self.texts = {"#ready": "Ready"}
        self._timeouts = {}

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

    def screenshot(self, type="png", full_page=False):
        self.calls.append(("screenshot", type, full_page))
        return b"png-bytes"

    def wait_for_selector(self, selector, timeout=0, state="visible"):
        self.calls.append(("wait_for_selector", selector, timeout, state))
        if selector not in self.visible:
            raise RuntimeError(f"missing selector: {selector}")

    def wait_for_timeout(self, timeout):
        self.calls.append(("wait_for_timeout", timeout))
        for selector, states in list(self._timeouts.items()):
            if states:
                self.texts[selector] = states.pop(0)
                if not states:
                    del self._timeouts[selector]

    def schedule_text_updates(self, selector, values):
        self._timeouts[selector] = list(values)

    def goto(self, url, timeout=0, wait_until="load"):
        self.calls.append(("goto", url, timeout, wait_until))
        self.url = url
        if url.endswith("/dashboard"):
            self.visible.add("#dashboard")

    def wait_for_url(self, pattern, timeout=0):
        self.calls.append(("wait_for_url", pattern, timeout))
        if not self.url:
            raise RuntimeError("missing url")

    def wait_for_function(self, expression, arg=None, timeout=0):
        self.calls.append(("wait_for_function", expression, arg, timeout))
        if self.url == arg:
            raise RuntimeError("navigation did not change URL")

    def locator(self, selector):
        self.calls.append(("locator", selector))
        return DummyLocator(self, selector)


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


def test_browser_run_actions_schema_matches_shared_definition():
    tool_entry = get_tools()[0]
    assert tool_entry.name == "browser_run_actions"
    assert tool_entry.schema == _browser_run_actions_schema()


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
    assert payload["results"][0]["checks"]["wait_for_navigation"]["previous_url"].endswith("/login")
    assert ("goto", "https://example.com/dashboard", 5000, "domcontentloaded") in page.calls
    assert not any(call[0] == "wait_for_url" for call in page.calls)


def test_browser_run_actions_can_extract_and_assert_text(monkeypatch):
    page = DummyPage()
    page.visible.update({"#flash", "#headline"})
    page.texts.update({"#flash": "Welcome back, user", "#headline": "Dashboard"})
    ctx = make_ctx(page)

    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "extract_text", "selector": "#headline"},
        {"action": "assert_text", "selector": "#flash", "value": "Welcome back"},
        {"action": "assert_text", "selector": "#flash", "value": "Error", "text_must_absent": True},
    ]))

    assert payload["success"] is True
    assert payload["results"][0]["text"] == "Dashboard"
    assert payload["results"][1]["checks"]["assert_text"]["matched"] is True
    assert payload["results"][2]["checks"]["assert_text"]["text_must_absent"] is True


def test_browser_run_actions_fails_on_exact_text_mismatch(monkeypatch):
    page = DummyPage()
    page.visible.add("#flash")
    page.texts["#flash"] = "Welcome back, user"
    ctx = make_ctx(page)

    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "assert_text", "selector": "#flash", "value": "Welcome back", "match_substring": False},
        {"action": "fill", "selector": "#after", "value": "x"},
    ]))

    assert payload["success"] is False
    assert payload["stopped_early"] is True
    assert payload["executed_steps"] == 1
    assert payload["results"][0]["checks"]["assert_text"]["matched"] is False


def test_browser_run_actions_waits_for_text(monkeypatch):
    page = DummyPage()
    page.visible.add("#status")
    page.texts["#status"] = "Loading"
    page.schedule_text_updates("#status", ["Still loading", "Done"])
    ctx = make_ctx(page)

    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "wait_for_text", "selector": "#status", "value": "Done", "timeout": 1000},
    ]))

    assert payload["success"] is True
    assert payload["results"][0]["wait_for_text_matched"] is True
    assert payload["results"][0]["checks"]["wait_for_text"]["matched"] is True
    assert ("wait_for_timeout", 100) in page.calls


def test_browser_run_actions_waits_for_url(monkeypatch):
    page = DummyPage()
    ctx = make_ctx(page)

    page.wait_for_timeout = lambda timeout: (page.calls.append(("wait_for_timeout", timeout)), setattr(page, "url", "https://example.com/dashboard?tab=home"))[-1]

    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "wait_for_url", "value": "/dashboard", "timeout": 1000},
    ]))

    assert payload["success"] is True
    assert payload["results"][0]["wait_for_url_matched"] is True
    assert payload["results"][0]["checks"]["wait_for_url"]["matched"] is True
    assert payload["results"][0]["checks"]["wait_for_url"]["url"].endswith("/dashboard?tab=home")


def test_browser_run_actions_waits_for_text_absence(monkeypatch):
    page = DummyPage()
    page.visible.add("#status")
    page.texts["#status"] = "Saving changes"
    page.schedule_text_updates("#status", ["Still saving", "Saved"])
    ctx = make_ctx(page)

    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "wait_for_text", "selector": "#status", "value": "Saving", "timeout": 1000, "text_must_absent": True},
    ]))

    assert payload["success"] is True
    assert payload["results"][0]["checks"]["wait_for_text"]["matched"] is True


def test_browser_run_actions_wait_for_text_absence_survives_selector_disappearance(monkeypatch):
    page = DummyPage()
    page.visible.add("#status")
    page.texts["#status"] = "Saving changes"
    ctx = make_ctx(page)

    page.wait_for_timeout = lambda timeout: (page.calls.append(("wait_for_timeout", timeout)), page.visible.discard("#status"), page.texts.pop("#status", None))[-1]

    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "wait_for_text", "selector": "#status", "value": "Saving", "timeout": 1000, "text_must_absent": True},
    ]))

    assert payload["success"] is True
    assert payload["results"][0]["wait_for_text_matched"] is True
    assert payload["results"][0]["checks"]["wait_for_text"]["matched"] is True
    assert payload["results"][0]["checks"]["wait_for_text"]["text"] == ""
    assert "missing selector" in payload["results"][0]["checks"]["wait_for_text"]["note"]



def test_browser_run_actions_wait_for_supports_hidden_state(monkeypatch):
    page = DummyPage()
    page.visible.add("#toast")
    ctx = make_ctx(page)

    hide_after_wait = lambda timeout: (page.calls.append(("wait_for_timeout", timeout)), page.visible.discard("#toast"))[-1]

    original_wait_for_selector = page.wait_for_selector

    def stateful_wait_for_selector(selector, timeout=0, state="visible"):
        page.calls.append(("wait_for_selector", selector, timeout, state))
        if state == "hidden":
            if selector in page.visible:
                hide_after_wait(100)
            if selector in page.visible:
                raise RuntimeError(f"selector still visible: {selector}")
            return
        return original_wait_for_selector(selector, timeout=timeout, state=state)

    page.wait_for_selector = stateful_wait_for_selector
    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "wait_for", "selector": "#toast", "timeout": 1000, "wait_for_state": "hidden"},
    ]))

    assert payload["success"] is True
    assert payload["results"][0]["wait_for_state"] == "hidden"
    assert ("wait_for_selector", "#toast", 1000, "hidden") in page.calls


def test_browser_run_actions_wait_for_supports_detached_state(monkeypatch):
    page = DummyPage()
    page.visible.add("#loading")
    ctx = make_ctx(page)

    original_wait_for_selector = page.wait_for_selector

    def stateful_wait_for_selector(selector, timeout=0, state="visible"):
        page.calls.append(("wait_for_selector", selector, timeout, state))
        if state == "detached":
            page.visible.discard(selector)
            return
        return original_wait_for_selector(selector, timeout=timeout, state=state)

    page.wait_for_selector = stateful_wait_for_selector
    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "wait_for", "selector": "#loading", "timeout": 1000, "wait_for_state": "detached"},
    ]))

    assert payload["success"] is True
    assert payload["results"][0]["wait_for_state"] == "detached"
    assert ("wait_for_selector", "#loading", 1000, "detached") in page.calls


def test_browser_run_actions_supports_hidden_expect_selector_state(monkeypatch):
    page = DummyPage()
    page.visible.add("#toast")
    ctx = make_ctx(page)

    def stateful_wait_for_selector(selector, timeout=0, state="visible"):
        page.calls.append(("wait_for_selector", selector, timeout, state))
        if selector == "#dismiss" and state == "visible":
            return
        if selector == "#toast" and state == "hidden":
            page.visible.discard("#toast")
            return
        if selector not in page.visible and state in {"visible", "attached"}:
            raise RuntimeError(f"missing selector: {selector}")

    page.wait_for_selector = stateful_wait_for_selector
    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "click", "selector": "#dismiss", "expect_selector": "#toast", "expect_selector_state": "hidden"},
    ]))

    assert payload["success"] is True
    assert payload["results"][0]["checks"]["expect_selector"]["matched"] is True
    assert payload["results"][0]["checks"]["expect_selector"]["state"] == "hidden"
    assert ("wait_for_selector", "#toast", 5000, "hidden") in page.calls


def test_browser_run_actions_rejects_invalid_expect_selector_state(monkeypatch):
    page = DummyPage()
    ctx = make_ctx(page)
    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    result = _browser_run_actions(ctx, actions=[
        {"action": "click", "selector": "#submit", "expect_selector": "#toast", "expect_selector_state": "gone"},
    ])

    assert result == "Error: action #1 has unsupported expect_selector_state 'gone'"


def test_browser_run_actions_rejects_invalid_wait_for_state(monkeypatch):
    page = DummyPage()
    ctx = make_ctx(page)
    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    result = _browser_run_actions(ctx, actions=[
        {"action": "wait_for", "selector": "#ready", "wait_for_state": "gone"},
    ])

    assert result == "Error: action #1 has unsupported wait_for_state 'gone'"

def test_browser_run_actions_can_capture_screenshot(monkeypatch):
    page = DummyPage()
    ctx = make_ctx(page)

    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "screenshot", "label": "capture current state"},
    ]))

    assert payload["success"] is True
    assert payload["results"][0]["screenshot_captured"] is True
    assert payload["results"][0]["last_screenshot_updated"] is True
    assert payload["results"][0]["screenshot_base64_bytes"] > 0
    assert "__last_screenshot__" in payload["results"][0]["screenshot_delivery_hint"]
    assert ctx.browser_state.last_screenshot_b64
    assert ("screenshot", "png", False) in page.calls


def test_browser_run_actions_fails_when_navigation_url_does_not_change(monkeypatch):
    page = DummyPage()
    ctx = make_ctx(page)

    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "wait_for", "timeout": 50, "wait_for_navigation": True},
        {"action": "fill", "selector": "#after", "value": "x"},
    ]))

    assert payload["success"] is False
    assert payload["stopped_early"] is True
    assert payload["executed_steps"] == 1
    assert payload["results"][0]["checks"]["wait_for_navigation"]["matched"] is False
    assert payload["results"][0]["checks"]["wait_for_navigation"]["previous_url"].endswith('/login')
    assert payload["results"][0]["checks"]["wait_for_navigation"]["current_url"].endswith('/login')
    assert any(call[0] == "wait_for_function" for call in page.calls)


def test_browser_run_actions_fails_when_wait_for_url_never_matches(monkeypatch):
    page = DummyPage()
    ctx = make_ctx(page)

    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "wait_for_url", "value": "/dashboard", "timeout": 50},
        {"action": "fill", "selector": "#after", "value": "x"},
    ]))

    assert payload["success"] is False
    assert payload["stopped_early"] is True
    assert payload["executed_steps"] == 1
    assert payload["results"][0]["checks"]["wait_for_url"]["matched"] is False
    assert payload["results"][0]["checks"]["wait_for_url"]["url"].endswith('/login')



def test_browser_run_actions_waits_for_url_absence_with_explicit_flag(monkeypatch):
    page = DummyPage()
    page.url = "https://example.com/dashboard/loading"
    ctx = make_ctx(page)

    page.wait_for_timeout = lambda timeout: (page.calls.append(("wait_for_timeout", timeout)), setattr(page, "url", "https://example.com/dashboard"))[-1]

    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "wait_for_url", "value": "loading", "timeout": 1000, "url_must_absent": True},
    ]))

    assert payload["success"] is True
    assert payload["results"][0]["url_must_absent"] is True
    assert payload["results"][0]["checks"]["wait_for_url"]["matched"] is True
    assert payload["results"][0]["checks"]["wait_for_url"]["url_must_absent"] is True
    assert payload["results"][0]["checks"]["wait_for_url"]["url"].endswith('/dashboard')



def test_browser_run_actions_supports_absent_expect_url_substring(monkeypatch):
    page = DummyPage()
    page.url = "https://example.com/logout"
    ctx = make_ctx(page)

    page.click = lambda selector, timeout=0: (page.calls.append(("click", selector, timeout)), setattr(page, "url", "https://example.com/login"))[-1]

    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "click", "selector": "#logout", "expect_url_substring": "/logout", "expect_url_must_absent": True},
    ]))

    assert payload["success"] is True
    assert payload["results"][0]["checks"]["expect_url_substring"]["matched"] is True
    assert payload["results"][0]["checks"]["expect_url_substring"]["must_absent"] is True
    assert payload["results"][0]["checks"]["expect_url_substring"]["url"].endswith('/login')


def test_browser_run_actions_fails_absent_expect_url_substring_when_substring_remains(monkeypatch):
    page = DummyPage()
    page.url = "https://example.com/logout"
    ctx = make_ctx(page)

    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "wait_for", "timeout": 10, "expect_url_substring": "/logout", "expect_url_must_absent": True},
        {"action": "fill", "selector": "#after", "value": "x"},
    ]))

    assert payload["success"] is False
    assert payload["stopped_early"] is True
    assert payload["executed_steps"] == 1
    assert payload["results"][0]["checks"]["expect_url_substring"]["matched"] is False
    assert payload["results"][0]["checks"]["expect_url_substring"]["must_absent"] is True
    assert payload["results"][0]["checks"]["expect_url_substring"]["url"].endswith('/logout')


def test_browser_run_actions_wait_for_url_absence_keeps_legacy_text_flag(monkeypatch):
    page = DummyPage()
    page.url = "https://example.com/dashboard/loading"
    ctx = make_ctx(page)

    page.wait_for_timeout = lambda timeout: (
        page.calls.append(("wait_for_timeout", timeout)),
        setattr(page, "url", "https://example.com/dashboard"),
    )[-1]

    monkeypatch.setattr('ouroboros.tools.browser_session_actions._ensure_browser', lambda _ctx: page)

    payload = json.loads(_browser_run_actions(ctx, actions=[
        {"action": "wait_for_url", "value": "loading", "timeout": 1000, "text_must_absent": True},
    ]))

    assert payload["success"] is True
    assert payload["results"][0]["url_must_absent"] is True
    assert payload["results"][0]["checks"]["wait_for_url"]["matched"] is True
    assert payload["results"][0]["checks"]["wait_for_url"]["url_must_absent"] is True
