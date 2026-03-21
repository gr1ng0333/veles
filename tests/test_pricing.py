"""Tests for ouroboros/pricing.py."""

import pytest
from ouroboros.pricing import estimate_cost, get_pricing, MODEL_PRICING_STATIC


def test_estimate_cost_known_model():
    cost = estimate_cost("openai/gpt-5.4", prompt_tokens=1000, completion_tokens=100)
    assert cost > 0


def test_estimate_cost_with_cache():
    cost_no_cache = estimate_cost(
        "openai/gpt-5.4", prompt_tokens=1000, completion_tokens=100, cached_tokens=0
    )
    cost_with_cache = estimate_cost(
        "openai/gpt-5.4", prompt_tokens=1000, completion_tokens=100, cached_tokens=800
    )
    assert cost_with_cache < cost_no_cache


def test_estimate_cost_unknown_model():
    cost = estimate_cost("unknown/model", prompt_tokens=1000, completion_tokens=100)
    assert cost == 0.0  # Unknown returns 0


def test_estimate_cost_free_model():
    cost = estimate_cost(
        "qwen/qwen3-coder:free", prompt_tokens=10000, completion_tokens=1000
    )
    assert cost == 0.0


def test_transport_prefix_stripped_codex():
    """gpt-5.3-codex should have pricing in static table."""
    cost = estimate_cost("gpt-5.3-codex", prompt_tokens=1000, completion_tokens=100)
    assert cost > 0


def test_transport_prefix_stripped_copilot():
    """claude-sonnet-4.6 (Copilot shadow) should have pricing."""
    cost = estimate_cost("claude-sonnet-4.6", prompt_tokens=1000, completion_tokens=100)
    assert cost > 0


def test_pricing_static_table_not_empty():
    assert len(MODEL_PRICING_STATIC) > 10


def test_estimate_cost_zero_tokens():
    cost = estimate_cost("openai/gpt-5.4", prompt_tokens=0, completion_tokens=0)
    assert cost == 0.0


def test_estimate_cost_negative_cached_handled():
    """Cached tokens > prompt tokens should not produce negative cost."""
    cost = estimate_cost(
        "openai/gpt-5.4",
        prompt_tokens=100,
        completion_tokens=50,
        cached_tokens=200,
    )
    assert cost >= 0


def test_get_pricing_returns_dict():
    pricing = get_pricing()
    assert isinstance(pricing, dict)
    assert len(pricing) > 0


def test_prefix_match():
    """Models with partial prefix should match."""
    # openai/gpt-5.4 should match openai/gpt-5.4-xxx
    cost = estimate_cost(
        "openai/gpt-5.4-turbo-extra", prompt_tokens=1000, completion_tokens=100
    )
    # Should match via prefix to openai/gpt-5.4
    assert cost > 0
