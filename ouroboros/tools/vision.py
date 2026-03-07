"""
Vision Language Model (VLM) tools for Ouroboros.

Allows the agent to analyze screenshots and images using LLM vision capabilities.
Integrates with the existing browser screenshot workflow:
  browse_page(output='screenshot') → analyze_screenshot() → insight

Two tools:
  - analyze_screenshot: analyze the last browser screenshot using VLM
  - vlm_query: analyze any image (URL or base64) with a custom prompt
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_DEFAULT_VLM_MODEL = "anthropic/claude-sonnet-4.6"


def _get_vlm_model() -> str:
    """Get VLM model from env or use default."""
    return os.environ.get("OUROBOROS_MODEL", _DEFAULT_VLM_MODEL)


def _get_llm_client():
    """Lazy-import LLMClient to avoid circular imports."""
    from ouroboros.llm import LLMClient
    return LLMClient()


def _analyze_screenshot(ctx: ToolContext, prompt: str = "Describe what you see in this screenshot. Note any important UI elements, text, errors, or visual issues.", model: str = "") -> str:
    """
    Analyze the last browser screenshot using a Vision LLM.

    Requires a prior browse_page(output='screenshot') or browser_action(action='screenshot') call.
    """
    b64 = ctx.browser_state.last_screenshot_b64
    if not b64:
        return (
            "⚠️ No screenshot available. "
            "First call browse_page(output='screenshot') or browser_action(action='screenshot')."
        )

    vlm_model = model or _get_vlm_model()

    try:
        client = _get_llm_client()
        text, usage = client.vision_query(
            prompt=prompt,
            images=[{"base64": b64, "mime": "image/png"}],
            model=vlm_model,
            max_tokens=1024,
            reasoning_effort="low",
        )

        # Emit usage event if event_queue is available
        _emit_usage(ctx, usage, vlm_model)

        return text or "(no response from VLM)"
    except Exception as e:
        log.warning("analyze_screenshot failed: %s", e, exc_info=True)
        return f"⚠️ VLM analysis failed: {e}"


def _vlm_query(ctx: ToolContext, prompt: str, image_url: str = "", image_base64: str = "", image_mime: str = "image/png", model: str = "") -> str:
    """
    Analyze any image using a Vision LLM. Provide either image_url or image_base64.
    """
    if not image_url and not image_base64:
        return "⚠️ Provide either image_url or image_base64."

    images: List[Dict[str, Any]] = []
    if image_url:
        images.append({"url": image_url})
    else:
        images.append({"base64": image_base64, "mime": image_mime})

    vlm_model = model or _get_vlm_model()

    try:
        client = _get_llm_client()
        text, usage = client.vision_query(
            prompt=prompt,
            images=images,
            model=vlm_model,
            max_tokens=1024,
            reasoning_effort="low",
        )

        _emit_usage(ctx, usage, vlm_model)

        return text or "(no response from VLM)"
    except Exception as e:
        log.warning("vlm_query failed: %s", e, exc_info=True)
        return f"⚠️ VLM query failed: {e}"


def _emit_usage(ctx: ToolContext, usage: Dict[str, Any], model: str) -> None:
    """Emit LLM usage event for budget tracking."""
    if ctx.event_queue is None:
        return
    try:
        event = {
            "type": "llm_usage",
            "model": model,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "cached_tokens": usage.get("cached_tokens", 0),
            "cost": usage.get("cost", 0.0),
            "task_id": ctx.task_id,
            "task_type": ctx.current_task_type or "task",
        }
        ctx.event_queue.put_nowait(event)
    except Exception:
        log.debug("Failed to emit VLM usage event", exc_info=True)

_UNCERTAIN_MARKERS = (
    "uncertain",
    "unsure",
    "not sure",
    "can't read",
    "cannot read",
    "unreadable",
    "не уверен",
    "не могу",
    "нечита",
)


def _normalize_captcha_guess(raw_text: str, max_length: int = 8) -> Dict[str, Any]:
    raw = (raw_text or "").strip()
    if not raw:
        return {"status": "uncertain", "text": "", "reason": "empty_response", "raw_response": raw_text or ""}

    lowered = raw.lower()
    if any(marker in lowered for marker in _UNCERTAIN_MARKERS):
        return {"status": "uncertain", "text": "", "reason": "model_uncertain", "raw_response": raw}

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    candidate_source = lines[0] if lines else raw
    if ':' in candidate_source and len(candidate_source) > max_length + 3:
        candidate_source = candidate_source.split(':', 1)[-1].strip()

    tokens = re.findall(r"[A-Za-z0-9]+", candidate_source)
    if not tokens:
        return {"status": "uncertain", "text": "", "reason": "no_alnum_token", "raw_response": raw}

    candidate = max(tokens, key=len)
    if len(candidate) > max_length:
        return {
            "status": "uncertain",
            "text": "",
            "reason": "candidate_too_long",
            "raw_response": raw,
            "candidate": candidate,
        }

    return {
        "status": "ok",
        "text": candidate,
        "length": len(candidate),
        "raw_response": raw,
    }


def _solve_simple_captcha(
    ctx: ToolContext,
    prompt: str = "",
    image_base64: str = "",
    image_url: str = "",
    image_mime: str = "image/png",
    model: str = "",
    max_length: int = 8,
) -> str:
    """Vision-only MVP for simple text captchas. Returns JSON with status ok/uncertain."""
    actual_b64 = (image_base64 or "").strip()
    if not image_url and not actual_b64:
        actual_b64 = ctx.browser_state.last_screenshot_b64 or ""

    if not image_url and not actual_b64:
        return json.dumps({
            "status": "uncertain",
            "text": "",
            "reason": "no_image",
            "message": "Provide image_base64/image_url or capture a browser screenshot first.",
        }, ensure_ascii=False)

    images: List[Dict[str, Any]] = []
    if image_url:
        images.append({"url": image_url})
    else:
        images.append({"base64": actual_b64, "mime": image_mime or "image/png"})

    vlm_model = model or _get_vlm_model()
    captcha_prompt = prompt or (
        "Read the captcha text in this image. Return ONLY the captcha characters with no explanation. "
        "If the captcha is unreadable or ambiguous, return exactly UNCERTAIN."
    )

    try:
        client = _get_llm_client()
        text, usage = client.vision_query(
            prompt=captcha_prompt,
            images=images,
            model=vlm_model,
            max_tokens=32,
            reasoning_effort="low",
        )
        _emit_usage(ctx, usage, vlm_model)
        result = _normalize_captcha_guess(text or "", max_length=max_length)
        result["model"] = vlm_model
        result["vision_only"] = True
        result["source"] = "image_url" if image_url else "image_base64"
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        log.warning("solve_simple_captcha failed: %s", e, exc_info=True)
        return json.dumps({
            "status": "uncertain",
            "text": "",
            "reason": "tool_error",
            "error": str(e),
            "model": vlm_model,
            "vision_only": True,
        }, ensure_ascii=False)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="solve_simple_captcha",
            schema={
                "name": "solve_simple_captcha",
                "description": (
                    "Vision-only MVP for simple text captchas. "
                    "Uses the last browser screenshot by default, or a provided image_url/image_base64. "
                    "Returns JSON with status ok/uncertain and the normalized captcha text when confident."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string", "description": "Optional custom prompt for the vision model"},
                        "image_base64": {"type": "string", "description": "Optional base64 image; defaults to last browser screenshot"},
                        "image_url": {"type": "string", "description": "Optional public image URL"},
                        "image_mime": {"type": "string", "description": "MIME type for image_base64 (default image/png)"},
                        "model": {"type": "string", "description": "Vision model override"},
                        "max_length": {"type": "integer", "description": "Maximum accepted captcha length before returning uncertain"},
                    },
                    "required": [],
                },
            },
            handler=_solve_simple_captcha,
            timeout_sec=30,
        ),
        ToolEntry(
            name="analyze_screenshot",
            schema={
                "name": "analyze_screenshot",
                "description": (
                    "Analyze the last browser screenshot using a Vision LLM. "
                    "Must call browse_page(output='screenshot') or browser_action(action='screenshot') first. "
                    "Returns a text description and analysis of the screenshot. "
                    "Use this to verify UI, check for visual errors, or understand page layout."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "What to look for or analyze in the screenshot (default: general description)",
                        },
                        "model": {
                            "type": "string",
                            "description": "VLM model to use (default: current OUROBOROS_MODEL)",
                        },
                    },
                    "required": [],
                },
            },
            handler=_analyze_screenshot,
            timeout_sec=30,
        ),
        ToolEntry(
            name="vlm_query",
            schema={
                "name": "vlm_query",
                "description": (
                    "Analyze any image using a Vision LLM. "
                    "Provide either image_url (public URL) or image_base64 (base64-encoded PNG/JPEG). "
                    "Use for: analyzing charts, reading diagrams, understanding screenshots, checking UI."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "What to analyze or describe about the image",
                        },
                        "image_url": {
                            "type": "string",
                            "description": "Public URL of the image to analyze",
                        },
                        "image_base64": {
                            "type": "string",
                            "description": "Base64-encoded image data",
                        },
                        "image_mime": {
                            "type": "string",
                            "description": "MIME type for base64 image (default: image/png)",
                        },
                        "model": {
                            "type": "string",
                            "description": "VLM model to use (default: current OUROBOROS_MODEL)",
                        },
                    },
                    "required": ["prompt"],
                },
            },
            handler=_vlm_query,
            timeout_sec=30,
        ),
    ]
