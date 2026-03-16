import pathlib
from unittest.mock import MagicMock

from ouroboros.tools.browser_diagnostics import capture_browser_failure_diagnostics, classify_browser_failure
from ouroboros.tools.browser import _browse_page, _browser_action
from ouroboros.tools.browser_runtime import _stabilize_browser_page
from ouroboros.tools.registry import ToolContext, BrowserState


class DummyLocator:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count


class DummyPage:
    def __init__(self, *, url='https://example.com/login', title='Just a moment...', ready_state='complete', text='Verify you are human', html='<html><body>Verify you are human</body></html>', body_child_count=1, has_app_root=False, script_count=1, loading_placeholder_count=0, dynamic_stats=None):
        self.url = url
        self._title = title
        self._ready_state = ready_state
        self._text = text
        self._html = html
        self._body_child_count = body_child_count
        self._has_app_root = has_app_root
        self._script_count = script_count
        self._loading_placeholder_count = loading_placeholder_count
        self._selectors = {}
        self._dynamic_stats = list(dynamic_stats or [])

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
        if script == "(sel) => !!document.querySelector(sel)":
            return self._selectors.get(arg, False)
        if 'bodyChildCount' in script:
            return {
                'bodyChildCount': self._body_child_count,
                'hasRoot': self._has_app_root,
                'scriptCount': self._script_count,
            }
        if 'meaningful_text' in script and 'loading_placeholder_count' in script:
            if self._dynamic_stats:
                current = self._dynamic_stats.pop(0)
                self._ready_state = current.get('ready_state', self._ready_state)
                self._text = current.get('meaningful_text', self._text)
                self._html = current.get('html', self._html)
                self._loading_placeholder_count = current.get('loading_placeholder_count', self._loading_placeholder_count)
                self.url = current.get('url', self.url)
            normalized = ' '.join((self._text or '').split())
            return {
                'ready_state': self._ready_state,
                'title': self._title,
                'visible_text_size': len(normalized),
                'dom_size': len(self._html),
                'body_child_count': self._body_child_count,
                'meaningful_text': normalized[:5000],
                'has_meaningful_text': len(normalized) >= 120,
                'loading_placeholder_count': self._loading_placeholder_count,
                'url': self.url,
            }
        raise AssertionError(f'unexpected evaluate: {script!r}')

    def locator(self, selector):
        return DummyLocator(1 if self._selectors.get(selector, False) else 0)

    def screenshot(self, type='png', full_page=False):
        assert type == 'png'
        return b'fake-png-bytes' * 8

    def goto(self, url, timeout, wait_until):
        self.url = url
        return None

    def wait_for_selector(self, selector, timeout, state='attached'):
        if self._selectors.get(selector, False):
            return object()
        raise TimeoutError(f'Timeout {timeout}ms waiting for selector {selector}')

    def wait_for_timeout(self, timeout):
        return None

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



def test_stabilize_browser_page_uses_meaningful_content_fallback():
    page = DummyPage(
        title='Catalog',
        text='Loading',
        html='<html><body><div id="root">Loading</div></body></html>',
        has_app_root=True,
        script_count=6,
        dynamic_stats=[
            {'ready_state': 'interactive', 'meaningful_text': 'Loading', 'html': '<html><body><div id="root">Loading</div></body></html>', 'loading_placeholder_count': 2},
            {'ready_state': 'complete', 'meaningful_text': 'Каталог товаров ' * 20, 'html': '<html><body><main>Каталог товаров</main></body></html>', 'loading_placeholder_count': 0},
            {'ready_state': 'complete', 'meaningful_text': 'Каталог товаров ' * 20, 'html': '<html><body><main>Каталог товаров</main></body></html>', 'loading_placeholder_count': 0},
            {'ready_state': 'complete', 'meaningful_text': 'Каталог товаров ' * 20, 'html': '<html><body><main>Каталог товаров</main></body></html>', 'loading_placeholder_count': 0},
        ],
    )

    result = _stabilize_browser_page(page, read_mode='stable', selector='.product-card', timeout_ms=1000)

    assert result['selector_found'] is False
    assert result['fallback_used'] is True
    assert result['meaningful_content']['meaningful'] is True


def test_browse_page_stable_mode_returns_prefixed_output_on_fallback(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    page = DummyPage(
        url='https://example.com/catalog',
        title='Catalog',
        text='Loading',
        html='<html><body><div id="root">Loading</div></body></html>',
        has_app_root=True,
        script_count=5,
        dynamic_stats=[
            {'ready_state': 'interactive', 'meaningful_text': 'Loading', 'html': '<html><body><div id="root">Loading</div></body></html>', 'loading_placeholder_count': 2},
            {'ready_state': 'complete', 'meaningful_text': 'Полезный каталог ' * 20, 'html': '<html><body><main>Полезный каталог</main></body></html>', 'loading_placeholder_count': 0},
            {'ready_state': 'complete', 'meaningful_text': 'Полезный каталог ' * 20, 'html': '<html><body><main>Полезный каталог</main></body></html>', 'loading_placeholder_count': 0},
            {'ready_state': 'complete', 'meaningful_text': 'Полезный каталог ' * 20, 'html': '<html><body><main>Полезный каталог</main></body></html>', 'loading_placeholder_count': 0},
        ],
    )
    monkeypatch.setattr('ouroboros.tools.browser._ensure_browser', lambda _ctx: page)

    result = _browse_page(ctx, url='https://example.com/catalog', wait_for='.product-card', timeout=1000, read_mode='stable')

    assert result.startswith('[browser_read mode=stable')
    assert 'fallback_used=true' in result
    assert 'Полезный каталог' in result
