from __future__ import annotations

import base64
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.browser_runtime import _ensure_browser
from ouroboros.tools.registry import ToolContext, ToolEntry

_ALLOWED_ACTIONS = {"assert_text", "click", "extract_text", "fill", "goto", "screenshot", "scroll", "select", "evaluate", "wait_for", "wait_for_text", "wait_for_url"}
_ALLOWED_WAIT_FOR_STATES = {"attached", "detached", "hidden", "visible"}


def _coerce_steps(actions: Any) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    if not isinstance(actions, list) or not actions:
        return None, "actions must be a non-empty array"
    normalized: List[Dict[str, Any]] = []
    for idx, raw in enumerate(actions):
        if not isinstance(raw, dict):
            return None, f"action #{idx + 1} must be an object"
        action = str(raw.get("action") or "").strip()
        if action not in _ALLOWED_ACTIONS:
            return None, f"action #{idx + 1} has unsupported action '{action}'"
        text_must_absent = bool(raw.get("text_must_absent") or False)
        url_must_absent = bool(raw.get("url_must_absent") or False)
        step = {
            "action": action,
            "selector": str(raw.get("selector") or "").strip(),
            "value": raw.get("value"),
            "timeout": int(raw.get("timeout") or 5000),
            "label": str(raw.get("label") or "").strip(),
            "expect_selector": str(raw.get("expect_selector") or "").strip(),
            "expect_selector_state": str(raw.get("expect_selector_state") or "").strip() or "visible",
            "expect_url_substring": str(raw.get("expect_url_substring") or "").strip(),
            "wait_for_navigation": bool(raw.get("wait_for_navigation") or False),
            "wait_until": str(raw.get("wait_until") or "").strip() or "load",
            "wait_for_state": str(raw.get("wait_for_state") or "").strip() or "visible",
            "match_substring": bool(raw.get("match_substring") if "match_substring" in raw else True),
            "text_must_absent": text_must_absent,
            "url_must_absent": url_must_absent,
        }
        if action == "wait_for_url" and text_must_absent and not url_must_absent:
            step["url_must_absent"] = True
        expect_selector_state = step["expect_selector_state"]
        if expect_selector_state not in _ALLOWED_WAIT_FOR_STATES:
            return None, f"action #{idx + 1} has unsupported expect_selector_state '{expect_selector_state}'"
        if action == "wait_for":
            wait_for_state = step["wait_for_state"]
            if wait_for_state not in _ALLOWED_WAIT_FOR_STATES:
                return None, f"action #{idx + 1} has unsupported wait_for_state '{wait_for_state}'"
        normalized.append(step)
    return normalized, None


def _scroll_page(page: Any, direction: str) -> None:
    if direction == "top":
        page.evaluate("window.scrollTo(0, 0)")
    elif direction == "bottom":
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    elif direction == "up":
        page.evaluate("window.scrollBy(0, -window.innerHeight)")
    elif direction == "down":
        page.evaluate("window.scrollBy(0, window.innerHeight)")
    else:
        raise ValueError("scroll action requires value: up/down/top/bottom")


def _extract_text(page: Any, selector: str, timeout: int) -> str:
    if not selector:
        raise ValueError("text action requires selector")
    page.wait_for_selector(selector, timeout=timeout, state="visible")
    locator = page.locator(selector).first
    text = locator.inner_text(timeout=timeout)
    if text is None:
        return ""
    return str(text).strip()


