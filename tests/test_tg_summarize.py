"""Tests for tg_summarize and tg_summarize_watchlist tools."""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from ouroboros.tools.registry import ToolContext
from ouroboros.tools.tg_summarize import (
    get_tools,
    _format_posts_for_prompt,
    _parse_llm_json,
)


def make_ctx(drive_root=None):
    dr = drive_root or pathlib.Path("/tmp")
    return ToolContext(repo_dir=pathlib.Path("/tmp"), drive_root=dr)


FAKE_POSTS = [
    {"id": 100, "date": "2026-03-01T10:00:00+00:00", "text": "New paper on LLM scaling laws", "views": 500, "links": ["https://arxiv.org/abs/1234"]},
    {"id": 101, "date": "2026-03-02T11:00:00+00:00", "text": "GPT-4 evaluation on coding benchmarks", "views": 800, "links": []},
    {"id": 102, "date": "2026-03-03T12:00:00+00:00", "text": "Mixture of Experts reduces costs 40%", "views": 1200, "links": ["https://example.com/moe"]},
]

FAKE_LLM_RESPONSE = json.dumps({
    "channel": "@testchan",
    "date_range": "2026-03-01 to 2026-03-03",
    "post_count": 3,
    "summary": "A research channel covering LLM scaling, evaluation, and efficiency.",
    "topics": [
        {"topic": "LLM Scaling", "details": "Papers on scaling laws."},
        {"topic": "Efficiency", "details": "MoE reduces costs 40%."},
    ],
    "notable_links": ["https://arxiv.org/abs/1234", "https://example.com/moe"],
})


class TestFormatPostsForPrompt(unittest.TestCase):
    def test_basic(self):
        result = _format_posts_for_prompt(FAKE_POSTS)
        self.assertIn("LLM scaling laws", result)
        self.assertIn("2026-03-01", result)

    def test_includes_links(self):
        result = _format_posts_for_prompt(FAKE_POSTS)
        self.assertIn("arxiv.org", result)

    def test_truncates_at_limit(self):
        # Construct many posts
        posts = [
            {"id": i, "date": "2026-01-01", "text": "x" * 300, "views": 0, "links": []}
            for i in range(100)
        ]
        result = _format_posts_for_prompt(posts, max_chars=1000)
        self.assertIn("truncated", result)

    def test_skips_empty_text(self):
        posts = [
            {"id": 1, "date": "2026-01-01", "text": "", "views": 0, "links": []},
            {"id": 2, "date": "2026-01-01", "text": "real content", "views": 0, "links": []},
        ]
        result = _format_posts_for_prompt(posts)
        self.assertNotIn("#1", result)
        self.assertIn("real content", result)


class TestParseLlmJson(unittest.TestCase):
    def test_valid_json(self):
        data = '{"channel": "@test", "summary": "hi"}'
        result = _parse_llm_json(data)
        self.assertEqual(result["channel"], "@test")

    def test_strips_markdown_fences(self):
        data = '```json\n{"key": "val"}\n```'
        result = _parse_llm_json(data)
        self.assertEqual(result["key"], "val")

    def test_extracts_json_block(self):
        data = 'here is the output: {"foo": "bar"} end'
        result = _parse_llm_json(data)
        self.assertEqual(result["foo"], "bar")

    def test_invalid_json_returns_raw(self):
        data = "not json at all"
        result = _parse_llm_json(data)
        self.assertIn("parse_error", result)


class TestTgSummarize(unittest.TestCase):
    def setUp(self):
        self.tools = {t.name: t for t in get_tools()}

    def test_get_tools_count(self):
        self.assertEqual(len(get_tools()), 2)
        names = {t.name for t in get_tools()}
        self.assertIn("tg_summarize", names)
        self.assertIn("tg_summarize_watchlist", names)

    def test_schema_valid(self):
        for t in get_tools():
            self.assertIn("name", t.schema)
            self.assertIn("parameters", t.schema)
            self.assertIn("properties", t.schema["parameters"])

    def test_empty_channel_error(self):
        ctx = make_ctx()
        result = json.loads(self.tools["tg_summarize"].handler(ctx, channel=""))
        self.assertIn("error", result)

    def test_fetch_error_propagated(self):
        with patch("ouroboros.tools.tg_channel_read._fetch_channel_posts",
                   return_value={"error": "Network error", "posts": []}):
            ctx = make_ctx()
            result = json.loads(self.tools["tg_summarize"].handler(ctx, channel="testchan"))
        self.assertIn("error", result)

    def test_no_posts_returns_empty_summary(self):
        with patch("ouroboros.tools.tg_channel_read._fetch_channel_posts",
                   return_value={"channel": "testchan", "posts": [], "posts_count": 0}):
            ctx = make_ctx()
            result = json.loads(self.tools["tg_summarize"].handler(ctx, channel="testchan"))
        self.assertEqual(result["post_count"], 0)
        self.assertEqual(result["topics"], [])

    def test_successful_summarize(self):
        mock_msg = {"content": FAKE_LLM_RESPONSE}
        mock_usage = {"cost": 0.001, "prompt_tokens": 100, "completion_tokens": 50}

        with patch("ouroboros.tools.tg_channel_read._fetch_channel_posts",
                   return_value={"channel": "testchan", "posts": FAKE_POSTS, "posts_count": 3}), \
             patch("ouroboros.tools.tg_summarize._call_llm_with_fallback",
                   return_value=(FAKE_LLM_RESPONSE, mock_usage)):
            ctx = make_ctx()
            result = json.loads(self.tools["tg_summarize"].handler(ctx, channel="testchan"))

        self.assertEqual(result["post_count"], 3)
        self.assertIn("topics", result)
        self.assertIn("notable_links", result)
        self.assertGreater(len(result["topics"]), 0)

    def test_llm_failure_returns_error(self):
        with patch("ouroboros.tools.tg_channel_read._fetch_channel_posts",
                   return_value={"channel": "testchan", "posts": FAKE_POSTS, "posts_count": 3}), \
             patch("ouroboros.tools.tg_summarize._call_llm_with_fallback",
                   side_effect=Exception("LLM timeout")):
            ctx = make_ctx()
            result = json.loads(self.tools["tg_summarize"].handler(ctx, channel="testchan"))
        self.assertIn("error", result)


