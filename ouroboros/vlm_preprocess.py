"""
VLM image pre-processing — automatic image-to-text via Codex VLM.

All images (from owner Telegram messages or browser screenshots) are routed
through this module BEFORE entering the main LLM context. This ensures:
- Images always go through Codex VLM (free, vision-capable) regardless of
  the active main model (Copilot, OpenRouter, etc.)
- The main LLM receives a text description, not raw image bytes
- No multipart image_url content is sent to non-vision models

Architecture:
  Owner photo → vlm_preprocess.describe_image() → text → main LLM
  Browser screenshot → vlm_preprocess.describe_screenshot() → text → main LLM
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# Always route through Codex VLM — free and vision-capable
_VLM_MODEL = "codex/gpt-5.4-mini"


def _get_vlm_model() -> str:
    """Get VLM model for image preprocessing. Always Codex unless overridden."""
    return os.environ.get("OUROBOROS_VLM_MODEL", _VLM_MODEL)


def describe_image(
    image_b64: str,
    image_mime: str = "image/jpeg",
    caption: str = "",
    max_tokens: int = 1024,
) -> Optional[str]:
    """
    Convert an image to text description via Codex VLM.

    Used for owner photos from Telegram. Returns a text description
    that replaces the raw image in LLM context.

    Returns None on failure (caller should fall back to caption or placeholder).
    """
    if not image_b64:
        return None

    prompt_parts = []
    if caption:
        prompt_parts.append(f'The user sent this image with caption: "{caption}"')
    else:
        prompt_parts.append("The user sent this image.")

    prompt_parts.append(
        "Describe what you see in detail. Include all visible text, numbers, "
        "UI elements, diagrams, code, errors, or any other important information. "
        "If this is a screenshot, describe the interface state. "
        "If this contains text/code, transcribe it accurately. "
        "Be thorough — this description replaces the image for the next processing step."
    )
    prompt = " ".join(prompt_parts)

    try:
        from ouroboros.llm import LLMClient
        client = LLMClient()
        text, usage = client.vision_query(
            prompt=prompt,
            images=[{"base64": image_b64, "mime": image_mime}],
            model=_get_vlm_model(),
            max_tokens=max_tokens,
            reasoning_effort="low",
        )
        if text and text.strip():
            log.info(
                "VLM preprocess: image described (%d chars), model=%s, "
                "prompt_tokens=%s, completion_tokens=%s",
                len(text), _get_vlm_model(),
                usage.get("prompt_tokens", "?"),
                usage.get("completion_tokens", "?"),
            )
            return text.strip()
        log.warning("VLM preprocess: empty response from model")
        return None
    except Exception as e:
        log.warning("VLM preprocess failed: %s", e, exc_info=True)
        return None


def describe_screenshot(
    screenshot_b64: str,
    context_hint: str = "",
    max_tokens: int = 1024,
) -> Optional[str]:
    """
    Convert a browser screenshot to text description via Codex VLM.

    Used automatically when browser takes a screenshot. Returns text
    description that is appended to the screenshot tool result.

    Returns None on failure (caller should return just the base64 ref).
    """
    if not screenshot_b64:
        return None

    prompt_parts = ["Describe this browser screenshot in detail."]
    if context_hint:
        prompt_parts.append(f"Context: {context_hint}")
    prompt_parts.append(
        "Include: page title, URL if visible, all visible text content, "
        "UI elements (buttons, forms, menus), any errors or warnings, "
        "layout structure. If there are tables or lists, describe their content. "
        "Transcribe any visible code or error messages accurately."
    )
    prompt = " ".join(prompt_parts)

    try:
        from ouroboros.llm import LLMClient
        client = LLMClient()
        text, usage = client.vision_query(
            prompt=prompt,
            images=[{"base64": screenshot_b64, "mime": "image/png"}],
            model=_get_vlm_model(),
            max_tokens=max_tokens,
            reasoning_effort="low",
        )
        if text and text.strip():
            log.info(
                "VLM screenshot: described (%d chars), model=%s",
                len(text), _get_vlm_model(),
            )
            return text.strip()
        log.warning("VLM screenshot: empty response from model")
        return None
    except Exception as e:
        log.warning("VLM screenshot preprocess failed: %s", e, exc_info=True)
        return None