def _wait_for_text(page: Any, selector: str, expected: str, timeout: int, *, match_substring: bool = True, text_must_absent: bool = False) -> Dict[str, Any]:
    if not selector:
        raise ValueError("wait_for_text action requires selector")
    if not expected:
        raise ValueError("wait_for_text action requires value")

    deadline = time.monotonic() + max(timeout, 0) / 1000.0
    last_text = ""
    last_error = ""
    while True:
        try:
            current = _extract_text(page, selector, timeout=max(50, min(timeout, 250)))
            last_text = current
            matched = (expected not in current) if text_must_absent else ((expected in current) if match_substring else (current == expected))
            if matched:
                return {
                    "text": current,
                    "expected_text": expected,
                    "match_substring": match_substring,
                    "text_must_absent": text_must_absent,
                    "wait_for_text_matched": True,
                }
        except Exception as exc:
            last_error = str(exc)
            if text_must_absent:
                return {
                    "text": "",
                    "expected_text": expected,
                    "match_substring": match_substring,
                    "text_must_absent": text_must_absent,
                    "wait_for_text_matched": True,
                    "wait_for_text_note": last_error,
                }
        if time.monotonic() >= deadline:
            detail = {
                "text": last_text,
                "expected_text": expected,
                "match_substring": match_substring,
                "text_must_absent": text_must_absent,
                "wait_for_text_matched": False,
            }
            if last_error:
                detail["wait_for_text_error"] = last_error
            return detail
        page.wait_for_timeout(100)


def _wait_for_url(page: Any, expected: str, timeout: int, *, match_substring: bool = True, url_must_absent: bool = False) -> Dict[str, Any]:
    if not expected:
        raise ValueError("wait_for_url action requires value")

    deadline = time.monotonic() + max(timeout, 0) / 1000.0
    last_url = str(page.url or "")
    while True:
        current_url = str(page.url or "")
        last_url = current_url
        matched = (expected not in current_url) if url_must_absent else ((expected in current_url) if match_substring else (current_url == expected))
        if matched:
            return {
                "url": current_url,
                "expected_url": expected,
                "match_substring": match_substring,
                "url_must_absent": url_must_absent,
                "wait_for_url_matched": True,
            }
        if time.monotonic() >= deadline:
            return {
                "url": last_url,
                "expected_url": expected,
                "match_substring": match_substring,
                "url_must_absent": url_must_absent,
                "wait_for_url_matched": False,
            }
        page.wait_for_timeout(100)


def _run_single_step(page: Any, step: Dict[str, Any]) -> Dict[str, Any]:
    action = step["action"]
    selector = step.get("selector") or ""
    value = step.get("value")
    timeout = int(step.get("timeout") or 5000)

    if action == "click":
        if not selector:
            raise ValueError("click action requires selector")
        page.click(selector, timeout=timeout)
    elif action == "fill":
        if not selector:
            raise ValueError("fill action requires selector")
        page.fill(selector, "" if value is None else str(value), timeout=timeout)
    elif action == "select":
        if not selector:
            raise ValueError("select action requires selector")
        page.select_option(selector, "" if value is None else str(value), timeout=timeout)
    elif action == "evaluate":
        if value is None or not str(value).strip():
            raise ValueError("evaluate action requires value")
        return {"evaluation_result": page.evaluate(str(value))}
    elif action == "screenshot":
        data = page.screenshot(type="png", full_page=False)
        b64 = base64.b64encode(data).decode()
        return {
            "screenshot_captured": True,
            "screenshot_base64_bytes": len(b64),
            "last_screenshot_updated": True,
            "screenshot_delivery_hint": "Call send_photo(image_base64='__last_screenshot__') to deliver it to the owner.",
            "_last_screenshot_b64": b64,
        }
    elif action == "extract_text":
        return {"text": _extract_text(page, selector, timeout)}
    elif action == "assert_text":
        expected = "" if value is None else str(value)
        if not expected:
            raise ValueError("assert_text action requires value")
        return {"text": _extract_text(page, selector, timeout), "expected_text": expected}
    elif action == "wait_for_text":
        expected = "" if value is None else str(value)
        return _wait_for_text(
            page,
            selector,
            expected,
            timeout,
            match_substring=bool(step.get("match_substring") if "match_substring" in step else True),
            text_must_absent=bool(step.get("text_must_absent") or False),
        )
    elif action == "wait_for_url":
        expected = "" if value is None else str(value)
        return _wait_for_url(
            page,
            expected,
            timeout,
            match_substring=bool(step.get("match_substring") if "match_substring" in step else True),
            url_must_absent=bool(step.get("url_must_absent") or False),
        )
    elif action == "scroll":
        _scroll_page(page, str(value or "").strip().lower())
    elif action == "wait_for":
        if selector:
            wait_for_state = str(step.get("wait_for_state") or "visible").strip() or "visible"
            page.wait_for_selector(selector, timeout=timeout, state=wait_for_state)
            return {"wait_for_state": wait_for_state, "wait_for_selector": selector}
        page.wait_for_timeout(timeout)
    elif action == "goto":
        url = "" if value is None else str(value).strip()
        if not url:
            raise ValueError("goto action requires value")
        wait_until = str(step.get("wait_until") or "load").strip() or "load"
        page.goto(url, timeout=timeout, wait_until=wait_until)
        return {"navigated_to": page.url, "wait_until": wait_until}
    else:
        raise ValueError(f"unsupported action: {action}")
    return {}


