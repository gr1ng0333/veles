"""Tests for multi_model_review tool вЂ” LLMClient-based routing."""

import json
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.tools.review import (
    _handle_multi_model_review,
    _multi_model_review,
    _query_model,
    get_tools,
)
from ouroboros.tools.registry import ToolContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(tmp_path: pathlib.Path) -> ToolContext:
    """Create a minimal ToolContext for tests."""
    return ToolContext(
        repo_dir=tmp_path,
        drive_root=tmp_path,
        pending_events=[],
        task_id="test-task-1",
    )


def _mock_chat_pass(messages, model, **kwargs):
    """Mock LLMClient.chat() returning PASS verdict."""
    return (
        {"role": "assistant", "content": "PASS\nCode looks good. No issues found."},
        {"prompt_tokens": 500, "completion_tokens": 100, "cost": 0.01},
    )


def _mock_chat_fail(messages, model, **kwargs):
    """Mock LLMClient.chat() returning FAIL verdict."""
    return (
        {"role": "assistant", "content": "FAIL\nCritical bug in line 42."},
        {"prompt_tokens": 500, "completion_tokens": 80, "cost": 0.008},
    )


def _mock_chat_unknown(messages, model, **kwargs):
    """Mock LLMClient.chat() returning ambiguous verdict."""
    return (
        {"role": "assistant", "content": "The code has some issues but overall acceptable."},
        {"prompt_tokens": 500, "completion_tokens": 120, "cost": 0.012},
    )


def _mock_chat_error(messages, model, **kwargs):
    """Mock LLMClient.chat() that raises an exception."""
    raise RuntimeError("API connection refused")


# ---------------------------------------------------------------------------
# get_tools() registry
# ---------------------------------------------------------------------------

