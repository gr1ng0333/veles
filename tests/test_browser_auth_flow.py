import json

from ouroboros.tools.browser_auth_flow import (
    build_auth_outcome,
    build_auth_page_snapshot,
    build_next_action_plan,
    build_verification_attempt_plan,
    build_verification_attempt_result,
    build_verification_boundary,
    build_verification_continuation,
    build_verification_handoff,
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


def test_infer_auth_state_uses_success_cookie_hits_when_login_form_is_gone():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/app",
        page_signals={
            "title": "Dashboard",
            "body_text": "Welcome",
            "cookie_names": ["sessionid"],
            "success_cookie_names": ["sessionid"],
            "login_form_visible": False,
            "visible_password_fields": 0,
        },
        matched=[],
        profile=normalize_site_profile({"success_cookie_names": ["sessionid"]}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )

    state = infer_auth_state(snapshot)
    diagnostics = summarize_auth_diagnostics(snapshot, state, build_next_action_plan(snapshot, state))

    assert state["state"] == "logged_in"
    assert "success_cookie" in diagnostics["evidence"]


def test_infer_auth_state_does_not_treat_cookie_as_success_while_login_form_visible():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/login",
        page_signals={
            "title": "Sign in",
            "body_text": "Sign in",
            "cookie_names": ["sessionid"],
            "success_cookie_names": ["sessionid"],
            "login_form_visible": True,
            "visible_login_inputs": 1,
            "visible_password_fields": 1,
        },
        matched=[],
        profile=normalize_site_profile({
            "success_cookie_names": ["sessionid"],
            "username_selector": "#email",
            "password_selector": "#password",
        }),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )

    state = infer_auth_state(snapshot)

    assert state["state"] == "login_form"


def test_infer_auth_state_uses_failure_text_from_runtime_signals():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/login",
        page_signals={
            "title": "Login",
            "body_text": "Account locked",
            "failure_text_substrings": ["account locked"],
            "error_texts": ["Account locked"],
            "login_form_visible": False,
            "visible_password_fields": 0,
        },
        matched=[],
        profile=normalize_site_profile({}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )

    state = infer_auth_state(snapshot)
    diagnostics = summarize_auth_diagnostics(snapshot, state, build_next_action_plan(snapshot, state))

    assert state["state"] == "error"
    assert "failure_text" in diagnostics["evidence"]


def test_build_verification_boundary_detects_captcha_as_auto_attemptable_blocker():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/login",
        page_signals={"title": "Login", "body_text": "Please verify you are human"},
        matched=["captcha_selector"],
        profile=normalize_site_profile({"captcha_selector": ".captcha-box", "submit_selector": "button[type=submit]"}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)

    verification = build_verification_boundary(snapshot, state, next_action)

    assert verification["detected"] is True
    assert verification["kind"] == "captcha"
    assert verification["can_auto_attempt"] is True
    assert verification["requires_owner_input"] is False
    assert verification["blocks_progress"] is True
    assert verification["recommended_action"] == "solve_captcha"
    assert verification["selectors"]["captcha_selector"] == ".captcha-box"


def test_build_verification_boundary_detects_mfa_as_owner_boundary():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/verify",
        page_signals={"title": "Verify", "body_text": "Enter the verification code from your app"},
        matched=["mfa_selector"],
        profile=normalize_site_profile({"mfa_selector": "input[name=otp]", "submit_selector": "button[type=submit]"}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)

    verification = build_verification_boundary(snapshot, state, next_action)

    assert verification["detected"] is True
    assert verification["kind"] == "mfa"
    assert verification["can_auto_attempt"] is False
    assert verification["requires_owner_input"] is True
    assert verification["blocks_progress"] is True
    assert verification["recommended_action"] == "wait_for_mfa"
    assert verification["selectors"]["mfa_selector"] == "input[name=otp]"


def test_summarize_auth_diagnostics_includes_verification_boundary_even_when_absent():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/app",
        page_signals={"title": "Dashboard", "body_text": "Welcome back"},
        matched=["success_selector"],
        profile=normalize_site_profile({"success_selector": ".dashboard"}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)

    diagnostics = summarize_auth_diagnostics(snapshot, state, next_action)

    assert diagnostics["verification"]["detected"] is False
    assert diagnostics["verification"]["kind"] == "none"
    assert diagnostics["verification"]["recommended_action"] == "continue"



