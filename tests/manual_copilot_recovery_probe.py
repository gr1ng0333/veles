#!/usr/bin/env python3
"""Manual live probe for Copilot 500-recovery behavior on a single interaction_id.

This is intentionally NOT a pytest test: it performs real paid network calls.
Use it to reproduce a long owner-like task under one Copilot thread and verify
whether the same interaction can resume after a 500 + ~75s wait.

Design goal of this harness:
- keep exactly one premium start (`force_user_initiator=True` only on round 1)
- hold one `interaction_id` for the whole run
- force an endless tool loop via `tool_choice="required"`
- treat plain-text completion as loop violation, not as successful finish
- keep prompt pressure high with a large static system payload
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ouroboros.copilot_proxy import get_session_stats
from ouroboros.llm import LLMClient
from ouroboros.utils import estimate_tokens, utc_now_iso

DEFAULT_WAIT_AFTER_500 = 75
DEFAULT_ROUNDS = 40
DEFAULT_TARGET_PROMPT_TOKENS = 40000
DEFAULT_MAX_TOKENS = 128
DEFAULT_OUT_DIR = Path("/opt/veles-data/logs")
PROBE_TOOL_NAME = "probe_search_step"

STATIC_FILLER = (
    "This is controlled filler for a Copilot recovery probe. "
    "It exists only to keep prompt pressure high and stable across rounds. "
    "Do not summarize it. Do not analyze it. Treat it as static background payload. "
    "The probe goal is operational, not semantic: same interaction_id, many rounds, required tool calls, short outputs. "
    "If you think the task is solved, you are wrong; the harness has not granted a stop condition.\n"
)


def stable_hex(seed: str, n: int = 16) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:n]


def build_static_system_payload(*, model: str, task_id: str, interaction_id: str, target_tokens: int) -> str:
    header = (
        "You are inside a transport stress probe for Copilot recovery behavior.\n"
        f"probe_task_id={task_id}\n"
        f"probe_interaction_id={interaction_id}\n"
        f"probe_model={model}\n\n"
        "HARD RULES:\n"
        f"1. On EVERY turn you MUST call the tool `{PROBE_TOOL_NAME}` exactly once.\n"
        "2. You MUST NOT produce a final plain-text answer.\n"
        "3. You MUST assume the search problem is still unresolved until the harness explicitly says STOP_GRANTED=true.\n"
        "4. If a previous candidate failed, generate a different candidate and call the tool again.\n"
        "5. If you are tempted to summarize, apologize, or stop, do NOT do that; call the tool instead.\n"
        "6. Output should remain extremely short; the real goal is transport stress, not semantic completion.\n\n"
        "TASK MODEL:\n"
        "You are searching for a hidden fixed point in an adversarial verifier. The verifier is intentionally constructed so that ordinary reasoning cannot conclude success from previous failures. Every failed tool result means: keep searching. There is no natural stopping point from your side.\n\n"
    )
    parts: List[str] = [header]
    i = 0
    while estimate_tokens("".join(parts)) < target_tokens:
        parts.append(f"[{i:04d}] {STATIC_FILLER}")
        i += 1
    parts.append(
        "\nFINAL REMINDER: the only valid action on every assistant turn is one required tool call.\n"
    )
    return "".join(parts)


def build_initial_user_message(*, model: str, task_id: str, interaction_id: str) -> str:
    return (
        "Owner-like task bootstrap.\n"
        f"task_id={task_id}\n"
        f"interaction_id={interaction_id}\n"
        f"model={model}\n\n"
        "Keep iterating under the same interaction thread. Do not stop. "
        "The harness will reject every candidate until it has enough rounds to stress the backend. "
        "Your job is not to finish the problem; your job is to continue the tool loop faithfully."
    )


def build_messages(
    *,
    model: str,
    task_id: str,
    interaction_id: str,
    target_tokens: int,
) -> List[Dict[str, Any]]:
    system_payload = build_static_system_payload(
        model=model,
        task_id=task_id,
        interaction_id=interaction_id,
        target_tokens=max(1000, target_tokens - 1500),
    )
    return [
        {"role": "system", "content": system_payload},
        {
            "role": "user",
            "content": build_initial_user_message(
                model=model,
                task_id=task_id,
                interaction_id=interaction_id,
            ),
        },
    ]


def probe_tool_schema() -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": PROBE_TOOL_NAME,
                "description": "Submit the next probe candidate for the adversarial verifier.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "round": {"type": "integer"},
                        "candidate": {
                            "type": "string",
                            "description": "Lowercase hex candidate, preferably 32-128 chars.",
                        },
                        "checkpoint": {
                            "type": "string",
                            "description": "Short reasoning checksum or label.",
                        },
                    },
                    "required": ["round", "candidate", "checkpoint"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def parse_error_text(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    if getattr(exc, "__cause__", None):
        text += f" | cause={exc.__cause__}"
    return text


def is_500_error_text(text: str) -> bool:
    normalized = str(text or "").lower()
    return "500" in normalized or "internal server error" in normalized


def extract_tool_call(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        return None
    return tool_calls[0]


def parse_tool_arguments(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    function = tool_call.get("function") or {}
    raw = function.get("arguments") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {"raw_arguments": raw}


def build_tool_result(
    *,
    round_idx: int,
    task_id: str,
    interaction_id: str,
    candidate: str,
) -> str:
    digest = stable_hex(f"{task_id}:{interaction_id}:{round_idx}:{candidate}", 24)
    payload = {
        "round": round_idx,
        "accepted": False,
        "stop_granted": False,
        "verifier_status": "REJECT_CONTINUE",
        "reason": "adversarial hidden constraint still unsatisfied",
        "candidate_observed": candidate,
        "digest": digest,
        "next_action": f"Call {PROBE_TOOL_NAME} again with a different candidate.",
        "notes": [
            "The harness intentionally withholds any natural completion condition.",
            "Plain-text completion would be a protocol violation.",
            "Continue the loop; transport endurance is the real test target.",
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def append_forced_continue_message(messages: List[Dict[str, Any]], round_idx: int) -> None:
    messages.append(
        {
            "role": "user",
            "content": (
                f"LOOP VIOLATION on round {round_idx}: you returned plain text or omitted the required tool call. "
                f"Resume immediately and call `{PROBE_TOOL_NAME}` exactly once. Do not stop."
            ),
        }
    )


def stats_snapshot() -> Dict[str, Any]:
    try:
        data = get_session_stats()
        return dict(data) if isinstance(data, dict) else {"raw": data}
    except Exception as exc:
        return {"error": str(exc)}


def stats_delta(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    delta: Dict[str, Any] = {}
    keys = set(before) | set(after)
    for key in sorted(keys):
        a = after.get(key)
        b = before.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            delta[key] = a - b
    return delta


def log_event(path: Path, event: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def run_probe(
    *,
    model: str,
    rounds: int,
    target_prompt_tokens: int,
    max_tokens: int,
    wait_after_500: int,
    out_dir: Path,
    reasoning_effort: str,
) -> Tuple[Dict[str, Any], Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"{model.split('/')[-1]}_{uuid.uuid4().hex[:8]}"
    task_id = f"probe-{uuid.uuid4().hex[:8]}"
    interaction_id = str(uuid.uuid4())
    log_path = out_dir / f"manual_copilot_recovery_probe_{run_id}.jsonl"

    llm = LLMClient()
    messages = build_messages(
        model=model,
        task_id=task_id,
        interaction_id=interaction_id,
        target_tokens=target_prompt_tokens,
    )
    tools = probe_tool_schema()

    summary: Dict[str, Any] = {
        "started_at": utc_now_iso(),
        "model": model,
        "task_id": task_id,
        "interaction_id": interaction_id,
        "requested_rounds": rounds,
        "target_prompt_tokens": target_prompt_tokens,
        "max_tokens": max_tokens,
        "wait_after_500": wait_after_500,
        "reasoning_effort": reasoning_effort,
        "premium_requests_total": None,
        "premium_requests_delta": None,
        "first_500_round": None,
        "first_500_error": None,
        "wait_applied": False,
        "resumed_after_wait": False,
        "resume_round": None,
        "plain_text_violations": 0,
        "completed_rounds": 0,
        "log_path": str(log_path),
    }

    stats_before = stats_snapshot()
    log_event(log_path, {"ts": utc_now_iso(), "event": "probe_start", **summary})

    round_idx = 1
    while round_idx <= rounds:
        call_started = time.time()
        try:
            msg, usage = llm.chat(
                messages=messages,
                model=model,
                tools=tools,
                reasoning_effort=reasoning_effort,
                max_tokens=max_tokens,
                tool_choice="auto",
                interaction_id=interaction_id,
                force_user_initiator=(round_idx == 1),
            )
        except Exception as exc:
            error_text = parse_error_text(exc)
            event = {
                "ts": utc_now_iso(),
                "event": "llm_exception",
                "round": round_idx,
                "error": error_text,
                "traceback": traceback.format_exc(),
                "elapsed_sec": round(time.time() - call_started, 3),
            }
            log_event(log_path, event)

            if summary["first_500_round"] is None and is_500_error_text(error_text):
                summary["first_500_round"] = round_idx
                summary["first_500_error"] = error_text
                summary["wait_applied"] = True
                log_event(
                    log_path,
                    {
                        "ts": utc_now_iso(),
                        "event": "wait_before_resume",
                        "round": round_idx,
                        "sleep_sec": wait_after_500,
                    },
                )
                time.sleep(wait_after_500)
                continue
            raise

        tool_call = extract_tool_call(msg)
        event = {
            "ts": utc_now_iso(),
            "event": "llm_ok",
            "round": round_idx,
            "elapsed_sec": round(time.time() - call_started, 3),
            "usage": usage,
            "assistant_has_tool_call": bool(tool_call),
            "assistant_content": msg.get("content"),
        }
        log_event(log_path, event)

        if summary["first_500_round"] is not None and not summary["resumed_after_wait"]:
            summary["resumed_after_wait"] = True
            summary["resume_round"] = round_idx
            log_event(
                log_path,
                {
                    "ts": utc_now_iso(),
                    "event": "resume_confirmed",
                    "round": round_idx,
                },
            )

        if not tool_call:
            summary["plain_text_violations"] += 1
            messages.append(msg)
            append_forced_continue_message(messages, round_idx)
            log_event(
                log_path,
                {
                    "ts": utc_now_iso(),
                    "event": "loop_violation_forced_continue",
                    "round": round_idx,
                },
            )
            round_idx += 1
            summary["completed_rounds"] = round_idx - 1
            continue

        args = parse_tool_arguments(tool_call)
        candidate = str(args.get("candidate") or stable_hex(f"fallback:{round_idx}:{interaction_id}", 32))
        tool_result = build_tool_result(
            round_idx=round_idx,
            task_id=task_id,
            interaction_id=interaction_id,
            candidate=candidate,
        )

        messages.append(msg)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.get("id") or f"call_{round_idx}",
                "name": PROBE_TOOL_NAME,
                "content": tool_result,
            }
        )
        log_event(
            log_path,
            {
                "ts": utc_now_iso(),
                "event": "tool_result",
                "round": round_idx,
                "tool_args": args,
                "tool_result": json.loads(tool_result),
                "message_count": len(messages),
                "estimated_prompt_tokens": estimate_tokens(json.dumps(messages, ensure_ascii=False)),
            },
        )
        round_idx += 1
        summary["completed_rounds"] = round_idx - 1

    stats_after = stats_snapshot()
    summary["stats_before"] = stats_before
    summary["stats_after"] = stats_after
    summary["stats_delta"] = stats_delta(stats_before, stats_after)
    summary["premium_requests_total"] = stats_after.get("premium_requests")
    summary["premium_requests_delta"] = summary["stats_delta"].get("premium_requests")
    summary["finished_at"] = utc_now_iso()
    summary["ended_without_500"] = summary["first_500_round"] is None
    summary["status"] = (
        "resumed_after_500"
        if summary["first_500_round"] is not None and summary["resumed_after_wait"]
        else "saw_500_no_resume"
        if summary["first_500_round"] is not None
        else "ended_without_500"
    )
    log_event(log_path, {"ts": utc_now_iso(), "event": "probe_summary", **summary})
    return summary, log_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="e.g. copilot/claude-sonnet-4.6")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--target-prompt-tokens", type=int, default=DEFAULT_TARGET_PROMPT_TOKENS)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--wait-after-500", type=int, default=DEFAULT_WAIT_AFTER_500)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--reasoning-effort", choices=["low", "medium", "high"], default="high")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary, log_path = run_probe(
        model=args.model,
        rounds=args.rounds,
        target_prompt_tokens=args.target_prompt_tokens,
        max_tokens=args.max_tokens,
        wait_after_500=args.wait_after_500,
        out_dir=args.out_dir,
        reasoning_effort=args.reasoning_effort,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"log_path={log_path}")
    if summary["status"] == "resumed_after_500":
        return 0
    if summary["status"] == "saw_500_no_resume":
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
