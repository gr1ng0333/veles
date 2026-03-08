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
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.browser_runtime import (
    _apply_stealth,
    _check_session_alive_via_protected_url,
    _ensure_browser,
    _extract_page_output,
    _post_submit_wait,
    _replace_browser_context,
    _reset_playwright_greenlet,
    cleanup_browser,
)

log = logging.getLogger(__name__)


from ouroboros.tools.browser_login_helpers import (
    _FILL_INPUT_JS,
    _LOGIN_SIGNALS_JS,
    _PASSWORD_CANDIDATE_JS,
    _SELECTOR_HELPERS_JS,
    _SUBMIT_LOGIN_FORM_JS,
    _USERNAME_CANDIDATE_JS,
    infer_login_state,
    plan_login_flow,
)
from ouroboros.tools.captcha_solver import solve_captcha_image


def _with_thread_safety_retry(func):
    """Retry browser operation if Playwright thread mismatch detected."""
    import functools

    @functools.wraps(func)
    def wrapper(ctx, *args, **kwargs):
        try:
            return func(ctx, *args, **kwargs)
        except Exception as e:
            err_msg = str(e).lower()
            if "cannot switch" in err_msg or "different thread" in err_msg or "greenlet" in err_msg:
                log.warning("Thread mismatch in %s, resetting Playwright", func.__name__)
                try:
                    cleanup_browser(ctx)
                    _reset_playwright_greenlet()
                except Exception:
                    pass
                return func(ctx, *args, **kwargs)
            raise
    return wrapper


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


@_with_thread_safety_retry
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
        _post_submit_wait(page)
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
    _post_submit_wait(page)

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


