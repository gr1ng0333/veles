from datetime import datetime, timezone

from ouroboros.consciousness import (
    _build_thought_preview,
    _calc_next_wakeup_at,
    _normalize_monitor_state,
)


def test_normalize_monitor_state_defaults_on_invalid_input():
    data = _normalize_monitor_state("bad")
    assert data["wakeup_count"] == 0
    assert data["known_issue_numbers"] == []
    assert data["last_budget_alert_level"] == "none"


def test_normalize_monitor_state_coerces_wakeup_and_known_list():
    data = _normalize_monitor_state({"wakeup_count": "7", "known_issue_numbers": "x"})
    assert data["wakeup_count"] == 7
    assert data["known_issue_numbers"] == []


def test_build_thought_preview_uses_fallback_when_content_empty():
    preview = _build_thought_preview("", rounds=3, tool_calls=2, end_reason="empty_response")
    assert "reason=model returned empty response" in preview
    assert "rounds=3" in preview
    assert "tool_calls=2" in preview


def test_build_thought_preview_uses_content_when_present():
    preview = _build_thought_preview("hello" * 100, rounds=1, tool_calls=0, end_reason="finalized")
    assert preview.startswith("hello")
    assert len(preview) <= 300


def test_calc_next_wakeup_at_returns_utc_iso_future():
    ts = _calc_next_wakeup_at(90)
    assert ts.endswith("Z")
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    delta = (dt - now).total_seconds()
    assert 30 <= delta <= 180


def test_normalize_monitor_state_preserves_existing_last_issues_check():
    ts = "2026-03-06T05:00:00Z"
    data = _normalize_monitor_state({"last_issues_check": ts})
    assert data["last_issues_check"] == ts
