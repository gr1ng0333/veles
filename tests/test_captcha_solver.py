"""Unit tests for captcha_solver helpers (no browser needed)."""

import io
import pytest

from PIL import Image, ImageDraw, ImageFont


# ---------------------------------------------------------------------------
# Helpers to generate simple test captcha images
# ---------------------------------------------------------------------------

def _make_captcha_image(text: str = "Ab12", size=(120, 40), bg="white", fg="black") -> bytes:
    """Draw *text* on a plain background and return PNG bytes."""
    img = Image.new("RGB", size, color=bg)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 24)
    except OSError:
        font = ImageFont.load_default()
    draw.text((10, 6), text, fill=fg, font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_color_captcha(text: str = "Xy34", size=(140, 50)) -> bytes:
    """Captcha with a coloured background and coloured text."""
    img = Image.new("RGB", size, color=(30, 80, 180))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 26)
    except OSError:
        font = ImageFont.load_default()
    draw.text((12, 8), text, fill=(255, 220, 50), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPreprocessImage:
    def test_returns_bytes(self):
        from ouroboros.tools.captcha_solver import preprocess_image
        raw = _make_captcha_image("Test")
        result = preprocess_image(raw)
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_output_is_valid_png(self):
        from ouroboros.tools.captcha_solver import preprocess_image
        result = preprocess_image(_make_captcha_image("AB12"))
        img = Image.open(io.BytesIO(result))
        assert img.format == "PNG"

    def test_color_image_processed(self):
        from ouroboros.tools.captcha_solver import preprocess_image
        result = preprocess_image(_make_color_captcha("Hi99"))
        assert isinstance(result, bytes)
        img = Image.open(io.BytesIO(result))
        assert img.mode == "L"  # should be grayscale after preprocessing


class TestSolveCaptchaImage:
    def test_returns_dict_with_required_keys(self):
        from ouroboros.tools.captcha_solver import solve_captcha_image
        raw = _make_captcha_image("Abc5")
        result = solve_captcha_image(raw)
        assert isinstance(result, dict)
        assert "text" in result
        assert "confidence" in result
        assert "method" in result

    def test_text_non_empty_on_clean_captcha(self):
        from ouroboros.tools.captcha_solver import solve_captcha_image
        raw = _make_captcha_image("HELLO")
        result = solve_captcha_image(raw)
        assert len(result["text"]) > 0

    def test_confidence_is_float(self):
        from ouroboros.tools.captcha_solver import solve_captcha_image
        result = solve_captcha_image(_make_captcha_image("X1Y2"))
        assert isinstance(result["confidence"], float)

    def test_method_is_known(self):
        from ouroboros.tools.captcha_solver import solve_captcha_image
        result = solve_captcha_image(_make_captcha_image("code"))
        assert result["method"] in ("ddddocr", "tesseract")

    def test_color_captcha_handled(self):
        from ouroboros.tools.captcha_solver import solve_captcha_image
        raw = _make_color_captcha("Zq78")
        result = solve_captcha_image(raw)
        assert isinstance(result, dict)
        assert "text" in result
