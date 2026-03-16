"""Playwright runtime and page extraction helpers for browser tools."""

from __future__ import annotations

import base64
import logging
import subprocess
import sys
import time
import threading
from typing import Any, Dict, Optional

try:
    from playwright_stealth import Stealth
    _HAS_STEALTH = True
except ImportError:
    _HAS_STEALTH = False

from ouroboros.tools.registry import BrowserState, ToolContext

log = logging.getLogger(__name__)

_playwright_ready = False
_pw_instance = None
_pw_thread_id = None


# ---------------------------------------------------------------------------
# BrowserSessionManager — persistent browser sessions across direct chat msgs
# ---------------------------------------------------------------------------

class BrowserSessionManager:
    """Module-level singleton managing long-lived browser sessions per chat_id.

    For direct-chat mode: browser/page/context survive across messages so
    cookies, sessions and page state are preserved.  Each chat_id also gets
    a dedicated single-thread executor to maintain Playwright thread-affinity.
    """

    _lock = threading.Lock()
    _sessions: Dict[int, Dict[str, Any]] = {}  # chat_id → {browser_state, executor, last_used}

    @classmethod
    def get_or_create(cls, chat_id: int) -> tuple:
        """Return (BrowserState, _StatefulToolExecutor) for *chat_id*, creating if needed.

        The executor wraps a single-worker ThreadPoolExecutor keeping Playwright
        calls on the same OS thread (greenlet affinity).
        """
        from ouroboros.loop import _StatefulToolExecutor

        with cls._lock:
            entry = cls._sessions.get(chat_id)
            if entry is not None:
                entry["last_used"] = time.monotonic()
                return entry["browser_state"], entry["executor"]
            bs = BrowserState()
            executor = _StatefulToolExecutor()
            cls._sessions[chat_id] = {
                "browser_state": bs,
                "executor": executor,
                "last_used": time.monotonic(),
            }
            return bs, executor

    @classmethod
    def touch(cls, chat_id: int) -> None:
        """Update last_used timestamp for *chat_id*."""
        with cls._lock:
            entry = cls._sessions.get(chat_id)
            if entry is not None:
                entry["last_used"] = time.monotonic()

    @classmethod
    def validate(cls, browser_state: BrowserState) -> str:
        """Check health of an existing BrowserState.

        Returns one of:
          "ok"              — page is alive
          "page_dead"       — page gone, but browser connected
          "context_dead"    — context gone, browser connected
          "browser_dead"    — browser disconnected or None
        """
        if browser_state.browser is None:
            return "browser_dead"
        try:
            if not browser_state.browser.is_connected():
                return "browser_dead"
        except Exception:
            return "browser_dead"
        if browser_state.context is None:
            return "context_dead"
        if browser_state.page is None:
            return "page_dead"
        try:
            # Probe liveliness — accessing url triggers comms with browser
            _ = browser_state.page.url
            return "ok"
        except Exception:
            return "page_dead"

    @classmethod
    def cleanup(cls, chat_id: int) -> None:
        """Explicitly close and remove session for *chat_id*."""
        with cls._lock:
            entry = cls._sessions.pop(chat_id, None)
        if entry is None:
            return
        bs = entry["browser_state"]
        executor = entry["executor"]
        # Close browser resources (best-effort)
        for obj_name in ("page", "context", "browser"):
            try:
                obj = getattr(bs, obj_name, None)
                if obj is not None:
                    obj.close()
            except Exception:
                log.debug("Failed to close %s during session cleanup for chat %s", obj_name, chat_id, exc_info=True)
        bs.page = None
        bs.context = None
        bs.browser = None
        bs.pw_instance = None
        bs.active_session_name = None
        # Shutdown the dedicated _StatefulToolExecutor
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            log.debug("Failed to shutdown executor for chat %s", chat_id, exc_info=True)

    @classmethod
    def cleanup_stale(cls, max_idle_seconds: float = 300) -> int:
        """Close sessions idle longer than *max_idle_seconds*.  Returns count closed."""
        now = time.monotonic()
        stale_ids: list = []
        with cls._lock:
            for cid, entry in cls._sessions.items():
                if now - entry["last_used"] > max_idle_seconds:
                    stale_ids.append(cid)
        closed = 0
        for cid in stale_ids:
            cls.cleanup(cid)
            closed += 1
            log.info("Cleaned up stale browser session for chat_id=%s (idle >%ss)", cid, max_idle_seconds)
        return closed

    @classmethod
    def has_session(cls, chat_id: int) -> bool:
        with cls._lock:
            return chat_id in cls._sessions

