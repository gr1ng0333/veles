"""Smoke tests for VLM (Vision Language Model) support."""

import sys
import json
import os
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
import pathlib

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestLLMVisionQuery(unittest.TestCase):
    """Test LLMClient.vision_query() message format."""

    def test_vision_query_url_format(self):
        """vision_query builds correct message format for URL images."""
        from ouroboros.llm import LLMClient

        client = LLMClient(api_key="test-key")

        captured_messages = []

        def mock_chat(messages, model, tools=None, reasoning_effort="low", max_tokens=1024, tool_choice="auto"):
            captured_messages.extend(messages)
            return {"content": "I see a test image."}, {"prompt_tokens": 10, "completion_tokens": 5}

        client.chat = mock_chat

        text, usage = client.vision_query(
            prompt="What do you see?",
            images=[{"url": "https://example.com/test.png"}],
            model="anthropic/claude-sonnet-4.6",
        )

        self.assertEqual(text, "I see a test image.")
        self.assertEqual(len(captured_messages), 1)
        content = captured_messages[0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[0]["text"], "What do you see?")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertIn("url", content[1]["image_url"])
        self.assertEqual(content[1]["image_url"]["url"], "https://example.com/test.png")

    def test_vision_query_base64_format(self):
        """vision_query builds correct data URI for base64 images."""
        from ouroboros.llm import LLMClient

        client = LLMClient(api_key="test-key")
        captured_messages = []

        def mock_chat(messages, model, tools=None, reasoning_effort="low", max_tokens=1024, tool_choice="auto"):
            captured_messages.extend(messages)
            return {"content": "Base64 image description."}, {}

        client.chat = mock_chat

        fake_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        text, _ = client.vision_query(
            prompt="Describe this.",
            images=[{"base64": fake_b64, "mime": "image/png"}],
        )

        self.assertEqual(text, "Base64 image description.")
        content = captured_messages[0]["content"]
        image_part = content[1]
        self.assertTrue(image_part["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertIn(fake_b64, image_part["image_url"]["url"])

    def test_vision_query_multiple_images(self):
        """vision_query handles multiple images in one call."""
        from ouroboros.llm import LLMClient

        client = LLMClient(api_key="test-key")
        captured_messages = []

        def mock_chat(messages, model, tools=None, reasoning_effort="low", max_tokens=1024, tool_choice="auto"):
            captured_messages.extend(messages)
            return {"content": "Two images."}, {}

        client.chat = mock_chat

        client.vision_query(
            prompt="Compare these images.",
            images=[
                {"url": "https://example.com/img1.png"},
                {"url": "https://example.com/img2.png"},
            ],
        )

        content = captured_messages[0]["content"]
        self.assertEqual(len(content), 3)  # text + 2 images

    def test_vision_query_empty_images(self):
        """vision_query works with no images (just text)."""
        from ouroboros.llm import LLMClient

        client = LLMClient(api_key="test-key")

        def mock_chat(messages, model, tools=None, reasoning_effort="low", max_tokens=1024, tool_choice="auto"):
            return {"content": "Text only."}, {}

        client.chat = mock_chat

        text, _ = client.vision_query(prompt="Hello", images=[])
        self.assertEqual(text, "Text only.")


class TestAnalyzeScreenshotTool(unittest.TestCase):
    """Test the analyze_screenshot tool."""

    def _make_ctx(self, with_screenshot=True):
        from ouroboros.tools.registry import ToolContext, BrowserState
        ctx = MagicMock(spec=ToolContext)
        ctx.browser_state = BrowserState()
        ctx.event_queue = None
        ctx.task_id = "test-task"
        ctx.current_task_type = "task"
        if with_screenshot:
            ctx.browser_state.last_screenshot_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        else:
            ctx.browser_state.last_screenshot_b64 = None
        return ctx

    def test_no_screenshot_returns_warning(self):
        """analyze_screenshot returns warning when no screenshot available."""
        from ouroboros.tools.vision import _analyze_screenshot

        ctx = self._make_ctx(with_screenshot=False)
        result = _analyze_screenshot(ctx, prompt="What do you see?")
        self.assertIn("⚠️", result)
        self.assertIn("screenshot", result.lower())

    def test_analyze_screenshot_calls_vlm(self):
        """analyze_screenshot calls VLM with the screenshot base64."""
        from ouroboros.tools.vision import _analyze_screenshot

        ctx = self._make_ctx(with_screenshot=True)

        with patch("ouroboros.tools.vision._get_llm_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.vision_query.return_value = ("Beautiful UI.", {"prompt_tokens": 100, "completion_tokens": 20})
            mock_get_client.return_value = mock_client

            result = _analyze_screenshot(ctx, prompt="Describe the UI.")

        self.assertEqual(result, "Beautiful UI.")
        mock_client.vision_query.assert_called_once()
        call_kwargs = mock_client.vision_query.call_args
        # Check that base64 image was passed
        images = call_kwargs[1].get("images") or call_kwargs[0][1]
        self.assertEqual(len(images), 1)
        self.assertIn("base64", images[0])


class TestVlmQueryTool(unittest.TestCase):
    """Test the vlm_query tool."""

    def _make_ctx(self):
        from ouroboros.tools.registry import ToolContext, BrowserState
        ctx = MagicMock(spec=ToolContext)
        ctx.browser_state = BrowserState()
        ctx.event_queue = None
        ctx.task_id = "test-task"
        ctx.current_task_type = "task"
        return ctx

    def test_vlm_query_requires_image(self):
        """vlm_query returns error when no image provided."""
        from ouroboros.tools.vision import _vlm_query

        ctx = self._make_ctx()
        result = _vlm_query(ctx, prompt="What is this?")
        self.assertIn("⚠️", result)

    def test_vlm_query_with_url(self):
        """vlm_query calls VLM with URL image."""
        from ouroboros.tools.vision import _vlm_query

        ctx = self._make_ctx()

        with patch("ouroboros.tools.vision._get_llm_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.vision_query.return_value = ("A logo.", {})
            mock_get_client.return_value = mock_client

            result = _vlm_query(ctx, prompt="What is the logo?", image_url="https://example.com/logo.png")

        self.assertEqual(result, "A logo.")
        call_kwargs = mock_client.vision_query.call_args
        images = call_kwargs[1].get("images") or call_kwargs[0][1]
        self.assertEqual(images[0]["url"], "https://example.com/logo.png")

    def test_vlm_query_tool_registered(self):
        """vlm_query and analyze_screenshot tools are properly registered."""
        import pathlib
        from ouroboros.tools.registry import ToolRegistry

        registry = ToolRegistry(
            repo_dir=pathlib.Path("/tmp"),
            drive_root=pathlib.Path("/tmp"),
        )
        tools = [s["name"] for s in registry.schemas()]
        self.assertIn("analyze_screenshot", tools, "analyze_screenshot must be registered")
        self.assertIn("vlm_query", tools, "vlm_query must be registered")
        self.assertIn("solve_simple_captcha", tools, "solve_simple_captcha must be registered")


class TestScreenshotSendTools(unittest.TestCase):
    def _make_ctx(self, with_screenshot=True):
        from ouroboros.tools.registry import ToolContext, BrowserState
        ctx = MagicMock(spec=ToolContext)
        ctx.browser_state = BrowserState()
        ctx.pending_events = []
        ctx.current_chat_id = 789
        ctx.current_task_type = "task"
        ctx.task_id = "task-123"
        ctx.is_direct_chat = True
        ctx.browser_state.last_screenshot_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==" * 2
            if with_screenshot else None
        )
        return ctx

    def test_send_browser_screenshot_captures_live_page_before_queueing_photo(self):
        from ouroboros.tools.core import _send_browser_screenshot

        ctx = self._make_ctx(with_screenshot=False)
        ctx.browser_state.page = MagicMock()
        ctx.browser_state.page.screenshot.return_value = b"fake-png-bytes" * 16

        result = _send_browser_screenshot(ctx, caption="captcha")

        self.assertIn("photo queued for delivery", result)
        ctx.browser_state.page.screenshot.assert_called_once_with(type="png", full_page=False)
        self.assertTrue(ctx.browser_state.last_screenshot_b64)
        self.assertEqual(len(ctx.pending_events), 1)
        evt = ctx.pending_events[0]
        self.assertEqual(evt["type"], "send_photo")
        self.assertEqual(evt["chat_id"], 789)
        self.assertEqual(evt["caption"], "captcha")
        self.assertEqual(evt["source"], "browser_last_screenshot")
        self.assertEqual(evt["task_id"], "task-123")
        self.assertEqual(evt["task_type"], "task")
        self.assertTrue(evt["is_direct_chat"])

    def test_send_browser_screenshot_falls_back_to_stored_screenshot(self):
        from ouroboros.tools.core import _send_browser_screenshot

        ctx = self._make_ctx(with_screenshot=True)
        result = _send_browser_screenshot(ctx, caption="captcha")

        self.assertIn("photo queued for delivery", result)
        self.assertEqual(len(ctx.pending_events), 1)
        evt = ctx.pending_events[0]
        self.assertEqual(evt["source"], "browser_last_screenshot")

    def test_send_browser_screenshot_requires_page_or_stored_screenshot(self):
        from ouroboros.tools.core import _send_browser_screenshot

        ctx = self._make_ctx(with_screenshot=False)
        result = _send_browser_screenshot(ctx)

        self.assertIn("No screenshot stored and no active browser page", result)
        self.assertEqual(ctx.pending_events, [])

    def test_send_browser_screenshot_is_routed_as_stateful_browser_tool(self):
        from ouroboros.loop import STATEFUL_BROWSER_TOOLS

        self.assertIn("send_browser_screenshot", STATEFUL_BROWSER_TOOLS)

    def test_send_browser_screenshot_end_to_end_dispatches_photo_and_logs_delivery(self):
        from supervisor.events import _handle_send_photo
        from ouroboros.tools.core import _send_browser_screenshot

        ctx = self._make_ctx(with_screenshot=False)
        ctx.browser_state.page = MagicMock()
        ctx.browser_state.page.screenshot.return_value = b"fake-png-bytes" * 16

        result = _send_browser_screenshot(ctx, caption="captcha")

        self.assertIn("photo queued for delivery", result)
        self.assertEqual(len(ctx.pending_events), 1)
        evt = ctx.pending_events[0]

        logs = []
        sent = []

        class DummyTG:
            def send_photo(self, chat_id, photo_bytes, caption=""):
                sent.append({"chat_id": chat_id, "photo_bytes": photo_bytes, "caption": caption})
                return True, None

        supervisor_ctx = MagicMock()
        supervisor_ctx.DRIVE_ROOT = pathlib.Path('/tmp')
        supervisor_ctx.TG = DummyTG()
        supervisor_ctx.append_jsonl = lambda path, row: logs.append(row)

        _handle_send_photo(evt, supervisor_ctx)

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat_id"], 789)
        self.assertEqual(sent[0]["caption"], "captcha")
        self.assertTrue(sent[0]["photo_bytes"])
        delivered = next(row for row in logs if row.get("type") == "send_photo_delivered")
        self.assertEqual(delivered["source"], "browser_last_screenshot")
        self.assertEqual(delivered["task_id"], "task-123")
        self.assertEqual(delivered["task_type"], "task")
        self.assertTrue(delivered["is_direct_chat"])


class TestSolveSimpleCaptchaTool(unittest.TestCase):
    def _make_ctx(self, with_screenshot=True):
        from ouroboros.tools.registry import ToolContext, BrowserState
        ctx = MagicMock(spec=ToolContext)
        ctx.browser_state = BrowserState()
        ctx.event_queue = None
        ctx.task_id = "test-task"
        ctx.current_task_type = "task"
        ctx.browser_state.last_screenshot_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
            if with_screenshot else None
        )
        return ctx

    def test_solve_simple_captcha_uses_screenshot_by_default(self):
        from ouroboros.tools.vision import _solve_simple_captcha

        ctx = self._make_ctx(with_screenshot=True)
        with patch("ouroboros.tools.vision._get_llm_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.vision_query.return_value = ("AB12", {})
            mock_get_client.return_value = mock_client

            result = json.loads(_solve_simple_captcha(ctx))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["text"], "AB12")
        images = mock_client.vision_query.call_args[1]["images"]
        self.assertIn("base64", images[0])

    def test_solve_simple_captcha_marks_uncertain_on_long_answer(self):
        from ouroboros.tools.vision import _solve_simple_captcha

        ctx = self._make_ctx(with_screenshot=True)
        with patch("ouroboros.tools.vision._get_llm_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.vision_query.return_value = ("captcha looks like ABC123XYZ", {})
            mock_get_client.return_value = mock_client

            result = json.loads(_solve_simple_captcha(ctx, max_length=6))

        self.assertEqual(result["status"], "uncertain")

    def test_solve_simple_captcha_requires_image(self):
        from ouroboros.tools.vision import _solve_simple_captcha

        ctx = self._make_ctx(with_screenshot=False)
        result = json.loads(_solve_simple_captcha(ctx))
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["reason"], "no_image")


if __name__ == "__main__":
    unittest.main()