def test_build_auth_outcome_marks_captcha_as_auto_attempt_verification():
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
    verification = build_verification_boundary(snapshot, state, next_action)

    outcome = build_auth_outcome(state, next_action, verification)

    assert outcome["status"] == "verification_required"
    assert outcome["continuation"] == "auto_attempt_verification"
    assert outcome["can_continue"] is False
    assert outcome["should_auto_attempt_verification"] is True
    assert outcome["requires_owner_input"] is False



def test_build_auth_outcome_marks_mfa_as_owner_blocking_boundary():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/verify",
        page_signals={"title": "Verify", "body_text": "Enter the verification code"},
        matched=["mfa_selector"],
        profile=normalize_site_profile({"mfa_selector": "input[name=otp]"}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)

    outcome = build_auth_outcome(state, next_action, verification)

    assert outcome["status"] == "blocked_by_verification"
    assert outcome["continuation"] == "await_owner"
    assert outcome["can_continue"] is False
    assert outcome["should_auto_attempt_verification"] is False
    assert outcome["requires_owner_input"] is True



def test_summarize_auth_diagnostics_exposes_machine_readable_outcome():
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

    diagnostics = summarize_auth_diagnostics(snapshot, state, next_action)

    assert diagnostics["verification"]["kind"] == "captcha"
    assert diagnostics["outcome"]["status"] == "verification_required"
    assert diagnostics["outcome"]["continuation"] == "auto_attempt_verification"



def test_build_verification_handoff_for_captcha_returns_auto_attempt_plan():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/login",
        page_signals={"title": "Login", "body_text": "Please verify you are human"},
        matched=["captcha_selector"],
        profile=normalize_site_profile({"captcha_selector": ".captcha-box", "submit_selector": "button[type=submit]"}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)

    handoff = build_verification_handoff(verification, outcome, next_action)

    assert handoff["active"] is True
    assert handoff["mode"] == "auto_attempt"
    assert handoff["kind"] == "captcha"
    assert handoff["required_inputs"] == []
    assert handoff["selectors"]["captcha_selector"] == ".captcha-box"



def test_build_verification_handoff_for_mfa_returns_owner_handoff_plan():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/verify",
        page_signals={"title": "Verify", "body_text": "Enter the verification code"},
        matched=["mfa_selector"],
        profile=normalize_site_profile({"mfa_selector": "input[name=otp]", "submit_selector": "button[type=submit]"}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)

    handoff = build_verification_handoff(verification, outcome, next_action)

    assert handoff["active"] is True
    assert handoff["mode"] == "owner_handoff"
    assert handoff["kind"] == "mfa"
    assert "owner_verification_code" in handoff["required_inputs"]
    assert handoff["selectors"]["mfa_selector"] == "input[name=otp]"



def test_summarize_auth_diagnostics_exposes_inactive_handoff_when_no_verification():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/app",
        page_signals={"title": "Dashboard", "body_text": "Welcome back"},
        matched=["success_selector"],
        profile=normalize_site_profile({"success_selector": ".dashboard"}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)

    diagnostics = summarize_auth_diagnostics(snapshot, state, next_action)

    assert diagnostics["verification_handoff"]["active"] is False
    assert diagnostics["verification_handoff"]["mode"] == "none"
    assert diagnostics["verification_handoff"]["continuation"] == "continue"


def test_infer_auth_state_does_not_treat_cookie_only_on_login_url_as_logged_in():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/login",
        page_signals={
            "title": "Sign in",
            "body_text": "Sign in to continue",
            "cookie_names": ["sessionid"],
            "success_cookie_names": ["sessionid"],
            "login_form_visible": False,
            "visible_password_fields": 0,
            "body_mentions_login": True,
        },
        matched=[],
        profile=normalize_site_profile({"success_cookie_names": ["sessionid"]}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )

    state = infer_auth_state(snapshot)

    assert state["state"] == "logged_out"


def test_infer_auth_state_does_not_treat_account_ui_on_login_url_as_logged_in_without_strong_signal():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/login",
        page_signals={
            "title": "Login",
            "body_text": "Manage your account",
            "has_profile_ui": True,
            "login_form_visible": False,
        },
        matched=[],
        profile=normalize_site_profile({}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )

    state = infer_auth_state(snapshot)

    assert state["state"] == "unknown"


def test_build_auth_outcome_blocks_captcha_auto_attempt_without_captcha_selector():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/login",
        page_signals={"title": "Login", "body_text": "Please verify you are human"},
        matched=[],
        profile=normalize_site_profile({}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)

    assert verification["detected"] is True
    assert verification["kind"] == "captcha"
    assert verification["actionable"] is False
    assert verification["missing_requirements"] == ["captcha_selector"]
    assert outcome["status"] == "blocked_by_verification"
    assert outcome["continuation"] == "stop"
    assert outcome["should_auto_attempt_verification"] is False
    assert handoff["mode"] == "blocked"
    assert handoff["required_inputs"] == ["captcha_selector"]


