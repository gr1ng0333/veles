import json

from ouroboros.tools.browser_auth_flow import (
    build_auth_page_snapshot,
    build_next_action_plan,
    infer_auth_state,
    normalize_site_profile,
    summarize_auth_diagnostics,
)


def test_normalize_site_profile_keeps_known_fields_only():
    profile = normalize_site_profile(
        {
            "domain": "example.com",
            "login_mode": "signup",
            "username_selector": "#email",
            "password_selector": "#password",
            "submit_selector": "button[type=submit]",
            "success_selector": ".dashboard",
            "captcha_selector": ".captcha-box",
            "unknown": "ignored",
        }
    )

    assert profile["domain"] == "example.com"
    assert profile["login_mode"] == "signup"
    assert profile["captcha_selector"] == ".captcha-box"
    assert "unknown" not in profile


def test_infer_auth_state_detects_captcha_before_success_guess():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/login",
        page_signals={"title": "Login", "body_text": "Please verify you are human"},
        matched=["captcha_selector"],
        profile=normalize_site_profile({"captcha_selector": ".captcha-box"}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )

    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)

    assert state["state"] == "captcha"
    assert next_action["action"] == "solve_captcha"


def test_infer_auth_state_detects_mfa_step():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/verify",
        page_signals={"title": "Verify code", "body_text": "Enter the 6-digit code"},
        matched=["mfa_selector"],
        profile=normalize_site_profile({"mfa_selector": "input[name=otp]"}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )

    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)

    assert state["state"] == "mfa"
    assert next_action["action"] == "wait_for_mfa"


def test_infer_auth_state_detects_logged_in_from_success_and_protected_url():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/app",
        page_signals={"title": "Dashboard", "body_text": "Welcome back", "cookie_names": ["session"]},
        matched=["success_selector"],
        profile=normalize_site_profile(
            {
                "success_selector": ".dashboard",
                "success_cookie_names": ["session"],
                "protected_url": "https://example.com/app",
            }
        ),
        protected_url_alive=True,
        submitted_from_login_url=True,
    )

    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    diagnostics = summarize_auth_diagnostics(snapshot, state, next_action)

    assert state["state"] == "logged_in"
    assert next_action["action"] == "continue"
    assert diagnostics["confidence"] == "high"
    assert "protected_url_alive" in diagnostics["evidence"]


def test_infer_auth_state_detects_logged_out_and_returns_fill_form_plan():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/login",
        page_signals={"title": "Sign in", "body_text": "Sign in to continue"},
        matched=["logged_out_selector"],
        profile=normalize_site_profile(
            {
                "logged_out_selector": "form.login",
                "username_selector": "#email",
                "password_selector": "#password",
                "submit_selector": "button[type=submit]",
            }
        ),
        protected_url_alive=False,
        submitted_from_login_url=False,
    )

    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)

    assert state["state"] == "logged_out"
    assert next_action["action"] == "fill_login_form"
    assert next_action["selectors"]["username_selector"] == "#email"


def test_infer_auth_state_detects_error_from_failure_signal():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/login",
        page_signals={
            "title": "Login error",
            "body_text": "Invalid password",
            "failure_text_substrings": ["invalid password"],
        },
        matched=["failure_selector"],
        profile=normalize_site_profile({"failure_selector": ".error"}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )

    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)

    assert state["state"] == "error"
    assert next_action["action"] == "inspect_error"


def test_browser_fill_login_form_returns_profile_aware_plan_without_browser():
    from ouroboros.tools.browser import _browser_fill_login_form

    class DummyCtx:
        browser_state = None

    payload = json.loads(
        _browser_fill_login_form(
            DummyCtx(),
            username="u",
            password="p",
            site_profile={
                "domain": "example.com",
                "username_selector": "#email",
                "password_selector": "#password",
                "submit_selector": "button[type=submit]",
            },
        )
    )

    assert payload["state"] == "planned"
    assert payload["next_action"]["action"] == "fill_login_form"
    assert payload["used_selectors"]["username_selector"] == "#email"
