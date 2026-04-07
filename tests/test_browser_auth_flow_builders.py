# Split from tests/test_browser_auth_flow.py to keep test modules readable.
import json
from ouroboros.tools.browser_auth_flow import build_auth_outcome, build_auth_page_snapshot, build_next_action_plan, build_verification_attempt_plan, build_verification_attempt_result, build_verification_boundary, build_verification_continuation, build_verification_handoff, build_owner_handoff_completion, compute_auth_flow_success, infer_auth_state, normalize_site_profile, summarize_auth_diagnostics
def test_build_verification_boundary_detects_captcha_as_auto_attemptable_blocker():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/login', page_signals={'title': 'Login', 'body_text': 'Please verify you are human'}, matched=['captcha_selector'], profile=normalize_site_profile({'captcha_selector': '.captcha-box', 'submit_selector': 'button[type=submit]'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    assert verification['detected'] is True
    assert verification['kind'] == 'captcha'
    assert verification['can_auto_attempt'] is True
    assert verification['requires_owner_input'] is False
    assert verification['blocks_progress'] is True
    assert verification['recommended_action'] == 'solve_captcha'
    assert verification['selectors']['captcha_selector'] == '.captcha-box'

def test_build_verification_boundary_detects_mfa_as_owner_boundary():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/verify', page_signals={'title': 'Verify', 'body_text': 'Enter the verification code from your app'}, matched=['mfa_selector'], profile=normalize_site_profile({'mfa_selector': 'input[name=otp]', 'submit_selector': 'button[type=submit]'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    assert verification['detected'] is True
    assert verification['kind'] == 'mfa'
    assert verification['can_auto_attempt'] is False
    assert verification['requires_owner_input'] is True
    assert verification['blocks_progress'] is True
    assert verification['recommended_action'] == 'wait_for_mfa'
    assert verification['selectors']['mfa_selector'] == 'input[name=otp]'

def test_build_auth_outcome_marks_captcha_as_auto_attempt_verification():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/login', page_signals={'title': 'Login', 'body_text': 'Please verify you are human'}, matched=['captcha_selector'], profile=normalize_site_profile({'captcha_selector': '.captcha-box'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    assert outcome['status'] == 'verification_required'
    assert outcome['continuation'] == 'auto_attempt_verification'
    assert outcome['can_continue'] is False
    assert outcome['should_auto_attempt_verification'] is True
    assert outcome['requires_owner_input'] is False

def test_build_auth_outcome_marks_mfa_as_owner_blocking_boundary():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/verify', page_signals={'title': 'Verify', 'body_text': 'Enter the verification code'}, matched=['mfa_selector'], profile=normalize_site_profile({'mfa_selector': 'input[name=otp]'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    assert outcome['status'] == 'blocked_by_verification'
    assert outcome['continuation'] == 'await_owner'
    assert outcome['can_continue'] is False
    assert outcome['should_auto_attempt_verification'] is False
    assert outcome['requires_owner_input'] is True

def test_build_verification_handoff_for_captcha_returns_auto_attempt_plan():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/login', page_signals={'title': 'Login', 'body_text': 'Please verify you are human'}, matched=['captcha_selector'], profile=normalize_site_profile({'captcha_selector': '.captcha-box', 'submit_selector': 'button[type=submit]'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)
    assert handoff['active'] is True
    assert handoff['mode'] == 'auto_attempt'
    assert handoff['kind'] == 'captcha'
    assert handoff['required_inputs'] == []
    assert handoff['selectors']['captcha_selector'] == '.captcha-box'

def test_build_verification_handoff_for_mfa_returns_owner_handoff_plan():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/verify', page_signals={'title': 'Verify', 'body_text': 'Enter the verification code'}, matched=['mfa_selector'], profile=normalize_site_profile({'mfa_selector': 'input[name=otp]', 'submit_selector': 'button[type=submit]'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)
    assert handoff['active'] is True
    assert handoff['mode'] == 'owner_handoff'
    assert handoff['kind'] == 'mfa'
    assert 'owner_verification_code' in handoff['required_inputs']
    assert handoff['selectors']['mfa_selector'] == 'input[name=otp]'

def test_build_auth_outcome_blocks_captcha_auto_attempt_without_captcha_selector():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/login', page_signals={'title': 'Login', 'body_text': 'Please verify you are human'}, matched=[], profile=normalize_site_profile({}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)
    assert verification['detected'] is True
    assert verification['kind'] == 'captcha'
    assert verification['actionable'] is False
    assert verification['missing_requirements'] == ['captcha_selector']
    assert outcome['status'] == 'blocked_by_verification'
    assert outcome['continuation'] == 'stop'
    assert outcome['should_auto_attempt_verification'] is False
    assert handoff['mode'] == 'blocked'
    assert handoff['required_inputs'] == ['captcha_selector']

