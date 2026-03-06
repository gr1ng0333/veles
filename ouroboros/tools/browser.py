"""
Browser automation tools via Playwright (sync API).

Provides browse_page (open URL, get content/screenshot),
browser_action (click, fill, evaluate JS on current page),
and lightweight login helpers for common auth forms.

Browser state lives in ToolContext (per-task lifecycle),
not module-level globals — safe across threads.
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional

try:
    from playwright_stealth import Stealth
    _HAS_STEALTH = True
except ImportError:
    _HAS_STEALTH = False

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_playwright_ready = False
# Module-level Playwright instance to avoid greenlet threading issues
# Persists across ToolContext recreations but can be reset on error
_pw_instance = None
_pw_thread_id = None  # Track which thread owns the Playwright instance


_USERNAME_CANDIDATE_JS = r"""() => {
    const toInfo = (el, index, source, score) => {
        const form = el.form || el.closest('form');
        const attrs = [el.id, el.name, el.getAttribute('placeholder'), el.getAttribute('aria-label'), el.getAttribute('autocomplete')]
            .filter(Boolean)
            .join(' ')
            .toLowerCase();
        return {
            selector: window.__veles_build_selector(el, index),
            type: (el.type || '').toLowerCase(),
            form_selector: form ? window.__veles_build_selector(form, 0) : '',
            attrs,
            source,
            score,
        };
    };

    const inputs = Array.from(document.querySelectorAll('input'));
    return inputs
        .filter((el) => {
            const type = (el.type || 'text').toLowerCase();
            return ['text', 'email', 'tel'].includes(type) || !type;
        })
        .map((el, index) => {
            const attrs = [el.id, el.name, el.getAttribute('placeholder'), el.getAttribute('aria-label'), el.getAttribute('autocomplete')]
                .filter(Boolean)
                .join(' ')
                .toLowerCase();
            let score = 0;
            if (attrs.includes('email')) score += 6;
            if (attrs.includes('user')) score += 5;
            if (attrs.includes('login')) score += 5;
            if (attrs.includes('username')) score += 6;
            if ((el.getAttribute('autocomplete') || '').toLowerCase() === 'username') score += 8;
            if ((el.type || '').toLowerCase() === 'email') score += 7;
            if (!attrs.includes('search')) score += 1;
            return toInfo(el, index, 'heuristic', score);
        })
        .sort((a, b) => b.score - a.score);
}"""

_PASSWORD_CANDIDATE_JS = r"""() => {
    const toInfo = (el, index, source, score) => {
        const form = el.form || el.closest('form');
        const attrs = [el.id, el.name, el.getAttribute('placeholder'), el.getAttribute('aria-label'), el.getAttribute('autocomplete')]
            .filter(Boolean)
            .join(' ')
            .toLowerCase();
        return {
            selector: window.__veles_build_selector(el, index),
            type: (el.type || '').toLowerCase(),
            form_selector: form ? window.__veles_build_selector(form, 0) : '',
            attrs,
            source,
            score,
        };
    };

    const inputs = Array.from(document.querySelectorAll('input[type="password"], input[autocomplete="current-password"], input[autocomplete="new-password"]'));
    return inputs
        .map((el, index) => {
            const attrs = [el.id, el.name, el.getAttribute('placeholder'), el.getAttribute('aria-label'), el.getAttribute('autocomplete')]
                .filter(Boolean)
                .join(' ')
                .toLowerCase();
            let score = 10;
            if (attrs.includes('password')) score += 8;
            if ((el.getAttribute('autocomplete') || '').toLowerCase() === 'current-password') score += 10;
            return toInfo(el, index, 'heuristic', score);
        })
        .sort((a, b) => b.score - a.score);
}"""

_FILL_INPUT_JS = r"""({selector, value}) => {
    const el = document.querySelector(selector);
    if (!el) return {ok: false, reason: 'not_found', selector};
    el.focus();
    el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return {ok: true, selector, tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase()};
}"""

_SUBMIT_LOGIN_FORM_JS = r"""({passwordSelector, submitSelector}) => {
    const passwordEl = passwordSelector ? document.querySelector(passwordSelector) : null;
    const explicitSubmit = submitSelector ? document.querySelector(submitSelector) : null;
    const form = passwordEl ? (passwordEl.form || passwordEl.closest('form')) : null;

    const clickAndDescribe = (el, selector, source) => {
        el.click();
        return {submitted: true, method: 'click', selector, source};
    };

    if (explicitSubmit) {
        return clickAndDescribe(explicitSubmit, submitSelector, 'explicit_submit_selector');
    }

    if (form) {
        const submitEl = form.querySelector('button[type="submit"], input[type="submit"], button:not([type]), [role="button"]');
        if (submitEl) {
            return clickAndDescribe(submitEl, window.__veles_build_selector(submitEl, 0), 'form_submit_control');
        }
        if (typeof form.requestSubmit === 'function') {
            form.requestSubmit();
            return {submitted: true, method: 'requestSubmit', selector: window.__veles_build_selector(form, 0), source: 'form_request_submit'};
        }
        form.submit();
        return {submitted: true, method: 'form_submit', selector: window.__veles_build_selector(form, 0), source: 'form_submit'};
    }

    if (passwordEl) {
        passwordEl.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true}));
        passwordEl.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', bubbles: true}));
        return {submitted: true, method: 'enter_key', selector: passwordSelector, source: 'password_enter'};
    }

    return {submitted: false, method: 'none', selector: '', source: 'no_submit_target'};
}"""

_LOGIN_SIGNALS_JS = r"""() => {
    const visible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const texts = Array.from(document.querySelectorAll('[role="alert"], .error, .alert, .alert-danger, .notification, .flash, [aria-live]'))
        .map((el) => (el.textContent || '').trim())
        .filter(Boolean)
        .slice(0, 10);
    const bodyText = (document.body.innerText || '').toLowerCase();
    const passwordFields = Array.from(document.querySelectorAll('input[type="password"], input[autocomplete="current-password"], input[autocomplete="new-password"]'));
    const profileUi = Array.from(document.querySelectorAll('a, button, [role="button"], nav, header'))
        .map((el) => (el.textContent || '').trim())
        .filter(Boolean)
        .slice(0, 50)
        .join(' | ')
        .toLowerCase();
    return {
        url: window.location.href,
        title: document.title || '',
        visible_password_fields: passwordFields.filter(visible).length,
        total_password_fields: passwordFields.length,
        error_texts: texts,
        body_mentions_logout: /logout|log out|sign out/.test(bodyText),
        body_mentions_profile: /profile|account|my account|dashboard/.test(bodyText),
        body_mentions_login: /login|log in|sign in/.test(bodyText),
        has_profile_ui: /logout|log out|sign out|profile|account|dashboard/.test(profileUi),
    };
}"""


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


def _ensure_browser(ctx: ToolContext):
    """Create or reuse browser for this task. Browser state lives in ctx,
    but Playwright instance is module-level to avoid greenlet issues."""
    global _pw_instance, _pw_thread_id

    current_thread_id = threading.get_ident()
    if _pw_instance is not None and _pw_thread_id != current_thread_id:
        log.info(f"Thread switch detected (old={_pw_thread_id}, new={current_thread_id}). Resetting Playwright...")
        _reset_playwright_greenlet()

    if ctx.browser_state.browser is not None:
        try:
            if ctx.browser_state.browser.is_connected():
                return ctx.browser_state.page
        except Exception:
            log.debug("Browser connection check failed in _ensure_browser", exc_info=True)
            pass
        cleanup_browser(ctx)

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
    ctx.browser_state.page = ctx.browser_state.browser.new_page(
        viewport={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    )

    if _HAS_STEALTH:
        stealth = Stealth()
        stealth.apply_stealth_sync(ctx.browser_state.page)

    ctx.browser_state.page.set_default_timeout(30000)
    return ctx.browser_state.page


def cleanup_browser(ctx: ToolContext) -> None:
    """Close browser and playwright. Called by agent.py in finally block.

    Note: We DON'T stop the module-level _pw_instance here to allow reuse
    across tasks. Only close the browser and page for this context.
    """
    global _pw_instance

    try:
        if ctx.browser_state.page is not None:
            ctx.browser_state.page.close()
    except Exception:
        log.debug("Failed to close browser page during cleanup", exc_info=True)
        pass
    try:
        if ctx.browser_state.browser is not None:
            ctx.browser_state.browser.close()
    except Exception as e:
        if "cannot switch" in str(e) or "different thread" in str(e):
            log.warning("Browser cleanup hit thread error, resetting Playwright...")
            _reset_playwright_greenlet()

    ctx.browser_state.page = None
    ctx.browser_state.browser = None
    ctx.browser_state.pw_instance = None


_MARKDOWN_JS = """() => {
    const walk = (el) => {
        let out = '';
        for (const child of el.childNodes) {
            if (child.nodeType === 3) {
                const t = child.textContent.trim();
                if (t) out += t + ' ';
            } else if (child.nodeType === 1) {
                const tag = child.tagName;
                if (['SCRIPT','STYLE','NOSCRIPT'].includes(tag)) continue;
                if (['H1','H2','H3','H4','H5','H6'].includes(tag))
                    out += '\n' + '#'.repeat(parseInt(tag[1])) + ' ';
                if (tag === 'P' || tag === 'DIV' || tag === 'BR') out += '\n';
                if (tag === 'LI') out += '\n- ';
                if (tag === 'A') out += '[';
                out += walk(child);
                if (tag === 'A') out += '](' + (child.href||'') + ')';
            }
        }
        return out;
    };
    return walk(document.body);
}"""

_SELECTOR_HELPERS_JS = r"""() => {
    if (window.__veles_build_selector) return true;
    window.__veles_build_selector = (el, fallbackIndex = 0) => {
        if (!el) return '';
        if (el.id) return `#${CSS.escape(el.id)}`;
        const name = el.getAttribute('name');
        if (name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
        const placeholder = el.getAttribute('placeholder');
        if (placeholder) return `${el.tagName.toLowerCase()}[placeholder="${CSS.escape(placeholder)}"]`;
        const type = el.getAttribute('type');
        const parts = [el.tagName.toLowerCase()];
        if (type) parts.push(`[type="${CSS.escape(type)}"]`);
        const classes = Array.from(el.classList || []).slice(0, 2).map((cls) => `.${CSS.escape(cls)}`).join('');
        const base = `${parts.join('')}${classes}`;
        const siblings = Array.from((el.parentElement || document.body).querySelectorAll(base));
        const index = siblings.indexOf(el);
        return `${base}:nth-of-type(${Math.max(1, index + 1 || fallbackIndex + 1)})`;
    };
    return true;
}"""


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
    elif output == "html":
        html = page.content()
        return html[:50000] + ("... [truncated]" if len(html) > 50000 else "")
    elif output == "markdown":
        text = page.evaluate(_MARKDOWN_JS)
        return text[:30000] + ("... [truncated]" if len(text) > 30000 else "")
    else:
        text = page.inner_text("body")
        return text[:30000] + ("... [truncated]" if len(text) > 30000 else "")


def _normalize_selector(value: Optional[str]) -> str:
    return (value or "").strip()


def choose_login_field_selectors(
    username_candidates: List[Dict[str, Any]],
    password_candidates: List[Dict[str, Any]],
    username_selector: str = "",
    password_selector: str = "",
) -> Dict[str, Any]:
    """Choose best username/password selectors from explicit inputs and heuristic candidates."""
    explicit_user = _normalize_selector(username_selector)
    explicit_pass = _normalize_selector(password_selector)
    if explicit_user and explicit_pass:
        return {
            "username_selector": explicit_user,
            "password_selector": explicit_pass,
            "username_source": "explicit",
            "password_source": "explicit",
            "shared_form": False,
        }

    password_pick = None
    if explicit_pass:
        password_pick = {"selector": explicit_pass, "form_selector": "", "source": "explicit"}
    elif password_candidates:
        password_pick = password_candidates[0]

    username_pick = None
    if explicit_user:
        username_pick = {"selector": explicit_user, "form_selector": "", "source": "explicit"}
    else:
        if password_pick and password_pick.get("form_selector"):
            same_form = [c for c in username_candidates if c.get("form_selector") == password_pick.get("form_selector")]
            if same_form:
                username_pick = same_form[0]
        if username_pick is None and username_candidates:
            username_pick = username_candidates[0]

    return {
        "username_selector": username_pick.get("selector", "") if username_pick else "",
        "password_selector": password_pick.get("selector", "") if password_pick else "",
        "username_source": username_pick.get("source", "") if username_pick else "",
        "password_source": password_pick.get("source", "") if password_pick else "",
        "shared_form": bool(
            username_pick
            and password_pick
            and username_pick.get("form_selector")
            and username_pick.get("form_selector") == password_pick.get("form_selector")
        ),
    }


def infer_login_state(signals: Dict[str, Any]) -> Dict[str, Any]:
    """Infer coarse login state from page signals. Pure function for unit tests."""
    matched = list(signals.get("matched") or [])
    reason_parts: List[str] = []

    if "success_selector" in matched:
        return {
            "state": "logged_in",
            "reason": "explicit success selector matched",
            "matched": matched,
        }
    if "failure_selector" in matched:
        return {
            "state": "login_failed",
            "reason": "explicit failure selector matched",
            "matched": matched,
        }
    if "logged_out_selector" in matched:
        return {
            "state": "logged_out",
            "reason": "explicit logged_out selector matched",
            "matched": matched,
        }

    error_text = " ".join(signals.get("error_texts") or []).lower()
    if error_text:
        failure_terms = ["invalid", "incorrect", "wrong", "try again", "error", "failed", "required"]
        if any(term in error_text for term in failure_terms):
            return {
                "state": "login_failed",
                "reason": "error or alert text suggests authentication failure",
                "matched": matched,
            }
        reason_parts.append("alert text present but not clearly auth-related")

    visible_password_fields = int(signals.get("visible_password_fields") or 0)
    body_mentions_login = bool(signals.get("body_mentions_login"))
    has_profile_ui = bool(signals.get("has_profile_ui"))
    body_mentions_profile = bool(signals.get("body_mentions_profile"))
    body_mentions_logout = bool(signals.get("body_mentions_logout"))

    if has_profile_ui or body_mentions_logout or body_mentions_profile:
        if visible_password_fields == 0:
            return {
                "state": "logged_in",
                "reason": "profile/logout UI present and password form no longer visible",
                "matched": matched,
            }
        reason_parts.append("profile-like UI present but password field still visible")

    if visible_password_fields > 0 and body_mentions_login:
        return {
            "state": "logged_out",
            "reason": "login/sign-in page still visible with password field",
            "matched": matched,
        }

    if visible_password_fields > 0:
        return {
            "state": "unclear",
            "reason": "password field still visible without explicit failure signal",
            "matched": matched,
        }

    if has_profile_ui or body_mentions_profile or body_mentions_logout:
        return {
            "state": "logged_in",
            "reason": "page suggests authenticated account UI",
            "matched": matched,
        }

    if body_mentions_login:
        return {
            "state": "logged_out",
            "reason": "page still looks like login screen",
            "matched": matched,
        }

    return {
        "state": "unclear",
        "reason": "; ".join(reason_parts) if reason_parts else "insufficient signals",
        "matched": matched,
    }


def _safe_selector_presence(page: Any, selector: str, timeout: int) -> bool:
    selector = _normalize_selector(selector)
    if not selector:
        return False
    try:
        handle = page.wait_for_selector(selector, timeout=timeout, state="attached")
        return handle is not None
    except Exception:
        return False


def _browse_page(ctx: ToolContext, url: str, output: str = "text",
                 wait_for: str = "", timeout: int = 30000) -> str:
    try:
        page = _ensure_browser(ctx)
        page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        if wait_for:
            page.wait_for_selector(wait_for, timeout=timeout)
        return _extract_page_output(page, output, ctx)
    except Exception as e:
        if "cannot switch" in str(e) or "different thread" in str(e) or "greenlet" in str(e).lower():
            log.warning(f"Browser thread error detected: {e}. Resetting Playwright and retrying...")
            cleanup_browser(ctx)
            _reset_playwright_greenlet()
            page = _ensure_browser(ctx)
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            if wait_for:
                page.wait_for_selector(wait_for, timeout=timeout)
            return _extract_page_output(page, output, ctx)
        raise


def _browser_fill_login_form(
    ctx: ToolContext,
    username: str,
    password: str,
    username_selector: str = "",
    password_selector: str = "",
    submit_selector: str = "",
    timeout: int = 5000,
) -> str:
    page = _ensure_browser(ctx)
    page.evaluate(_SELECTOR_HELPERS_JS)

    username_candidates = page.evaluate(_USERNAME_CANDIDATE_JS)
    password_candidates = page.evaluate(_PASSWORD_CANDIDATE_JS)
    chosen = choose_login_field_selectors(
        username_candidates=username_candidates,
        password_candidates=password_candidates,
        username_selector=username_selector,
        password_selector=password_selector,
    )

    user_sel = chosen["username_selector"]
    pass_sel = chosen["password_selector"]
    submit_sel = _normalize_selector(submit_selector)

    if not pass_sel:
        return (
            "Error: password field not found. "
            f"password_candidates={len(password_candidates)} current_url={page.url}"
        )
    if not user_sel:
        return (
            "Error: username/email field not found. "
            f"username_candidates={len(username_candidates)} current_url={page.url}"
        )

    try:
        page.wait_for_selector(user_sel, timeout=timeout, state="visible")
        page.wait_for_selector(pass_sel, timeout=timeout, state="visible")
    except Exception as e:
        return f"Error: login fields resolved but not interactable ({e}). current_url={page.url}"

    user_fill = page.evaluate(_FILL_INPUT_JS, {"selector": user_sel, "value": username})
    pass_fill = page.evaluate(_FILL_INPUT_JS, {"selector": pass_sel, "value": password})
    if not user_fill.get("ok"):
        return f"Error: failed to fill username field ({user_sel}). current_url={page.url}"
    if not pass_fill.get("ok"):
        return f"Error: failed to fill password field ({pass_sel}). current_url={page.url}"

    submit_result = page.evaluate(
        _SUBMIT_LOGIN_FORM_JS,
        {"passwordSelector": pass_sel, "submitSelector": submit_sel},
    )
    page.wait_for_timeout(800)

    return (
        "Login form filled. "
        f"username_selector={user_sel} ({chosen.get('username_source') or 'heuristic'}); "
        f"password_selector={pass_sel} ({chosen.get('password_source') or 'heuristic'}); "
        f"shared_form={chosen.get('shared_form', False)}; "
        f"submit={submit_result.get('submitted', False)} via {submit_result.get('source', 'none')} "
        f"[{submit_result.get('method', 'none')}]; "
        f"submit_selector={submit_result.get('selector', submit_sel or '')}; "
        f"current_url={page.url}"
    )


def _browser_check_login_state(
    ctx: ToolContext,
    success_selector: str = "",
    failure_selector: str = "",
    logged_out_selector: str = "",
    timeout: int = 5000,
) -> str:
    page = _ensure_browser(ctx)
    page.evaluate(_SELECTOR_HELPERS_JS)

    matched: List[str] = []
    if _safe_selector_presence(page, success_selector, timeout):
        matched.append("success_selector")
    if _safe_selector_presence(page, failure_selector, timeout):
        matched.append("failure_selector")
    if _safe_selector_presence(page, logged_out_selector, timeout):
        matched.append("logged_out_selector")

    signals = page.evaluate(_LOGIN_SIGNALS_JS)
    signals["matched"] = matched

    inferred = infer_login_state(signals)
    result = {
        "state": inferred["state"],
        "url": signals.get("url", page.url),
        "title": signals.get("title", ""),
        "matched": matched,
        "signals": signals,
        "reason": inferred["reason"],
    }
    return json.dumps(result, ensure_ascii=False)


def _browser_action(ctx: ToolContext, action: str, selector: str = "",
                    value: str = "", timeout: int = 5000) -> str:
    def _do_action():
        page = _ensure_browser(ctx)

        if action == "click":
            if not selector:
                return "Error: selector required for click"
            page.click(selector, timeout=timeout)
            page.wait_for_timeout(500)
            return f"Clicked: {selector}"
        elif action == "fill":
            if not selector:
                return "Error: selector required for fill"
            page.fill(selector, value, timeout=timeout)
            return f"Filled {selector} with: {value}"
        elif action == "select":
            if not selector:
                return "Error: selector required for select"
            page.select_option(selector, value, timeout=timeout)
            return f"Selected {value} in {selector}"
        elif action == "screenshot":
            data = page.screenshot(type="png", full_page=False)
            b64 = base64.b64encode(data).decode()
            ctx.browser_state.last_screenshot_b64 = b64
            return (
                f"Screenshot captured ({len(b64)} bytes base64). "
                f"Call send_photo(image_base64='__last_screenshot__') to deliver it to the owner."
            )
        elif action == "evaluate":
            if not value:
                return "Error: value (JS code) required for evaluate"
            result = page.evaluate(value)
            out = str(result)
            return out[:20000] + ("... [truncated]" if len(out) > 20000 else "")
        elif action == "scroll":
            direction = value or "down"
            if direction == "down":
                page.evaluate("window.scrollBy(0, 600)")
            elif direction == "up":
                page.evaluate("window.scrollBy(0, -600)")
            elif direction == "top":
                page.evaluate("window.scrollTo(0, 0)")
            elif direction == "bottom":
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            return f"Scrolled {direction}"
        else:
            return f"Unknown action: {action}. Use: click, fill, select, screenshot, evaluate, scroll"

    try:
        return _do_action()
    except (RuntimeError, Exception) as e:
        if "cannot switch" in str(e) or "different thread" in str(e) or "greenlet" in str(e).lower():
            log.warning(f"Browser thread error detected: {e}. Resetting Playwright and retrying...")
            cleanup_browser(ctx)
            _reset_playwright_greenlet()
            return _do_action()
        else:
            raise


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="browse_page",
            schema={
                "name": "browse_page",
                "description": (
                    "Open a URL in headless browser. Returns page content as text, "
                    "html, markdown, or screenshot (base64 PNG). "
                    "Browser persists across calls within a task. "
                    "For screenshots: use send_photo tool to deliver it to owner."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL to open"},
                        "output": {
                            "type": "string",
                            "enum": ["text", "html", "markdown", "screenshot"],
                            "description": "Output format (default: text)",
                        },
                        "wait_for": {
                            "type": "string",
                            "description": "CSS selector to wait for before extraction",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Page load timeout in ms (default: 30000)",
                        },
                    },
                    "required": ["url"],
                },
            },
            handler=_browse_page,
            timeout_sec=60,
        ),
        ToolEntry(
            name="browser_action",
            schema={
                "name": "browser_action",
                "description": (
                    "Perform action on current browser page. Actions: "
                    "click (selector), fill (selector + value), select (selector + value), "
                    "screenshot (base64 PNG), evaluate (JS code in value), "
                    "scroll (value: up/down/top/bottom)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["click", "fill", "select", "screenshot", "evaluate", "scroll"],
                            "description": "Action to perform",
                        },
                        "selector": {
                            "type": "string",
                            "description": "CSS selector for click/fill/select",
                        },
                        "value": {
                            "type": "string",
                            "description": "Value for fill/select, JS for evaluate, direction for scroll",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Action timeout in ms (default: 5000)",
                        },
                    },
                    "required": ["action"],
                },
            },
            handler=_browser_action,
            timeout_sec=60,
        ),
        ToolEntry(
            name="browser_fill_login_form",
            schema={
                "name": "browser_fill_login_form",
                "description": (
                    "Fill a login form on the current page using explicit selectors or "
                    "simple heuristics for username/email and password fields, then submit it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "username": {"type": "string", "description": "Username or email to enter"},
                        "password": {"type": "string", "description": "Password to enter"},
                        "username_selector": {"type": "string", "description": "Optional CSS selector for username/email input"},
                        "password_selector": {"type": "string", "description": "Optional CSS selector for password input"},
                        "submit_selector": {"type": "string", "description": "Optional CSS selector for submit button/control"},
                        "timeout": {"type": "integer", "description": "Field interaction timeout in ms (default: 5000)"},
                    },
                    "required": ["username", "password"],
                },
            },
            handler=_browser_fill_login_form,
            timeout_sec=60,
        ),
        ToolEntry(
            name="browser_check_login_state",
            schema={
                "name": "browser_check_login_state",
                "description": (
                    "Inspect the current page and infer whether login succeeded, failed, "
                    "is still logged out, or remains unclear."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "success_selector": {"type": "string", "description": "Optional CSS selector that indicates authenticated state"},
                        "failure_selector": {"type": "string", "description": "Optional CSS selector that indicates login failure/error"},
                        "logged_out_selector": {"type": "string", "description": "Optional CSS selector that indicates logged-out/login screen state"},
                        "timeout": {"type": "integer", "description": "Selector wait timeout in ms (default: 5000)"},
                    },
                },
            },
            handler=_browser_check_login_state,
            timeout_sec=60,
        ),
    ]