def _verify_step(page: Any, step: Dict[str, Any], *, previous_url: str = "", execution: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    verified = True
    checks: Dict[str, Any] = {}
    expect_selector = step.get("expect_selector") or ""
    expect_url_substring = step.get("expect_url_substring") or ""
    timeout = int(step.get("timeout") or 5000)

    if step.get("wait_for_navigation"):
        current_url = str(page.url or "")
        matched = bool(current_url) and current_url != str(previous_url or "")
        if not matched and hasattr(page, "wait_for_function"):
            try:
                page.wait_for_function(
                    "previousUrl => window.location.href !== previousUrl",
                    arg=str(previous_url or ""),
                    timeout=timeout,
                )
                current_url = str(page.url or "")
                matched = bool(current_url) and current_url != str(previous_url or "")
            except Exception as exc:
                checks["wait_for_navigation"] = {
                    "matched": False,
                    "previous_url": str(previous_url or ""),
                    "current_url": current_url,
                    "error": str(exc),
                }
                verified = False
        if "wait_for_navigation" not in checks:
            checks["wait_for_navigation"] = {
                "matched": matched,
                "previous_url": str(previous_url or ""),
                "current_url": current_url,
            }
            verified = verified and matched

    action = step.get("action") or ""
    if action in {"assert_text", "wait_for_text"}:
        expected_text = "" if step.get("value") is None else str(step.get("value"))
        match_substring = bool(step.get("match_substring") if "match_substring" in step else True)
        text_must_absent = bool(step.get("text_must_absent") or False)
        execution_text = execution.get("text") if isinstance(execution, dict) and "text" in execution else None
        extracted = str(execution_text) if execution_text is not None else _extract_text(page, step.get("selector") or "", timeout)
        matched = (expected_text not in extracted) if text_must_absent else ((expected_text in extracted) if match_substring else (extracted == expected_text))
        checks[action] = {
            "selector": step.get("selector") or "",
            "expected_text": expected_text,
            "matched": matched,
            "match_substring": match_substring,
            "text_must_absent": text_must_absent,
            "text": extracted,
        }
        if isinstance(execution, dict):
            if "wait_for_text_error" in execution:
                checks[action]["error"] = execution["wait_for_text_error"]
            if "wait_for_text_note" in execution:
                checks[action]["note"] = execution["wait_for_text_note"]
        verified = verified and matched

    if action == "wait_for_url":
        expected_url = "" if step.get("value") is None else str(step.get("value"))
        match_substring = bool(step.get("match_substring") if "match_substring" in step else True)
        url_must_absent = bool(step.get("url_must_absent") or False)
        execution_url = execution.get("url") if isinstance(execution, dict) and "url" in execution else None
        current_url = str(execution_url) if execution_url is not None else str(page.url or "")
        matched = (expected_url not in current_url) if url_must_absent else ((expected_url in current_url) if match_substring else (current_url == expected_url))
        checks["wait_for_url"] = {
            "expected_url": expected_url,
            "matched": matched,
            "match_substring": match_substring,
            "url_must_absent": url_must_absent,
            "url": current_url,
        }
        verified = verified and matched

    if expect_selector:
        expect_selector_state = str(step.get("expect_selector_state") or "visible").strip() or "visible"
        try:
            page.wait_for_selector(expect_selector, timeout=timeout, state=expect_selector_state)
            checks["expect_selector"] = {"selector": expect_selector, "state": expect_selector_state, "matched": True}
        except Exception as exc:
            verified = False
            checks["expect_selector"] = {"selector": expect_selector, "state": expect_selector_state, "matched": False, "error": str(exc)}

    if expect_url_substring:
        current_url = page.url
        matched = expect_url_substring in current_url
        checks["expect_url_substring"] = {
            "substring": expect_url_substring,
            "matched": matched,
            "current_url": current_url,
        }
        verified = verified and matched

    return {"verified": verified, "checks": checks}


def _browser_run_actions(
    ctx: ToolContext,
    actions: List[Dict[str, Any]],
    stop_on_error: bool = True,
) -> str:
    steps, error = _coerce_steps(actions)
    if error:
        return f"Error: {error}"

    page = _ensure_browser(ctx)
    results: List[Dict[str, Any]] = []
    overall_success = True

    for index, step in enumerate(steps or [], start=1):
        item: Dict[str, Any] = {
            "index": index,
            "action": step["action"],
            "label": step.get("label") or "",
            "selector": step.get("selector") or "",
            "current_url": page.url,
        }
        try:
            previous_url = str(page.url or "")
            execution = _run_single_step(page, step)
            screenshot_b64 = execution.pop("_last_screenshot_b64", None) if isinstance(execution, dict) else None
            if screenshot_b64:
                ctx.browser_state.last_screenshot_b64 = screenshot_b64
            item.update(execution)
            verification = _verify_step(page, step, previous_url=previous_url, execution=execution if isinstance(execution, dict) else None)
            item.update(verification)
            item["success"] = bool(verification["verified"])
            item["current_url"] = page.url
        except Exception as exc:
            overall_success = False
            item.update({"success": False, "verified": False, "error": str(exc), "current_url": page.url})
            results.append(item)
            if stop_on_error:
                break
            continue

        results.append(item)
        if not item["success"]:
            overall_success = False
            if stop_on_error:
                break

    payload = {
        "success": overall_success and len(results) == len(steps or []),
        "executed_steps": len(results),
        "requested_steps": len(steps or []),
        "stopped_early": len(results) != len(steps or []),
        "current_url": page.url,
        "active_session_name": ctx.browser_state.active_session_name,
        "results": results,
    }
    return json.dumps(payload, ensure_ascii=False)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="browser_run_actions",
            schema={
                "name": "browser_run_actions",
                "description": (
                    "Run a reusable batch of browser actions against the current live/restored browser session, "
                    "with per-step verification and structured results."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "actions": {
                            "type": "array",
                            "description": "Ordered action list. Supported actions: click, fill, select, scroll, evaluate, screenshot, wait_for, goto, extract_text, assert_text, wait_for_text, wait_for_url.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "action": {"type": "string", "enum": sorted(_ALLOWED_ACTIONS)},
                                    "selector": {"type": "string"},
                                    "value": {"type": ["string", "number", "boolean"]},
                                    "timeout": {"type": "integer", "description": "Timeout in ms for the step (default: 5000)"},
                                    "label": {"type": "string", "description": "Optional human-readable step label"},
                                    "expect_selector": {"type": "string", "description": "Optional selector that must become visible after the step"},
                                    "expect_url_substring": {"type": "string", "description": "Optional URL substring expected after the step"},
                                    "wait_for_navigation": {"type": "boolean", "description": "Wait for page URL to change/become available after the step"},
                                    "wait_until": {"type": "string", "enum": ["commit", "domcontentloaded", "load", "networkidle"], "description": "Navigation readiness target for goto (default: load)"},
                                    "match_substring": {"type": "boolean", "description": "For assert_text: substring match (default: true). If false, require exact equality."},
                                    "text_must_absent": {"type": "boolean", "description": "For assert_text: require expected text to be absent instead of present."},
                                },
                                "required": ["action"],
                            },
                        },
                        "stop_on_error": {"type": "boolean", "description": "Stop at first failed step or failed verification (default: true)"},
                    },
                    "required": ["actions"],
                },
            },
            handler=_browser_run_actions,
            timeout_sec=60,
        )
    ]
