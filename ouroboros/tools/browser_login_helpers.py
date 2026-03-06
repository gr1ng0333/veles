"""Helpers for browser login/session tools."""

from __future__ import annotations

from typing import Any, Dict, List


def plan_login_flow(user_selector: str, password_selector: str, allow_multi_step: bool = False) -> Dict[str, Any]:
    """Plan whether login can proceed in one step, two steps, or not at all."""
    user_sel = str(user_selector or "").strip()
    pass_sel = str(password_selector or "").strip()
    if user_sel and pass_sel:
        return {"mode": "single_step", "can_proceed": True, "reason": "username and password fields available"}
    if user_sel and allow_multi_step and not pass_sel:
        return {"mode": "multi_step_username_first", "can_proceed": True, "reason": "username field available; waiting for password after next step"}
    if not user_sel:
        return {"mode": "missing_username", "can_proceed": False, "reason": "username/email field not found"}
    return {"mode": "missing_password", "can_proceed": False, "reason": "password field not found"}

_USERNAME_CANDIDATE_JS = r"""() => {
    const toInfo = (el, index, source, score) => {
        const form = el.form || el.closest('form');
        const attrs = [el.id, el.name, el.getAttribute('placeholder'), el.getAttribute('aria-label'), el.getAttribute('autocomplete')]
            .filter(Boolean)
            .join(' ')
            .toLowerCase();
        return {
            selector: window.__veles_build_selector(el, index),
            type: (el.type || '').toLowerCase(),
            form_selector: form ? window.__veles_build_selector(form, 0) : '',
            attrs,
            source,
            score,
        };
    };

    const inputs = Array.from(document.querySelectorAll('input'));
    return inputs
        .filter((el) => {
            const type = (el.type || 'text').toLowerCase();
            return ['text', 'email', 'tel'].includes(type) || !type;
        })
        .map((el, index) => {
            const attrs = [el.id, el.name, el.getAttribute('placeholder'), el.getAttribute('aria-label'), el.getAttribute('autocomplete')]
                .filter(Boolean)
                .join(' ')
                .toLowerCase();
            let score = 0;
            if (attrs.includes('email')) score += 6;
            if (attrs.includes('user')) score += 5;
            if (attrs.includes('login')) score += 5;
            if (attrs.includes('username')) score += 6;
            if ((el.getAttribute('autocomplete') || '').toLowerCase() === 'username') score += 8;
            if ((el.type || '').toLowerCase() === 'email') score += 7;
            if (!attrs.includes('search')) score += 1;
            return toInfo(el, index, 'heuristic', score);
        })
        .sort((a, b) => b.score - a.score);
}"""

_PASSWORD_CANDIDATE_JS = r"""() => {
    const toInfo = (el, index, source, score) => {
        const form = el.form || el.closest('form');
        const attrs = [el.id, el.name, el.getAttribute('placeholder'), el.getAttribute('aria-label'), el.getAttribute('autocomplete')]
            .filter(Boolean)
            .join(' ')
            .toLowerCase();
        return {
            selector: window.__veles_build_selector(el, index),
            type: (el.type || '').toLowerCase(),
            form_selector: form ? window.__veles_build_selector(form, 0) : '',
            attrs,
            source,
            score,
        };
    };

    const inputs = Array.from(document.querySelectorAll('input[type="password"], input[autocomplete="current-password"], input[autocomplete="new-password"]'));
    return inputs
        .map((el, index) => {
            const attrs = [el.id, el.name, el.getAttribute('placeholder'), el.getAttribute('aria-label'), el.getAttribute('autocomplete')]
                .filter(Boolean)
                .join(' ')
                .toLowerCase();
            let score = 10;
            if (attrs.includes('password')) score += 8;
            if ((el.getAttribute('autocomplete') || '').toLowerCase() === 'current-password') score += 10;
            return toInfo(el, index, 'heuristic', score);
        })
        .sort((a, b) => b.score - a.score);
}"""

_FILL_INPUT_JS = r"""({selector, value}) => {
    const el = document.querySelector(selector);
    if (!el) return {ok: false, reason: 'not_found', selector};
    el.focus();
    el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return {ok: true, selector, tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase()};
}"""