_MARKDOWN_JS = """() => {
    const walk = (el) => {
        let out = '';
        for (const child of el.childNodes) {
            if (child.nodeType === 3) {
                const t = child.textContent.trim();
                if (t) out += t + ' ' ;
            } else if (child.nodeType === 1) {
                const tag = child.tagName;
                if (['SCRIPT','STYLE','NOSCRIPT'].includes(tag)) continue;
                if (['H1','H2','H3','H4','H5','H6'].includes(tag))
                    out += '\n' + '#'.repeat(parseInt(tag[1])) + ' ' ;
                if (tag === 'P' || tag === 'DIV' || tag === 'BR') out += '\n';
                if (tag === 'LI') out += '\n- ' ;
                if (tag === 'A') out += '[';
                out += walk(child);
                if (tag === 'A') out += '](' + (child.href||'') + ')';
            }
        }
        return out;
    };
    return walk(document.body);
}"""


_READINESS_STATS_JS = """() => {
    const body = document.body;
    const text = body ? (body.innerText || '') : '';
    const normalized = text.replace(/\s+/g, ' ').trim();
    const domSize = document.documentElement ? document.documentElement.outerHTML.length : 0;
    const visibleTextSize = normalized.length;
    const bodyChildrenTotal = body ? body.children.length : 0;
    const title = document.title || '';
    const readyState = document.readyState || '';
    const placeholderSelectors = [
        '.skeleton', '.skeleton-loader', '.loading', '.loader', '.spinner', '[aria-busy="true"]',
        '[data-testid*="skeleton"]', '[class*="skeleton"]', '[class*="placeholder"]', '[class*="loading"]'
    ];
    let placeholderCount = 0;
    for (const sel of placeholderSelectors) {
        try {
            placeholderCount += document.querySelectorAll(sel).length;
        } catch (e) {}
    }
    return {
        ready_state: readyState,
        title,
        visible_text_size: visibleTextSize,
        dom_size: domSize,
        body_child_count: bodyChildrenTotal,
        meaningful_text: normalized.slice(0, 5000),
        has_meaningful_text: visibleTextSize >= 120,
        loading_placeholder_count: placeholderCount,
        url: window.location.href,
    };
}"""


def _safe_readiness_stats(page: Any) -> Dict[str, Any]:
    try:
        stats = page.evaluate(_READINESS_STATS_JS) or {}
    except Exception:
        stats = {}
    return {
        'ready_state': str(stats.get('ready_state') or ''),
        'title': str(stats.get('title') or ''),
        'visible_text_size': int(stats.get('visible_text_size') or 0),
        'dom_size': int(stats.get('dom_size') or 0),
        'body_child_count': int(stats.get('body_child_count') or 0),
        'meaningful_text': str(stats.get('meaningful_text') or ''),
        'has_meaningful_text': bool(stats.get('has_meaningful_text')),
        'loading_placeholder_count': int(stats.get('loading_placeholder_count') or 0),
        'url': str(stats.get('url') or getattr(page, 'url', '') or ''),
    }


