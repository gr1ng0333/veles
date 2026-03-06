import pytest

from ouroboros.tools.browser import choose_login_field_selectors, infer_login_state


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
        ({"matched": ["success_selector"]}, "logged_in"),
        ({"matched": ["failure_selector"]}, "login_failed"),
        ({"matched": ["logged_out_selector"]}, "logged_out"),
        ({"error_texts": ["Invalid password"], "visible_password_fields": 1}, "login_failed"),
        ({"visible_password_fields": 1, "body_mentions_login": True}, "logged_out"),
        ({"visible_password_fields": 0, "has_profile_ui": True}, "logged_in"),
        ({"visible_password_fields": 1, "body_mentions_profile": True}, "unclear"),
        ({"visible_password_fields": 0}, "unclear"),
    ],
)
def test_infer_login_state(signals, expected_state):
    result = infer_login_state(signals)
    assert result["state"] == expected_state
    assert "reason" in result
