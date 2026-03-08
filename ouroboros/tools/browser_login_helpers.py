"""Helpers for browser login/session tools."""

from __future__ import annotations

from typing import Any, Dict, List

_SELECTOR_HELPERS_JS = r"""
(function() {
    if (window.__veles_build_selector) return;
    window.__veles_build_selector = function(el, index) {
        if (el.id) return '#' + CSS.escape(el.id);

        var selector = el.tagName.toLowerCase();

        if (el.name) {
            selector += '[name="' + el.name.replace(/"/g, '\\"') + '"]';
            if (document.querySelectorAll(selector).length === 1) return selector;
        }

        if (el.type && el.type !== 'text') {
            selector += '[type="' + el.type + '"]';
        }
        if (el.placeholder) {
            selector += '[placeholder="' + el.placeholder.replace(/"/g, '\\"') + '"]';
        }
        if (document.querySelectorAll(selector).length === 1) return selector;

        if (el.autocomplete && el.autocomplete !== 'off') {
            selector += '[autocomplete="' + el.autocomplete + '"]';
            if (document.querySelectorAll(selector).length === 1) return selector;
        }

        if (typeof index === 'number') {
            selector = el.tagName.toLowerCase() + ':nth-of-type(' + (index + 1) + ')';
        }

        return selector;
    };
})();
"""  # noqa: E501


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
    const loginInputs = Array.from(document.querySelectorAll('input')).filter((el) => {
        const type = (el.type || 'text').toLowerCase();
        return ['text', 'email', 'tel', 'password'].includes(type) || !type;
    });
    const visibleLoginInputs = loginInputs.filter(visible).length;
    const texts = Array.from(document.querySelectorAll('[role="alert"], .error, .alert, .alert-danger, .notification, .flash, [aria-live]'))
        .map((el) => (el.textContent || '').trim())
        .filter(Boolean)
        .slice(0, 10);
    const errorClassCount = document.querySelectorAll('.error, .alert, .alert-danger, [role="alert"], [aria-invalid="true"]').length;
    const bodyText = (document.body.innerText || '').toLowerCase();
    const passwordFields = Array.from(document.querySelectorAll('input[type="password"], input[autocomplete="current-password"], input[autocomplete="new-password"]'));
    const profileUi = Array.from(document.querySelectorAll('a, button, [role="button"], nav, header, img, [class], [id]'))
        .map((el) => [el.textContent || '', el.getAttribute && el.getAttribute('aria-label') || '', el.getAttribute && el.getAttribute('alt') || '', el.id || '', el.className || ''].join(' '))
        .filter(Boolean)
        .slice(0, 120)
        .join(' | ')
        .toLowerCase();
    return {
        url: window.location.href,
        title: document.title || '',
        visible_password_fields: passwordFields.filter(visible).length,
        total_password_fields: passwordFields.length,
        visible_login_inputs: visibleLoginInputs,
        login_form_visible: visibleLoginInputs > 0,
        error_texts: texts,
        error_class_count: errorClassCount,
        body_mentions_logout: /logout|log out|sign out/.test(bodyText),
        body_mentions_profile: /profile|account|my account|dashboard/.test(bodyText),
        body_mentions_login: /login|log in|sign in/.test(bodyText),
        has_profile_ui: /logout|log out|sign out|profile|account|dashboard|avatar/.test(profileUi),
    };
}"""


def infer_login_state(signals: Dict[str, Any]) -> Dict[str, Any]:
    """Infer post-login state with failure priority. Pure function for unit tests."""
    matched = list(signals.get("matched") or [])

    error_text = " ".join(signals.get("error_texts") or []).lower()
    body_text = str(signals.get("body_text") or "").lower()
    failure_text_substrings = [str(x).lower() for x in (signals.get("failure_text_substrings") or []) if str(x).strip()]
    matched_failure_substrings = [term for term in failure_text_substrings if term in error_text or term in body_text]
    has_error_classes = bool(signals.get("has_error_classes")) or int(signals.get("error_class_count") or 0) > 0
    login_form_visible = bool(signals.get("login_form_visible"))
    visible_password_fields = int(signals.get("visible_password_fields") or 0)
    submitted_from_login_url = bool(signals.get("submitted_from_login_url"))
    redirected_away_from_login = bool(signals.get("redirected_away_from_login"))
    body_mentions_login = bool(signals.get("body_mentions_login"))
    has_profile_ui = bool(signals.get("has_profile_ui"))
    body_mentions_profile = bool(signals.get("body_mentions_profile"))
    body_mentions_logout = bool(signals.get("body_mentions_logout"))
    protected_url_alive = bool(signals.get("protected_url_alive"))

    if "failure_selector" in matched:
        return {"state": "failure", "reason": "explicit failure selector matched", "matched": matched}
    if matched_failure_substrings:
        return {
            "state": "failure",
            "reason": f"failure text matched: {', '.join(matched_failure_substrings[:3])}",
            "matched": matched,
        }
    if error_text:
        failure_terms = ["invalid", "incorrect", "wrong", "try again", "error", "failed", "required"]
        if any(term in error_text for term in failure_terms):
            return {
                "state": "failure",
                "reason": "error or alert text suggests authentication failure",
                "matched": matched,
            }
    if has_error_classes:
        return {
            "state": "failure",
            "reason": "error/alert classes present in DOM after submit",
            "matched": matched,
        }
    if submitted_from_login_url and not redirected_away_from_login and login_form_visible:
        return {
            "state": "failure",
            "reason": "still on login URL with login form visible after submit",
            "matched": matched,
        }

    success_reasons: List[str] = []
    if "success_selector" in matched:
        success_reasons.append("explicit success selector matched")
    if redirected_away_from_login:
        success_reasons.append("redirected away from login URL")
    if not login_form_visible:
        success_reasons.append("login form disappeared after submit")
    if has_profile_ui or body_mentions_profile or body_mentions_logout:
        success_reasons.append("account/logout/dashboard UI present")
    if protected_url_alive:
        success_reasons.append("protected URL stayed accessible")

    strong_success = any([
        "success_selector" in matched,
        redirected_away_from_login,
        has_profile_ui or body_mentions_profile or body_mentions_logout,
        protected_url_alive,
    ])
    if strong_success or (success_reasons and not login_form_visible and submitted_from_login_url):
        return {
            "state": "success",
            "reason": "; ".join(success_reasons[:3]),
            "matched": matched,
        }

    if "logged_out_selector" in matched:
        return {"state": "unclear", "reason": "logged-out selector matched without explicit failure", "matched": matched}
    if visible_password_fields > 0 or body_mentions_login:
        return {"state": "unclear", "reason": "login UI is still present without explicit failure", "matched": matched}
    return {"state": "unclear", "reason": "insufficient post-login signals", "matched": matched}

