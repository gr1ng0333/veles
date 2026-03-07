"""
Local captcha solver helpers.

Pipeline: preprocess image → ddddocr (primary) → tesseract (fallback).
Models are lazily initialised — no heavy imports at module load time.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy singletons
# ---------------------------------------------------------------------------
_ddddocr_instance = None


def _get_ddddocr():
    global _ddddocr_instance
    if _ddddocr_instance is None:
        # ddddocr >=1.6.0 ships a broken top-level __init__ on some Python
        # versions, so we try multiple import paths.
        DdddOcr = None
        for _import_fn in [
            lambda: __import__("ddddocr", fromlist=["DdddOcr"]).DdddOcr,
            lambda: __import__("ddddocr.compat.legacy", fromlist=["DdddOcr"]).DdddOcr,
            lambda: __import__("ddddocr.compat", fromlist=["DdddOcr"]).DdddOcr,
        ]:
            try:
                DdddOcr = _import_fn()
                break
            except (ImportError, AttributeError):
                continue
        if DdddOcr is None:
            raise ImportError("ddddocr is not installed or cannot be imported")
        _ddddocr_instance = DdddOcr(show_ad=False)
    return _ddddocr_instance


# ---------------------------------------------------------------------------
# Image preprocessing
# ---------------------------------------------------------------------------

def preprocess_image(image_bytes: bytes) -> bytes:
    """Grayscale → contrast boost → binarise → median denoise.

    Returns PNG bytes suitable for OCR.
    """
    from PIL import Image, ImageFilter, ImageEnhance

    img = Image.open(io.BytesIO(image_bytes)).convert("L")  # grayscale
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = img.point(lambda px: 255 if px > 140 else 0, "1")  # binarise
    img = img.convert("L").filter(ImageFilter.MedianFilter(size=3))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# ddddocr recogniser
# ---------------------------------------------------------------------------

def recognize_ddddocr(image_bytes: bytes) -> Tuple[str, float]:
    """Return (text, confidence) using ddddocr.

    Missing ddddocr is not fatal: the solver should fall through to
    other recognizers instead of raising at call sites.
    """
    try:
        ocr = _get_ddddocr()
    except ImportError as exc:
        log.debug("ddddocr unavailable: %s", exc)
        return "", 0.0

    text = ocr.classification(image_bytes)
    text = re.sub(r"[^A-Za-z0-9]", "", text)  # strip stray symbols

    if 4 <= len(text) <= 8 and text.isalnum():
        confidence = 0.9
    elif len(text) >= 3:
        confidence = 0.6
    else:
        confidence = 0.3
    return text, confidence


# ---------------------------------------------------------------------------
# Tesseract fallback
# ---------------------------------------------------------------------------

def recognize_tesseract(image_bytes: bytes) -> Tuple[str, float]:
    """Return (text, confidence) via pytesseract. May raise if not installed."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return "", 0.0

    try:
        img = Image.open(io.BytesIO(image_bytes))
        raw = pytesseract.image_to_string(
            img,
            config="--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
        )
        text = re.sub(r"[^A-Za-z0-9]", "", raw).strip()

        if 4 <= len(text) <= 8 and text.isalnum():
            confidence = 0.85
        elif len(text) >= 3:
            confidence = 0.55
        else:
            confidence = 0.25
        return text, confidence
    except Exception as exc:
        log.debug("Tesseract fallback failed: %s", exc)
        return "", 0.0


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def solve_captcha_image(image_bytes: bytes) -> dict:
    """Preprocess → ddddocr → optional tesseract fallback.

    Returns dict with keys: text, confidence, method. Never raises merely
    because an optional OCR backend is absent.
    """
    processed = preprocess_image(image_bytes)

    text, conf = recognize_ddddocr(processed)
    if conf >= 0.5:
        return {"text": text, "confidence": conf, "method": "ddddocr"}

    tess_text, tess_conf = recognize_tesseract(processed)
    if tess_conf > conf:
        return {"text": tess_text, "confidence": float(tess_conf), "method": "tesseract"}

    fallback_method = "ddddocr" if conf > 0 else "tesseract"
    return {"text": text, "confidence": float(conf), "method": fallback_method}
