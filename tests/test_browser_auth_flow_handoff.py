# Split from tests/test_browser_auth_flow.py to keep test modules readable.
import json
from ouroboros.tools.browser_auth_flow import build_auth_outcome, build_auth_page_snapshot, build_next_action_plan, build_verification_attempt_plan, build_verification_attempt_result, build_verification_boundary, build_verification_continuation, build_verification_handoff, build_owner_handoff_completion, compute_auth_flow_success, infer_auth_state, normalize_site_profile, summarize_auth_diagnostics
def test_summarize_auth_diagnostics_includes_verification_boundary_even_when_absent():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/app', page_signals={'title': 'Dashboard', 'body_text': 'Welcome back'}, matched=['success_selector'], profile=normalize_site_profile({'success_selector': '.dashboard'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    diagnostics = summarize_auth_diagnostics(snapshot, state, next_action)
    assert diagnostics['verification']['detected'] is False
    assert diagnostics['verification']['kind'] == 'none'
    assert diagnostics['verification']['recommended_action'] == 'continue'

def test_summarize_auth_diagnostics_exposes_machine_readable_outcome():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/login', page_signals={'title': 'Login', 'body_text': 'Please verify you are human'}, matched=['captcha_selector'], profile=normalize_site_profile({'captcha_selector': '.captcha-box'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    diagnostics = summarize_auth_diagnostics(snapshot, state, next_action)
    assert diagnostics['verification']['kind'] == 'captcha'
    assert diagnostics['outcome']['status'] == 'verification_required'
    assert diagnostics['outcome']['continuation'] == 'auto_attempt_verification'

def test_summarize_auth_diagnostics_exposes_inactive_handoff_when_no_verification():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/app', page_signals={'title': 'Dashboard', 'body_text': 'Welcome back'}, matched=['success_selector'], profile=normalize_site_profile({'success_selector': '.dashboard'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    diagnostics = summarize_auth_diagnostics(snapshot, state, next_action)
    assert diagnostics['verification_handoff']['active'] is False
    assert diagnostics['verification_handoff']['mode'] == 'none'
    assert diagnostics['verification_handoff']['continuation'] == 'continue'
    assert diagnostics['owner_handoff']['required'] is False
    assert diagnostics['owner_handoff']['kind'] == 'none'

def test_summarize_auth_diagnostics_exposes_verification_attempt_plan():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/login', page_signals={'title': 'Login', 'body_text': 'Please verify you are human'}, matched=['captcha_selector'], profile=normalize_site_profile({'captcha_selector': '.captcha-box'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    diagnostics = summarize_auth_diagnostics(snapshot, state, next_action)
    assert diagnostics['verification_attempt']['status'] == 'ready'
    assert diagnostics['verification_attempt']['strategy'] == 'solve_simple_captcha_from_screenshot'

def test_summarize_auth_diagnostics_exposes_structured_owner_handoff_for_mfa():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/verify', page_signals={'title': 'Verify', 'body_text': 'Enter the verification code'}, matched=['mfa_selector'], profile=normalize_site_profile({'mfa_selector': 'input[name=otp]', 'submit_selector': 'button[type=submit]'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    diagnostics = summarize_auth_diagnostics(snapshot, state, next_action)
    assert diagnostics['owner_handoff']['required'] is True
    assert diagnostics['owner_handoff']['kind'] == 'mfa'
    assert diagnostics['owner_handoff']['blocking'] is True
    assert 'owner_verification_code' in diagnostics['owner_handoff']['required_inputs']
    assert 'MFA' in diagnostics['owner_handoff']['instruction']

def test_summarize_auth_diagnostics_exposes_verification_attempt_result():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/login', page_signals={'title': 'Login', 'body_text': 'Please verify you are human'}, matched=['captcha_selector'], profile=normalize_site_profile({'captcha_selector': '.captcha-box'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    diagnostics = summarize_auth_diagnostics(snapshot, state, next_action, raw_verification_attempt_result={'success': True, 'text': 'AB12', 'confidence': 0.91, 'method': 'browser_solve_captcha', 'attempts': 1, 'error': None, 'reason': 'captcha auto-attempt succeeded'})
    assert diagnostics['verification_attempt_result']['status'] == 'succeeded'
    assert diagnostics['verification_attempt_result']['success'] is True
    assert diagnostics['verification_attempt_result']['text'] == 'AB12'