def test_build_auth_outcome_blocks_owner_handoff_without_mfa_selector():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/verify', page_signals={'title': 'Verify', 'body_text': 'Enter the 6-digit code from your authenticator app'}, matched=[], profile=normalize_site_profile({}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)
    assert verification['detected'] is True
    assert verification['kind'] == 'mfa'
    assert verification['actionable'] is False
    assert verification['missing_requirements'] == ['mfa_selector']
    assert outcome['status'] == 'blocked_by_verification'
    assert outcome['continuation'] == 'stop'
    assert outcome['requires_owner_input'] is False
    assert handoff['mode'] == 'blocked'
    assert handoff['required_inputs'] == ['mfa_selector']

def test_build_verification_attempt_plan_marks_captcha_as_ready_for_simple_auto_attempt():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/login', page_signals={'title': 'Login', 'body_text': 'Please verify you are human'}, matched=['captcha_selector'], profile=normalize_site_profile({'captcha_selector': '.captcha-box', 'submit_selector': 'button[type=submit]'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)
    attempt = build_verification_attempt_plan(verification, outcome, handoff)
    assert attempt['status'] == 'ready'
    assert attempt['kind'] == 'captcha'
    assert attempt['strategy'] == 'solve_simple_captcha_from_screenshot'
    assert attempt['can_auto_attempt'] is True
    assert attempt['requires_screenshot'] is True
    assert attempt['requires_owner_input'] is False
    assert attempt['selectors']['captcha_selector'] == '.captcha-box'

def test_build_verification_attempt_plan_marks_mfa_as_owner_required():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/verify', page_signals={'title': 'Verify', 'body_text': 'Enter the verification code'}, matched=['mfa_selector'], profile=normalize_site_profile({'mfa_selector': 'input[name=otp]'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)
    attempt = build_verification_attempt_plan(verification, outcome, handoff)
    assert attempt['status'] == 'owner_required'
    assert attempt['kind'] == 'mfa'
    assert attempt['strategy'] == 'owner_handoff'
    assert attempt['can_auto_attempt'] is False
    assert attempt['requires_owner_input'] is True
    assert 'owner_verification_code' in attempt['attempt_inputs']

def test_build_verification_attempt_plan_returns_not_applicable_when_no_boundary_exists():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/app', page_signals={'title': 'Dashboard', 'body_text': 'Welcome back'}, matched=['success_selector'], profile=normalize_site_profile({'success_selector': '.dashboard'}), protected_url_alive=True, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)
    attempt = build_verification_attempt_plan(verification, outcome, handoff)
    assert attempt['status'] == 'not_applicable'
    assert attempt['kind'] == 'none'
    assert attempt['strategy'] == 'none'
    assert attempt['can_auto_attempt'] is False

def test_build_verification_attempt_plan_blocks_captcha_without_selector_structure():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/login', page_signals={'title': 'Login', 'body_text': 'Please verify you are human'}, matched=[], profile=normalize_site_profile({}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)
    attempt = build_verification_attempt_plan(verification, outcome, handoff)
    assert attempt['status'] == 'blocked'
    assert attempt['kind'] == 'captcha'
    assert attempt['strategy'] == 'none'
    assert attempt['can_auto_attempt'] is False
    assert 'captcha_selector' in attempt['missing_requirements']

def test_build_verification_attempt_result_reports_not_attempted_without_boundary():
    result = build_verification_attempt_result({}, {'strategy': 'none'}, None)
    assert result['status'] == 'not_attempted'
    assert result['attempted'] is False
    assert result['kind'] == 'none'

def test_build_verification_attempt_result_marks_successful_captcha_auto_attempt():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/login', page_signals={'title': 'Login', 'body_text': 'Please verify you are human'}, matched=['captcha_selector'], profile=normalize_site_profile({'captcha_selector': '.captcha-box'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)
    attempt = build_verification_attempt_plan(verification, outcome, handoff)
    result = build_verification_attempt_result(verification, attempt, {'success': True, 'text': 'AB12', 'confidence': 0.91, 'method': 'browser_solve_captcha', 'attempts': 1, 'error': None, 'reason': 'captcha auto-attempt succeeded'})
    assert result['status'] == 'succeeded'
    assert result['attempted'] is True
    assert result['success'] is True
    assert result['text'] == 'AB12'
    assert result['method'] == 'browser_solve_captcha'

