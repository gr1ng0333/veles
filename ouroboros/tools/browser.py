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
from ouroboros.tools.browser_auth_flow import (
    build_fill_login_plan_response,
    build_post_submit_auth_result,
    normalize_site_profile,
)
from ouroboros.tools.browser_tool_defs import build_browser_tool_entries
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

def _login_json_error(message: str, *, error: str, **extra: Any) -> str:
    payload = {"success": False, "message": message, "error": error}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _advance_multi_step_login(
    page: Any,
    *,
    plan: Dict[str, Any],
    profile: Dict[str, Any],
    username_selector: str,
    password_selector: str,
    submit_sel: str,
    next_sel: str,
    username_candidates_js: str,
    password_candidates_js: str,
    chosen: Dict[str, Any],
    user_fill: Dict[str, Any],
) -> Dict[str, Any]:
    next_result = page.evaluate(
        _SUBMIT_LOGIN_FORM_JS,
        {"anchorSelector": username_selector, "submitSelector": next_sel or submit_sel},
    )
    _post_submit_wait(page)
    chosen2 = choose_login_field_selectors(
        username_candidates=page.evaluate(username_candidates_js),
        password_candidates=page.evaluate(password_candidates_js),
        username_selector=profile.get("username_selector") or username_selector,
        password_selector=profile.get("password_selector") or password_selector,
    )
    pass_sel = chosen2["password_selector"]
    if not pass_sel:
        return {
            "ok": False,
            "response": _login_json_error(
                "Error: multi-step login advanced past username but password field not found. "
                f"submit_source={next_result.get('source', 'none')} current_url={page.url}",
                error="password field not found after username step",
                flow_plan=plan,
                selectors=chosen2,
                fill_results=[{"step": "username_fill", **user_fill}],
                submit=next_result,
                diagnostics={
                    "site_profile": {"site_name": profile.get("site_name", ""), "domain": profile.get("domain", ""), "flow_type": profile.get("flow_type", "login")},
                    "state": "username_step",
                    "reason": "password field not found after username step",
                    "next_action": {"action": "inspect_page", "reason": "password field not found after username step", "can_proceed": False, "required_selectors": {"next_selector": next_sel or submit_sel}},
                },
            ),
        }
    return {"ok": True, "submit": next_result, "chosen": chosen2, "pass_sel": pass_sel, "step_result": {"step": "username_submit", **next_result}}


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
    site_profile: Optional[Dict[str, Any]] = None,
    protected_url: str = "",
) -> str:
    profile = normalize_site_profile(site_profile)

    if getattr(ctx, "browser_state", None) is None:
        return build_fill_login_plan_response(
            profile=profile,
            username_selector=username_selector,
            password_selector=password_selector,
        )

    page = _ensure_browser(ctx)
    page.evaluate(_SELECTOR_HELPERS_JS)

    username_candidates = page.evaluate(_USERNAME_CANDIDATE_JS)
    password_candidates = page.evaluate(_PASSWORD_CANDIDATE_JS)
    chosen = choose_login_field_selectors(
        username_candidates=username_candidates,
        password_candidates=password_candidates,
        username_selector=profile.get("username_selector") or username_selector,
        password_selector=profile.get("password_selector") or password_selector,
    )

    user_sel = chosen["username_selector"]
    pass_sel = chosen["password_selector"]
    submit_sel = _normalize_selector(profile.get("submit_selector") or submit_selector)
    next_sel = _normalize_selector(profile.get("next_selector") or next_selector)
    plan = plan_login_flow(user_sel, pass_sel, allow_multi_step=allow_multi_step)

    if not plan["can_proceed"]:
        diagnostics = {
            "site_profile": {"site_name": profile.get("site_name", ""), "domain": profile.get("domain", ""), "flow_type": profile.get("flow_type", "login")},
            "state": "blocked",
            "reason": plan.get("reason", "cannot proceed"),
            "next_action": {"action": "inspect_page", "reason": plan.get("reason", "cannot proceed"), "can_proceed": False, "required_selectors": {}},
        }
        return _login_json_error(
            f"Error: {plan['reason']}. username_candidates={len(username_candidates)} password_candidates={len(password_candidates)} current_url={page.url}",
            error=plan.get("reason", "login form not ready"),
            flow_plan=plan,
            selectors=chosen,
            diagnostics=diagnostics,
        )

    try:
        page.wait_for_selector(user_sel, timeout=timeout, state="visible")
    except Exception as e:
        return _login_json_error(
            f"Error: username/email field resolved but not interactable ({e}). current_url={page.url}",
            error=f"username/email field resolved but not interactable ({e})",
            flow_plan=plan,
            selectors=chosen,
        )

    user_fill = page.evaluate(_FILL_INPUT_JS, {"selector": user_sel, "value": username})
    if not user_fill.get("ok"):
        return _login_json_error(
            f"Error: failed to fill username field ({user_sel}). current_url={page.url}",
            error=f"failed to fill username field ({user_sel})",
            flow_plan=plan,
            selectors=chosen,
        )

    step_results = []

    if plan["mode"] == "multi_step_username_first":
        advanced = _advance_multi_step_login(
            page,
            plan=plan,
            profile=profile,
            username_selector=user_sel,
            password_selector=password_selector,
            submit_sel=submit_sel,
            next_sel=next_sel,
            username_candidates_js=_USERNAME_CANDIDATE_JS,
            password_candidates_js=_PASSWORD_CANDIDATE_JS,
            chosen=chosen,
            user_fill=user_fill,
        )
        if not advanced["ok"]:
            return advanced["response"]
        step_results.append(advanced["step_result"])
        chosen = advanced["chosen"]
        pass_sel = advanced["pass_sel"]

    try:
        page.wait_for_selector(pass_sel, timeout=timeout, state="visible")
    except Exception as e:
        return _login_json_error(
            f"Error: password field resolved but not interactable ({e}). current_url={page.url}",
            error=f"password field resolved but not interactable ({e})",
            flow_plan=plan,
            selectors=chosen,
        )

    pass_fill = page.evaluate(_FILL_INPUT_JS, {"selector": pass_sel, "value": password})
    if not pass_fill.get("ok"):
        return _login_json_error(
            f"Error: failed to fill password field ({pass_sel}). current_url={page.url}",
            error=f"failed to fill password field ({pass_sel})",
            flow_plan=plan,
            selectors=chosen,
        )

    try:
        captcha_check = page.evaluate(r"""(function() {
            var imgs = document.querySelectorAll('img');
            for (var i = 0; i < imgs.length; i++) {
                var src = (imgs[i].src || '').toLowerCase();
                var attrs = ((imgs[i].id || '') + ' ' + (imgs[i].className || '') + ' ' + (imgs[i].alt || '') + ' ' + (imgs[i].getAttribute('name') || '')).toLowerCase();
                if (/captcha|verify|code|vcode|yzm|kaptcha|securimage/.test(src + ' ' + attrs)) return {found: true};
            }
            var canvases = document.querySelectorAll('canvas');
            for (var i = 0; i < canvases.length; i++) {
                var a = ((canvases[i].id || '') + ' ' + (canvases[i].className || '')).toLowerCase();
                if (/captcha|verify|code/.test(a)) return {found: true};
            }
            return {found: false};
        })()""")
        if captcha_check and captcha_check.get("found"):
            log.info("Captcha detected on login form, auto-solving")
            solve_fn = getattr(_browser_solve_captcha, "__wrapped__", _browser_solve_captcha)
            captcha_raw = solve_fn(
                ctx,
                captcha_image_selector="",
                captcha_input_selector="",
                submit_selector="",
                max_retries=3,
            )
            try:
                captcha_res = json.loads(captcha_raw) if isinstance(captcha_raw, str) else captcha_raw
                log.info("Auto-captcha result: success=%s", captcha_res.get("success", False))
            except Exception:
                pass
    except Exception as e:
        log.warning("Auto-captcha detection/solving failed: %s", e)

    submit_result = page.evaluate(
        _SUBMIT_LOGIN_FORM_JS,
        {"anchorSelector": pass_sel, "submitSelector": submit_sel},
    )
    step_results.append({"step": "password_submit", **submit_result})
    _post_submit_wait(page)

    post_signals = page.evaluate(_LOGIN_SIGNALS_JS)
    auth_result = build_post_submit_auth_result(
        page=page,
        profile=profile,
        protected_url=protected_url,
        timeout=timeout,
        post_signals=post_signals,
        session_probe=_check_session_alive_via_protected_url,
    )

    message = (
        "Login form filled. "
        f"mode={plan['mode']}; "
        f"username_selector={user_sel} ({chosen.get('username_source') or 'heuristic'}); "
        f"password_selector={pass_sel} ({chosen.get('password_source') or 'heuristic'}); "
        f"shared_form={chosen.get('shared_form', False)}; "
        f"steps={json.dumps(step_results, ensure_ascii=False)}; "
        f"submit_selector={submit_result.get('selector', submit_sel or '')}; "
        f"current_url={page.url}"
    )
    return json.dumps({
        "success": auth_result["post_submit_state"].get("state") != "error",
        "message": message,
        "flow_plan": plan,
        "selectors": chosen,
        "fill_results": [{"step": "username_fill", **user_fill}, {"step": "password_fill", **pass_fill}],
        "steps": step_results,
        "submit": submit_result,
        **auth_result,
        "error": None if auth_result["post_submit_state"].get("state") != "error" else auth_result["post_submit_state"].get("reason"),
    }, ensure_ascii=False)


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
    site_profile: Optional[Dict[str, Any]] = None,
) -> str:
    page = _ensure_browser(ctx)
    page.evaluate(_SELECTOR_HELPERS_JS)
    _post_submit_wait(page)
    profile = normalize_site_profile(site_profile)

    current_url = str(page.url or "")
    expected_url = _normalize_selector(profile.get("expected_url_substring") or expected_url_substring).lower()
    login_url_markers = ["login", "sign-in", "signin", "auth", "signup", "register"]
    submitted_from_login_url = any(marker in current_url.lower() for marker in login_url_markers)

    effective_success_selector = profile.get("success_selector") or success_selector
    effective_failure_selector = profile.get("failure_selector") or failure_selector
    effective_logged_out_selector = profile.get("logged_out_selector") or logged_out_selector
    effective_protected_url = profile.get("protected_url") or protected_url

    matched: List[str] = []
    if _safe_selector_presence(page, effective_success_selector, timeout):
        matched.append("success_selector")
    if _safe_selector_presence(page, effective_failure_selector, timeout):
        matched.append("failure_selector")
    if _safe_selector_presence(page, effective_logged_out_selector, timeout):
        matched.append("logged_out_selector")
    if _safe_selector_presence(page, profile.get("captcha_selector", ""), timeout):
        matched.append("captcha_selector")
    if _safe_selector_presence(page, profile.get("mfa_selector", ""), timeout):
        matched.append("mfa_selector")

    signals = page.evaluate(_LOGIN_SIGNALS_JS)
    cookies = []
    try:
        cookies = ctx.browser_state.context.cookies() if ctx.browser_state.context is not None else []
    except Exception:
        log.debug("Failed to read browser cookies during login state check", exc_info=True)

    cookie_names = [str(cookie.get("name") or "") for cookie in cookies if cookie.get("name")]
    redirected_away_from_login = submitted_from_login_url and not any(marker in str(signals.get("url") or page.url).lower() for marker in login_url_markers)
    protected_probe = _check_session_alive_via_protected_url(ctx, protected_url=effective_protected_url, timeout=timeout)
    protected_url_alive = bool(protected_probe.get("alive")) if protected_probe.get("checked") else False

    signals.update({
        "matched": matched,
        "cookie_names": cookie_names,
        "success_cookie_names": list(success_cookie_names or []),
        "failure_text_substrings": list(failure_text_substrings or []),
        "expected_url_substring": profile.get("expected_url_substring") or expected_url_substring or "",
        "expected_url_matched": bool(expected_url and expected_url in str(signals.get("url") or page.url).lower()),
        "body_text": str(signals.get("body_text") or signals.get("title") or ""),
        "submitted_from_login_url": submitted_from_login_url,
        "redirected_away_from_login": redirected_away_from_login,
        "has_error_classes": bool(signals.get("error_class_count") or 0),
        "protected_url_checked": bool(protected_probe.get("checked")),
        "protected_url_alive": protected_url_alive,
    })

    legacy_inferred = infer_login_state(signals)
    snapshot = build_auth_page_snapshot(
        current_url=str(signals.get("url") or page.url),
        page_signals=signals,
        matched=matched,
        profile=profile,
        protected_url_alive=protected_url_alive,
        submitted_from_login_url=submitted_from_login_url,
    )
    auth_state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, auth_state)
    diagnostics = summarize_auth_diagnostics(snapshot, auth_state, next_action)

    result = {
        "state": auth_state["state"],
        "url": signals.get("url", page.url),
        "title": signals.get("title", ""),
        "matched": matched,
        "signals": signals,
        "reason": auth_state["reason"],
        "active_session_name": ctx.browser_state.active_session_name,
        "protected_url_check": protected_probe if protected_probe.get("checked") else None,
        "legacy_state": legacy_inferred["state"],
        "legacy_reason": legacy_inferred["reason"],
        "diagnostics": diagnostics,
        "site_profile": profile,
        "next_action": next_action,
    }
    return json.dumps(result, ensure_ascii=False)