def test_summarize_auth_diagnostics_exposes_owner_handoff_after_failed_verification_escalation():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/login', page_signals={'title': 'Login', 'body_text': 'Please verify you are human'}, matched=['captcha_selector'], profile=normalize_site_profile({'captcha_selector': '.captcha-box'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    diagnostics = summarize_auth_diagnostics(snapshot, state, next_action, raw_verification_attempt_result={'success': False, 'confidence': 0.92, 'attempts': 2, 'error': 'captcha remained unsolved', 'reason': 'captcha auto-attempt failed', 'method': 'browser_solve_captcha'})
    assert diagnostics['verification_continuation']['status'] == 'await_owner'
    assert diagnostics['owner_handoff']['required'] is True
    assert diagnostics['owner_handoff']['kind'] == 'captcha'
    assert diagnostics['owner_handoff']['reason'] == 'captcha remained unsolved'

def test_summarize_auth_diagnostics_exposes_verification_continuation():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/login', page_signals={'title': 'Login', 'body_text': 'Please verify you are human'}, matched=['captcha_selector'], profile=normalize_site_profile({'captcha_selector': '.captcha-box'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    diagnostics = summarize_auth_diagnostics(snapshot, state, next_action, raw_verification_attempt_result={'success': False, 'text': '', 'confidence': 0.12, 'method': 'browser_solve_captcha', 'attempts': 1, 'error': 'OCR confidence too low', 'reason': 'captcha auto-attempt failed'})
    assert diagnostics['verification_continuation']['status'] == 'retry_verification'
    assert diagnostics['verification_continuation']['should_retry_verification'] is True

def test_summarize_auth_diagnostics_exposes_owner_handoff_resume_for_mfa():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/verify', page_signals={'title': 'Verify', 'body_text': 'Enter the verification code'}, matched=['mfa_selector'], profile=normalize_site_profile({'mfa_selector': 'input[name=otp]', 'submit_selector': 'button[type=submit]'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    diagnostics = summarize_auth_diagnostics(snapshot, state, next_action)
    assert diagnostics['owner_handoff_resume']['status'] == 'awaiting_owner'
    assert diagnostics['owner_handoff_resume']['kind'] == 'mfa'
    assert diagnostics['owner_handoff_resume']['can_resume_auth'] is False

def test_summarize_auth_diagnostics_exposes_owner_handoff_completion_for_mfa():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/verify', page_signals={'title': 'Verify', 'body_text': 'Enter the verification code'}, matched=['mfa_selector'], profile=normalize_site_profile({'mfa_selector': 'input[name=otp]', 'submit_selector': 'button[type=submit]'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    diagnostics = summarize_auth_diagnostics(snapshot, state, next_action)
    assert diagnostics['owner_handoff_completion']['status'] == 'still_waiting'
    assert diagnostics['owner_handoff_completion']['kind'] == 'mfa'
    assert diagnostics['owner_handoff_completion']['can_resume_auth'] is False

def test_summarize_auth_diagnostics_exposes_completed_owner_handoff_after_verification_clears():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/app', page_signals={'title': 'Dashboard', 'body_text': 'Welcome back'}, matched=['success_selector'], profile=normalize_site_profile({'success_selector': '.dashboard'}), protected_url_alive=True, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    diagnostics = summarize_auth_diagnostics(snapshot, state, next_action)
    assert diagnostics['owner_handoff_completion']['status'] == 'not_applicable'
    assert diagnostics['owner_handoff_completion']['can_resume_auth'] is True

def test_compute_auth_flow_success_prefers_owner_handoff_completion():
    success = compute_auth_flow_success({'state': 'unknown'}, {'success': False, 'can_continue': False}, {'status': 'await_owner', 'can_resume_auth': False}, {'status': 'completed', 'can_resume_auth': True})
    assert success is True
