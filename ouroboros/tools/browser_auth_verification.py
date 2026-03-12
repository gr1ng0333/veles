"""Verification planning, continuation, and owner handoff for browser auth flows."""

from __future__ import annotations

from typing import Any, Dict, Optional


def compute_auth_flow_success(
    auth_state: Dict[str, Any],
    outcome: Dict[str, Any],
    verification_continuation: Dict[str, Any],
    owner_handoff_completion: Dict[str, Any],
) -> bool:
    state = str(auth_state.get("state") or "unknown")
    if state == "logged_in":
        return True

    continuation_can_resume = bool(verification_continuation.get("can_resume_auth"))
    completion_can_resume = bool(owner_handoff_completion.get("can_resume_auth"))
    completion_status = str(owner_handoff_completion.get("status") or "")

    if completion_status == "completed" and completion_can_resume:
        return True

    if continuation_can_resume and state not in {"captcha", "mfa", "error"}:
        return True

    return bool(outcome.get("success"))


def build_verification_attempt_plan(
    verification: Dict[str, Any],
    outcome: Dict[str, Any],
    verification_handoff: Dict[str, Any],
) -> Dict[str, Any]:
    verification = dict(verification or {})
    outcome = dict(outcome or {})
    verification_handoff = dict(verification_handoff or {})

    kind = str(verification.get("kind") or "none")
    selectors = dict(verification.get("selectors") or verification_handoff.get("selectors") or {})
    missing_requirements = list(verification.get("missing_requirements") or [])
    continuation = str(outcome.get("continuation") or "continue")
    handoff_mode = str(verification_handoff.get("mode") or "none")

    if not verification.get("detected"):
        return {
            "status": "not_applicable",
            "kind": "none",
            "strategy": "none",
            "can_auto_attempt": False,
            "requires_screenshot": False,
            "requires_owner_input": False,
            "next_step": str(outcome.get("continuation") or "continue"),
            "reason": "no verification attempt is needed",
            "selectors": selectors,
            "missing_requirements": [],
            "attempt_inputs": [],
        }

    if continuation == "await_owner" or verification.get("requires_owner_input") or handoff_mode == "owner_handoff":
        return {
            "status": "owner_required",
            "kind": kind,
            "strategy": "owner_handoff",
            "can_auto_attempt": False,
            "requires_screenshot": False,
            "requires_owner_input": True,
            "next_step": "await_owner",
            "reason": verification.get("reason") or "verification requires owner input",
            "selectors": selectors,
            "missing_requirements": missing_requirements,
            "attempt_inputs": ["owner_verification_code", *missing_requirements],
        }

    if kind == "captcha":
        captcha_selector = str(selectors.get("captcha_selector") or "").strip()
        if captcha_selector:
            return {
                "status": "ready",
                "kind": kind,
                "strategy": "solve_simple_captcha_from_screenshot",
                "can_auto_attempt": True,
                "requires_screenshot": True,
                "requires_owner_input": False,
                "next_step": "capture_and_solve_captcha",
                "reason": verification.get("reason") or "simple captcha attempt can be prepared automatically",
                "selectors": selectors,
                "missing_requirements": [],
                "attempt_inputs": ["captcha_image", "captcha_answer"],
            }
        blocked_missing = missing_requirements or ["captcha_selector"]
        return {
            "status": "blocked",
            "kind": kind,
            "strategy": "none",
            "can_auto_attempt": False,
            "requires_screenshot": False,
            "requires_owner_input": False,
            "next_step": "inspect_page",
            "reason": "captcha boundary detected but there is not enough structure for a safe auto-attempt",
            "selectors": selectors,
            "missing_requirements": blocked_missing,
            "attempt_inputs": [],
        }

    return {
        "status": "blocked",
        "kind": kind,
        "strategy": "none",
        "can_auto_attempt": False,
        "requires_screenshot": False,
        "requires_owner_input": False,
        "next_step": "inspect_page",
        "reason": verification.get("reason") or "verification boundary detected but no safe automatic attempt is defined",
        "selectors": selectors,
        "missing_requirements": missing_requirements,
        "attempt_inputs": [],
    }