_SUBMIT_LOGIN_FORM_JS = r"""({anchorSelector, submitSelector}) => {
    const keywords = ['sign in', 'log in', 'login', 'continue', 'next', 'submit', 'verify'];
    const anchorEl = anchorSelector ? document.querySelector(anchorSelector) : null;
    const explicitSubmit = submitSelector ? document.querySelector(submitSelector) : null;
    const form = anchorEl ? (anchorEl.form || anchorEl.closest('form')) : null;

    const visible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && !el.disabled && rect.width > 0 && rect.height > 0;
    };

    const labelScore = (el) => {
        const text = [el.innerText, el.textContent, el.value, el.getAttribute('aria-label'), el.getAttribute('title')]
            .filter(Boolean)
            .join(' ')
            .trim()
            .toLowerCase();
        if (!text) return 0;
        let score = 0;
        for (const keyword of keywords) {
            if (text.includes(keyword)) score += keyword === 'continue' || keyword === 'next' ? 4 : 6;
        }
        return score;
    };

    const clickAndDescribe = (el, selector, source) => {
        el.click();
        return {submitted: true, method: 'click', selector, source, text: ((el.innerText || el.textContent || el.value || '') + '').trim().slice(0, 120)};
    };

    const bestButton = (nodes) => {
        return nodes
            .filter(visible)
            .map((el) => ({ el, score: labelScore(el) }))
            .sort((a, b) => b.score - a.score)[0] || null;
    };

    if (explicitSubmit && visible(explicitSubmit)) {
        return clickAndDescribe(explicitSubmit, submitSelector, 'explicit_submit_selector');
    }

    if (form) {
        const controls = Array.from(form.querySelectorAll('button, input[type="submit"], input[type="button"], [role="button"]'));
        const scored = bestButton(controls);
        if (scored && scored.score > 0) {
            return clickAndDescribe(scored.el, window.__veles_build_selector(scored.el, 0), 'form_keyword_control');
        }
        const submitEl = form.querySelector('button[type="submit"], input[type="submit"], button:not([type]), [role="button"]');
        if (submitEl && visible(submitEl)) {
            return clickAndDescribe(submitEl, window.__veles_build_selector(submitEl, 0), 'form_submit_control');
        }
        if (typeof form.requestSubmit === 'function') {
            form.requestSubmit();
            return {submitted: true, method: 'requestSubmit', selector: window.__veles_build_selector(form, 0), source: 'form_request_submit'};
        }
        form.submit();
        return {submitted: true, method: 'form_submit', selector: window.__veles_build_selector(form, 0), source: 'form_submit'};
    }

    const globalControls = Array.from(document.querySelectorAll('button, input[type="submit"], input[type="button"], [role="button"]'));
    const globalBest = bestButton(globalControls);
    if (globalBest && globalBest.score > 0) {
        return clickAndDescribe(globalBest.el, window.__veles_build_selector(globalBest.el, 0), 'global_keyword_control');
    }

    if (anchorEl) {
        anchorEl.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true}));
        anchorEl.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', bubbles: true}));
        return {submitted: true, method: 'enter_key', selector: anchorSelector, source: 'anchor_enter'};
    }

    return {submitted: false, method: 'none', selector: '', source: 'no_submit_target'};
}"""

_LOGIN_SIGNALS_JS = r"""() => {
    const visible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const texts = Array.from(document.querySelectorAll('[role="alert"], .error, .alert, .alert-danger, .notification, .flash, [aria-live]'))
        .map((el) => (el.textContent || '').trim())
        .filter(Boolean)
        .slice(0, 10);
    const bodyText = (document.body.innerText || '').toLowerCase();
    const passwordFields = Array.from(document.querySelectorAll('input[type="password"], input[autocomplete="current-password"], input[autocomplete="new-password"]'));
    const profileUi = Array.from(document.querySelectorAll('a, button, [role="button"], nav, header'))
        .map((el) => (el.textContent || '').trim())
        .filter(Boolean)
        .slice(0, 50)
        .join(' | ')
        .toLowerCase();
    return {
        url: window.location.href,
        title: document.title || '',
        visible_password_fields: passwordFields.filter(visible).length,
        total_password_fields: passwordFields.length,
        error_texts: texts,
        body_mentions_logout: /logout|log out|sign out/.test(bodyText),
        body_mentions_profile: /profile|account|my account|dashboard/.test(bodyText),
        body_mentions_login: /login|log in|sign in/.test(bodyText),
        has_profile_ui: /logout|log out|sign out|profile|account|dashboard/.test(profileUi),
    };
}"""


