"""Tests for OpenAlex academic search tool."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.tools.openalex import (
    _academic_search,
    _invert_abstract,
    _parse_work,
    search_openalex,
)


# ------------------------------------------------------------------
# _invert_abstract
# ------------------------------------------------------------------


class TestInvertAbstract:
    def test_basic(self):
        inverted = {"Machine": [0], "learning": [1], "is": [2], "great": [3]}
        assert _invert_abstract(inverted) == "Machine learning is great"

    def test_empty_dict(self):
        assert _invert_abstract({}) == ""

    def test_none(self):
        assert _invert_abstract(None) == ""

    def test_repeated_words(self):
        inverted = {"the": [0, 4], "cat": [1], "sat": [2], "on": [3], "mat": [5]}
        result = _invert_abstract(inverted)
        assert result == "the cat sat on the mat"

    def test_non_dict_input(self):
        assert _invert_abstract("not a dict") == ""  # type: ignore[arg-type]
        assert _invert_abstract(42) == ""  # type: ignore[arg-type]

    def test_malformed_positions(self):
        """Non-list positions should be skipped gracefully."""
        inverted = {"hello": [0], "world": "bad"}
        assert _invert_abstract(inverted) == "hello"


# ------------------------------------------------------------------
# _parse_work
# ------------------------------------------------------------------


class TestParseWork:
    MOCK_ITEM = {
        "id": "https://openalex.org/W123",
        "title": "Test Paper on Transformers",
        "authorships": [
            {"author": {"display_name": "Alice Smith"}},
            {"author": {"display_name": "Bob Jones"}},
        ],
        "publication_year": 2025,
        "cited_by_count": 42,
        "doi": "https://doi.org/10.1234/test",
        "abstract_inverted_index": {"Test": [0], "abstract": [1], "text": [2]},
        "open_access": {"oa_url": "https://example.com/paper.pdf"},
    }

    def test_full_parse(self):
        result = _parse_work(self.MOCK_ITEM)
        assert result["title"] == "Test Paper on Transformers"
        assert result["authors"] == ["Alice Smith", "Bob Jones"]
        assert result["year"] == 2025
        assert result["cited_by_count"] == 42
        assert result["doi"] == "10.1234/test"
        assert result["abstract"] == "Test abstract text"
        assert result["url"] == "https://example.com/paper.pdf"
        assert result["source"] == "openalex"

    def test_missing_fields(self):
        item = {"title": "Minimal Paper", "id": "https://openalex.org/W999"}
        result = _parse_work(item)
        assert result["title"] == "Minimal Paper"
        assert result["authors"] == []
        assert result["year"] == 0
        assert result["cited_by_count"] == 0
        assert result["abstract"] == ""

    def test_doi_fallback_url(self):
        item = {
            "title": "No OA",
            "doi": "https://doi.org/10.5555/test",
            "open_access": {},
        }
        result = _parse_work(item)
        assert result["url"] == "https://doi.org/10.5555/test"


# ------------------------------------------------------------------
# search_openalex (mocked HTTP)
# ------------------------------------------------------------------


class TestSearchOpenalex:
    MOCK_RESPONSE = json.dumps({
        "results": [
            {
                "id": "https://openalex.org/W100",
                "title": "Deep Learning Survey",
                "authorships": [{"author": {"display_name": "Researcher"}}],
                "publication_year": 2024,
                "cited_by_count": 100,
                "doi": "https://doi.org/10.9999/survey",
                "abstract_inverted_index": {"A": [0], "survey": [1]},
                "open_access": {"oa_url": "https://arxiv.org/pdf/2401.00001"},
            }
        ]
    }).encode("utf-8")

    @patch("ouroboros.tools.openalex._openalex_breaker")
    @patch("ouroboros.tools.openalex.urllib.request.urlopen")
    def test_successful_search(self, mock_urlopen, mock_breaker):
        mock_breaker.allow_request.return_value = True
        resp = MagicMock()
        resp.read.return_value = self.MOCK_RESPONSE
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        results = search_openalex("deep learning", limit=5)
        assert len(results) == 1
        assert results[0]["title"] == "Deep Learning Survey"
        assert results[0]["cited_by_count"] == 100
        mock_breaker.record_success.assert_called_once()

    @patch("ouroboros.tools.openalex._openalex_breaker")
    def test_circuit_breaker_open(self, mock_breaker):
        mock_breaker.allow_request.return_value = False
        results = search_openalex("test query")
        assert results == []

    @patch("ouroboros.tools.openalex._openalex_breaker")
    @patch("ouroboros.tools.openalex._request_with_retry")
    def test_network_failure(self, mock_retry, mock_breaker):
        mock_breaker.allow_request.return_value = True
        mock_retry.return_value = None
        results = search_openalex("test query")
        assert results == []
        mock_breaker.record_failure.assert_called_once()


# ------------------------------------------------------------------
# _academic_search tool handler
# ------------------------------------------------------------------


class TestAcademicSearchHandler:
    @patch("ouroboros.tools.openalex.search_openalex")
    def test_returns_json(self, mock_search):
        mock_search.return_value = [
            {
                "title": "Result 1",
                "authors": ["A"],
                "year": 2025,
                "cited_by_count": 10,
                "doi": "10.0000/x",
                "abstract": "Short abstract.",
                "url": "https://example.com",
                "source": "openalex",
            }
        ]
        ctx = MagicMock()
        raw = _academic_search(ctx, "transformers")
        data = json.loads(raw)
        assert data["status"] == "ok"
        assert data["count"] == 1
        assert data["results"][0]["title"] == "Result 1"

    @patch("ouroboros.tools.openalex.search_openalex")
    def test_no_results(self, mock_search):
        mock_search.return_value = []
        ctx = MagicMock()
        raw = _academic_search(ctx, "nonexistent query")
        data = json.loads(raw)
        assert data["status"] == "no_results"
        assert data["count"] == 0

    @patch("ouroboros.tools.openalex.search_openalex")
    def test_abstract_trimmed(self, mock_search):
        mock_search.return_value = [
            {
                "title": "Long Abstract Paper",
                "authors": [],
                "year": 2025,
                "cited_by_count": 0,
                "doi": "",
                "abstract": "x" * 600,
                "url": "",
                "source": "openalex",
            }
        ]
        ctx = MagicMock()
        raw = _academic_search(ctx, "test")
        data = json.loads(raw)
        assert len(data["results"][0]["abstract"]) <= 502  # 500 + "…"

    @patch("ouroboros.tools.openalex.search_openalex")
    def test_max_results_clamped(self, mock_search):
        mock_search.return_value = []
        ctx = MagicMock()
        _academic_search(ctx, "test", max_results=50)
        mock_search.assert_called_once_with("test", limit=20)
