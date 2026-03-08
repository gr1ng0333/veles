"""
Local captcha solver helpers.

Pipeline: generate several preprocessing variants -> run ddddocr first ->
optionally try tesseract -> return the strongest alphanumeric candidate.
Models are lazily initialised — no heavy imports at module load time.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Dict, List, Tuple

log = logging.getLogger(__name__)

_ddddocr_instance = None


# ---------------------------------------------------------------------------
# Lazy singletons
# ---------------------------------------------------------------------------

def _get_ddddocr():
    global _ddddocr_instance
    if _ddddocr_instance is None:
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
# Normalization and scoring
# ---------------------------------------------------------------------------

def _clean_text(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", raw or "").strip()


def _score_candidate(text: str, *, backend: str, variant: str) -> float:
    cleaned = _clean_text(text)
    if not cleaned:
        return 0.0

    score = 0.2
    length = len(cleaned)
    if 4 <= length <= 8:
        score += 0.45
    elif length == 3:
        score += 0.25
    elif 9 <= length <= 10:
        score += 0.1
    else:
        score += 0.02

    if cleaned.isalnum():
        score += 0.1
    if any(ch.isdigit() for ch in cleaned):
        score += 0.08
    if any(ch.isalpha() for ch in cleaned):
        score += 0.08
    if any(ch.islower() for ch in cleaned) and any(ch.isupper() for ch in cleaned):
        score += 0.03

    if backend == "ddddocr":
        score += 0.05
    if variant in {"threshold_140", "autocontrast_threshold_160", "upscale_threshold_160"}:
        score += 0.03

    # Penalties for suspicious results
    if len(set(cleaned)) == 1 and length > 2:
        score -= 0.4
    if re.search(r'(.)\1{2,}', cleaned):
        score -= 0.3
    if length == 1:
        score -= 0.3
    if ' ' in text:
        score -= 0.2

    return max(min(score, 0.99), 0.0)


# ---------------------------------------------------------------------------
# Image preprocessing
# ---------------------------------------------------------------------------

def _image_to_png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _build_preprocessed_variants(image_bytes: bytes) -> List[Tuple[str, bytes]]:
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    base = Image.open(io.BytesIO(image_bytes)).convert("L")
    variants: List[Tuple[str, bytes]] = []

    def add_variant(name: str, img) -> None:
        variants.append((name, _image_to_png_bytes(img)))

    contrast = ImageEnhance.Contrast(base).enhance(2.0)
    add_variant("grayscale", base)
    add_variant("contrast", contrast)
    add_variant(
        "threshold_140",
        contrast.point(lambda px: 255 if px > 140 else 0, "1").convert("L").filter(ImageFilter.MedianFilter(size=3)),
    )
    add_variant(
        "threshold_170",
        contrast.point(lambda px: 255 if px > 170 else 0, "1").convert("L").filter(ImageFilter.MedianFilter(size=3)),
    )
    auto = ImageOps.autocontrast(base)
    add_variant(
        "autocontrast_threshold_160",
        auto.point(lambda px: 255 if px > 160 else 0, "1").convert("L").filter(ImageFilter.MedianFilter(size=3)),
    )
    upscale = contrast.resize((base.width * 2, base.height * 2))
    add_variant(
        "upscale_threshold_160",
        upscale.point(lambda px: 255 if px > 160 else 0, "1").convert("L").filter(ImageFilter.MedianFilter(size=3)),
    )
    add_variant("inverted", ImageOps.invert(contrast))
    return variants


def preprocess_image(image_bytes: bytes) -> bytes:
    """Compatibility helper: return the default OCR-oriented variant as PNG bytes."""
    for name, data in _build_preprocessed_variants(image_bytes):
        if name == "threshold_140":
            return data
    return _build_preprocessed_variants(image_bytes)[0][1]


# ---------------------------------------------------------------------------
# OCR backends
# ---------------------------------------------------------------------------

def recognize_ddddocr(image_bytes: bytes) -> Tuple[str, float]:
    try:
        ocr = _get_ddddocr()
    except ImportError as exc:
        log.debug("ddddocr unavailable: %s", exc)
        return "", 0.0

    raw = ocr.classification(image_bytes)
    text = _clean_text(raw)
    return text, _score_candidate(text, backend="ddddocr", variant="single")


def recognize_tesseract(image_bytes: bytes) -> Tuple[str, float]:
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
        text = _clean_text(raw)
        return text, _score_candidate(text, backend="tesseract", variant="single") - 0.03
    except Exception as exc:
        log.debug("Tesseract fallback failed: %s", exc)
        return "", 0.0


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _best_backend_candidate(variants: List[Tuple[str, bytes]], backend: str) -> Dict[str, object]:
    best = {"text": "", "confidence": 0.0, "method": backend, "variant": ""}

    for variant_name, variant_bytes in variants:
        if backend == "ddddocr":
            text, _ = recognize_ddddocr(variant_bytes)
        else:
            text, _ = recognize_tesseract(variant_bytes)

        confidence = _score_candidate(text, backend=backend, variant=variant_name)
        if confidence > float(best["confidence"]):
            best = {
                "text": text,
                "confidence": float(confidence),
                "method": backend,
                "variant": variant_name,
            }

    return best


def solve_captcha_image(image_bytes: bytes) -> dict:
    """Try several preprocessing variants and return the strongest OCR candidate."""
    variants = _build_preprocessed_variants(image_bytes)
    dddd_best = _best_backend_candidate(variants, backend="ddddocr")
    tess_best = _best_backend_candidate(variants, backend="tesseract")

    best = dddd_best
    if float(tess_best["confidence"]) > float(dddd_best["confidence"]):
        best = tess_best

    # Cross-backend agreement bonus
    dddd_text = _clean_text(str(dddd_best.get("text", "")))
    tess_text = _clean_text(str(tess_best.get("text", "")))
    agreement_bonus = 0.0
    if dddd_text and tess_text and dddd_text == tess_text:
        agreement_bonus = 0.15
        best = dddd_best  # prefer ddddocr when they agree

    final_confidence = min(float(best["confidence"]) + agreement_bonus, 0.99)

    return {
        "text": str(best["text"]),
        "confidence": final_confidence,
        "method": str(best["method"]) + ("+agreement" if agreement_bonus else ""),
        "variant": str(best.get("variant") or ""),
        "attempts": len(variants),
    }