def _stabilize_browser_page(
    page: Any,
    *,
    read_mode: str = 'quick',
    selector: str = '',
    timeout_ms: int = 5000,
) -> Dict[str, Any]:
    mode = (read_mode or 'quick').strip().lower()
    selector = (selector or '').strip()
    before_url = getattr(page, 'url', '') or ''
    initial_stats = _safe_readiness_stats(page)
    result: Dict[str, Any] = {
        'read_mode': mode,
        'initial_stats': initial_stats,
        'selector': selector,
        'selector_found': False,
        'selector_wait_error': '',
        'fallback_used': False,
        'url_change': None,
        'text_growth': None,
        'meaningful_content': None,
        'stability': None,
    }

    if selector:
        try:
            page.wait_for_selector(selector, timeout=min(timeout_ms, 1200), state='attached')
            result['selector_found'] = True
        except Exception as exc:
            result['selector_wait_error'] = str(exc)

    def _phase(kind: str, phase_timeout_ms: int, min_text_size: int = 120, require_stable: bool = False, allow_placeholders: bool = False) -> Dict[str, Any]:
        deadline = time.monotonic() + max(phase_timeout_ms, 0) / 1000.0
        best = _safe_readiness_stats(page)
        prev_signature = None
        stable_cycles = 0
        while time.monotonic() < deadline:
            current = _safe_readiness_stats(page)
            if current['visible_text_size'] >= best['visible_text_size']:
                best = current
            signature = (
                current['ready_state'],
                current['dom_size'],
                current['visible_text_size'],
                current['loading_placeholder_count'],
            )
            stable_cycles = stable_cycles + 1 if signature == prev_signature else 0
            prev_signature = signature
            changed = bool(current.get('url')) and current.get('url') != before_url
            grew = current['visible_text_size'] >= initial_stats['visible_text_size'] + 80
            meaningful = current['ready_state'] in {'interactive', 'complete'} and current['visible_text_size'] >= min_text_size
            placeholders_ok = allow_placeholders or current['loading_placeholder_count'] == 0
            stable = (not require_stable) or (current['ready_state'] == 'complete' and placeholders_ok and stable_cycles >= 1)
            if (kind == 'url_change' and changed) or (kind == 'text_growth' and grew) or (kind == 'meaningful' and meaningful and placeholders_ok) or (kind == 'stable' and meaningful and stable):
                return {
                    'ok': True,
                    'kind': kind,
                    'matched': True,
                    'stats': current,
                    'stable_cycles': stable_cycles + 1,
                    'changed': changed,
                    'grew': grew,
                    'meaningful': meaningful and placeholders_ok,
                }
            try:
                page.wait_for_timeout(200)
            except Exception:
                time.sleep(0.2)
        return {
            'ok': True,
            'kind': kind,
            'matched': False,
            'stats': best,
            'stable_cycles': stable_cycles,
            'changed': bool(best.get('url')) and best.get('url') != before_url,
            'grew': best['visible_text_size'] >= initial_stats['visible_text_size'] + 80,
            'meaningful': best['visible_text_size'] >= min_text_size and (allow_placeholders or best['loading_placeholder_count'] == 0),
        }

    if mode == 'quick':
        if not result['selector_found'] and selector:
            result['meaningful_content'] = _phase('meaningful', min(timeout_ms, 1500), allow_placeholders=True)
            result['fallback_used'] = bool(result['meaningful_content'].get('meaningful'))
        result['final_stats'] = _safe_readiness_stats(page)
        return result

    result['url_change'] = _phase('url_change', min(timeout_ms, 1200))
    result['text_growth'] = _phase('text_growth', min(timeout_ms, 1600))
    result['meaningful_content'] = _phase('meaningful', timeout_ms, allow_placeholders=False)
    result['stability'] = _phase('stable', timeout_ms, require_stable=True, allow_placeholders=False)
    final_stats = result['stability']['stats'] if result['stability'].get('matched') else result['meaningful_content']['stats']
    result['final_stats'] = final_stats
    if not result['selector_found'] and selector:
        result['fallback_used'] = bool(result['meaningful_content'].get('meaningful') or result['stability'].get('matched'))
    return result

def _ensure_playwright_installed():
    """Install Playwright and Chromium if not already available."""
    global _playwright_ready
    if _playwright_ready:
        return

    try:
        import playwright  # noqa: F401
    except ImportError:
        log.info("Playwright not found, installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright"])

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.executable_path
        log.info("Playwright chromium binary found")
    except Exception:
        log.info("Installing Playwright chromium binary...")
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        subprocess.check_call([sys.executable, "-m", "playwright", "install-deps", "chromium"])

    _playwright_ready = True