def infer_login_state(signals: Dict[str, Any]) -> Dict[str, Any]:
    """Infer coarse login state from page signals. Pure function for unit tests."""
    matched = list(signals.get("matched") or [])
    reason_parts: List[str] = []

    if "success_selector" in matched:
        return {"state": "logged_in", "reason": "explicit success selector matched", "matched": matched}
    if "failure_selector" in matched:
        return {"state": "login_failed", "reason": "explicit failure selector matched", "matched": matched}
    if "logged_out_selector" in matched:
        return {"state": "logged_out", "reason": "explicit logged_out selector matched", "matched": matched}

    error_text = " ".join(signals.get("error_texts") or []).lower()
    body_text = str(signals.get("body_text") or "").lower()
    failure_text_substrings = [str(x).lower() for x in (signals.get("failure_text_substrings") or []) if str(x).strip()]
    matched_failure_substrings = [term for term in failure_text_substrings if term in error_text or term in body_text]
    if matched_failure_substrings:
        return {
            "state": "login_failed",
            "reason": f"failure text matched: {', '.join(matched_failure_substrings[:3])}",
            "matched": matched,
        }

    if error_text:
        failure_terms = ["invalid", "incorrect", "wrong", "try again", "error", "failed", "required"]
        if any(term in error_text for term in failure_terms):
            return {
                "state": "login_failed",
                "reason": "error or alert text suggests authentication failure",
                "matched": matched,
            }
        reason_parts.append("alert text present but not clearly auth-related")

    visible_password_fields = int(signals.get("visible_password_fields") or 0)
    body_mentions_login = bool(signals.get("body_mentions_login"))
    has_profile_ui = bool(signals.get("has_profile_ui"))
    body_mentions_profile = bool(signals.get("body_mentions_profile"))
    body_mentions_logout = bool(signals.get("body_mentions_logout"))
    expected_url_matched = bool(signals.get("expected_url_matched"))
    success_cookie_names = [str(x).lower() for x in (signals.get("success_cookie_names") or []) if str(x).strip()]
    cookie_names = [str(x).lower() for x in (signals.get("cookie_names") or []) if str(x).strip()]
    matched_success_cookies = [name for name in success_cookie_names if name in cookie_names]

    positive_signals = 0
    if has_profile_ui or body_mentions_profile or body_mentions_logout:
        positive_signals += 1
    if expected_url_matched:
        positive_signals += 1
    if matched_success_cookies:
        positive_signals += 1

    if matched_success_cookies and visible_password_fields == 0 and not body_mentions_login:
        return {
            "state": "logged_in",
            "reason": f"success cookies present: {', '.join(matched_success_cookies[:3])}",
            "matched": matched,
        }

    if expected_url_matched and visible_password_fields == 0 and positive_signals >= 1:
        return {
            "state": "logged_in",
            "reason": "expected post-login URL matched and password field is gone",
            "matched": matched,
        }

    if (has_profile_ui or body_mentions_logout or body_mentions_profile) and visible_password_fields == 0:
        return {
            "state": "logged_in",
            "reason": "profile/logout UI present and password form no longer visible",
            "matched": matched,
        }

    if positive_signals >= 2 and visible_password_fields == 0:
        return {
            "state": "logged_in",
            "reason": "multiple post-login signals agree",
            "matched": matched,
        }

    if positive_signals > 0 and visible_password_fields > 0:
        return {
            "state": "unclear",
            "reason": "post-login signals conflict with still-visible password field",
            "matched": matched,
        }

    if visible_password_fields > 0 and body_mentions_login:
        return {
            "state": "logged_out",
            "reason": "login/sign-in page still visible with password field",
            "matched": matched,
        }

    if visible_password_fields > 0:
        return {
            "state": "unclear",
            "reason": "password field still visible without explicit failure signal",
            "matched": matched,
        }

    if has_profile_ui or body_mentions_profile or body_mentions_logout:
        return {
            "state": "logged_in",
            "reason": "page suggests authenticated account UI",
            "matched": matched,
        }

    if expected_url_matched and not body_mentions_login:
        return {
            "state": "unclear",
            "reason": "expected URL matched, but other login signals remain weak",
            "matched": matched,
        }

    if body_mentions_login:
        return {
            "state": "logged_out",
            "reason": "page still looks like login screen",
            "matched": matched,
        }

    return {
        "state": "unclear",
        "reason": "; ".join(reason_parts) if reason_parts else "insufficient signals",
        "matched": matched,
    }

