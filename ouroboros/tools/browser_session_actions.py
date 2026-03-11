from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.browser_runtime import _ensure_browser
from ouroboros.tools.registry import ToolContext, ToolEntry

_ALLOWED_ACTIONS = {"click", "fill", "select", "scroll", "evaluate", "wait_for"}


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
        step = {
            "action": action,
            "selector": str(raw.get("selector") or "").strip(),
            "value": raw.get("value"),
            "timeout": int(raw.get("timeout") or 5000),
            "label": str(raw.get("label") or "").strip(),
            "expect_selector": str(raw.get("expect_selector") or "").strip(),
            "expect_url_substring": str(raw.get("expect_url_substring") or "").strip(),
        }
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
    elif action == "scroll":
        _scroll_page(page, str(value or "").strip().lower())
    elif action == "wait_for":
        if selector:
            page.wait_for_selector(selector, timeout=timeout, state="visible")
        else:
            page.wait_for_timeout(timeout)
    else:
        raise ValueError(f"unsupported action: {action}")
    return {}


def _verify_step(page: Any, step: Dict[str, Any]) -> Dict[str, Any]:
    verified = True
    checks: Dict[str, Any] = {}
    expect_selector = step.get("expect_selector") or ""
    expect_url_substring = step.get("expect_url_substring") or ""

    if expect_selector:
        try:
            page.wait_for_selector(expect_selector, timeout=step.get("timeout") or 5000, state="visible")
            checks["expect_selector"] = {"selector": expect_selector, "matched": True}
        except Exception as exc:
            verified = False
            checks["expect_selector"] = {"selector": expect_selector, "matched": False, "error": str(exc)}

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
            execution = _run_single_step(page, step)
            item.update(execution)
            verification = _verify_step(page, step)
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
                            "description": "Ordered action list. Supported actions: click, fill, select, scroll, evaluate, wait_for.",
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