def _reset_playwright_greenlet():
    """
    Fully reset Playwright's greenlet state by purging all related modules.
    This is necessary because sync_playwright() uses greenlets internally,
    and once a greenlet dies, it cannot be reused across "threads".
    """
    global _pw_instance, _pw_thread_id

    log.info("Resetting Playwright greenlet state...")

    try:
        subprocess.run(["pkill", "-9", "-f", "chromium"], capture_output=True, timeout=5)
    except Exception:
        log.debug("Failed to kill chromium processes during reset", exc_info=True)
        pass

    mods_to_remove = [k for k in sys.modules.keys() if k.startswith('playwright')]
    for k in mods_to_remove:
        del sys.modules[k]

    mods_to_remove = [k for k in sys.modules.keys() if 'greenlet' in k.lower()]
    for k in mods_to_remove:
        try:
            del sys.modules[k]
        except Exception:
            log.debug(f"Failed to delete greenlet module {k} during reset", exc_info=True)
            pass

    _pw_instance = None
    _pw_thread_id = None
    log.info("Playwright greenlet state reset complete")


def _browser_context_options(storage_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    options: Dict[str, Any] = {
        "viewport": {"width": 1920, "height": 1080},
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    if storage_state:
        options["storage_state"] = storage_state
    return options


def _apply_stealth(page: Any) -> None:
    if _HAS_STEALTH:
        stealth = Stealth()
        stealth.apply_stealth_sync(page)


def _replace_browser_context(ctx: ToolContext, storage_state: Optional[Dict[str, Any]] = None) -> Any:
    try:
        if ctx.browser_state.page is not None:
            ctx.browser_state.page.close()
    except Exception:
        log.debug("Failed to close browser page during context replace", exc_info=True)
    try:
        if ctx.browser_state.context is not None:
            ctx.browser_state.context.close()
    except Exception:
        log.debug("Failed to close browser context during context replace", exc_info=True)

    ctx.browser_state.context = ctx.browser_state.browser.new_context(**_browser_context_options(storage_state))
    ctx.browser_state.page = ctx.browser_state.context.new_page()
    _apply_stealth(ctx.browser_state.page)
    ctx.browser_state.page.set_default_timeout(30000)
    return ctx.browser_state.page


def _ensure_browser(ctx: ToolContext):
    """Create or reuse browser for this task. Browser state lives in ctx,
    but Playwright instance is module-level to avoid greenlet issues.

    When a persistent BrowserState is attached (direct-chat mode) this
    performs tiered recovery instead of a full teardown:
      - ok            → reuse page as-is ("browser_session_reused")
      - page_dead     → new page in same context ("browser_page_recovered")
      - context_dead  → new context+page ("browser_context_recovered")
      - browser_dead  → full restart ("browser_full_restart")
    """
    global _pw_instance, _pw_thread_id

    current_thread_id = threading.get_ident()

    # --- Tiered recovery for existing browser state ---
    health = BrowserSessionManager.validate(ctx.browser_state)
    if health == "ok":
        log.debug("browser_session_reused (thread=%s)", current_thread_id)
        return ctx.browser_state.page
    if health == "page_dead" and ctx.browser_state.context is not None:
        log.info("browser_page_recovered — creating new page in existing context")
        try:
            ctx.browser_state.page = ctx.browser_state.context.new_page()
            _apply_stealth(ctx.browser_state.page)
            ctx.browser_state.page.set_default_timeout(30000)
            return ctx.browser_state.page
        except Exception:
            log.debug("Page recovery failed, falling through to context recovery", exc_info=True)
            health = "context_dead"
    if health == "context_dead" and ctx.browser_state.browser is not None:
        log.info("browser_context_recovered — creating new context+page (cookies lost)")
        try:
            if not ctx.browser_state.browser.is_connected():
                raise RuntimeError("browser disconnected")
            return _replace_browser_context(ctx)
        except Exception:
            log.debug("Context recovery failed, falling through to full restart", exc_info=True)
            health = "browser_dead"
    # browser_dead → full teardown + recreate
    if ctx.browser_state.browser is not None:
        log.info("browser_full_restart — previous browser is dead, cleaning up")
        cleanup_browser(ctx)

    # --- Thread-affinity check for module-level Playwright ---
    if _pw_instance is not None and _pw_thread_id != current_thread_id:
        log.info(f"Thread switch detected (old={_pw_thread_id}, new={current_thread_id}). Resetting Playwright...")
        _reset_playwright_greenlet()

    _ensure_playwright_installed()

    if _pw_instance is None:
        from playwright.sync_api import sync_playwright

        try:
            _pw_instance = sync_playwright().start()
            _pw_thread_id = current_thread_id
            log.info(f"Created Playwright instance in thread {_pw_thread_id}")
        except RuntimeError as e:
            if "cannot switch" in str(e) or "different thread" in str(e):
                _reset_playwright_greenlet()
                from playwright.sync_api import sync_playwright
                _pw_instance = sync_playwright().start()
                _pw_thread_id = current_thread_id
                log.info(f"Recreated Playwright instance in thread {_pw_thread_id} after error")
            else:
                raise

    ctx.browser_state.pw_instance = _pw_instance
    ctx.browser_state.browser = _pw_instance.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=site-per-process",
            "--window-size=1920,1080",
        ],
    )
    return _replace_browser_context(ctx)