def test_build_auth_outcome_blocks_owner_handoff_without_mfa_selector():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/verify",
        page_signals={"title": "Verify", "body_text": "Enter the 6-digit code from your authenticator app"},
        matched=[],
        profile=normalize_site_profile({}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)

    assert verification["detected"] is True
    assert verification["kind"] == "mfa"
    assert verification["actionable"] is False
    assert verification["missing_requirements"] == ["mfa_selector"]
    assert outcome["status"] == "blocked_by_verification"
    assert outcome["continuation"] == "stop"
    assert outcome["requires_owner_input"] is False
    assert handoff["mode"] == "blocked"
    assert handoff["required_inputs"] == ["mfa_selector"]


def test_build_verification_attempt_plan_marks_captcha_as_ready_for_simple_auto_attempt():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/login",
        page_signals={"title": "Login", "body_text": "Please verify you are human"},
        matched=["captcha_selector"],
        profile=normalize_site_profile({"captcha_selector": ".captcha-box", "submit_selector": "button[type=submit]"}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)

    attempt = build_verification_attempt_plan(verification, outcome, handoff)

    assert attempt["status"] == "ready"
    assert attempt["kind"] == "captcha"
    assert attempt["strategy"] == "solve_simple_captcha_from_screenshot"
    assert attempt["can_auto_attempt"] is True
    assert attempt["requires_screenshot"] is True
    assert attempt["requires_owner_input"] is False
    assert attempt["selectors"]["captcha_selector"] == ".captcha-box"


def test_build_verification_attempt_plan_marks_mfa_as_owner_required():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/verify",
        page_signals={"title": "Verify", "body_text": "Enter the verification code"},
        matched=["mfa_selector"],
        profile=normalize_site_profile({"mfa_selector": "input[name=otp]"}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)

    attempt = build_verification_attempt_plan(verification, outcome, handoff)

    assert attempt["status"] == "owner_required"
    assert attempt["kind"] == "mfa"
    assert attempt["strategy"] == "owner_handoff"
    assert attempt["can_auto_attempt"] is False
    assert attempt["requires_owner_input"] is True
    assert "owner_verification_code" in attempt["attempt_inputs"]


def test_build_verification_attempt_plan_returns_not_applicable_when_no_boundary_exists():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/app",
        page_signals={"title": "Dashboard", "body_text": "Welcome back"},
        matched=["success_selector"],
        profile=normalize_site_profile({"success_selector": ".dashboard"}),
        protected_url_alive=True,
        submitted_from_login_url=True,
    )
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)

    attempt = build_verification_attempt_plan(verification, outcome, handoff)

    assert attempt["status"] == "not_applicable"
    assert attempt["kind"] == "none"
    assert attempt["strategy"] == "none"
    assert attempt["can_auto_attempt"] is False


def test_build_verification_attempt_plan_blocks_captcha_without_selector_structure():
    snapshot = build_auth_page_snapshot(
        current_url="https://example.com/login",
        page_signals={"title": "Login", "body_text": "Please verify you are human"},
        matched=[],
        profile=normalize_site_profile({}),
        protected_url_alive=False,
        submitted_from_login_url=True,
    )
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)

    attempt = build_verification_attempt_plan(verification, outcome, handoff)

    assert attempt["status"] == "blocked"
    assert attempt["kind"] == "captcha"
    assert attempt["strategy"] == "none"
    assert attempt["can_auto_attempt"] is False
    assert "captcha_selector" in attempt["missing_requirements"]


def test_summarize_auth_diagnostics_exposes_verification_attempt_plan():
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

    diagnostics = summarize_auth_diagnostics(snapshot, state, next_action)

    assert diagnostics["verification_attempt"]["status"] == "ready"
    assert diagnostics["verification_attempt"]["strategy"] == "solve_simple_captcha_from_screenshot"


def test_build_verification_attempt_result_reports_not_attempted_without_boundary():
    result = build_verification_attempt_result({}, {"strategy": "none"}, None)

    assert result["status"] == "not_attempted"
    assert result["attempted"] is False
    assert result["kind"] == "none"


