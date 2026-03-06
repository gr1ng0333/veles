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


from ouroboros.tools.browser_login_helpers import (
    _FILL_INPUT_JS,
    _LOGIN_SIGNALS_JS,
    _PASSWORD_CANDIDATE_JS,
    _SUBMIT_LOGIN_FORM_JS,
    _USERNAME_CANDIDATE_JS,
    infer_login_state,
    plan_login_flow,
)


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
    but Playwright instance is module-level to avoid greenlet issues."""
    global _pw_instance, _pw_thread_id

    current_thread_id = threading.get_ident()
    if _pw_instance is not None and _pw_thread_id != current_thread_id:
        log.info(f"Thread switch detected (old={_pw_thread_id}, new={current_thread_id}). Resetting Playwright...")
        _reset_playwright_greenlet()

    if ctx.browser_state.browser is not None:
        try:
            if ctx.browser_state.browser.is_connected() and ctx.browser_state.page is not None:
                return ctx.browser_state.page
        except Exception:
            log.debug("Browser connection check failed in _ensure_browser", exc_info=True)
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


def _safe_selector_presence(page: Any, selector: str, timeout: int) -> bool:
    selector = _normalize_selector(selector)
    if not selector:
        return False
    try:
        handle = page.wait_for_selector(selector, timeout=timeout, state="attached")
        return handle is not None
    except Exception:
        return False


def _session_snapshot(context: Any) -> Dict[str, Any]:
    state = context.storage_state()
    cookies = list(state.get("cookies") or [])
    origins = list(state.get("origins") or [])
    return {
        "storage_state": state,
        "cookies_count": len(cookies),
        "origins_count": len(origins),
    }


def _browser_save_session(ctx: ToolContext, session_name: str) -> str:
    name = (session_name or "").strip()
    if not name:
        return "Error: session_name is required"
    page = _ensure_browser(ctx)
    snapshot = _session_snapshot(ctx.browser_state.context)
    ctx.browser_state.saved_sessions[name] = snapshot
    ctx.browser_state.active_session_name = name
    result = {
        "session_name": name,
        "cookies_count": snapshot["cookies_count"],
        "origins_count": snapshot["origins_count"],
        "current_url": page.url,
    }
    return json.dumps(result, ensure_ascii=False)


def _browser_restore_session(ctx: ToolContext, session_name: str, url: str = "") -> str:
    name = (session_name or "").strip()
    if not name:
        return "Error: session_name is required"
    saved = ctx.browser_state.saved_sessions.get(name)
    if not saved:
        return f"Error: session '{name}' not found in current task context"
    _ensure_browser(ctx)
    page = _replace_browser_context(ctx, storage_state=saved.get("storage_state") or {})
    ctx.browser_state.active_session_name = name
    target_url = (url or "").strip()
    if target_url:
        page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
    result = {
        "session_name": name,
        "cookies_count": saved.get("cookies_count", 0),
        "origins_count": saved.get("origins_count", 0),
        "current_url": page.url,
        "navigated": bool(target_url),
    }
    return json.dumps(result, ensure_ascii=False)


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
    allow_multi_step: bool = False,
    next_selector: str = "",
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
    next_sel = _normalize_selector(next_selector)
    plan = plan_login_flow(user_sel, pass_sel, allow_multi_step=allow_multi_step)

    if not plan["can_proceed"]:
        return (
            f"Error: {plan['reason']}. "
            f"username_candidates={len(username_candidates)} password_candidates={len(password_candidates)} current_url={page.url}"
        )

    try:
        page.wait_for_selector(user_sel, timeout=timeout, state="visible")
    except Exception as e:
        return f"Error: username/email field resolved but not interactable ({e}). current_url={page.url}"

    user_fill = page.evaluate(_FILL_INPUT_JS, {"selector": user_sel, "value": username})
    if not user_fill.get("ok"):
        return f"Error: failed to fill username field ({user_sel}). current_url={page.url}"

    step_results = []

    if plan["mode"] == "multi_step_username_first":
        next_result = page.evaluate(
            _SUBMIT_LOGIN_FORM_JS,
            {"anchorSelector": user_sel, "submitSelector": next_sel or submit_sel},
        )
        step_results.append({"step": "username_submit", **next_result})
        page.wait_for_timeout(800)
        password_candidates = page.evaluate(_PASSWORD_CANDIDATE_JS)
        chosen = choose_login_field_selectors(
            username_candidates=page.evaluate(_USERNAME_CANDIDATE_JS),
            password_candidates=password_candidates,
            username_selector=username_selector,
            password_selector=password_selector,
        )
        pass_sel = chosen["password_selector"]
        if not pass_sel:
            return (
                "Error: multi-step login advanced past username but password field not found. "
                f"submit_source={next_result.get('source', 'none')} current_url={page.url}"
            )

    try:
        page.wait_for_selector(pass_sel, timeout=timeout, state="visible")
    except Exception as e:
        return f"Error: password field resolved but not interactable ({e}). current_url={page.url}"

    pass_fill = page.evaluate(_FILL_INPUT_JS, {"selector": pass_sel, "value": password})
    if not pass_fill.get("ok"):
        return f"Error: failed to fill password field ({pass_sel}). current_url={page.url}"

    submit_result = page.evaluate(
        _SUBMIT_LOGIN_FORM_JS,
        {"anchorSelector": pass_sel, "submitSelector": submit_sel},
    )
    step_results.append({"step": "password_submit", **submit_result})
    page.wait_for_timeout(800)

    return (
        "Login form filled. "
        f"mode={plan['mode']}; "
        f"username_selector={user_sel} ({chosen.get('username_source') or 'heuristic'}); "
        f"password_selector={pass_sel} ({chosen.get('password_source') or 'heuristic'}); "
        f"shared_form={chosen.get('shared_form', False)}; "
        f"steps={json.dumps(step_results, ensure_ascii=False)}; "
        f"submit_selector={submit_result.get('selector', submit_sel or '')}; "
        f"current_url={page.url}"
    )


def _browser_check_login_state(
    ctx: ToolContext,
    success_selector: str = "",
    failure_selector: str = "",
    logged_out_selector: str = "",
    expected_url_substring: str = "",
    success_cookie_names: Optional[List[str]] = None,
    failure_text_substrings: Optional[List[str]] = None,
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
    cookies = []
    try:
        cookies = ctx.browser_state.context.cookies() if ctx.browser_state.context is not None else []
    except Exception:
        log.debug("Failed to read browser cookies during login state check", exc_info=True)

    expected_url = _normalize_selector(expected_url_substring).lower()
    cookie_names = [str(cookie.get("name") or "") for cookie in cookies if cookie.get("name")]
    signals.update({
        "matched": matched,
        "cookie_names": cookie_names,
        "success_cookie_names": list(success_cookie_names or []),
        "failure_text_substrings": list(failure_text_substrings or []),
        "expected_url_substring": expected_url_substring or "",
        "expected_url_matched": bool(expected_url and expected_url in str(signals.get("url") or page.url).lower()),
        "body_text": str(signals.get("body_text") or signals.get("title") or ""),
    })

    inferred = infer_login_state(signals)
    result = {
        "state": inferred["state"],
        "url": signals.get("url", page.url),
        "title": signals.get("title", ""),
        "matched": matched,
        "signals": signals,
        "reason": inferred["reason"],
        "active_session_name": ctx.browser_state.active_session_name,
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
                    "simple heuristics for username/email and password fields, submit it, and optionally handle username-first multi-step flows."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "username": {"type": "string", "description": "Username or email to enter"},
                        "password": {"type": "string", "description": "Password to enter"},
                        "username_selector": {"type": "string", "description": "Optional CSS selector for username/email input"},
                        "password_selector": {"type": "string", "description": "Optional CSS selector for password input"},
                        "submit_selector": {"type": "string", "description": "Optional CSS selector for submit button/control"},
                        "allow_multi_step": {"type": "boolean", "description": "Allow username-first multi-step login flows (default: false)"},
                        "next_selector": {"type": "string", "description": "Optional CSS selector for the intermediate next/continue control in multi-step login"},
                        "timeout": {"type": "integer", "description": "Field interaction timeout in ms (default: 5000)"},
                    },
                    "required": ["username", "password"],
                },
            },
            handler=_browser_fill_login_form,
            timeout_sec=60,
        ),
        ToolEntry(
            name="browser_save_session",
            schema={
                "name": "browser_save_session",
                "description": "Save the current browser context storage state in task memory for later reuse.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_name": {"type": "string", "description": "Name for the saved in-memory browser session"},
                    },
                    "required": ["session_name"],
                },
            },
            handler=_browser_save_session,
            timeout_sec=60,
        ),
        ToolEntry(
            name="browser_restore_session",
            schema={
                "name": "browser_restore_session",
                "description": "Restore a previously saved in-memory browser session inside the current task and optionally open a URL.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_name": {"type": "string", "description": "Previously saved session name"},
                        "url": {"type": "string", "description": "Optional URL to open after restoring session"},
                    },
                    "required": ["session_name"],
                },
            },
            handler=_browser_restore_session,
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
                        "expected_url_substring": {"type": "string", "description": "Optional URL substring expected after successful login"},
                        "success_cookie_names": {"type": "array", "items": {"type": "string"}, "description": "Optional cookie names that suggest authenticated state"},
                        "failure_text_substrings": {"type": "array", "items": {"type": "string"}, "description": "Optional substrings that indicate login failure"},
                        "timeout": {"type": "integer", "description": "Selector wait timeout in ms (default: 5000)"},
                    },
                },
            },
            handler=_browser_check_login_state,
            timeout_sec=60,
        ),
    ]