def cleanup_browser(ctx: ToolContext) -> None:
    """Close browser and playwright. Called by agent.py in finally block.

    Note: We DON'T stop the module-level _pw_instance here to allow reuse
    across tasks. Only close the browser/page/context for this context.
    """
    global _pw_instance

    try:
        if ctx.browser_state.page is not None:
            ctx.browser_state.page.close()
    except Exception:
        log.debug("Failed to close browser page during cleanup", exc_info=True)
    try:
        if ctx.browser_state.context is not None:
            ctx.browser_state.context.close()
    except Exception:
        log.debug("Failed to close browser context during cleanup", exc_info=True)
    try:
        if ctx.browser_state.browser is not None:
            ctx.browser_state.browser.close()
    except Exception as e:
        if "cannot switch" in str(e) or "different thread" in str(e):
            log.warning("Browser cleanup hit thread error, resetting Playwright...")
            _reset_playwright_greenlet()

    ctx.browser_state.page = None
    ctx.browser_state.context = None
    ctx.browser_state.browser = None
    ctx.browser_state.pw_instance = None
    ctx.browser_state.active_session_name = None
    ctx.browser_state.last_failure_diagnostics = None


def _extract_page_output(page: Any, output: str, ctx: ToolContext) -> str:
    """Extract page content in the requested format."""
    if output == "screenshot":
        data = page.screenshot(type="png", full_page=False)
        b64 = base64.b64encode(data).decode()
        ctx.browser_state.last_screenshot_b64 = b64
        return (
            f"Screenshot captured ({len(b64)} bytes base64). "
            f"Call send_photo(image_base64='__last_screenshot__') to deliver it to the owner."
        )
    if output == "html":
        html = page.content()
        return html[:50000] + ("... [truncated]" if len(html) > 50000 else "")
    if output == "markdown":
        text = page.evaluate(_MARKDOWN_JS)
        return text[:30000] + ("... [truncated]" if len(text) > 30000 else "")
    text = page.inner_text("body")
    return text[:30000] + ("... [truncated]" if len(text) > 30000 else "")




def _post_submit_wait(page: Any, wait_ms: int = 1200) -> None:
    page.wait_for_timeout(wait_ms)


def _check_session_alive_via_protected_url(ctx: ToolContext, protected_url: str, timeout: int = 5000) -> Dict[str, Any]:
    if not protected_url:
        return {"checked": False, "alive": False, "reason": "no_url"}

    try:
        page = ctx.browser_state.page
        if not page or page.is_closed():
            return {"checked": False, "alive": False, "reason": "no_page"}

        probe_page = ctx.browser_state.context.new_page()
        try:
            resp = probe_page.goto(protected_url, timeout=timeout, wait_until="domcontentloaded")
            final_url = probe_page.url
            status = resp.status if resp else 0
            alive = (status < 400) and ("login" not in final_url.lower()) and ("signin" not in final_url.lower())
            return {
                "checked": True,
                "alive": alive,
                "url": final_url,
                "status": status,
            }
        finally:
            probe_page.close()
    except Exception as exc:
        return {"checked": True, "alive": False, "reason": str(exc)}
