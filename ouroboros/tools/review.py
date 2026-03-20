"""Multi-model review tool — sends code/text to multiple LLMs for consensus review.

Models are NOT hardcoded — the LLM chooses which models to use based on
prompt guidance. Budget is tracked via llm_usage events.

Routes through LLMClient — supports Codex OAuth, Copilot, and OpenRouter transports.
Any model accepted by LLMClient.chat() works: codex/*, copilot/*, or plain OpenRouter ids.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from ouroboros.utils import utc_now_iso
from ouroboros.tools.registry import ToolEntry, ToolContext


log = logging.getLogger(__name__)

# Maximum number of models allowed per review
MAX_MODELS = 10
# Concurrency limit for parallel model queries
CONCURRENCY_LIMIT = 5


def get_tools():
    """Return list of ToolEntry for registry."""
    return [
        ToolEntry(
            name="multi_model_review",
            schema={
                "name": "multi_model_review",
                "description": (
                    "Send code or text to multiple LLM models for review/consensus. "
                    "Each model reviews independently. Returns structured verdicts. "
                    "Choose diverse models yourself. Budget is tracked automatically. "
                    "Models can use any transport: codex/ (Codex OAuth), copilot/ (GitHub Copilot), "
                    "or plain OpenRouter identifiers."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "The code or text to review",
                        },
                        "prompt": {
                            "type": "string",
                            "description": (
                                "Review instructions — what to check for. "
                                "Fully specified by the LLM at call time."
                            ),
                        },
                        "models": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Model identifiers with optional transport prefix "
                                "(e.g. 'codex/gpt-5.4', 'copilot/claude-sonnet-4.6', "
                                "'anthropic/claude-haiku-4.5'). "
                                "Use 3 diverse models for good coverage."
                            ),
                        },
                    },
                    "required": ["content", "prompt", "models"],
                },
            },
            handler=_handle_multi_model_review,
        )
    ]


def _handle_multi_model_review(ctx: ToolContext, content: str = "", prompt: str = "", models: list = None) -> str:
    """Sync handler for multi-model review. Routes through LLMClient."""
    if models is None:
        models = []
    try:
        result = _multi_model_review(content, prompt, models, ctx)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        log.error("Multi-model review failed: %s", e, exc_info=True)
        return json.dumps({"error": f"Review failed: {e}"}, ensure_ascii=False)


def _query_model(llm_client, model: str, messages: list) -> dict:
    """Query a single model via LLMClient.chat(). Returns structured review_result dict."""
    try:
        msg, usage = llm_client.chat(
            messages=messages,
            model=model,
            tools=None,
            reasoning_effort="medium",
            max_tokens=4096,
        )
        text = msg.get("content") or ""

        # Robust verdict parsing: check first 3 lines for PASS/FAIL (case-insensitive)
        verdict = "UNKNOWN"
        for line in text.split("\n")[:3]:
            line_upper = line.upper()
            if "PASS" in line_upper:
                verdict = "PASS"
                break
            elif "FAIL" in line_upper:
                verdict = "FAIL"
                break

        return {
            "model": model,
            "verdict": verdict,
            "text": text,
            "tokens_in": int(usage.get("prompt_tokens") or 0),
            "tokens_out": int(usage.get("completion_tokens") or 0),
            "cost_estimate": float(usage.get("cost") or usage.get("shadow_cost") or 0.0),
        }
    except Exception as e:
        error_msg = str(e)[:200]
        return {
            "model": model,
            "verdict": "ERROR",
            "text": f"Error: {error_msg}",
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_estimate": 0.0,
        }


def _multi_model_review(content: str, prompt: str, models: list, ctx: ToolContext) -> dict:
    """Sync orchestration: validate → query models in parallel via LLMClient → emit → return."""
    # Validation
    if not content:
        return {"error": "content is required"}
    if not prompt:
        return {"error": "prompt is required"}
    if not models:
        return {"error": "models list is required (e.g. ['codex/gpt-5.4', 'copilot/claude-sonnet-4.6'])"}
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        return {"error": "models must be a list of strings"}
    if len(models) > MAX_MODELS:
        return {"error": f"Too many models requested ({len(models)}). Maximum is {MAX_MODELS}."}

    from ouroboros.llm import LLMClient
    llm_client = LLMClient()

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": content},
    ]

    # Query all models with bounded concurrency via ThreadPoolExecutor
    max_workers = min(len(models), CONCURRENCY_LIMIT)
    results_by_model = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_model = {
            executor.submit(_query_model, llm_client, model, messages): model
            for model in models
        }
        for future in as_completed(future_to_model):
            model = future_to_model[future]
            try:
                results_by_model[model] = future.result()
            except Exception as exc:
                results_by_model[model] = {
                    "model": model,
                    "verdict": "ERROR",
                    "text": f"Error: {str(exc)[:200]}",
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "cost_estimate": 0.0,
                }

    # Emit usage events and collect results in original model order
    review_results = []
    for model in models:
        review_result = results_by_model[model]
        _emit_usage_event(review_result, ctx)
        review_results.append(review_result)

    return {
        "model_count": len(models),
        "results": review_results,
    }


def _emit_usage_event(review_result: dict, ctx: ToolContext) -> None:
    """Emit llm_usage event for budget tracking (for ALL cases, including errors)."""
    if ctx is None:
        return

    usage_event = {
        "type": "llm_usage",
        "ts": utc_now_iso(),
        "task_id": ctx.task_id if ctx.task_id else "",
        "usage": {
            "prompt_tokens": review_result["tokens_in"],
            "completion_tokens": review_result["tokens_out"],
            "cost": review_result["cost_estimate"],
        },
        "category": "review",
    }

    if ctx.event_queue is not None:
        try:
            ctx.event_queue.put_nowait(usage_event)
        except Exception:
            # Fallback to pending_events if queue fails
            if hasattr(ctx, "pending_events"):
                ctx.pending_events.append(usage_event)
    elif hasattr(ctx, "pending_events"):
        # No event_queue — use pending_events
        ctx.pending_events.append(usage_event)
