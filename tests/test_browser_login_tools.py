import pytest

from ouroboros.tools.browser import choose_login_field_selectors, infer_login_state
from ouroboros.tools.browser_login_helpers import plan_login_flow


def test_choose_login_field_selectors_prefers_same_form_as_password():
    username_candidates = [
        {"selector": "#newsletter-email", "form_selector": "#newsletter", "source": "heuristic", "score": 12},
        {"selector": "#login-email", "form_selector": "#login-form", "source": "heuristic", "score": 10},
    ]
    password_candidates = [
        {"selector": "#login-password", "form_selector": "#login-form", "source": "heuristic", "score": 20},
    ]

    chosen = choose_login_field_selectors(username_candidates, password_candidates)

    assert chosen["username_selector"] == "#login-email"
    assert chosen["password_selector"] == "#login-password"
    assert chosen["shared_form"] is True


def test_choose_login_field_selectors_respects_explicit_overrides():
    chosen = choose_login_field_selectors(
        username_candidates=[{"selector": "#auto-user", "form_selector": "#f1", "source": "heuristic"}],
        password_candidates=[{"selector": "#auto-pass", "form_selector": "#f1", "source": "heuristic"}],
        username_selector="#manual-user",
        password_selector="#manual-pass",
    )

    assert chosen == {
        "username_selector": "#manual-user",
        "password_selector": "#manual-pass",
        "username_source": "explicit",
        "password_source": "explicit",
        "shared_form": False,
    }


@pytest.mark.parametrize(
    ("signals", "expected_state"),
    [
        ({"matched": ["success_selector"]}, "success"),
        ({"matched": ["failure_selector"]}, "failure"),
        ({"matched": ["logged_out_selector"], "login_form_visible": True}, "unclear"),
        ({"error_texts": ["Invalid password"], "visible_password_fields": 1}, "failure"),
        ({"visible_password_fields": 1, "body_mentions_login": True, "login_form_visible": True}, "unclear"),
        ({"visible_password_fields": 0, "has_profile_ui": True}, "success"),
        ({"visible_password_fields": 1, "body_mentions_profile": True}, "success"),
        ({"visible_password_fields": 0, "login_form_visible": False, "body_mentions_login": False}, "unclear"),
    ],
)
def test_infer_login_state(signals, expected_state):
    result = infer_login_state(signals)
    assert result["state"] == expected_state
    assert "reason" in result


def test_infer_login_state_failure_text_substrings_override_weak_signals():
    result = infer_login_state({
        "matched": [],
        "visible_password_fields": 1,
        "body_mentions_login": True,
        "has_profile_ui": False,
        "body_mentions_profile": False,
        "body_mentions_logout": False,
        "cookie_names": ["sessionid"],
        "success_cookie_names": ["sessionid"],
        "failure_text_substrings": ["account locked"],
        "error_texts": ["Account locked. Try later."],
        "body_text": "account locked",
        "expected_url_matched": False,
    })
    assert result["state"] == "failure"


def test_infer_login_state_failure_overrides_success_signals():
    result = infer_login_state({
        "matched": ["success_selector"],
        "error_texts": ["Invalid password"],
        "visible_password_fields": 0,
        "has_profile_ui": True,
        "redirected_away_from_login": True,
    })
    assert result["state"] == "failure"


def test_infer_login_state_same_login_url_with_form_is_failure():
    result = infer_login_state({
        "matched": [],
        "submitted_from_login_url": True,
        "redirected_away_from_login": False,
        "login_form_visible": True,
        "visible_password_fields": 1,
    })
    assert result["state"] == "failure"


def test_infer_login_state_protected_url_alive_is_success():
    result = infer_login_state({
        "matched": [],
        "protected_url_alive": True,
        "login_form_visible": True,
        "visible_password_fields": 1,
    })
    assert result["state"] == "success"


def test_infer_login_state_no_signals_is_unclear():
    result = infer_login_state({
        "matched": [],
        "visible_password_fields": 0,
        "login_form_visible": False,
        "body_mentions_login": False,
    })
    assert result["state"] == "unclear"


def test_plan_login_flow_single_step_when_both_fields_present():
    result = plan_login_flow('#user', '#pass', allow_multi_step=False)
    assert result['can_proceed'] is True
    assert result['mode'] == 'single_step'


def test_plan_login_flow_allows_username_first_multi_step():
    result = plan_login_flow('#user', '', allow_multi_step=True)
    assert result['can_proceed'] is True
    assert result['mode'] == 'multi_step_username_first'


def test_plan_login_flow_rejects_missing_password_without_multi_step():
    result = plan_login_flow('#user', '', allow_multi_step=False)
    assert result['can_proceed'] is False
    assert result['mode'] == 'missing_password'


def test_plan_login_flow_rejects_missing_username():
    result = plan_login_flow('', '#pass', allow_multi_step=True)
    assert result['can_proceed'] is False
    assert result['mode'] == 'missing_username'