@_with_thread_safety_retry
def _browser_check_login_state(
    ctx: ToolContext,
    success_selector: str = "",
    failure_selector: str = "",
    logged_out_selector: str = "",
    expected_url_substring: str = "",
    success_cookie_names: Optional[List[str]] = None,
    failure_text_substrings: Optional[List[str]] = None,
    protected_url: str = "",
    timeout: int = 5000,
) -> str:
    page = _ensure_browser(ctx)
    page.evaluate(_SELECTOR_HELPERS_JS)
    _post_submit_wait(page)

    current_url = str(page.url or "")
    expected_url = _normalize_selector(expected_url_substring).lower()
    login_url_markers = ["login", "sign-in", "signin", "auth"]
    submitted_from_login_url = any(marker in current_url.lower() for marker in login_url_markers)

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

    cookie_names = [str(cookie.get("name") or "") for cookie in cookies if cookie.get("name")]
    redirected_away_from_login = submitted_from_login_url and not any(marker in str(signals.get("url") or page.url).lower() for marker in login_url_markers)
    protected_probe = _check_session_alive_via_protected_url(ctx, protected_url=protected_url, timeout=timeout)
    signals.update({
        "matched": matched,
        "cookie_names": cookie_names,
        "success_cookie_names": list(success_cookie_names or []),
        "failure_text_substrings": list(failure_text_substrings or []),
        "expected_url_substring": expected_url_substring or "",
        "expected_url_matched": bool(expected_url and expected_url in str(signals.get("url") or page.url).lower()),
        "body_text": str(signals.get("body_text") or signals.get("title") or ""),
        "submitted_from_login_url": submitted_from_login_url,
        "redirected_away_from_login": redirected_away_from_login,
        "has_error_classes": bool(signals.get("error_class_count") or 0),
        "protected_url_checked": bool(protected_probe.get("checked")),
        "protected_url_alive": bool(protected_probe.get("alive")) if protected_probe.get("checked") else False,
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
        "protected_url_check": protected_probe if protected_probe.get("checked") else None,
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


# ---------------------------------------------------------------------------
# JS helpers for captcha heuristics
# ---------------------------------------------------------------------------

_CAPTCHA_IMG_HEURISTIC_JS = r"""() => {
    const keywords = ['captcha', 'verify', 'code', 'vcode', 'checkcode', 'seccode', 'imgcode'];
    const imgs = Array.from(document.querySelectorAll('img'));
    for (const img of imgs) {
        const haystack = [
            img.src || '',
            img.className || '',
            img.alt || '',
            img.id || '',
            img.getAttribute('name') || '',
        ].join(' ').toLowerCase();
        if (keywords.some(kw => haystack.includes(kw))) {
            return window.__veles_build_selector
                ? window.__veles_build_selector(img)
                : (img.id ? '#' + img.id : null);
        }
    }
    return null;
}"""

_CAPTCHA_INPUT_HEURISTIC_JS = r"""() => {
    const keywords = ['captcha', 'verify', 'code', 'vcode', 'checkcode', 'seccode', 'imgcode'];
    const inputs = Array.from(document.querySelectorAll('input[type="text"], input:not([type])'));
    for (const inp of inputs) {
        const haystack = [
            inp.name || '',
            inp.placeholder || '',
            inp.id || '',
            inp.className || '',
            inp.getAttribute('aria-label') || '',
        ].join(' ').toLowerCase();
        if (keywords.some(kw => haystack.includes(kw))) {
            return window.__veles_build_selector
                ? window.__veles_build_selector(inp)
                : (inp.id ? '#' + inp.id : null);
        }
    }
    return null;
}"""


# ---------------------------------------------------------------------------
# browser_solve_captcha implementation
# ---------------------------------------------------------------------------

@_with_thread_safety_retry
def _browser_solve_captcha(
    ctx: ToolContext,
    captcha_image_selector: str = "",
    captcha_input_selector: str = "",
    submit_selector: str = "",
    max_retries: int = 3,
) -> str:
    page = _ensure_browser(ctx)
    page.evaluate(_SELECTOR_HELPERS_JS)

    img_sel = _normalize_selector(captcha_image_selector)
    input_sel = _normalize_selector(captcha_input_selector)
    sub_sel = _normalize_selector(submit_selector)
    max_retries = max(1, min(max_retries, 10))

    # --- locate captcha image ---
    if not img_sel:
        img_sel = page.evaluate(_CAPTCHA_IMG_HEURISTIC_JS) or ""
    if not img_sel:
        return json.dumps({
            "success": False, "text": "", "confidence": 0,
            "method": "", "attempts": 0,
            "error": "Cannot locate captcha image element on the page",
        }, ensure_ascii=False)

    # --- locate captcha input ---
    if not input_sel:
        input_sel = page.evaluate(_CAPTCHA_INPUT_HEURISTIC_JS) or ""
    if not input_sel:
        return json.dumps({
            "success": False, "text": "", "confidence": 0,
            "method": "", "attempts": 0,
            "error": "Cannot locate captcha input field on the page",
        }, ensure_ascii=False)

    last_result: dict = {}
    for attempt in range(1, max_retries + 1):
        try:
            el = page.wait_for_selector(img_sel, timeout=5000, state="visible")
            if el is None:
                last_result = {
                    "success": False, "text": "", "confidence": 0,
                    "method": "", "attempts": attempt,
                    "error": f"Captcha image selector '{img_sel}' not visible",
                }
                continue
            screenshot_bytes = el.screenshot(type="png")
        except Exception as exc:
            last_result = {
                "success": False, "text": "", "confidence": 0,
                "method": "", "attempts": attempt,
                "error": f"Failed to screenshot captcha element: {exc}",
            }
            continue

        try:
            result = solve_captcha_image(screenshot_bytes)
        except Exception as e:
            log.warning("captcha OCR failed: %s", e)
            result = {"text": "", "confidence": 0.0, "method": "error", "variant": "none", "attempts": 0}
        text = result.get("text", "")
        confidence = result.get("confidence", 0)
        method = result.get("method", "")

        if confidence < 0.3 and attempt < max_retries:
            # low confidence — try refreshing captcha by clicking the image
            try:
                page.click(img_sel, timeout=2000)
                page.wait_for_timeout(1500)
            except Exception:
                log.debug("Failed to click captcha image to refresh", exc_info=True)
            last_result = {
                "success": False, "text": text, "confidence": confidence,
                "method": method, "attempts": attempt,
                "error": "Confidence too low, retrying",
            }
            continue

        # --- vision fallback + low confidence guard ---
        if not text or confidence < 0.3:
            try:
                img_b64 = base64.b64encode(screenshot_bytes).decode()
                from ouroboros.tools.vision import _solve_simple_captcha
                vision_raw = _solve_simple_captcha(
                    ctx,
                    image_base64=img_b64,
                    prompt="Read the characters in this image. Return ONLY the text characters, nothing else.",
                    max_length=8,
                )
                vision_result = json.loads(vision_raw) if isinstance(vision_raw, str) else vision_raw
                vision_text = vision_result.get("text", "")
                if vision_text and vision_result.get("status") == "ok":
                    text = vision_text
                    confidence = 0.7
                    method = "vision_fallback"
                    log.info("captcha vision fallback succeeded: %s", text)
            except Exception as e:
                log.warning("captcha vision fallback failed: %s", e)

        if not text or confidence < 0.3:
            return json.dumps({
                "success": False,
                "text": text,
                "confidence": confidence,
                "reason": "OCR confidence too low after all retries",
                "method": method,
                "attempts": attempt,
            }, ensure_ascii=False)

        # --- fill input ---
        try:
            page.wait_for_selector(input_sel, timeout=3000, state="visible")
            page.fill(input_sel, text, timeout=3000)
        except Exception as exc:
            return json.dumps({
                "success": False, "text": text, "confidence": confidence,
                "method": method, "attempts": attempt,
                "error": f"Failed to fill captcha input: {exc}",
            }, ensure_ascii=False)

        # --- optional submit ---
        if sub_sel:
            try:
                page.click(sub_sel, timeout=3000)
                page.wait_for_timeout(1000)
            except Exception as exc:
                log.debug("Failed to click captcha submit: %s", exc)

        return json.dumps({
            "success": True, "text": text, "confidence": confidence,
            "method": method, "attempts": attempt, "error": None,
        }, ensure_ascii=False)

    # exhausted retries
    return json.dumps(last_result or {
        "success": False, "text": "", "confidence": 0,
        "method": "", "attempts": max_retries,
        "error": "Max retries exhausted",
    }, ensure_ascii=False)


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
                        "protected_url": {"type": "string", "description": "Optional authenticated URL used for an internal session-alive probe"},
                        "timeout": {"type": "integer", "description": "Selector wait timeout in ms (default: 5000)"},
                    },
                },
            },
            handler=_browser_check_login_state,
            timeout_sec=60,
        ),
        ToolEntry(
            name="browser_solve_captcha",
            schema={
                "name": "browser_solve_captcha",
                "description": (
                    "Solve an image captcha on the current page using local OCR "
                    "(ddddocr + tesseract fallback). Finds the captcha image and "
                    "input field by explicit selectors or heuristics, recognises "
                    "the text, fills the input, and optionally clicks submit."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "captcha_image_selector": {
                            "type": "string",
                            "description": "CSS selector for the captcha image element (auto-detected if empty)",
                        },
                        "captcha_input_selector": {
                            "type": "string",
                            "description": "CSS selector for the captcha text input (auto-detected if empty)",
                        },
                        "submit_selector": {
                            "type": "string",
                            "description": "CSS selector for the submit button (skipped if empty)",
                        },
                        "max_retries": {
                            "type": "integer",
                            "description": "Number of OCR attempts before giving up (default: 3)",
                        },
                    },
                },
            },
            handler=_browser_solve_captcha,
            timeout_sec=60,
        ),
    ]