def _auto_solve_captcha_if_present(ctx, page):
    """Silently detect and solve captcha on page before submit. No-op if none found."""
    img_sel = page.evaluate(_CAPTCHA_IMG_HEURISTIC_JS)
    if not img_sel:
        return
    input_sel = page.evaluate(_CAPTCHA_INPUT_HEURISTIC_JS)
    if not input_sel:
        return
    # Check input is empty
    current_value = page.evaluate(f'''
        (function() {{
            var el = document.querySelector({json.dumps(input_sel)});
            return el ? el.value : null;
        }})()
    ''')
    if current_value:
        return

    log.info("Auto-captcha: found empty captcha input, solving")
    try:
        page.evaluate(f'''
            (function() {{
                var el = document.querySelector({json.dumps(img_sel)});
                if (el) el.scrollIntoView({{behavior: "instant", block: "center"}});
            }})()
        ''')
        page.wait_for_timeout(300)
    except Exception:
        pass

    try:
        el = page.wait_for_selector(img_sel, timeout=5000, state="visible")
    except Exception:
        return
    if not el:
        return
    try:
        screenshot_bytes = el.screenshot(type="png")
    except Exception:
        return

    from ouroboros.tools.captcha_solver import solve_captcha_image
    try:
        result = solve_captcha_image(screenshot_bytes)
    except Exception:
        result = {"text": "", "confidence": 0.0}

    text = result.get("text", "")
    confidence = result.get("confidence", 0.0)

    if not text or confidence < 0.3:
        try:
            from ouroboros.tools.captcha_solver import solve_captcha_vision
            vr = solve_captcha_vision(screenshot_bytes)
            if vr.get("text") and vr.get("status") == "ok":
                text = vr["text"]
                confidence = vr.get("confidence", 0.7)
        except Exception:
            pass

    if not text:
        log.warning("auto-captcha: could not solve, leaving empty")
        return

    log.info("auto-captcha: entering '%s' (confidence: %.2f)", text, confidence)
    try:
        page.fill(input_sel, text, timeout=3000)
    except Exception:
        try:
            page.evaluate(f'''
                (function() {{
                    var el = document.querySelector({json.dumps(input_sel)});
                    if (el) {{ el.value = {json.dumps(text)}; el.dispatchEvent(new Event('input', {{bubbles:true}})); }}
                }})()
            ''')
        except Exception as e:
            log.warning("auto-captcha: failed to fill input: %s", e)