def test_get_tools_returns_tool_entry():
    tools = get_tools()
    assert len(tools) == 1
    entry = tools[0]
    assert entry.name == "multi_model_review"
    assert "content" in entry.schema["parameters"]["properties"]
    assert "prompt" in entry.schema["parameters"]["properties"]
    assert "models" in entry.schema["parameters"]["properties"]
    # Description mentions all transports
    assert "codex/" in entry.schema["description"]
    assert "copilot/" in entry.schema["description"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_validation_empty_content(tmp_path):
    ctx = _make_ctx(tmp_path)
    result = _multi_model_review("", "check for bugs", ["codex/gpt-5.4"], ctx)
    assert "error" in result
    assert "content" in result["error"]


def test_validation_empty_prompt(tmp_path):
    ctx = _make_ctx(tmp_path)
    result = _multi_model_review("print('hello')", "", ["codex/gpt-5.4"], ctx)
    assert "error" in result
    assert "prompt" in result["error"]


def test_validation_empty_models(tmp_path):
    ctx = _make_ctx(tmp_path)
    result = _multi_model_review("print('hello')", "check bugs", [], ctx)
    assert "error" in result
    assert "models" in result["error"]


def test_validation_too_many_models(tmp_path):
    ctx = _make_ctx(tmp_path)
    models = [f"model/{i}" for i in range(15)]
    result = _multi_model_review("code", "review", models, ctx)
    assert "error" in result
    assert "Too many" in result["error"]


def test_validation_models_not_strings(tmp_path):
    ctx = _make_ctx(tmp_path)
    result = _multi_model_review("code", "review", [123, 456], ctx)
    assert "error" in result
    assert "list of strings" in result["error"]


# ---------------------------------------------------------------------------
# _query_model
# ---------------------------------------------------------------------------

def test_query_model_pass():
    mock_llm = MagicMock()
    mock_llm.chat.side_effect = _mock_chat_pass

    result = _query_model(
        mock_llm,
        "codex/gpt-5.4",
        [{"role": "system", "content": "review"}, {"role": "user", "content": "code"}],
    )

    assert result["model"] == "codex/gpt-5.4"
    assert result["verdict"] == "PASS"
    assert result["tokens_in"] == 500
    assert result["tokens_out"] == 100
    assert result["cost_estimate"] == 0.01
    mock_llm.chat.assert_called_once()


def test_query_model_fail():
    mock_llm = MagicMock()
    mock_llm.chat.side_effect = _mock_chat_fail

    result = _query_model(
        mock_llm,
        "copilot/claude-sonnet-4.6",
        [{"role": "system", "content": "review"}, {"role": "user", "content": "code"}],
    )

    assert result["model"] == "copilot/claude-sonnet-4.6"
    assert result["verdict"] == "FAIL"
    assert result["cost_estimate"] == 0.008


def test_query_model_unknown_verdict():
    mock_llm = MagicMock()
    mock_llm.chat.side_effect = _mock_chat_unknown

    result = _query_model(
        mock_llm,
        "anthropic/claude-haiku-4.5",
        [{"role": "system", "content": "review"}, {"role": "user", "content": "code"}],
    )

    assert result["verdict"] == "UNKNOWN"
    assert result["tokens_in"] == 500


def test_query_model_error_handling():
    mock_llm = MagicMock()
    mock_llm.chat.side_effect = _mock_chat_error

    result = _query_model(
        mock_llm,
        "codex/gpt-5.4",
        [{"role": "system", "content": "review"}, {"role": "user", "content": "code"}],
    )

    assert result["verdict"] == "ERROR"
    assert "API connection refused" in result["text"]
    assert result["tokens_in"] == 0
    assert result["cost_estimate"] == 0.0


def test_query_model_shadow_cost_fallback():
    """When cost is absent but shadow_cost is present, use shadow_cost."""
    mock_llm = MagicMock()
    mock_llm.chat.return_value = (
        {"role": "assistant", "content": "PASS\nLooks fine."},
        {"prompt_tokens": 200, "completion_tokens": 50, "shadow_cost": 0.005},
    )

    result = _query_model(mock_llm, "copilot/claude-haiku-4.5", [])
    assert result["cost_estimate"] == 0.005


# ---------------------------------------------------------------------------
# Full pipeline with mocked LLMClient
# ---------------------------------------------------------------------------

@patch("ouroboros.llm.LLMClient")
def test_multi_model_review_three_models(MockLLMClient, tmp_path):
    """Three models queried in parallel, results in original order."""
    ctx = _make_ctx(tmp_path)

    call_count = {"n": 0}

    def side_effect(messages, model, **kwargs):
        call_count["n"] += 1
        if "codex" in model:
            return _mock_chat_pass(messages, model)
        elif "copilot" in model:
            return _mock_chat_fail(messages, model)
        else:
            return _mock_chat_unknown(messages, model)

    mock_instance = MockLLMClient.return_value
    mock_instance.chat.side_effect = side_effect

    models = ["codex/gpt-5.4", "copilot/claude-sonnet-4.6", "anthropic/claude-haiku-4.5"]
    result = _multi_model_review("def foo(): pass", "Check for bugs", models, ctx)

    assert "error" not in result
    assert result["model_count"] == 3
    assert len(result["results"]) == 3

    # Results in original model order
    assert result["results"][0]["model"] == "codex/gpt-5.4"
    assert result["results"][0]["verdict"] == "PASS"
    assert result["results"][1]["model"] == "copilot/claude-sonnet-4.6"
    assert result["results"][1]["verdict"] == "FAIL"
    assert result["results"][2]["model"] == "anthropic/claude-haiku-4.5"
    assert result["results"][2]["verdict"] == "UNKNOWN"

    # All 3 models were called
    assert mock_instance.chat.call_count == 3

    # Usage events emitted
    assert len(ctx.pending_events) == 3
    for ev in ctx.pending_events:
        assert ev["type"] == "llm_usage"
        assert ev["category"] == "review"
        assert ev["task_id"] == "test-task-1"


@patch("ouroboros.llm.LLMClient")
def test_multi_model_review_one_model_fails(MockLLMClient, tmp_path):
    """One model errors, others succeed вЂ” partial results returned."""
    ctx = _make_ctx(tmp_path)

    def side_effect(messages, model, **kwargs):
        if "broken" in model:
            raise ConnectionError("Network down")
        return _mock_chat_pass(messages, model)

    mock_instance = MockLLMClient.return_value
    mock_instance.chat.side_effect = side_effect

    models = ["codex/gpt-5.4", "broken/model", "anthropic/claude-haiku-4.5"]
    result = _multi_model_review("code", "review", models, ctx)

    assert result["model_count"] == 3
    assert result["results"][0]["verdict"] == "PASS"
    assert result["results"][1]["verdict"] == "ERROR"
    assert "Network down" in result["results"][1]["text"]
    assert result["results"][2]["verdict"] == "PASS"


@patch("ouroboros.llm.LLMClient")
def test_multi_model_review_single_model(MockLLMClient, tmp_path):
    """Single model review works."""
    ctx = _make_ctx(tmp_path)

    mock_instance = MockLLMClient.return_value
    mock_instance.chat.side_effect = _mock_chat_pass

    result = _multi_model_review("code", "review", ["codex/gpt-5.4"], ctx)
    assert result["model_count"] == 1
    assert result["results"][0]["verdict"] == "PASS"


# ---------------------------------------------------------------------------
# Handler (JSON serialization)
# ---------------------------------------------------------------------------

@patch("ouroboros.llm.LLMClient")
def test_handle_returns_json(MockLLMClient, tmp_path):
    """_handle_multi_model_review returns valid JSON string."""
    ctx = _make_ctx(tmp_path)

    mock_instance = MockLLMClient.return_value
    mock_instance.chat.side_effect = _mock_chat_pass

    raw = _handle_multi_model_review(ctx, content="code", prompt="review", models=["codex/gpt-5.4"])
    parsed = json.loads(raw)
    assert "results" in parsed
    assert parsed["results"][0]["verdict"] == "PASS"


def test_handle_empty_models_returns_error(tmp_path):
    """Empty models list returns JSON error."""
    ctx = _make_ctx(tmp_path)
    raw = _handle_multi_model_review(ctx, content="code", prompt="review", models=[])
    parsed = json.loads(raw)
    assert "error" in parsed


@patch("ouroboros.llm.LLMClient")
def test_handle_exception_returns_json_error(MockLLMClient, tmp_path):
    """If everything blows up, handler still returns JSON error."""
    ctx = _make_ctx(tmp_path)

    MockLLMClient.side_effect = Exception("catastrophic failure")

    raw = _handle_multi_model_review(ctx, content="code", prompt="review", models=["m1"])
    parsed = json.loads(raw)
    assert "error" in parsed
    assert "catastrophic" in parsed["error"]


# ---------------------------------------------------------------------------
# Usage events
# ---------------------------------------------------------------------------

@patch("ouroboros.llm.LLMClient")
def test_usage_events_have_correct_structure(MockLLMClient, tmp_path):
    """Each model query emits an llm_usage event with review category."""
    ctx = _make_ctx(tmp_path)

    mock_instance = MockLLMClient.return_value
    mock_instance.chat.side_effect = _mock_chat_pass

    _multi_model_review("code", "review", ["codex/gpt-5.4", "copilot/claude-haiku-4.5"], ctx)

    assert len(ctx.pending_events) == 2
    for ev in ctx.pending_events:
        assert ev["type"] == "llm_usage"
        assert ev["category"] == "review"
        assert "ts" in ev
        assert ev["task_id"] == "test-task-1"
        usage = ev["usage"]
        assert "prompt_tokens" in usage
        assert "completion_tokens" in usage
        assert "cost" in usage


@patch("ouroboros.llm.LLMClient")
def test_usage_event_via_event_queue(MockLLMClient, tmp_path):
    """When event_queue is set, events go there instead of pending_events."""
    import queue
    ctx = _make_ctx(tmp_path)
    ctx.event_queue = queue.Queue()

    mock_instance = MockLLMClient.return_value
    mock_instance.chat.side_effect = _mock_chat_pass

    _multi_model_review("code", "review", ["codex/gpt-5.4"], ctx)

    assert len(ctx.pending_events) == 0
    assert ctx.event_queue.qsize() == 1
    ev = ctx.event_queue.get_nowait()
    assert ev["type"] == "llm_usage"
    assert ev["category"] == "review"


# ---------------------------------------------------------------------------
# Transport diversity
# ---------------------------------------------------------------------------

@patch("ouroboros.llm.LLMClient")
def test_models_passed_to_chat_with_transport_prefix(MockLLMClient, tmp_path):
    """Model strings are passed as-is to LLMClient.chat() which routes by prefix."""
    ctx = _make_ctx(tmp_path)

    called_models = []

    def capture_model(messages, model, **kwargs):
        called_models.append(model)
        return _mock_chat_pass(messages, model)

    mock_instance = MockLLMClient.return_value
    mock_instance.chat.side_effect = capture_model

    models = ["codex/gpt-5.4", "copilot/claude-sonnet-4.6", "anthropic/claude-haiku-4.5"]
    _multi_model_review("code", "review", models, ctx)

    # Each model passed exactly as provided вЂ” LLMClient handles routing
    assert set(called_models) == set(models)


@patch("ouroboros.llm.LLMClient")
def test_messages_contain_system_and_user(MockLLMClient, tmp_path):
    """LLMClient.chat() receives [system prompt, user content] messages."""
    ctx = _make_ctx(tmp_path)

    captured_messages = []

    def capture_msgs(messages, model, **kwargs):
        captured_messages.append(messages)
        return _mock_chat_pass(messages, model)

    mock_instance = MockLLMClient.return_value
    mock_instance.chat.side_effect = capture_msgs

    _multi_model_review("def foo(): pass", "Check for bugs", ["codex/gpt-5.4"], ctx)

    assert len(captured_messages) == 1
    msgs = captured_messages[0]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "Check for bugs"
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "def foo(): pass"