def test_build_verification_attempt_result_marks_successful_captcha_auto_attempt():
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
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)
    attempt = build_verification_attempt_plan(verification, outcome, handoff)

    result = build_verification_attempt_result(
        verification,
        attempt,
        {
            "success": True,
            "text": "AB12",
            "confidence": 0.91,
            "method": "browser_solve_captcha",
            "attempts": 1,
            "error": None,
            "reason": "captcha auto-attempt succeeded",
        },
    )

    assert result["status"] == "succeeded"
    assert result["attempted"] is True
    assert result["success"] is True
    assert result["text"] == "AB12"
    assert result["method"] == "browser_solve_captcha"


def test_build_verification_attempt_result_marks_failed_captcha_auto_attempt():
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
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)
    attempt = build_verification_attempt_plan(verification, outcome, handoff)

    result = build_verification_attempt_result(
        verification,
        attempt,
        {
            "success": False,
            "text": "",
            "confidence": 0.12,
            "method": "browser_solve_captcha",
            "attempts": 2,
            "error": "OCR confidence too low",
            "reason": "captcha auto-attempt failed",
        },
    )

    assert result["status"] == "failed"
    assert result["attempted"] is True
    assert result["success"] is False
    assert result["error"] == "OCR confidence too low"


def test_summarize_auth_diagnostics_exposes_verification_attempt_result():
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

    diagnostics = summarize_auth_diagnostics(
        snapshot,
        state,
        next_action,
        raw_verification_attempt_result={
            "success": True,
            "text": "AB12",
            "confidence": 0.91,
            "method": "browser_solve_captcha",
            "attempts": 1,
            "error": None,
            "reason": "captcha auto-attempt succeeded",
        },
    )

    assert diagnostics["verification_attempt_result"]["status"] == "succeeded"
    assert diagnostics["verification_attempt_result"]["success"] is True
    assert diagnostics["verification_attempt_result"]["text"] == "AB12"


def test_build_verification_continuation_returns_continue_without_boundary():
    continuation = build_verification_continuation({}, {}, {}, {})

    assert continuation["status"] == "continue_login"
    assert continuation["can_resume_auth"] is True
    assert continuation["should_retry_verification"] is False



def test_build_verification_continuation_returns_continue_after_successful_attempt():
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
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)
    attempt = build_verification_attempt_plan(verification, outcome, handoff)
    attempt_result = build_verification_attempt_result(
        verification,
        attempt,
        {
            "success": True,
            "text": "AB12",
            "confidence": 0.91,
            "method": "browser_solve_captcha",
            "attempts": 1,
            "error": None,
            "reason": "captcha auto-attempt succeeded",
        },
    )

    continuation = build_verification_continuation(verification, outcome, attempt, attempt_result)

    assert continuation["status"] == "continue_login"
    assert continuation["can_resume_auth"] is True
    assert continuation["source"] == "verification_attempt_result"



def test_build_verification_continuation_retries_after_light_failed_attempt():
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
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)
    attempt = build_verification_attempt_plan(verification, outcome, handoff)
    attempt_result = build_verification_attempt_result(
        verification,
        attempt,
        {
            "success": False,
            "text": "",
            "confidence": 0.12,
            "method": "browser_solve_captcha",
            "attempts": 1,
            "error": "OCR confidence too low",
            "reason": "captcha auto-attempt failed",
        },
    )

    continuation = build_verification_continuation(verification, outcome, attempt, attempt_result)

    assert continuation["status"] == "retry_verification"
    assert continuation["should_retry_verification"] is True
    assert continuation["requires_owner_input"] is False



def test_build_verification_continuation_escalates_after_heavier_failed_attempt():
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
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)
    attempt = build_verification_attempt_plan(verification, outcome, handoff)
    attempt_result = build_verification_attempt_result(
        verification,
        attempt,
        {
            "success": False,
            "text": "",
            "confidence": 0.88,
            "method": "browser_solve_captcha",
            "attempts": 2,
            "error": "verification still failed after retry",
            "reason": "captcha auto-attempt failed twice",
        },
    )

    continuation = build_verification_continuation(verification, outcome, attempt, attempt_result)

    assert continuation["status"] == "await_owner"
    assert continuation["should_retry_verification"] is False
    assert continuation["requires_owner_input"] is True



def test_summarize_auth_diagnostics_exposes_verification_continuation():
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

    diagnostics = summarize_auth_diagnostics(
        snapshot,
        state,
        next_action,
        raw_verification_attempt_result={
            "success": False,
            "text": "",
            "confidence": 0.12,
            "method": "browser_solve_captcha",
            "attempts": 1,
            "error": "OCR confidence too low",
            "reason": "captcha auto-attempt failed",
        },
    )

    assert diagnostics["verification_continuation"]["status"] == "retry_verification"
    assert diagnostics["verification_continuation"]["should_retry_verification"] is True