def build_verification_attempt_result(
    verification: Dict[str, Any],
    verification_attempt: Dict[str, Any],
    raw_attempt_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    verification = dict(verification or {})
    verification_attempt = dict(verification_attempt or {})
    raw_attempt_result = dict(raw_attempt_result or {})

    if not verification.get("detected"):
        return {
            "status": "not_attempted",
            "kind": "none",
            "attempted": False,
            "strategy": str(verification_attempt.get("strategy") or "none"),
            "success": False,
            "confidence": None,
            "text": "",
            "attempts": 0,
            "reason": "no verification boundary was active",
            "error": None,
        }

    if not raw_attempt_result:
        planned_status = str(verification_attempt.get("status") or "blocked")
        if planned_status == "ready":
            return {
                "status": "planned_but_not_executed",
                "kind": str(verification.get("kind") or "none"),
                "attempted": False,
                "strategy": str(verification_attempt.get("strategy") or "none"),
                "success": False,
                "confidence": None,
                "text": "",
                "attempts": 0,
                "reason": "verification attempt was planned but not executed",
                "error": None,
            }
        return {
            "status": "not_attempted",
            "kind": str(verification.get("kind") or "none"),
            "attempted": False,
            "strategy": str(verification_attempt.get("strategy") or "none"),
            "success": False,
            "confidence": None,
            "text": "",
            "attempts": 0,
            "reason": str(verification_attempt.get("reason") or verification.get("reason") or "verification attempt was not executed"),
            "error": None,
        }

    success = bool(raw_attempt_result.get("success"))
    error = raw_attempt_result.get("error")
    confidence = raw_attempt_result.get("confidence")
    attempts = int(raw_attempt_result.get("attempts") or 0)
    text = str(raw_attempt_result.get("text") or "")
    method = str(raw_attempt_result.get("method") or raw_attempt_result.get("strategy") or verification_attempt.get("strategy") or "none")

    return {
        "status": "succeeded" if success else "failed",
        "kind": str(verification.get("kind") or "none"),
        "attempted": True,
        "strategy": str(verification_attempt.get("strategy") or "none"),
        "method": method,
        "success": success,
        "confidence": confidence,
        "text": text,
        "attempts": attempts,
        "reason": str(raw_attempt_result.get("reason") or verification_attempt.get("reason") or verification.get("reason") or "verification attempt executed"),
        "error": error,
        "selectors": dict(verification.get("selectors") or verification_attempt.get("selectors") or {}),
    }


def build_verification_continuation(
    verification: Dict[str, Any],
    outcome: Dict[str, Any],
    verification_attempt: Dict[str, Any],
    verification_attempt_result: Dict[str, Any],
) -> Dict[str, Any]:
    verification = dict(verification or {})
    outcome = dict(outcome or {})
    verification_attempt = dict(verification_attempt or {})
    verification_attempt_result = dict(verification_attempt_result or {})

    if not verification.get("detected"):
        return {
            "status": "continue_login",
            "action": "continue_login",
            "can_resume_auth": True,
            "requires_owner_input": False,
            "should_retry_verification": False,
            "reason": "no verification boundary is active",
            "source": "none",
        }

    continuation = str(outcome.get("continuation") or "stop")
    attempt_status = str(verification_attempt_result.get("status") or "not_attempted")
    attempt_success = bool(verification_attempt_result.get("success"))
    handoff_required = bool(verification.get("requires_owner_input")) or continuation == "await_owner"

    if handoff_required:
        return {
            "status": "await_owner",
            "action": "await_owner",
            "can_resume_auth": False,
            "requires_owner_input": True,
            "should_retry_verification": False,
            "reason": verification_attempt_result.get("reason") or verification.get("reason") or "verification requires owner input",
            "source": "verification_boundary",
        }

    if attempt_success:
        return {
            "status": "continue_login",
            "action": "continue_login",
            "can_resume_auth": True,
            "requires_owner_input": False,
            "should_retry_verification": False,
            "reason": verification_attempt_result.get("reason") or "verification attempt succeeded",
            "source": "verification_attempt_result",
        }

    if attempt_status == "failed":
        attempts = int(verification_attempt_result.get("attempts") or 0)
        confidence = verification_attempt_result.get("confidence")
        should_retry = attempts <= 1 and (confidence is None or float(confidence) < 0.5)
        return {
            "status": "retry_verification" if should_retry else "await_owner",
            "action": "retry_verification" if should_retry else "await_owner",
            "can_resume_auth": False,
            "requires_owner_input": not should_retry,
            "should_retry_verification": should_retry,
            "reason": verification_attempt_result.get("error") or verification_attempt_result.get("reason") or "verification attempt failed",
            "source": "verification_attempt_result",
        }

    if attempt_status == "planned_but_not_executed":
        return {
            "status": "retry_verification",
            "action": "retry_verification",
            "can_resume_auth": False,
            "requires_owner_input": False,
            "should_retry_verification": True,
            "reason": verification_attempt_result.get("reason") or verification_attempt.get("reason") or "verification attempt was planned but not executed",
            "source": "verification_attempt_plan",
        }

    if continuation == "auto_attempt_verification":
        return {
            "status": "retry_verification",
            "action": "retry_verification",
            "can_resume_auth": False,
            "requires_owner_input": False,
            "should_retry_verification": True,
            "reason": verification_attempt_result.get("reason") or verification.get("reason") or "verification should be attempted before auth can continue",
            "source": "auth_outcome",
        }

    return {
        "status": "stop",
        "action": "stop",
        "can_resume_auth": False,
        "requires_owner_input": False,
        "should_retry_verification": False,
        "reason": verification_attempt_result.get("reason") or verification.get("reason") or "no safe continuation is available",
        "source": "auth_outcome",
    }



def build_verification_handoff(
    verification: Dict[str, Any],
    outcome: Dict[str, Any],
    next_action: Dict[str, Any],
) -> Dict[str, Any]:
    verification = dict(verification or {})
    outcome = dict(outcome or {})
    next_action = dict(next_action or {})

    selectors = dict(verification.get("selectors") or next_action.get("selectors") or {})
    action = str(next_action.get("action") or verification.get("recommended_action") or "continue")
    continuation = str(outcome.get("continuation") or "continue")
    kind = str(verification.get("kind") or "none")
    missing_requirements = list(verification.get("missing_requirements") or [])

    if not verification.get("detected"):
        return {
            "active": False,
            "mode": "none",
            "kind": "none",
            "action": action,
            "continuation": continuation,
            "message": "No verification handoff required.",
            "instructions": [],
            "required_inputs": [],
            "selectors": selectors,
        }

    if continuation == "auto_attempt_verification":
        instructions = [
            "Capture or reuse the verification image/element from the current page.",
            "Attempt the configured captcha flow automatically using the detected selectors.",
            "Re-check auth state after submit instead of assuming verification success.",
        ]
        return {
            "active": True,
            "mode": "auto_attempt",
            "kind": kind,
            "action": action,
            "continuation": continuation,
            "message": "Verification can be attempted automatically before the auth flow continues.",
            "instructions": instructions,
            "required_inputs": missing_requirements,
            "selectors": selectors,
        }

    if continuation == "await_owner":
        instructions = [
            "Pause automatic progress at the current verification step.",
            "Request the missing owner-provided code or approval needed to continue.",
            "Resume only after the owner input is supplied and re-check auth state after submission.",
        ]
        return {
            "active": True,
            "mode": "owner_handoff",
            "kind": kind,
            "action": action,
            "continuation": continuation,
            "message": "Verification requires owner input before the auth flow can continue.",
            "instructions": instructions,
            "required_inputs": ["owner_verification_code", *missing_requirements],
            "selectors": selectors,
        }

    return {
        "active": True,
        "mode": "blocked",
        "kind": kind,
        "action": action,
        "continuation": continuation,
        "message": "Verification boundary detected, but no safe continuation is available.",
        "instructions": [
            "Do not continue the auth flow automatically.",
            "Inspect the page or escalate to the owner before taking further action.",
        ],
        "required_inputs": missing_requirements,
        "selectors": selectors,
    }


def build_owner_handoff(
    verification: Dict[str, Any],
    outcome: Dict[str, Any],
    verification_handoff: Dict[str, Any],
    verification_continuation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    verification = dict(verification or {})
    outcome = dict(outcome or {})
    verification_handoff = dict(verification_handoff or {})
    verification_continuation = dict(verification_continuation or {})

    selectors = dict(verification.get("selectors") or verification_handoff.get("selectors") or {})
    kind = str(verification.get("kind") or verification_handoff.get("kind") or "none")
    continuation = str(verification_continuation.get("status") or outcome.get("continuation") or "continue")
    handoff_mode = str(verification_handoff.get("mode") or "none")
    missing_requirements = list(verification.get("missing_requirements") or [])
    verification_reason = str(verification.get("reason") or verification_handoff.get("message") or "")
    continuation_reason = str(verification_continuation.get("reason") or "")
    blocking = bool(verification.get("blocks_progress"))

    owner_required = bool(
        verification_continuation.get("requires_owner_input")
        or continuation == "await_owner"
        or handoff_mode == "owner_handoff"
    )

    if not owner_required:
        return {
            "required": False,
            "kind": "none",
            "reason": "owner handoff is not required",
            "instruction": "Continue the flow without asking the owner for verification input.",
            "resume_hint": "no owner action needed",
            "blocking": False,
            "required_inputs": [],
            "selectors": selectors,
        }

    instruction_parts = []
    if kind == "mfa":
        instruction_parts.append("Попросить владельца ввести или подтвердить MFA-код на текущем шаге")
    elif kind == "captcha":
        instruction_parts.append("Попросить владельца вручную пройти verification на текущей странице")
    else:
        instruction_parts.append("Попросить владельца вручную завершить verification step")
    if selectors:
        selector_names = ", ".join(sorted(selectors.keys())[:3])
        instruction_parts.append(f"ориентир по селекторам: {selector_names}")
    instruction_parts.append("после этого повторно проверить auth state и продолжить сценарий только по новому состоянию")

    required_inputs = ["owner_verification_code", *missing_requirements]
    dedup_required_inputs = []
    for item in required_inputs:
        if item and item not in dedup_required_inputs:
            dedup_required_inputs.append(item)

    if kind == "mfa":
        resume_hint = "после ввода кода повторно вызвать post-submit auth diagnostics и проверить, стал ли доступен continue_login"
    elif kind == "captcha":
        resume_hint = "после ручного прохождения captcha повторно проверить verification boundary и auth state"
    else:
        resume_hint = "после ручного verification шага повторно снять diagnostics и решить следующий шаг по новому состоянию"

    return {
        "required": True,
        "kind": kind,
        "reason": continuation_reason or verification_reason or "owner verification input is required",
        "instruction": "; ".join(instruction_parts),
        "resume_hint": resume_hint,
        "blocking": blocking,
        "required_inputs": dedup_required_inputs,
        "selectors": selectors,
    }



def build_owner_handoff_completion(
    verification: Dict[str, Any],
    owner_handoff: Dict[str, Any],
    owner_handoff_resume: Dict[str, Any],
    verification_continuation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    verification = dict(verification or {})
    owner_handoff = dict(owner_handoff or {})
    owner_handoff_resume = dict(owner_handoff_resume or {})
    verification_continuation = dict(verification_continuation or {})

    kind = str(owner_handoff.get("kind") or verification.get("kind") or "none")
    selectors = dict(owner_handoff.get("selectors") or verification.get("selectors") or {})
    resume_status = str(owner_handoff_resume.get("status") or "not_needed")
    continuation_status = str(verification_continuation.get("status") or "continue_login")
    continuation_reason = str(verification_continuation.get("reason") or "")
    resume_reason = str(owner_handoff_resume.get("reason") or "")

    if not owner_handoff.get("required"):
        return {
            "status": "not_applicable",
            "completed": False,
            "can_resume_auth": bool(verification_continuation.get("can_resume_auth", True)),
            "kind": "none",
            "completion_reason": "owner handoff was not required",
            "next_action": str(owner_handoff_resume.get("resume_action") or verification_continuation.get("action") or "continue_login"),
            "source": "owner_handoff",
            "selectors": selectors,
        }

    if resume_status == "resume_ready" or continuation_status == "continue_login":
        return {
            "status": "completed",
            "completed": True,
            "can_resume_auth": True,
            "kind": kind,
            "completion_reason": resume_reason or continuation_reason or "owner handoff appears completed and auth can resume",
            "next_action": str(owner_handoff_resume.get("resume_action") or verification_continuation.get("action") or "continue_login"),
            "source": "owner_handoff_resume",
            "selectors": selectors,
        }

    if resume_status == "awaiting_owner":
        return {
            "status": "still_waiting",
            "completed": False,
            "can_resume_auth": False,
            "kind": kind,
            "completion_reason": resume_reason or owner_handoff.get("reason") or "owner handoff is still waiting for manual completion",
            "next_action": str(owner_handoff_resume.get("resume_action") or verification_continuation.get("action") or "await_owner"),
            "source": "owner_handoff_resume",
            "selectors": selectors,
        }

    if resume_status in {"still_blocked", "retry_auto_before_owner"}:
        return {
            "status": "blocked",
            "completed": False,
            "can_resume_auth": False,
            "kind": kind,
            "completion_reason": resume_reason or continuation_reason or "owner handoff has not cleared the verification boundary yet",
            "next_action": str(owner_handoff_resume.get("resume_action") or verification_continuation.get("action") or "stop"),
            "source": "owner_handoff_resume",
            "selectors": selectors,
        }

    return {
        "status": "blocked",
        "completed": False,
        "can_resume_auth": False,
        "kind": kind,
        "completion_reason": resume_reason or continuation_reason or owner_handoff.get("reason") or "owner handoff completion could not be confirmed",
        "next_action": str(owner_handoff_resume.get("resume_action") or verification_continuation.get("action") or "stop"),
        "source": "owner_handoff_resume",
        "selectors": selectors,
    }


def build_owner_handoff_resume(
    verification: Dict[str, Any],
    verification_attempt: Dict[str, Any],
    verification_attempt_result: Dict[str, Any],
    verification_continuation: Dict[str, Any],
    owner_handoff: Dict[str, Any],
) -> Dict[str, Any]:
    verification = dict(verification or {})
    verification_attempt = dict(verification_attempt or {})
    verification_attempt_result = dict(verification_attempt_result or {})
    verification_continuation = dict(verification_continuation or {})
    owner_handoff = dict(owner_handoff or {})

    continuation_status = str(verification_continuation.get("status") or "continue_login")
    continuation_action = str(verification_continuation.get("action") or continuation_status)
    attempt_status = str(verification_attempt_result.get("status") or "not_attempted")
    attempt_strategy = str(verification_attempt.get("strategy") or verification_attempt_result.get("strategy") or "none")
    kind = str(owner_handoff.get("kind") or verification.get("kind") or "none")
    selectors = dict(owner_handoff.get("selectors") or verification.get("selectors") or {})

    if not owner_handoff.get("required"):
        return {
            "status": "not_needed",
            "can_resume_auth": bool(verification_continuation.get("can_resume_auth", True)),
            "resume_action": continuation_action,
            "kind": "none",
            "reason": "owner handoff is not required",
            "source": "owner_handoff",
            "selectors": selectors,
        }

    if continuation_status == "continue_login":
        return {
            "status": "resume_ready",
            "can_resume_auth": True,
            "resume_action": continuation_action,
            "kind": kind,
            "reason": verification_continuation.get("reason") or "owner verification step appears completed; auth flow can resume",
            "source": "verification_continuation",
            "selectors": selectors,
        }

    if continuation_status == "retry_verification":
        return {
            "status": "retry_auto_before_owner",
            "can_resume_auth": False,
            "resume_action": continuation_action,
            "kind": kind,
            "reason": verification_continuation.get("reason") or "retry automatic verification before asking the owner again",
            "source": "verification_continuation",
            "selectors": selectors,
            "attempt_status": attempt_status,
            "attempt_strategy": attempt_strategy,
        }

    if continuation_status == "await_owner":
        blocked_statuses = {"failed", "planned_but_not_executed"}
        status = "still_blocked" if attempt_status in blocked_statuses else "awaiting_owner"
        return {
            "status": status,
            "can_resume_auth": False,
            "resume_action": continuation_action,
            "kind": kind,
            "reason": verification_continuation.get("reason") or owner_handoff.get("reason") or "waiting for owner verification input",
            "source": "verification_continuation",
            "selectors": selectors,
            "attempt_status": attempt_status,
            "attempt_strategy": attempt_strategy,
        }

    return {
        "status": "still_blocked",
        "can_resume_auth": False,
        "resume_action": continuation_action,
        "kind": kind,
        "reason": verification_continuation.get("reason") or owner_handoff.get("reason") or "verification remains blocked after owner handoff",
        "source": "verification_continuation",
        "selectors": selectors,
        "attempt_status": attempt_status,
        "attempt_strategy": attempt_strategy,
    }
