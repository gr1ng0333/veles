#!/usr/bin/env python3
"""Manual live probe for Copilot 500-recovery behavior on a single interaction_id.

This is intentionally NOT a pytest test: it performs real paid network calls.
Use it to reproduce a long owner-like task under one Copilot thread and verify
whether the same interaction can resume after a 500 + ~75s wait.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ouroboros.copilot_proxy import get_session_stats
from ouroboros.llm import LLMClient
from ouroboros.utils import estimate_tokens, utc_now_iso

DEFAULT_WAIT_AFTER_500 = 75
DEFAULT_ROUNDS = 40
DEFAULT_TARGET_PROMPT_TOKENS = 40000
DEFAULT_MAX_TOKENS = 96
DEFAULT_OUT_DIR = Path("/opt/veles-data/logs")
PROBE_TOOL_NAME = "probe_echo"


FILLER_PARAGRAPH = (
    "This is controlled filler for a Copilot recovery probe. "
    "It exists only to keep prompt pressure high and stable across rounds. "
    "Do not summarize it. Do not analyze it. Treat it as background payload.\n"
    "The probe goal is operational, not semantic: same interaction_id, many rounds, short replies.\n"
)


def build_large_payload(*, model: str, task_id: str, interaction_id: str, round_idx: int, target_tokens: int) -> str:
    header = (
        f"[probe_task_id={task_id}]\n"
        f"[probe_interaction_id={interaction_id}]\n"
        f"[probe_model={model}]\n"
        f"[probe_round={round_idx}]\n"
        "You are inside a transport stress probe. "
        "Reply with exactly one short line in the format ACK <round> <hex>. "
        "No markdown. No explanations. No extra words.\n\n"
    )
    body_parts: List[str] = [header]
    i = 0
    while estimate_tokens("".join(body_parts)) < target_tokens:
        body_parts.append(f"[{i:04d}] {FILLER_PARAGRAPH}")
        i += 1
    body_parts.append(f"\nFinal instruction: answer exactly ACK {round_idx} {task_id[:8]}\n")
    return "".join(body_parts)


def build_messages(
    *,
    model: str,
    task_id: str,
    interaction_id: str,
    target_tokens: int,
) -> List[Dict[str, Any]]:
    owner_message = build_large_payload(
        model=model,
        task_id=task_id,
        interaction_id=interaction_id,
        round_idx=1,
        target_tokens=target_tokens,
    )
    control = (
        "You are inside a synthetic tool loop that imitates a long owner task under one Copilot interaction. "
        f"Task id: {task_id}. On every assistant turn, call the tool `{PROBE_TOOL_NAME}` exactly once. "
        "Do not answer with plain text. Do not stop on your own. "
        "Read the latest tool result, then call the tool again for the next probe round."
    )
    return [
        {"role": "system", "content": control},
        {"role": "user", "content": owner_message},
    ]


def probe_tool_schema() -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": PROBE_TOOL_NAME,
                "description": "Local probe tool for synthetic Copilot loop testing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "round": {"type": "integer"},
                        "task_id": {"type": "string"},
                    },
                    "required": ["round", "task_id"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def append_tool_exchange(messages: List[Dict[str, Any]], assistant_message: Dict[str, Any], *, round_idx: int, task_id: str) -> str:
    tool_calls = assistant_message.get("tool_calls") or []
    if not tool_calls:
        raise RuntimeError(f"Assistant returned no tool_calls on round {round_idx}")
    call = tool_calls[0]
    function = call.get("function") or {}
    name = function.get("name")
    if name != PROBE_TOOL_NAME:
        raise RuntimeError(f"Unexpected tool name on round {round_idx}: {name!r}")
    messages.append(assistant_message)
    tool_call_id = call.get("id") or f"probe-call-{round_idx}"
    tool_payload = {
        "ok": True,
        "round": round_idx,
        "task_id": task_id,
        "note": "Synthetic local tool result for Copilot recovery probe.",
        "next": f"Continue same interaction and call {PROBE_TOOL_NAME} again on the next turn.",
    }
    messages.append(
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": PROBE_TOOL_NAME,
            "content": json.dumps(tool_payload, ensure_ascii=False),
        }
    )
    return json.dumps(tool_payload, ensure_ascii=False)


def classify_error(exc: BaseException) -> Dict[str, Any]:
    text = f"{type(exc).__name__}: {exc}"
    lower = text.lower()
    return {
        "error_type": type(exc).__name__,
        "error_text": text,
        "is_http_500": ("500" in lower and ("http" in lower or "internal server error" in lower))
        or "internal server error" in lower,
        "is_http_400": "400" in lower and "bad request" in lower,
        "is_401_403": "401" in lower or "403" in lower,
        "is_429": "429" in lower or "rate limit" in lower,
    }


def write_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def safe_session_stats(interaction_id: str) -> Dict[str, Any]:
    stats = get_session_stats(interaction_id) or {}
    return {
        "rounds": int(stats.get("rounds", 0) or 0),
        "total_prompt_tokens": int(stats.get("total_prompt_tokens", 0) or 0),
        "total_completion_tokens": int(stats.get("total_completion_tokens", 0) or 0),
        "premium_requests": int(stats.get("premium_requests", 0) or 0),
        "started": stats.get("started"),
        "last_activity": stats.get("last_activity"),
    }


def run_probe(
    *,
    model: str,
    rounds: int,
    target_prompt_tokens: int,
    wait_after_500: int,
    max_tokens: int,
    output_path: Path,
) -> Dict[str, Any]:
    llm = LLMClient()
    task_id = uuid.uuid4().hex[:8]
    interaction_id = str(uuid.uuid4())
    first_500_round: Optional[int] = None
    resumed_after_wait = False
    post_wait_success_round: Optional[int] = None
    wait_started_at: Optional[float] = None

    header = {
        "ts": utc_now_iso(),
        "event": "probe_start",
        "task_id": task_id,
        "interaction_id": interaction_id,
        "model": model,
        "rounds": rounds,
        "target_prompt_tokens": target_prompt_tokens,
        "wait_after_500": wait_after_500,
        "max_tokens": max_tokens,
    }
    write_jsonl(output_path, header)
    print(json.dumps(header, ensure_ascii=False), flush=True)

    messages = build_messages(
        model=model,
        task_id=task_id,
        interaction_id=interaction_id,
        target_tokens=target_prompt_tokens,
    )

    for round_idx in range(1, rounds + 1):
        started = time.time()
        row: Dict[str, Any] = {
            "ts": utc_now_iso(),
            "event": "round_result",
            "task_id": task_id,
            "interaction_id": interaction_id,
            "model": model,
            "round": round_idx,
            "force_user_initiator": round_idx == 1,
            "expected_initiator": "user" if round_idx == 1 else "agent",
            "estimated_prompt_tokens_local": estimate_tokens(json.dumps(messages, ensure_ascii=False)),
        }
        try:
            message, usage = llm.chat(
                messages,
                model=model,
                tools=probe_tool_schema(),
                reasoning_effort="high",
                max_tokens=max_tokens,
                tool_choice="auto",
                interaction_id=interaction_id,
                force_user_initiator=(round_idx == 1),
            )
            assistant_content = message.get("content") if isinstance(message, dict) else str(message)
            tool_result_payload = append_tool_exchange(messages, message, round_idx=round_idx, task_id=task_id)
            row.update(
                {
                    "status": "ok",
                    "duration_sec": round(time.time() - started, 3),
                    "usage": usage,
                    "assistant_content": assistant_content,
                    "tool_calls": message.get("tool_calls") if isinstance(message, dict) else None,
                    "tool_result_payload": tool_result_payload,
                    "session_stats": safe_session_stats(interaction_id),
                }
            )
            if wait_started_at is not None and post_wait_success_round is None:
                resumed_after_wait = True
                post_wait_success_round = round_idx
                row["post_wait_recovery_success"] = True
        except Exception as exc:  # real live probe, keep going
            error_info = classify_error(exc)
            row.update(
                {
                    "status": "error",
                    "duration_sec": round(time.time() - started, 3),
                    "session_stats": safe_session_stats(interaction_id),
                    **error_info,
                    "traceback_tail": traceback.format_exc(limit=3),
                }
            )
            if error_info["is_http_500"] and first_500_round is None:
                first_500_round = round_idx
                row["first_500_round"] = round_idx
                write_jsonl(output_path, row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
                wait_started_at = time.time()
                time.sleep(wait_after_500)
                continue
        write_jsonl(output_path, row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    summary = {
        "ts": utc_now_iso(),
        "event": "probe_summary",
        "task_id": task_id,
        "interaction_id": interaction_id,
        "model": model,
        "rounds_requested": rounds,
        "first_500_round": first_500_round,
        "wait_after_500": wait_after_500,
        "wait_applied": wait_started_at is not None,
        "resumed_after_wait": resumed_after_wait,
        "post_wait_success_round": post_wait_success_round,
        "final_session_stats": safe_session_stats(interaction_id),
        "output_path": str(output_path),
    }
    write_jsonl(output_path, summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Live Copilot 500 recovery probe")
    parser.add_argument("--model", required=True, help="e.g. copilot/claude-sonnet-4.6")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--target-prompt-tokens", type=int, default=DEFAULT_TARGET_PROMPT_TOKENS)
    parser.add_argument("--wait-after-500", type=int, default=DEFAULT_WAIT_AFTER_500)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--output", default="", help="Optional explicit JSONL path")
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    slug = args.model.replace("/", "_").replace(".", "-")
    output_path = Path(args.output) if args.output else (DEFAULT_OUT_DIR / f"copilot-recovery-{slug}-{stamp}.jsonl")

    summary = run_probe(
        model=args.model,
        rounds=args.rounds,
        target_prompt_tokens=args.target_prompt_tokens,
        wait_after_500=args.wait_after_500,
        max_tokens=args.max_tokens,
        output_path=output_path,
    )
    return 0 if summary.get("resumed_after_wait") or summary.get("first_500_round") is None else 2


if __name__ == "__main__":
    raise SystemExit(main())
