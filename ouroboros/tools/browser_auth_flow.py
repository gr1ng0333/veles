"""Structured site profile + auth state diagnostics for browser login flows."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .browser_auth_verification import (
    build_owner_handoff,
    build_verification_attempt_plan,
    build_verification_attempt_result,
    build_verification_continuation,
    build_verification_handoff,
)


AuthState = str


DEFAULT_LOGIN_URL_MARKERS = ["login", "sign-in", "signin", "auth"]
DEFAULT_CAPTCHA_TERMS = [
    "captcha", "verification code", "verify you are human", "security code",
    "prove you are human", "i am human", "robot", "anti-bot",
]
DEFAULT_MFA_TERMS = [
    "two-factor", "2fa", "one-time", "otp", "verification code",
    "authenticator", "confirm it's you", "security key", "enter code",
]


def normalize_site_profile(site_profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = dict(site_profile or {})
    profile = {
        "site_name": str(raw.get("site_name") or "").strip(),
        "domain": str(raw.get("domain") or "").strip().lower(),
        "entry_url": str(raw.get("entry_url") or "").strip(),
        "login_mode": str(raw.get("login_mode") or raw.get("flow_type") or "login").strip().lower() or "login",
        "username_selector": str(raw.get("username_selector") or "").strip(),
        "password_selector": str(raw.get("password_selector") or "").strip(),
        "submit_selector": str(raw.get("submit_selector") or "").strip(),
        "next_selector": str(raw.get("next_selector") or "").strip(),
        "success_selector": str(raw.get("success_selector") or "").strip(),
        "failure_selector": str(raw.get("failure_selector") or "").strip(),
        "logged_out_selector": str(raw.get("logged_out_selector") or "").strip(),
        "captcha_selector": str(raw.get("captcha_selector") or "").strip(),
        "mfa_selector": str(raw.get("mfa_selector") or "").strip(),
        "expected_url_substring": str(raw.get("expected_url_substring") or "").strip().lower(),
        "protected_url": str(raw.get("protected_url") or "").strip(),
        "success_cookie_names": [str(x).strip() for x in (raw.get("success_cookie_names") or []) if str(x).strip()],
        "failure_text_substrings": [str(x).strip() for x in (raw.get("failure_text_substrings") or []) if str(x).strip()],
        "captcha_text_substrings": [str(x).strip().lower() for x in (raw.get("captcha_text_substrings") or DEFAULT_CAPTCHA_TERMS) if str(x).strip()],
        "mfa_text_substrings": [str(x).strip().lower() for x in (raw.get("mfa_text_substrings") or DEFAULT_MFA_TERMS) if str(x).strip()],
        "login_url_markers": [str(x).strip().lower() for x in (raw.get("login_url_markers") or DEFAULT_LOGIN_URL_MARKERS) if str(x).strip()],
    }
    return profile


def _selector_matched(selector: str, matched: List[str], key: str) -> bool:
    return bool(selector and key in matched)


def build_auth_page_snapshot(
    *,
    current_url: str,
    page_signals: Dict[str, Any],
    matched: Optional[List[str]] = None,
    profile: Optional[Dict[str, Any]] = None,
    protected_url_alive: bool = False,
    submitted_from_login_url: Optional[bool] = None,
) -> Dict[str, Any]:
    profile = normalize_site_profile(profile)
    matched = list(matched or [])
    signals = dict(page_signals or {})
    url = str(current_url or signals.get("url") or "")
    lower_url = url.lower()
    title = str(signals.get("title") or "")
    body_text = str(signals.get("body_text") or "")
    error_texts = [str(x) for x in (signals.get("error_texts") or []) if str(x).strip()]
    body_text_lower = body_text.lower()
    if submitted_from_login_url is None:
        markers = profile.get("login_url_markers") or DEFAULT_LOGIN_URL_MARKERS
        submitted_from_login_url = any(marker in lower_url for marker in markers)
    expected_url_substring = profile.get("expected_url_substring") or ""
    redirected_away_from_login = bool(expected_url_substring and expected_url_substring in lower_url)
    login_form_visible = bool(signals.get("login_form_visible"))

    captcha_selector_hit = _selector_matched(profile.get("captcha_selector", ""), matched, "captcha_selector")
    mfa_selector_hit = _selector_matched(profile.get("mfa_selector", ""), matched, "mfa_selector")
    captcha_text_hits = [term for term in profile.get("captcha_text_substrings", []) if term in body_text_lower]
    mfa_text_hits = [term for term in profile.get("mfa_text_substrings", []) if term in body_text_lower]
    success_cookie_names = [str(x).strip() for x in (signals.get("success_cookie_names") or profile.get("success_cookie_names") or []) if str(x).strip()]
    cookie_names = [str(x).strip() for x in (signals.get("cookie_names") or []) if str(x).strip()]
    success_cookie_hits = [name for name in success_cookie_names if name in cookie_names]
    failure_terms = [str(x).strip() for x in (signals.get("failure_text_substrings") or profile.get("failure_text_substrings") or []) if str(x).strip()]
    failure_text_hits = [
        term for term in failure_terms
        if term.lower() in body_text_lower or term.lower() in " ".join(error_texts).lower()
    ]

    return {
        "profile": profile,
        "current_url": url,
        "current_url_lower": lower_url,
        "title": title,
        "matched": matched,
        "error_texts": error_texts,
        "body_text": body_text,
        "body_text_lower": body_text_lower,
        "login_form_visible": login_form_visible,
        "visible_login_inputs": int(signals.get("visible_login_inputs") or 0),
        "visible_password_fields": int(signals.get("visible_password_fields") or 0),
        "has_profile_ui": bool(signals.get("has_profile_ui")),
        "body_mentions_profile": bool(signals.get("body_mentions_profile")),
        "body_mentions_logout": bool(signals.get("body_mentions_logout")),
        "body_mentions_login": bool(signals.get("body_mentions_login")),
        "error_class_count": int(signals.get("error_class_count") or 0),
        "protected_url_alive": bool(protected_url_alive),
        "submitted_from_login_url": bool(submitted_from_login_url),
        "redirected_away_from_login": redirected_away_from_login,
        "captcha_selector_hit": captcha_selector_hit,
        "mfa_selector_hit": mfa_selector_hit,
        "captcha_text_hits": captcha_text_hits,
        "mfa_text_hits": mfa_text_hits,
        "cookie_names": cookie_names,
        "success_cookie_names": success_cookie_names,
        "success_cookie_hits": success_cookie_hits,
        "failure_text_hits": failure_text_hits,
    }


def infer_auth_state(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    matched = list(snapshot.get("matched") or [])
    profile = snapshot.get("profile") or {}
    flow_type = str(profile.get("login_mode") or "login")
    captcha_hits = list(snapshot.get("captcha_text_hits") or [])
    mfa_hits = list(snapshot.get("mfa_text_hits") or [])

    if snapshot.get("captcha_selector_hit"):
        return {"state": "captcha", "reason": "captcha selector matched", "matched": matched}

    if snapshot.get("mfa_selector_hit"):
        return {"state": "mfa", "reason": "mfa selector matched", "matched": matched}

    if captcha_hits and not mfa_hits:
        return {"state": "captcha", "reason": f"captcha text matched: {', '.join(captcha_hits[:3])}", "matched": matched}

    if mfa_hits and not captcha_hits:
        return {"state": "mfa", "reason": f"mfa text matched: {', '.join(mfa_hits[:3])}", "matched": matched}

    if captcha_hits and mfa_hits:
        captcha_only = [hit for hit in captcha_hits if hit not in mfa_hits]
        mfa_only = [hit for hit in mfa_hits if hit not in captcha_hits]
        if captcha_only and not mfa_only:
            return {"state": "captcha", "reason": f"captcha text matched: {', '.join(captcha_only[:3])}", "matched": matched}
        if mfa_only and not captcha_only:
            return {"state": "mfa", "reason": f"mfa text matched: {', '.join(mfa_only[:3])}", "matched": matched}
        if snapshot.get("current_url", "").lower().find("verify") >= 0:
            return {"state": "mfa", "reason": f"ambiguous verification text, leaning mfa: {', '.join(mfa_hits[:3])}", "matched": matched}
        return {"state": "captcha", "reason": f"ambiguous verification text, leaning captcha: {', '.join(captcha_hits[:3])}", "matched": matched}

    if profile.get("failure_selector") and "failure_selector" in matched:
        return {"state": "error", "reason": "explicit failure selector matched", "matched": matched}

    failure_hits = list(snapshot.get("failure_text_hits") or [])
    if failure_hits:
        return {"state": "error", "reason": f"failure text matched: {', '.join(failure_hits[:3])}", "matched": matched}

    cookie_hit = bool(snapshot.get("success_cookie_hits"))
    current_url_lower = str(snapshot.get("current_url_lower") or snapshot.get("current_url") or "").lower()
    login_markers = [str(x).strip().lower() for x in (profile.get("login_url_markers") or DEFAULT_LOGIN_URL_MARKERS) if str(x).strip()]
    current_url_looks_like_login = any(marker in current_url_lower for marker in login_markers)
    strong_success_signal = bool(
        snapshot.get("protected_url_alive")
        or (profile.get("success_selector") and "success_selector" in matched)
        or snapshot.get("redirected_away_from_login")
    )
    account_ui_signal = bool(
        snapshot.get("has_profile_ui") or snapshot.get("body_mentions_profile") or snapshot.get("body_mentions_logout")
    )
    weak_success_guard_ok = bool(
        not snapshot.get("login_form_visible")
        and not snapshot.get("body_mentions_login")
        and not current_url_looks_like_login
    )
    ui_cookie_success = account_ui_signal and cookie_hit and weak_success_guard_ok
    ui_only_success = account_ui_signal and weak_success_guard_ok
    cookie_only_success = cookie_hit and weak_success_guard_ok and not account_ui_signal

    if strong_success_signal or ui_cookie_success or ui_only_success or cookie_only_success:
        success_reasons: List[str] = []
        if profile.get("success_selector") and "success_selector" in matched:
            success_reasons.append("success selector matched")
        if snapshot.get("protected_url_alive"):
            success_reasons.append("protected_url_alive")
        if snapshot.get("redirected_away_from_login"):
            success_reasons.append("redirected to expected URL")
        if account_ui_signal:
            success_reasons.append("account UI present")
        if cookie_hit:
            success_reasons.append(f"auth cookie present: {', '.join((snapshot.get('success_cookie_hits') or [])[:3])}")
        return {"state": "logged_in", "reason": "; ".join(success_reasons[:3]) or "authenticated signals present", "matched": matched}

    if profile.get("logged_out_selector") and "logged_out_selector" in matched:
        return {"state": "logged_out", "reason": "logged-out selector matched", "matched": matched}

    if snapshot.get("login_form_visible"):
        if snapshot.get("visible_password_fields", 0) > 0:
            state = "login_form" if flow_type == "login" else "signup_form"
            return {"state": state, "reason": "credentials form is visible", "matched": matched}
        return {"state": "username_step", "reason": "identifier step visible without password field", "matched": matched}

    if snapshot.get("body_mentions_login"):
        return {"state": "logged_out", "reason": "page still mentions login/sign-in", "matched": matched}

    return {"state": "unknown", "reason": "insufficient auth-state signals", "matched": matched}


def build_next_action_plan(snapshot: Dict[str, Any], auth_state: Dict[str, Any]) -> Dict[str, Any]:
    state = str(auth_state.get("state") or "unknown")
    profile = snapshot.get("profile") or {}
    flow_type = str(profile.get("login_mode") or "login")
    can_fill_credentials = state in {"login_form", "signup_form", "username_step", "logged_out"}

    def _with_selectors(action: str, reason: str, can_proceed: bool, selectors: Dict[str, Any]) -> Dict[str, Any]:
        clean = {k: v for k, v in selectors.items() if v}
        return {
            "action": action,
            "reason": reason,
            "can_proceed": can_proceed,
            "required_selectors": clean,
            "selectors": clean,
        }

    if state == "captcha":
        return _with_selectors(
            "solve_captcha",
            auth_state.get("reason") or "captcha challenge detected",
            True,
            {
                "captcha_selector": profile.get("captcha_selector", ""),
                "submit_selector": profile.get("submit_selector", ""),
            },
        )
    if state == "mfa":
        return _with_selectors(
            "wait_for_mfa",
            auth_state.get("reason") or "mfa step detected",
            False,
            {
                "mfa_selector": profile.get("mfa_selector", ""),
                "submit_selector": profile.get("submit_selector", ""),
            },
        )
    if state == "logged_in":
        return _with_selectors(
            "continue",
            auth_state.get("reason") or "already authenticated",
            True,
            {},
        )
    if state == "error":
        return _with_selectors(
            "inspect_error",
            auth_state.get("reason") or "explicit auth error detected",
            False,
            {
                "failure_selector": profile.get("failure_selector", ""),
            },
        )
    if can_fill_credentials:
        return _with_selectors(
            "fill_login_form",
            auth_state.get("reason") or f"{flow_type} form is ready",
            True,
            {
                "username_selector": profile.get("username_selector", ""),
                "password_selector": profile.get("password_selector", ""),
                "submit_selector": profile.get("submit_selector", ""),
                "next_selector": profile.get("next_selector", ""),
            },
        )
    return _with_selectors(
        "inspect_page",
        auth_state.get("reason") or "not enough signals to choose next step",
        False,
        {},
    )


def build_verification_boundary(snapshot: Dict[str, Any], auth_state: Dict[str, Any], next_action: Dict[str, Any]) -> Dict[str, Any]:
    state = str(auth_state.get("state") or "unknown")
    profile = snapshot.get("profile") or {}
    matched = snapshot.get("matched") or []
    captcha_hits = list(snapshot.get("captcha_text_hits") or [])
    mfa_hits = list(snapshot.get("mfa_text_hits") or [])

    def _selectors(*pairs: tuple[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in pairs if value}

    if state == "captcha":
        selectors = _selectors(
            ("captcha_selector", profile.get("captcha_selector", "")),
            ("submit_selector", profile.get("submit_selector", "")),
        )
        actionable = bool(selectors.get("captcha_selector"))
        missing_requirements = [] if actionable else ["captcha_selector"]
        return {
            "detected": True,
            "kind": "captcha",
            "reason": auth_state.get("reason") or "captcha challenge detected",
            "source": "selector" if snapshot.get("captcha_selector_hit") else "text",
            "can_auto_attempt": True,
            "requires_owner_input": False,
            "actionable": actionable,
            "missing_requirements": missing_requirements,
            "blocks_progress": True,
            "recommended_action": next_action.get("action") or "solve_captcha",
            "confidence": "high" if snapshot.get("captcha_selector_hit") else "medium",
            "matched": [item for item in matched if item == "captcha_selector"],
            "text_hits": captcha_hits,
            "selectors": selectors,
        }

    if state == "mfa":
        selectors = _selectors(
            ("mfa_selector", profile.get("mfa_selector", "")),
            ("submit_selector", profile.get("submit_selector", "")),
        )
        actionable = bool(selectors.get("mfa_selector"))
        missing_requirements = [] if actionable else ["mfa_selector"]
        return {
            "detected": True,
            "kind": "mfa",
            "reason": auth_state.get("reason") or "mfa step detected",
            "source": "selector" if snapshot.get("mfa_selector_hit") else "text",
            "can_auto_attempt": False,
            "requires_owner_input": True,
            "actionable": actionable,
            "missing_requirements": missing_requirements,
            "blocks_progress": True,
            "recommended_action": next_action.get("action") or "wait_for_mfa",
            "confidence": "high" if snapshot.get("mfa_selector_hit") else "medium",
            "matched": [item for item in matched if item == "mfa_selector"],
            "text_hits": mfa_hits,
            "selectors": selectors,
        }

    return {
        "detected": False,
        "kind": "none",
        "reason": "no verification boundary detected",
        "source": "none",
        "can_auto_attempt": False,
        "requires_owner_input": False,
        "actionable": True,
        "missing_requirements": [],
        "blocks_progress": False,
        "recommended_action": next_action.get("action") or "continue",
        "confidence": "low",
        "matched": [],
        "text_hits": [],
        "selectors": {},
    }



def build_auth_outcome(auth_state: Dict[str, Any], next_action: Dict[str, Any], verification: Dict[str, Any]) -> Dict[str, Any]:
    state = str(auth_state.get("state") or "unknown")
    action = str(next_action.get("action") or "inspect_page")
    verification = dict(verification or {})

    if verification.get("detected"):
        actionable = bool(verification.get("actionable", True))
        if verification.get("requires_owner_input") and actionable:
            status = "blocked_by_verification"
            continuation = "await_owner"
        elif verification.get("can_auto_attempt") and actionable:
            status = "verification_required"
            continuation = "auto_attempt_verification"
        else:
            status = "blocked_by_verification"
            continuation = "stop"
        return {
            "status": status,
            "continuation": continuation,
            "state": state,
            "action": action,
            "can_continue": False,
            "should_auto_attempt_verification": bool(verification.get("can_auto_attempt")) and actionable,
            "requires_owner_input": bool(verification.get("requires_owner_input")) and actionable,
            "is_authenticated": False,
            "is_error": False,
            "blocks_progress": bool(verification.get("blocks_progress")),
        }

    if state == "logged_in":
        return {
            "status": "continue",
            "continuation": "continue",
            "state": state,
            "action": action,
            "can_continue": True,
            "should_auto_attempt_verification": False,
            "requires_owner_input": False,
            "is_authenticated": True,
            "is_error": False,
            "blocks_progress": False,
        }

    if state == "error":
        return {
            "status": "blocked_by_error",
            "continuation": "stop",
            "state": state,
            "action": action,
            "can_continue": False,
            "should_auto_attempt_verification": False,
            "requires_owner_input": False,
            "is_authenticated": False,
            "is_error": True,
            "blocks_progress": True,
        }

    can_continue = bool(next_action.get("can_proceed"))
    return {
        "status": "continue" if can_continue else "needs_inspection",
        "continuation": "continue" if can_continue else "inspect",
        "state": state,
        "action": action,
        "can_continue": can_continue,
        "should_auto_attempt_verification": False,
        "requires_owner_input": False,
        "is_authenticated": False,
        "is_error": False,
        "blocks_progress": not can_continue,
    }


def summarize_auth_diagnostics(
    snapshot: Dict[str, Any],
    auth_state: Dict[str, Any],
    next_action: Dict[str, Any],
    raw_verification_attempt_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    profile = snapshot.get("profile") or {}
    state = auth_state.get("state", "unknown")
    evidence: List[str] = []
    if snapshot.get("protected_url_alive"):
        evidence.append("protected_url_alive")
    if "success_selector" in (snapshot.get("matched") or []):
        evidence.append("success_selector")
    if "failure_selector" in (snapshot.get("matched") or []):
        evidence.append("failure_selector")
    if snapshot.get("captcha_selector_hit"):
        evidence.append("captcha_selector")
    if snapshot.get("mfa_selector_hit"):
        evidence.append("mfa_selector")
    if snapshot.get("redirected_away_from_login"):
        evidence.append("redirected_away_from_login")
    if snapshot.get("has_profile_ui") or snapshot.get("body_mentions_profile") or snapshot.get("body_mentions_logout"):
        evidence.append("account_ui_present")
    if snapshot.get("login_form_visible"):
        evidence.append("login_form_visible")
    if snapshot.get("captcha_text_hits"):
        evidence.append("captcha_text")
    if snapshot.get("mfa_text_hits"):
        evidence.append("mfa_text")
    if snapshot.get("success_cookie_hits"):
        evidence.append("success_cookie")
    if snapshot.get("failure_text_hits"):
        evidence.append("failure_text")

    if state == "logged_in" and (
        snapshot.get("protected_url_alive")
        or "success_selector" in (snapshot.get("matched") or [])
        or snapshot.get("success_cookie_hits")
    ):
        confidence = "high"
    elif state in {"captcha", "mfa", "error", "logged_out", "login_form", "signup_form", "username_step"}:
        confidence = "medium"
    else:
        confidence = "low"

    verification = build_verification_boundary(snapshot, auth_state, next_action)
    outcome = build_auth_outcome(auth_state, next_action, verification)
    verification_handoff = build_verification_handoff(verification, outcome, next_action)
    verification_attempt = build_verification_attempt_plan(verification, outcome, verification_handoff)
    verification_attempt_result = build_verification_attempt_result(verification, verification_attempt, raw_verification_attempt_result)
    verification_continuation = build_verification_continuation(
        verification,
        outcome,
        verification_attempt,
        verification_attempt_result,
    )
    owner_handoff = build_owner_handoff(
        verification,
        outcome,
        verification_handoff,
        verification_continuation,
    )

    return {
        "site_profile": {
            "site_name": profile.get("site_name", ""),
            "domain": profile.get("domain", ""),
            "login_mode": profile.get("login_mode", "login"),
        },
        "state": state,
        "reason": auth_state.get("reason", ""),
        "confidence": confidence,
        "evidence": evidence,
        "verification": verification,
        "outcome": outcome,
        "verification_handoff": verification_handoff,
        "verification_attempt": verification_attempt,
        "verification_attempt_result": verification_attempt_result,
        "verification_continuation": verification_continuation,
        "owner_handoff": owner_handoff,
        "next_action": next_action,
        "current_url": snapshot.get("current_url", ""),
        "matched": snapshot.get("matched", []),
        "signals": {
            "login_form_visible": snapshot.get("login_form_visible", False),
            "visible_login_inputs": snapshot.get("visible_login_inputs", 0),
            "visible_password_fields": snapshot.get("visible_password_fields", 0),
            "has_profile_ui": snapshot.get("has_profile_ui", False),
            "error_class_count": snapshot.get("error_class_count", 0),
            "captcha_text_hits": snapshot.get("captcha_text_hits", []),
            "mfa_text_hits": snapshot.get("mfa_text_hits", []),
            "protected_url_alive": snapshot.get("protected_url_alive", False),
            "success_cookie_hits": snapshot.get("success_cookie_hits", []),
            "failure_text_hits": snapshot.get("failure_text_hits", []),
        },
    }


def _selector_is_visible(page: Any, selector: str) -> bool:
    selector = str(selector or "").strip()
    if not selector:
        return False
    try:
        locator = page.locator(selector)
        if locator.count() == 0:
            return False
        return bool(locator.first.is_visible())
    except Exception:
        return False


import json


def build_fill_login_plan_response(
    *,
    profile: Optional[Dict[str, Any]],
    username_selector: str = "",
    password_selector: str = "",
) -> str:
    profile = normalize_site_profile(profile)
    chosen = {
        "username_selector": profile.get("username_selector") or username_selector,
        "password_selector": profile.get("password_selector") or password_selector,
        "username_source": "site_profile" if profile.get("username_selector") else ("explicit" if username_selector else ""),
        "password_source": "site_profile" if profile.get("password_selector") else ("explicit" if password_selector else ""),
        "shared_form": False,
    }
    snapshot = build_auth_page_snapshot(
        current_url=profile.get("entry_url") or "",
        page_signals={
            "title": profile.get("site_name") or profile.get("domain") or "",
            "body_text": "",
            "login_form_visible": bool(chosen["username_selector"] or chosen["password_selector"]),
            "visible_login_inputs": 1 if chosen["username_selector"] else 0,
            "visible_password_fields": 1 if chosen["password_selector"] else 0,
        },
        matched=[],
        profile=profile,
        protected_url_alive=False,
        submitted_from_login_url=None,
    )
    auth_state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, auth_state)
    diagnostics = summarize_auth_diagnostics(
        snapshot,
        auth_state,
        next_action,
    )
    return json.dumps({
        "success": True,
        "state": "planned",
        "message": "No live browser state; returned site-profile auth plan.",
        "next_action": next_action,
        "used_selectors": next_action.get("selectors", chosen),
        "selectors": chosen,
        "site_profile": profile,
        "diagnostics": diagnostics,
        "post_submit_state": auth_state,
        "error": None,
    }, ensure_ascii=False)


def build_post_submit_auth_result(
    *,
    page: Any,
    profile: Optional[Dict[str, Any]],
    protected_url: str,
    timeout: int,
    post_signals: Dict[str, Any],
    session_probe: Any,
    raw_verification_attempt_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    profile = normalize_site_profile(profile)
    protected_url_to_check = profile.get("protected_url") or protected_url
    protected_url_alive = False
    if protected_url_to_check:
        try:
            protected_url_alive = bool(session_probe(page, protected_url_to_check, timeout))
        except Exception:
            protected_url_alive = False

    matched: List[str] = []
    selector_checks = [
        ("success_selector", profile.get("success_selector", "")),
        ("failure_selector", profile.get("failure_selector", "")),
        ("logged_out_selector", profile.get("logged_out_selector", "")),
        ("captcha_selector", profile.get("captcha_selector", "")),
        ("mfa_selector", profile.get("mfa_selector", "")),
    ]
    for label, selector in selector_checks:
        if _selector_is_visible(page, selector):
            matched.append(label)

    snapshot = build_auth_page_snapshot(
        current_url=page.url,
        page_signals=post_signals,
        matched=matched,
        profile=profile,
        protected_url_alive=protected_url_alive,
        submitted_from_login_url=True,
    )
    auth_state = infer_auth_state(snapshot)
    next_action = build_next_action_plan(snapshot, auth_state)
    diagnostics = summarize_auth_diagnostics(
        snapshot,
        auth_state,
        next_action,
        raw_verification_attempt_result=raw_verification_attempt_result,
    )
    return {
        "diagnostics": diagnostics,
        "verification": diagnostics.get("verification"),
        "outcome": diagnostics.get("outcome"),
        "verification_handoff": diagnostics.get("verification_handoff"),
        "verification_attempt": diagnostics.get("verification_attempt"),
        "verification_attempt_result": diagnostics.get("verification_attempt_result"),
        "verification_continuation": diagnostics.get("verification_continuation"),
        "owner_handoff": diagnostics.get("owner_handoff"),
        "post_submit_state": auth_state,
        "post_submit_signals": post_signals,
        "protected_url_alive": protected_url_alive,
    }