class TestTgSummarizeWatchlist(unittest.TestCase):
    def setUp(self):
        self.tools = {t.name: t for t in get_tools()}

    def test_empty_watchlist(self):
        with patch("ouroboros.tools.tg_summarize._load_watchlist", return_value={}):
            ctx = make_ctx()
            result = json.loads(self.tools["tg_summarize_watchlist"].handler(ctx))
        self.assertEqual(result["channels_processed"], 0)
        self.assertIn("message", result)

    def test_watchlist_no_new_posts(self):
        watchlist = {"testchan": {"last_id": 200, "added_at": "2026-01-01", "last_checked": None}}
        with patch("ouroboros.tools.tg_summarize._load_watchlist", return_value=watchlist), \
             patch("ouroboros.tools.tg_channel_read._fetch_channel_posts",
                   return_value={"channel": "testchan", "posts": [], "posts_count": 0}):
            ctx = make_ctx()
            result = json.loads(self.tools["tg_summarize_watchlist"].handler(ctx))
        self.assertEqual(result["channels_with_new_content"], 0)
        self.assertIn("No new posts", result["results"][0]["summary"])

    def test_watchlist_with_new_posts(self):
        watchlist = {"testchan": {"last_id": 0, "added_at": "2026-01-01", "last_checked": None}}
        mock_usage = {"cost": 0.001, "prompt_tokens": 100, "completion_tokens": 50}

        with patch("ouroboros.tools.tg_summarize._load_watchlist", return_value=watchlist), \
             patch("ouroboros.tools.tg_channel_read._fetch_channel_posts",
                   return_value={"channel": "testchan", "posts": FAKE_POSTS, "posts_count": 3}), \
             patch("ouroboros.tools.tg_summarize._call_llm_with_fallback",
                   return_value=(FAKE_LLM_RESPONSE, mock_usage)):
            ctx = make_ctx()
            result = json.loads(self.tools["tg_summarize_watchlist"].handler(ctx))

        self.assertEqual(result["channels_with_new_content"], 1)
        chan_result = result["results"][0]
        self.assertEqual(chan_result["new_posts"], 3)
        self.assertIn("topics", chan_result)

    def test_watchlist_llm_failure_continues(self):
        """LLM failure for one channel should not crash the whole run."""
        watchlist = {
            "chan1": {"last_id": 0, "added_at": "2026-01-01", "last_checked": None},
        }
        with patch("ouroboros.tools.tg_summarize._load_watchlist", return_value=watchlist), \
             patch("ouroboros.tools.tg_channel_read._fetch_channel_posts",
                   return_value={"channel": "chan1", "posts": FAKE_POSTS, "posts_count": 3}), \
             patch("ouroboros.tools.tg_summarize._call_llm_with_fallback",
                   side_effect=Exception("model error")):
            ctx = make_ctx()
            result = json.loads(self.tools["tg_summarize_watchlist"].handler(ctx))
        # should complete without raising, error in result
        self.assertEqual(result["channels_processed"], 1)
        self.assertIn("error", result["results"][0])

    def test_watchlist_fetch_error(self):
        watchlist = {"chan1": {"last_id": 0, "added_at": "2026-01-01", "last_checked": None}}
        with patch("ouroboros.tools.tg_summarize._load_watchlist", return_value=watchlist), \
             patch("ouroboros.tools.tg_channel_read._fetch_channel_posts",
                   return_value={"channel": "chan1", "error": "channel not found", "posts": []}):
            ctx = make_ctx()
            result = json.loads(self.tools["tg_summarize_watchlist"].handler(ctx))
        self.assertIn("error", result["results"][0])


if __name__ == "__main__":
    unittest.main()
