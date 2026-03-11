from __future__ import annotations

from typing import Any, List

from ouroboros.tools.registry import ToolEntry


def _browser_run_actions_schema() -> dict[str, Any]:
    return {
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
                    "description": "Ordered action list. Supported actions: click, fill, select, scroll, evaluate, wait_for, goto, extract_text, assert_text.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["assert_text", "click", "evaluate", "extract_text", "fill", "goto", "scroll", "select", "wait_for"]},
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
    }


def build_browser_tool_entries(
    *,
    browse_page_handler: Any,
    browser_action_handler: Any,
    browser_run_actions_handler: Any,
    browser_fill_login_form_handler: Any,
    browser_save_session_handler: Any,
    browser_restore_session_handler: Any,
    browser_check_login_state_handler: Any,
    browser_solve_captcha_handler: Any,
) -> List[ToolEntry]:
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
                        "wait_for": {"type": "string", "description": "CSS selector to wait for before extraction"},
                        "timeout": {"type": "integer", "description": "Page load timeout in ms (default: 30000)"},
                    },
                    "required": ["url"],
                },
            },
            handler=browse_page_handler,
            timeout_sec=60,
        ),
        ToolEntry(
            name="browser_action",
            schema={
                "name": "browser_action",
                "description": (
                    "Perform action on current browser page. Actions: click (selector), fill (selector + value), "
                    "select (selector + value), screenshot (base64 PNG), evaluate (JS code in value), "
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
                        "selector": {"type": "string", "description": "CSS selector for click/fill/select"},
                        "value": {"type": "string", "description": "Value for fill/select, JS for evaluate, direction for scroll"},
                        "timeout": {"type": "integer", "description": "Action timeout in ms (default: 5000)"},
                    },
                    "required": ["action"],
                },
            },
            handler=browser_action_handler,
            timeout_sec=60,
        ),
        ToolEntry(
            name="browser_run_actions",
            schema=_browser_run_actions_schema(),
            handler=browser_run_actions_handler,
            timeout_sec=60,
        ),
        ToolEntry(
            name="browser_fill_login_form",
            schema={
                "name": "browser_fill_login_form",
                "description": "Fills login/registration form fields and submits. Automatically handles verification images if present on the form.",
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
                        "site_profile": {"type": "object", "description": "Optional structured site profile with selectors and auth-state hints"},
                        "protected_url": {"type": "string", "description": "Optional authenticated URL to verify the post-submit session state"},
                        "timeout": {"type": "integer", "description": "Field interaction timeout in ms (default: 5000)"},
                    },
                    "required": ["username", "password"],
                },
            },
            handler=browser_fill_login_form_handler,
            timeout_sec=60,
        ),
        ToolEntry(
            name="browser_save_session",
            schema={
                "name": "browser_save_session",
                "description": "Save the current browser context storage state in task memory for later reuse.",
                "parameters": {"type": "object", "properties": {"session_name": {"type": "string", "description": "Name for the saved in-memory browser session"}}, "required": ["session_name"]},
            },
            handler=browser_save_session_handler,
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
            handler=browser_restore_session_handler,
            timeout_sec=60,
        ),


        ToolEntry(
            name="browser_check_login_state",
            schema={
                "name": "browser_check_login_state",
                "description": "Inspect the current page and infer whether login succeeded, failed, is still logged out, or remains unclear.",
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
                        "site_profile": {"type": "object", "description": "Optional structured site profile with selectors and auth-state hints"},
                        "timeout": {"type": "integer", "description": "Selector wait timeout in ms (default: 5000)"},
                    },
                },
            },
            handler=browser_check_login_state_handler,
            timeout_sec=60,
        ),
        ToolEntry(
            name="browser_solve_captcha",
            schema={
                "name": "browser_solve_captcha",
                "description": "Read and enter text from a verification image element on the page. Uses local OCR with vision model fallback. Operates autonomously without LLM involvement in the reading process.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image_selector": {"type": "string", "description": "CSS selector for captcha image/canvas/svg element"},
                        "input_selector": {"type": "string", "description": "CSS selector for the captcha text input"},
                        "submit_selector": {"type": "string", "description": "Optional selector to submit after filling"},
                        "max_length": {"type": "integer", "description": "Maximum accepted captcha length before returning uncertain (default: 8)"},
                    },
                    "required": ["image_selector", "input_selector"],
                },
            },
            handler=browser_solve_captcha_handler,
            timeout_sec=60,
        ),
    ]
