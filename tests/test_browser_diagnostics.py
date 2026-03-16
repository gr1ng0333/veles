import pathlib
from unittest.mock import MagicMock

from ouroboros.tools.browser_diagnostics import capture_browser_failure_diagnostics, classify_browser_failure
from ouroboros.tools.browser import _browse_page, _browser_action
from ouroboros.tools.registry import ToolContext, BrowserState


class DummyLocator:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count


class DummyPage:
    def __init__(self, *, url='https://example.com/login', title='Just a moment...', ready_state='complete', text='Verify you are human', html='<html><body>Verify you are human</body></html>', body_child_count=1, has_app_root=False, script_count=1):
        self.url = url
        self._title = title
        self._ready_state = ready_state
        self._text = text
        self._html = html
        self._body_child_count = body_child_count
        self._has_app_root = has_app_root
        self._script_count = script_count
        self._selectors = {}

    def title(self):
        return self._title

    def inner_text(self, selector):
        assert selector == 'body'
        return self._text

    def content(self):
        return self._html

    def evaluate(self, script, arg=None):
        if script == "() => document.readyState":
            return self._ready_state
        if 'bodyChildCount' in script:
            return {
                'bodyChildCount': self._body_child_count,
                'hasRoot': self._has_app_root,
                'scriptCount': self._script_count,
            }
        if script == "(sel) => !!document.querySelector(sel)":
            return self._selectors.get(arg, False)
        raise AssertionError(f'unexpected evaluate: {script!r}')

    def locator(self, selector):
        return DummyLocator(1 if self._selectors.get(selector, False) else 0)

    def screenshot(self, type='png', full_page=False):
        assert type == 'png'
        return b'fake-png-bytes' * 8

    def goto(self, url, timeout, wait_until):
        self.url = url
        return None

    def wait_for_selector(self, selector, timeout):
        raise TimeoutError(f'Timeout {timeout}ms waiting for selector {selector}')

    def click(self, selector, timeout):
        raise RuntimeError('another element would receive the click')


def _ctx(tmp_path):
    ctx = ToolContext(repo_dir=pathlib.Path('/opt/veles'), drive_root=tmp_path)
    ctx.browser_state = BrowserState()
    ctx.task_id = 'browser-diag-test'
    return ctx


def test_classify_browser_failure_timeout_variants():
    timeout_result = classify_browser_failure(
        message='Timeout 5000ms waiting for selector .card',
        final_url='https://example.com',
        title='Example',
        ready_state='complete',
        visible_text='Hello world',
        dom_size=500,
        selector_waited='.card',
    )
    hydration_result = classify_browser_failure(
        message='Timeout 5000ms waiting for selector .product-card',
        final_url='https://shop.example.com',
        title='Shop',
        ready_state='complete',
        visible_text='Loading',
        dom_size=900,
        selector_waited='.product-card',
        body_child_count=3,
        has_app_root=True,
        script_count=7,
    )
    assert timeout_result['probable_failure_class'] == 'timeout_wait_selector'
    assert hydration_result['probable_failure_class'] == 'hydration_incomplete'


def test_capture_browser_failure_diagnostics_writes_artifacts(tmp_path):
    ctx = _ctx(tmp_path)
    page = DummyPage()
    diag = capture_browser_failure_diagnostics(
        ctx,
        page=page,
        operation='browse_page',
        selector_waited='.results',
        attempted_selectors=['.results', '.card'],
        exception=TimeoutError('Timeout 5000ms waiting for selector .results'),
    )
    assert diag['probable_failure_class'] in {'blocked_or_challenge_page', 'anti_bot_suspected'}
    assert pathlib.Path(diag['artifacts']['html_snapshot']).exists()
    assert pathlib.Path(diag['artifacts']['text_snapshot']).exists()
    assert pathlib.Path(diag['artifacts']['attempts']).exists()
    assert pathlib.Path(diag['artifacts']['screenshot']).exists()
    assert ctx.browser_state.last_screenshot_b64
    assert ctx.browser_state.last_failure_diagnostics == diag


def test_browse_page_returns_diagnostic_message_on_failure(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    page = DummyPage(title='Catalog', text='Loading', html='<html><body><div id="root">Loading</div></body></html>', body_child_count=1, has_app_root=True, script_count=5)
    monkeypatch.setattr('ouroboros.tools.browser._ensure_browser', lambda _ctx: page)

    result = _browse_page(ctx, url='https://example.com/catalog', wait_for='.product-card', timeout=1000)

    assert 'Browser failure [' in result
    assert 'selector_waited=' in result
    assert ctx.browser_state.last_failure_diagnostics is not None


def test_browser_action_returns_diagnostic_message_on_click_intercept(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    page = DummyPage(title='Checkout', text='Buy now', html='<html><body><button id="buy">Buy</button></body></html>')
    monkeypatch.setattr('ouroboros.tools.browser._ensure_browser', lambda _ctx: page)

    result = _browser_action(ctx, action='click', selector='#buy', timeout=1000)

    assert 'interaction_intercepted' in result
    assert ctx.browser_state.last_failure_diagnostics['probable_failure_class'] == 'interaction_intercepted'