def _browser_action(ctx: ToolContext, action: str, selector: str = "",
                    value: str = "", timeout: int = 5000) -> str:
    def _do_action():
        page = _ensure_browser(ctx)

        if action == "click":
            if not selector:
                return "Error: selector required for click"
            try:
                if page.evaluate("!!document.querySelector('form')"):
                    _auto_solve_captcha_if_present(ctx, page)
            except Exception:
                pass
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
    const keywords = ['captcha', 'verify', 'code', 'vcode', 'checkcode', 'seccode', 'imgcode',
        'yzm', 'yanzhengma', 'kaptcha', 'securimage', 'validation',
        'captcha-image', 'verify-code', 'img-code', 'auth-code', 'pic-code'];
    var found = null;
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
            found = img; break;
        }
    }
    if (!found) {
        var canvases = document.querySelectorAll('canvas');
        for (var i = 0; i < canvases.length; i++) {
            var c = canvases[i];
            var attrs = ((c.id || '') + ' ' + (c.className || '') + ' ' + (c.getAttribute('data-role') || '')).toLowerCase();
            if (/captcha|verify|code|auth/.test(attrs)) { found = c; break; }
        }
    }
    if (!found) {
        var divs = document.querySelectorAll('div[style*="background"], span[style*="background"]');
        for (var i = 0; i < divs.length; i++) {
            var d = divs[i];
            var style = d.getAttribute('style') || '';
            var dattrs = ((d.id || '') + ' ' + (d.className || '')).toLowerCase();
            if (/captcha|verify|code/.test(dattrs) && /url\(/.test(style)) { found = d; break; }
        }
    }
    if (!found) return null;
    return window.__veles_build_selector
        ? window.__veles_build_selector(found)
        : (found.id ? '#' + found.id : null);
}"""

_CAPTCHA_INPUT_HEURISTIC_JS = r"""() => {
    const keywords = ['captcha', 'verify', 'code', 'vcode', 'checkcode', 'seccode', 'imgcode',
        'yzm', 'yanzhengma', 'kaptcha', 'securimage', 'validation',
        'captcha-image', 'verify-code', 'img-code', 'auth-code', 'pic-code'];
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
        # Scroll captcha into view before waiting for visibility
        try:
            page.evaluate(f'''
                (function() {{
                    var el = document.querySelector({json.dumps(img_sel)});
                    if (el) el.scrollIntoView({{behavior: "instant", block: "center"}});
                }})()
            ''')
            page.wait_for_timeout(300)
        except Exception:
            pass

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
                from ouroboros.tools.captcha_solver import solve_captcha_vision
                vision_result = solve_captcha_vision(screenshot_bytes)
                vision_text = vision_result.get("text", "")
                if vision_text and vision_result.get("status") == "ok":
                    text = vision_text
                    confidence = vision_result.get("confidence", 0.7)
                    method = "vision_isolated"
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
            page.evaluate(f'''
                (function() {{
                    var el = document.querySelector({json.dumps(input_sel)});
                    if (el) el.scrollIntoView({{behavior: "instant", block: "center"}});
                }})()
            ''')
            page.wait_for_timeout(300)
        except Exception:
            pass
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
    return build_browser_tool_entries(
        browse_page_handler=_browse_page,
        browser_action_handler=_browser_action,
        browser_fill_login_form_handler=_browser_fill_login_form,
        browser_save_session_handler=_browser_save_session,
        browser_restore_session_handler=_browser_restore_session,
        browser_check_login_state_handler=_browser_check_login_state,
        browser_solve_captcha_handler=_browser_solve_captcha,
    )