def test_build_verification_attempt_result_marks_failed_captcha_auto_attempt():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/login', page_signals={'title': 'Login', 'body_text': 'Please verify you are human'}, matched=['captcha_selector'], profile=normalize_site_profile({'captcha_selector': '.captcha-box'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)
    attempt = build_verification_attempt_plan(verification, outcome, handoff)
    result = build_verification_attempt_result(verification, attempt, {'success': False, 'text': '', 'confidence': 0.12, 'method': 'browser_solve_captcha', 'attempts': 2, 'error': 'OCR confidence too low', 'reason': 'captcha auto-attempt failed'})
    assert result['status'] == 'failed'
    assert result['attempted'] is True
    assert result['success'] is False
    assert result['error'] == 'OCR confidence too low'

def test_build_verification_continuation_returns_continue_without_boundary():
    continuation = build_verification_continuation({}, {}, {}, {})
    assert continuation['status'] == 'continue_login'
    assert continuation['can_resume_auth'] is True
    assert continuation['should_retry_verification'] is False

def test_build_verification_continuation_returns_continue_after_successful_attempt():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/login', page_signals={'title': 'Login', 'body_text': 'Please verify you are human'}, matched=['captcha_selector'], profile=normalize_site_profile({'captcha_selector': '.captcha-box'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)
    attempt = build_verification_attempt_plan(verification, outcome, handoff)
    attempt_result = build_verification_attempt_result(verification, attempt, {'success': True, 'text': 'AB12', 'confidence': 0.91, 'method': 'browser_solve_captcha', 'attempts': 1, 'error': None, 'reason': 'captcha auto-attempt succeeded'})
    continuation = build_verification_continuation(verification, outcome, attempt, attempt_result)
    assert continuation['status'] == 'continue_login'
    assert continuation['can_resume_auth'] is True
    assert continuation['source'] == 'verification_attempt_result'

def test_build_verification_continuation_retries_after_light_failed_attempt():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/login', page_signals={'title': 'Login', 'body_text': 'Please verify you are human'}, matched=['captcha_selector'], profile=normalize_site_profile({'captcha_selector': '.captcha-box'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)
    attempt = build_verification_attempt_plan(verification, outcome, handoff)
    attempt_result = build_verification_attempt_result(verification, attempt, {'success': False, 'text': '', 'confidence': 0.12, 'method': 'browser_solve_captcha', 'attempts': 1, 'error': 'OCR confidence too low', 'reason': 'captcha auto-attempt failed'})
    continuation = build_verification_continuation(verification, outcome, attempt, attempt_result)
    assert continuation['status'] == 'retry_verification'
    assert continuation['should_retry_verification'] is True
    assert continuation['requires_owner_input'] is False

def test_build_verification_continuation_escalates_after_heavier_failed_attempt():
    snapshot = build_auth_page_snapshot(current_url='https://example.com/login', page_signals={'title': 'Login', 'body_text': 'Please verify you are human'}, matched=['captcha_selector'], profile=normalize_site_profile({'captcha_selector': '.captcha-box'}), protected_url_alive=False, submitted_from_login_url=True)
    state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, state)
    verification = build_verification_boundary(snapshot, state, next_action)
    outcome = build_auth_outcome(state, next_action, verification)
    handoff = build_verification_handoff(verification, outcome, next_action)
    attempt = build_verification_attempt_plan(verification, outcome, handoff)
    attempt_result = build_verification_attempt_result(verification, attempt, {'success': False, 'text': '', 'confidence': 0.88, 'method': 'browser_solve_captcha', 'attempts': 2, 'error': 'verification still failed after retry', 'reason': 'captcha auto-attempt failed twice'})
    continuation = build_verification_continuation(verification, outcome, attempt, attempt_result)
    assert continuation['status'] == 'await_owner'
    assert continuation['should_retry_verification'] is False
    assert continuation['requires_owner_input'] is True

def test_build_owner_handoff_resume_not_needed_without_owner_handoff():
    from ouroboros.tools.browser_auth_flow import build_owner_handoff_resume
    resume = build_owner_handoff_resume({}, {}, {}, {'status': 'continue_login', 'action': 'continue_login', 'can_resume_auth': True}, {'required': False})
    assert resume['status'] == 'not_needed'
    assert resume['can_resume_auth'] is True
    assert resume['kind'] == 'none'

def test_build_owner_handoff_resume_awaiting_owner_for_mfa_boundary():
    from ouroboros.tools.browser_auth_flow import build_owner_handoff_resume
    verification = {'detected': True, 'kind': 'mfa', 'selectors': {'mfa_selector': 'input[name=otp]'}}
    attempt = {'strategy': 'owner_handoff'}
    attempt_result = {'status': 'not_attempted', 'strategy': 'owner_handoff'}
    continuation = {'status': 'await_owner', 'action': 'await_owner', 'can_resume_auth': False, 'requires_owner_input': True, 'reason': 'verification requires owner input'}
    owner_handoff = {'required': True, 'kind': 'mfa', 'selectors': {'mfa_selector': 'input[name=otp]'}}
    resume = build_owner_handoff_resume(verification, attempt, attempt_result, continuation, owner_handoff)
    assert resume['status'] == 'awaiting_owner'
    assert resume['can_resume_auth'] is False
    assert resume['kind'] == 'mfa'

