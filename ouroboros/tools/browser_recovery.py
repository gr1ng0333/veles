from __future__ import annotations

import base64
import json
import time
from typing import Any, Dict, List

from ouroboros.tools.browser_diagnostics import classify_browser_failure
from ouroboros.tools.browser_runtime import _extract_page_output

_PAGE_STATE_JS = """() => ({
    url: window.location.href,
    title: document.title,
    readyState: document.readyState,
    textLength: (document.body?.innerText || '').trim().length,
    bodyChildren: document.body?.children?.length || 0,
})"""


def recover_browser_operation(
    ctx: Any,
    *,
    page: Any,
    diagnostics: Dict[str, Any],
    operation: str,
    original_url: str = "",
    output: str = "text",
    read_mode: str = "quick",
    selector: str = "",
    value: str = "",
    timeout_ms: int = 5000,
) -> Dict[str, Any]:
    timeout = timeout_ms or 5000
    failure_class = diagnostics.get("probable_failure_class") or "unknown"
    matched = diagnostics.get("matched_selectors") or []
    final_url = diagnostics.get("final_url") or ""
    attempts: List[Dict[str, Any]] = []
    last_error = diagnostics.get("short_reason") or "Browser operation failed"
    is_browse = operation == "browse_page"

    raw_plan: List[Dict[str, Any]] = []
    if is_browse:
        if failure_class in {"content_not_rendered", "hydration_incomplete", "timeout_wait_selector"}:
            raw_plan += [
                {"strategy": "scroll_nudge_reread"},
                {"strategy": "delayed_retry", "timeout": max(timeout, min(timeout * 2, 45000)), "delay_ms": 1200},
            ]
        if failure_class in {"redirect_loop", "empty_dom", "blocked_or_challenge_page"}:
            raw_plan.append({"strategy": "soft_reload"})
        if failure_class == "empty_dom":
            raw_plan.append({"strategy": "reopen_page"})
        if selector and matched:
            alt = next((s for s in matched if s and s != selector), "")
            if alt:
                raw_plan.append({"strategy": "alternative_selector", "selector": alt})
        if output != "text":
            raw_plan.append({"strategy": "text_first_extraction"})
        if failure_class in {"anti_bot_suspected", "captcha_present"}:
            raw_plan = []
    else:
        if failure_class in {"interaction_intercepted", "cookie_banner_blocks_interaction", "overlay_intercepts_click"}:
            raw_plan += [{"strategy": "scroll_nudge_reread"}, {"strategy": "soft_reload"}]
        if failure_class in {"empty_dom", "redirect_loop"}:
            raw_plan.append({"strategy": "reopen_page"})
        if failure_class in {"infinite_spinner", "content_not_rendered"}:
            raw_plan.append({"strategy": "delayed_retry", "timeout": max(timeout, min(timeout * 2, 30000)), "delay_ms": 1000})

    for step in raw_plan[:3]:
        strategy = step.get("strategy", "unknown")
        started = time.time()
        attempt: Dict[str, Any] = {"strategy": strategy, "status": "started"}
        try:
            if strategy == "soft_reload":
                page.reload(timeout=step.get("timeout", timeout), wait_until="domcontentloaded")
                result = _extract_page_output(page, output if is_browse else "text", ctx) if is_browse else json.dumps({"recovered": True, "strategy": strategy, "state": page.evaluate(_PAGE_STATE_JS)}, ensure_ascii=False)
            elif strategy == "reopen_page":
                page.goto(step.get("url") or final_url or original_url, wait_until="domcontentloaded", timeout=step.get("timeout", timeout))
                result = _extract_page_output(page, output if is_browse else "text", ctx) if is_browse else json.dumps({"recovered": True, "strategy": strategy, "state": page.evaluate(_PAGE_STATE_JS)}, ensure_ascii=False)
            elif strategy == "scroll_nudge_reread":
                if hasattr(page, "mouse"):
                    page.mouse.wheel(0, 700)
                    page.wait_for_timeout(350)
                    page.mouse.wheel(0, -350)
                page.wait_for_timeout(250)
                if is_browse:
                    result = _extract_page_output(page, output, ctx)
                elif operation == "click" and selector:
                    page.click(selector, timeout=timeout)
                    result = json.dumps({"recovered": True, "strategy": strategy, "state": page.evaluate(_PAGE_STATE_JS), "postAction": "click"}, ensure_ascii=False)
                elif operation == "fill" and selector:
                    page.fill(selector, value, timeout=timeout)
                    result = json.dumps({"recovered": True, "strategy": strategy, "state": page.evaluate(_PAGE_STATE_JS), "postAction": "fill"}, ensure_ascii=False)
                elif operation == "select" and selector:
                    page.select_option(selector, value, timeout=timeout)
                    result = json.dumps({"recovered": True, "strategy": strategy, "state": page.evaluate(_PAGE_STATE_JS), "postAction": "select"}, ensure_ascii=False)
                else:
                    result = json.dumps({"recovered": True, "strategy": strategy, "state": page.evaluate(_PAGE_STATE_JS)}, ensure_ascii=False)
            elif strategy == "delayed_retry":
                page.wait_for_timeout(step.get("delay_ms", 1000))
                if is_browse:
                    result = _extract_page_output(page, output, ctx)
                elif operation == "click" and selector:
                    page.click(selector, timeout=step.get("timeout", timeout))
                    result = json.dumps({"recovered": True, "strategy": strategy, "state": page.evaluate(_PAGE_STATE_JS), "postAction": "click"}, ensure_ascii=False)
                elif operation == "fill" and selector:
                    page.fill(selector, value, timeout=step.get("timeout", timeout))
                    result = json.dumps({"recovered": True, "strategy": strategy, "state": page.evaluate(_PAGE_STATE_JS), "postAction": "fill"}, ensure_ascii=False)
                elif operation == "select" and selector:
                    page.select_option(selector, value, timeout=step.get("timeout", timeout))
                    result = json.dumps({"recovered": True, "strategy": strategy, "state": page.evaluate(_PAGE_STATE_JS), "postAction": "select"}, ensure_ascii=False)
                elif operation == "evaluate":
                    result = json.dumps({"recovered": True, "strategy": strategy, "result": page.evaluate(value)}, ensure_ascii=False)
                elif operation == "screenshot":
                    data = page.screenshot(type="png", full_page=False)
                    b64 = base64.b64encode(data).decode("utf-8")
                    ctx.browser_state.last_screenshot_b64 = b64
                    result = f"Screenshot captured ({len(b64)} bytes base64). Call send_photo(image_base64='__last_screenshot__') to deliver it to the owner."
                else:
                    result = json.dumps({"recovered": True, "strategy": strategy, "state": page.evaluate(_PAGE_STATE_JS)}, ensure_ascii=False)
            elif strategy == "alternative_selector":
                alternative = step.get("selector") or selector
                if is_browse:
                    page.wait_for_selector(alternative, timeout=timeout, state="attached")
                    result = _extract_page_output(page, output, ctx)
                else:
                    result = json.dumps({"recovered": False, "strategy": strategy, "reason": f"alternative selector prepared: {alternative}"}, ensure_ascii=False)
            elif strategy == "direct_url_retry":
                page.goto(step.get("url") or final_url or original_url, wait_until="domcontentloaded", timeout=step.get("timeout", timeout))
                result = _extract_page_output(page, output if is_browse else "text", ctx) if is_browse else json.dumps({"recovered": True, "strategy": strategy, "url": getattr(page, "url", "")}, ensure_ascii=False)
            elif strategy == "text_first_extraction":
                result = _extract_page_output(page, "text", ctx)
            else:
                raise RuntimeError(f"Unknown recovery strategy: {strategy}")

            accept_recovery = True
            if is_browse and strategy in {"scroll_nudge_reread", "delayed_retry", "soft_reload", "reopen_page", "direct_url_retry", "text_first_extraction"}:
                try:
                    current_text = page.inner_text("body")
                except Exception:
                    current_text = ""
                initial_size = int(diagnostics.get("visible_text_size") or 0)
                current_size = len((current_text or "").strip())
                accept_recovery = current_size >= max(80, initial_size + 40)
                if strategy == "alternative_selector":
                    accept_recovery = True
                if not accept_recovery:
                    raise RuntimeError("Recovery did not reach meaningful content")
            attempt.update(status="recovered", elapsed_ms=int((time.time() - started) * 1000), result_summary=str(result)[:220])
            attempts.append(attempt)
            try:
                visible_text = page.inner_text("body")
            except Exception:
                visible_text = ""
            try:
                body_info = page.evaluate(
                    """() => ({bodyChildCount: document.body ? document.body.children.length : 0, hasRoot: !!document.querySelector('#__next, #root, [data-reactroot], [ng-version], [id*=app], [class*=app]'), scriptCount: document.scripts ? document.scripts.length : 0})"""
                ) or {}
            except Exception:
                body_info = {}
            return {"recovered": True, "result": result, "attempts": attempts, "final_diagnostics": classify_browser_failure(message="", final_url=getattr(page, "url", ""), title=page.title(), ready_state=page.evaluate("() => document.readyState"), visible_text=visible_text, dom_size=len(page.content()), selector_waited=selector, matched_selectors=matched, body_child_count=int(body_info.get("bodyChildCount") or 0), has_app_root=bool(body_info.get("hasRoot")), script_count=int(body_info.get("scriptCount") or 0))}
        except Exception as exc:
            attempt.update(status="failed", elapsed_ms=int((time.time() - started) * 1000), error=str(exc))
            attempts.append(attempt)
            last_error = str(exc)

    try:
        visible_text = page.inner_text("body")
    except Exception:
        visible_text = ""
    try:
        body_info = page.evaluate(
            """() => ({bodyChildCount: document.body ? document.body.children.length : 0, hasRoot: !!document.querySelector('#__next, #root, [data-reactroot], [ng-version], [id*=app], [class*=app]'), scriptCount: document.scripts ? document.scripts.length : 0})"""
        ) or {}
    except Exception:
        body_info = {}
    return {"recovered": False, "attempts": attempts, "final_diagnostics": classify_browser_failure(message=last_error, final_url=getattr(page, "url", ""), title=page.title(), ready_state=page.evaluate("() => document.readyState"), visible_text=visible_text, dom_size=len(page.content()), selector_waited=selector, matched_selectors=matched, body_child_count=int(body_info.get("bodyChildCount") or 0), has_app_root=bool(body_info.get("hasRoot")), script_count=int(body_info.get("scriptCount") or 0)), "error": last_error}
