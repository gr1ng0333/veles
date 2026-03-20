from ouroboros.accounts_status_format import format_codex_accounts_status


def test_format_codex_accounts_status_surfaces_reason_and_quota_hints() -> None:
    text = format_codex_accounts_status([
        {
            "index": 3,
            "dead": False,
            "in_cooldown": True,
            "cooldown_remaining": 180,
            "has_access": True,
            "active": False,
            "quota_5h_used_pct": 0,
            "quota_7d_used_pct": 100,
            "quota_plan": "plus",
            "last_error_category": "rate_limit",
            "last_error_reason": "usage_limit_reached",
            "last_error_status_code": 429,
        },
        {
            "index": 4,
            "dead": False,
            "in_cooldown": False,
            "cooldown_remaining": 0,
            "has_access": True,
            "active": True,
            "quota_5h_used_pct": 12,
            "quota_7d_used_pct": 34,
            "last_error_category": "auth",
            "last_error_reason": "unauthorized",
            "last_error_status_code": 401,
        },
        {
            "index": 5,
            "dead": False,
            "in_cooldown": True,
            "cooldown_remaining": 60,
            "has_access": True,
            "active": False,
            "quota_5h_used_pct": 55,
            "quota_7d_used_pct": 66,
            "last_error_category": "rate_limit",
            "last_error_reason": "rate_limit",
            "last_error_status_code": 429,
        },
    ])

    assert "#3: cooldown 3m" in text
    assert "reason=usage_limit_reached" in text
    assert "quota hints: 5h used 0% | 7d used 100% | plan plus" in text
    assert "reason=auth_failure" in text
    assert "http=401" in text
    assert "reason=temporary 429" in text
    assert "Σ Средняя квота:" in text
