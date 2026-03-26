"""Tests for search query utilities — shortening and expansion."""

from __future__ import annotations

import pytest

from ouroboros.search_utils import (
    _extract_keywords,
    dedupe_signature,
    detect_query_type,
    expand_search_queries,
    extract_core_subject,
    recency_signal,
    score_result_signals,
    shorten_query,
)


# ------------------------------------------------------------------
# shorten_query
# ------------------------------------------------------------------


class TestShortenQuery:
    def test_under_limit_unchanged(self):
        assert shorten_query("machine learning") == "machine learning"

    def test_removes_stop_words(self):
        result = shorten_query(
            "a comprehensive overview of the novel approaches for using "
            "machine learning in the analysis of very large datasets",
            max_len=60,
        )
        assert len(result) <= 60
        assert "machine" in result.lower()
        assert "learning" in result.lower()
        assert "comprehensive" not in result.lower()
        # Stop words removed
        for sw in ("the", "of", "for", "a"):
            assert sw not in result.split(), f"stop word '{sw}' still present"

    def test_preserves_suffix_benchmark(self):
        result = shorten_query(
            "a comprehensive study of transformer architectures for "
            "natural language processing benchmark",
            max_len=60,
        )
        assert result.endswith("benchmark")
        assert len(result) <= 60

    def test_preserves_suffix_survey(self):
        result = shorten_query(
            "a comprehensive investigation of the latest deep learning "
            "methods applied to computer vision survey",
            max_len=60,
        )
        assert result.endswith("survey")

    def test_empty_input(self):
        assert shorten_query("") == ""

    def test_whitespace_only(self):
        assert shorten_query("   ") == ""

    def test_exact_limit(self):
        query = "x" * 60
        assert shorten_query(query, max_len=60) == query

    def test_one_over_limit(self):
        # 61 chars should trigger shortening
        query = "a " * 31  # 62 chars
        result = shorten_query(query.strip(), max_len=60)
        assert len(result) <= 60

    def test_custom_max_len(self):
        long_query = "transformer attention mechanism self supervised learning pretraining finetuning evaluation"
        result = shorten_query(long_query, max_len=40)
        assert len(result) <= 40


# ------------------------------------------------------------------
# _extract_keywords
# ------------------------------------------------------------------


class TestExtractKeywords:
    def test_basic(self):
        kw = _extract_keywords("the quick brown fox jumps over a lazy dog")
        assert "quick" in kw
        assert "brown" in kw
        assert "fox" in kw
        assert "the" not in kw
        assert "a" not in kw
        assert "over" in kw  # not a stop word, len > 1

    def test_empty(self):
        assert _extract_keywords("") == []

    def test_all_stop_words(self):
        assert _extract_keywords("the a an of for in on") == []

    def test_single_char_removed(self):
        kw = _extract_keywords("I am a x test")
        assert "I" not in kw  # single char
        assert "x" not in kw  # single char
        assert "test" in kw


# ------------------------------------------------------------------
# expand_search_queries
# ------------------------------------------------------------------


class TestExpandSearchQueries:
    def test_basic_expansion(self):
        queries = expand_search_queries("transformer architectures")
        assert len(queries) >= 2
        assert any("transformer" in q.lower() for q in queries)

    def test_contains_original(self):
        queries = expand_search_queries("transformer architectures")
        assert queries[0].lower() == "transformer architectures"

    def test_has_suffix_variants(self):
        queries = expand_search_queries("transformer architectures")
        all_text = " ".join(queries).lower()
        assert "survey" in all_text
        assert "benchmark" in all_text

    def test_short_topic(self):
        queries = expand_search_queries("BERT")
        assert "BERT" in queries

    def test_empty_input(self):
        assert expand_search_queries("") == []

    def test_long_topic_expanded(self):
        topic = "a comprehensive study of graph neural network applications in drug discovery and molecular property prediction"
        queries = expand_search_queries(topic)
        assert len(queries) >= 3
        for q in queries:
            assert len(q) <= 60

    def test_no_duplicates(self):
        queries = expand_search_queries("neural networks survey")
        lowered = [q.lower() for q in queries]
        assert len(lowered) == len(set(lowered))


class TestQuerySignals:
    def test_detect_query_type_docs(self):
        assert detect_query_type("openai api rate limit docs") == "docs_api"

    def test_extract_core_subject(self):
        assert extract_core_subject("What are OpenAI API rate limits in docs") == "OpenAI API rate limits"

    def test_dedupe_signature_normalizes_url(self):
        a = dedupe_signature(title="OpenAI Docs", url="https://platform.openai.com/docs/")
        b = dedupe_signature(title="OpenAI Docs", url="http://platform.openai.com/docs")
        assert a == b

    def test_recency_signal_respects_priority(self):
        assert recency_signal(text="updated 2026 latest", freshness_priority="high") > recency_signal(text="updated 2026 latest", freshness_priority="low")

    def test_score_result_signals_bundle(self):
        data = score_result_signals("openai api docs", title="OpenAI API docs", snippet="latest reference", url="https://platform.openai.com/docs")
        assert data["query_type"] == "docs_api"
        assert data["relevance"] > 0
        assert data["dedupe_signature"]