def test_build_owner_handoff_resume_still_blocked_after_failed_captcha_escalation():
    from ouroboros.tools.browser_auth_flow import build_owner_handoff_resume
    verification = {'detected': True, 'kind': 'captcha', 'selectors': {'captcha_selector': '.captcha'}}
    attempt = {'strategy': 'solve_simple_captcha_from_screenshot'}
    attempt_result = {'status': 'failed', 'strategy': 'solve_simple_captcha_from_screenshot'}
    continuation = {'status': 'await_owner', 'action': 'await_owner', 'can_resume_auth': False, 'requires_owner_input': True, 'reason': 'captcha remained unsolved'}
    owner_handoff = {'required': True, 'kind': 'captcha', 'selectors': {'captcha_selector': '.captcha'}}
    resume = build_owner_handoff_resume(verification, attempt, attempt_result, continuation, owner_handoff)
    assert resume['status'] == 'still_blocked'
    assert resume['attempt_status'] == 'failed'
    assert resume['attempt_strategy'] == 'solve_simple_captcha_from_screenshot'

def test_build_owner_handoff_resume_ready_after_owner_completed_step():
    from ouroboros.tools.browser_auth_flow import build_owner_handoff_resume
    verification = {'detected': True, 'kind': 'mfa', 'selectors': {'mfa_selector': 'input[name=otp]'}}
    attempt = {'strategy': 'owner_handoff'}
    attempt_result = {'status': 'not_attempted', 'strategy': 'owner_handoff'}
    continuation = {'status': 'continue_login', 'action': 'continue_login', 'can_resume_auth': True, 'requires_owner_input': False, 'reason': 'verification boundary is cleared'}
    owner_handoff = {'required': True, 'kind': 'mfa', 'selectors': {'mfa_selector': 'input[name=otp]'}}
    resume = build_owner_handoff_resume(verification, attempt, attempt_result, continuation, owner_handoff)
    assert resume['status'] == 'resume_ready'
    assert resume['can_resume_auth'] is True
    assert resume['resume_action'] == 'continue_login'

def test_build_owner_handoff_completion_not_applicable_without_owner_handoff():
    completion = build_owner_handoff_completion({}, {'required': False}, {'status': 'not_needed', 'resume_action': 'continue_login'}, {'status': 'continue_login', 'action': 'continue_login', 'can_resume_auth': True})
    assert completion['status'] == 'not_applicable'
    assert completion['completed'] is False
    assert completion['can_resume_auth'] is True

def test_build_owner_handoff_completion_marks_completed_when_resume_ready():
    completion = build_owner_handoff_completion({'detected': True, 'kind': 'mfa', 'selectors': {'mfa_selector': 'input[name=otp]'}}, {'required': True, 'kind': 'mfa', 'selectors': {'mfa_selector': 'input[name=otp]'}}, {'status': 'resume_ready', 'resume_action': 'continue_login', 'reason': 'owner completed MFA step'}, {'status': 'continue_login', 'action': 'continue_login', 'can_resume_auth': True})
    assert completion['status'] == 'completed'
    assert completion['completed'] is True
    assert completion['can_resume_auth'] is True
    assert completion['kind'] == 'mfa'

def test_build_owner_handoff_completion_marks_still_waiting_for_owner():
    completion = build_owner_handoff_completion({'detected': True, 'kind': 'mfa', 'selectors': {'mfa_selector': 'input[name=otp]'}}, {'required': True, 'kind': 'mfa', 'selectors': {'mfa_selector': 'input[name=otp]'}}, {'status': 'awaiting_owner', 'resume_action': 'await_owner', 'reason': 'waiting for owner verification input'}, {'status': 'await_owner', 'action': 'await_owner', 'can_resume_auth': False})
    assert completion['status'] == 'still_waiting'
    assert completion['completed'] is False
    assert completion['can_resume_auth'] is False

def test_build_owner_handoff_completion_marks_blocked_when_boundary_not_cleared():
    completion = build_owner_handoff_completion({'detected': True, 'kind': 'captcha', 'selectors': {'captcha_selector': '.captcha'}}, {'required': True, 'kind': 'captcha', 'selectors': {'captcha_selector': '.captcha'}}, {'status': 'still_blocked', 'resume_action': 'await_owner', 'reason': 'captcha remained unsolved'}, {'status': 'await_owner', 'action': 'await_owner', 'can_resume_auth': False})
    assert completion['status'] == 'blocked'
    assert completion['completed'] is False
    assert completion['can_resume_auth'] is False
