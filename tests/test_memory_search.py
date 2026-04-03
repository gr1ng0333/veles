"""Tests for memory_search tool."""
from __future__ import annotations

import json
import pathlib
import tempfile
import textwrap

import pytest

from ouroboros.tools.memory_search import (
    _tokenize,
    _Chunk,
    _split_into_chunks,
    _load_chat,
    _load_md,
    _build_corpus,
    _compute_idf,
    _score,
    _run_search,
    _memory_search,
    get_tools,
)
from ouroboros.tools.registry import ToolEntry, ToolContext


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_ctx(drive_root: pathlib.Path) -> ToolContext:
    return ToolContext(
        repo_dir=pathlib.Path("/opt/veles"),
        drive_root=drive_root,
    )


def _write_jsonl(path: pathlib.Path, messages: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")


def _write_md(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


# ── Tokenizer ─────────────────────────────────────────────────────────────────


def test_tokenize_basic():
    tokens = _tokenize("Hello world, это тест 123")
    assert "hello" in tokens
    assert "world" in tokens
    assert "тест" in tokens


def test_tokenize_removes_stop_words():
    tokens = _tokenize("the cat is on the mat")
    assert "the" not in tokens
    assert "is" not in tokens
    assert "cat" in tokens
    assert "mat" in tokens


def test_tokenize_min_length():
    # Single-char tokens should be excluded
    tokens = _tokenize("a b c hello")
    assert "a" not in tokens
    assert "hello" in tokens


def test_tokenize_empty():
    assert _tokenize("") == []
    assert _tokenize("   ") == []


# ── Chunk splitter ────────────────────────────────────────────────────────────


def test_split_into_chunks_short_text():
    text = "Short text with enough tokens here and there."
    chunks = _split_into_chunks(text, "test", "2026-01-01", chunk_size=600)
    assert len(chunks) == 1
    assert chunks[0].source == "test"
    assert chunks[0].date == "2026-01-01"


def test_split_into_chunks_long_text():
    text = ("word " * 200).strip()  # 1000 chars
    chunks = _split_into_chunks(text, "test", "2026-01-01", chunk_size=200)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c.tokens) >= 3


def test_split_into_chunks_skips_tiny():
    chunks = _split_into_chunks("hi", "test", "2026-01-01")
    assert chunks == []


# ── Loaders ───────────────────────────────────────────────────────────────────


def test_load_chat_basic(tmp_path):
    chat_path = tmp_path / "logs" / "chat.jsonl"
    msgs = [
        {"ts": "2026-04-01T10:00:00+00:00", "direction": "in",
         "text": "How to configure 3x-ui webBasePath properly?"},
        {"ts": "2026-04-01T10:01:00+00:00", "direction": "out",
         "text": "The webBasePath must be set in the panel settings before any client configuration."},
    ]
    _write_jsonl(chat_path, msgs)
    chunks = _load_chat(tmp_path)
    assert len(chunks) >= 2
    texts = " ".join(c.text for c in chunks)
    assert "webBasePath" in texts


def test_load_chat_missing_file(tmp_path):
    chunks = _load_chat(tmp_path)
    assert chunks == []


def test_load_chat_skips_short_messages(tmp_path):
    chat_path = tmp_path / "logs" / "chat.jsonl"
    msgs = [
        {"ts": "2026-04-01T10:00:00+00:00", "direction": "in", "text": "ok"},
        {"ts": "2026-04-01T10:01:00+00:00", "direction": "out",
         "text": "This message is long enough to pass the minimum threshold check."},
    ]
    _write_jsonl(chat_path, msgs)
    chunks = _load_chat(tmp_path)
    assert all(len(c.text) >= 20 for c in chunks)


def test_load_md_basic(tmp_path):
    md = tmp_path / "test.md"
    _write_md(md, """\
        # Title

        First paragraph with enough content to be included in search results.

        Second paragraph about SSH key deployment and remote server health check.
    """)
    chunks = _load_md(md, "test-topic")
    assert len(chunks) >= 1
    texts = " ".join(c.text for c in chunks)
    assert "SSH" in texts or "ssh" in texts.lower()


def test_load_md_missing_file(tmp_path):
    chunks = _load_md(tmp_path / "nonexistent.md", "missing")
    assert chunks == []


def test_build_corpus_empty(tmp_path):
    corpus = _build_corpus(tmp_path)
    assert corpus == []


def test_build_corpus_with_data(tmp_path):
    # chat
    chat_path = tmp_path / "logs" / "chat.jsonl"
    _write_jsonl(chat_path, [
        {"ts": "2026-04-01T10:00:00+00:00", "direction": "in",
         "text": "Tell me about Copilot session reset billing mechanism."},
    ])
    # knowledge
    kb_dir = tmp_path / "memory" / "knowledge"
    _write_md(kb_dir / "copilot-test.md",
              "Copilot billing: premium request is counted per interaction.\n\n"
              "Session reset creates a new interaction_id and resets the round counter.")
    # identity
    _write_md(tmp_path / "memory" / "identity.md",
              "I am Veles. I evolved from Ouroboros.\n\nI work with Copilot and Codex APIs.")

    corpus = _build_corpus(tmp_path)
    sources = {c.source for c in corpus}
    assert any("chat" in s for s in sources)
    assert any("knowledge" in s for s in sources)
    assert any("identity" in s for s in sources)


# ── TF-IDF scoring ────────────────────────────────────────────────────────────


def test_compute_idf_basic():
    chunks = [
        _Chunk("s1", "2026-01-01", "copilot billing session reset"),
        _Chunk("s2", "2026-01-01", "ssh key deploy remote server"),
        _Chunk("s3", "2026-01-01", "copilot accounting interaction id"),
    ]
    # tokenize manually
    for c in chunks:
        c.tokens = _tokenize(c.text)

    idf = _compute_idf(chunks)
    # "copilot" appears in 2/3 docs → lower IDF than rare word
    assert "copilot" in idf
    assert "ssh" in idf
    # rare word has higher idf
    assert idf["ssh"] > idf["copilot"]


def test_score_relevant_chunk():
    chunk = _Chunk("kb", "2026-01-01",
                   "The webBasePath must be set correctly for 3x-ui panel login.")
    chunk.tokens = _tokenize(chunk.text)
    idf = {"webbasepath": 2.0, "must": 1.0, "correctly": 2.0, "3x": 2.0}
    q_tokens = _tokenize("webBasePath 3x-ui panel")
    s = _score(q_tokens, chunk, idf)
    assert s > 0


def test_score_irrelevant_chunk():
    chunk = _Chunk("kb", "2026-01-01", "Fitness tracking macros protein carbs calories")
    chunk.tokens = _tokenize(chunk.text)
    idf = {"fitness": 2.0, "macros": 2.0}
    q_tokens = _tokenize("copilot billing session")
    s = _score(q_tokens, chunk, idf)
    assert s == 0.0


def test_score_empty_query():
    chunk = _Chunk("kb", "2026-01-01", "Some content")
    chunk.tokens = _tokenize(chunk.text)
    assert _score([], chunk, {}) == 0.0


# ── End-to-end search ─────────────────────────────────────────────────────────


def test_run_search_returns_top_k(tmp_path):
    # Build a small corpus
    chat_path = tmp_path / "logs" / "chat.jsonl"
    _write_jsonl(chat_path, [
        {"ts": "2026-04-01T10:00:00+00:00", "direction": "in",
         "text": "How does Copilot session reset work with billing and interaction tracking?"},
        {"ts": "2026-04-01T10:01:00+00:00", "direction": "out",
         "text": "Session reset creates a new interaction_id every 28 rounds for Copilot billing."},
        {"ts": "2026-04-01T10:02:00+00:00", "direction": "in",
         "text": "What about SSH key deployment on remote servers with password auth?"},
    ])
    results = _run_search("copilot session billing", tmp_path, top_k=2)
    assert len(results) <= 2
    assert all(score > 0 for score, _ in results)
    # Most relevant should be about copilot billing
    _, top_chunk = results[0]
    assert "copilot" in top_chunk.text.lower() or "session" in top_chunk.text.lower()


def test_run_search_no_results(tmp_path):
    # Empty corpus
    results = _run_search("anything", tmp_path, top_k=5)
    assert results == []


def test_run_search_empty_query(tmp_path):
    results = _run_search("", tmp_path, top_k=5)
    assert results == []


# ── Tool API ──────────────────────────────────────────────────────────────────


def test_memory_search_tool_no_results(tmp_path):
    ctx = _make_ctx(tmp_path)
    result = _memory_search(ctx, "nonexistent unique query xyzzy")
    assert "no results" in result


def test_memory_search_tool_with_data(tmp_path):
    chat_path = tmp_path / "logs" / "chat.jsonl"
    _write_jsonl(chat_path, [
        {"ts": "2026-04-01T10:00:00+00:00", "direction": "in",
         "text": "Explain the 3x-ui webBasePath configuration for VPN panel access control."},
    ])
    ctx = _make_ctx(tmp_path)
    result = _memory_search(ctx, "webBasePath 3x-ui")
    assert "memory_search" in result
    assert "chat" in result
    assert "score=" in result


def test_memory_search_tool_empty_query(tmp_path):
    ctx = _make_ctx(tmp_path)
    result = _memory_search(ctx, "")
    assert "⚠️" in result


def test_memory_search_tool_top_k_clamp(tmp_path):
    chat_path = tmp_path / "logs" / "chat.jsonl"
    _write_jsonl(chat_path, [
        {"ts": "2026-04-01T10:00:00+00:00", "direction": "in",
         "text": "SSH key deployment on remote server using password bootstrap method."},
    ])
    ctx = _make_ctx(tmp_path)
    # top_k=100 should be clamped to 20
    result = _memory_search(ctx, "ssh key", top_k=100)
    # Should not error; just return at most 20 results
    assert "⚠️" not in result or "no results" in result


# ── Registry ──────────────────────────────────────────────────────────────────


def test_get_tools_returns_one_entry():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "memory_search"


def test_get_tools_schema_valid():
    tools = get_tools()
    schema = tools[0].schema
    assert schema["name"] == "memory_search"
    assert "description" in schema
    assert schema["parameters"]["required"] == ["query"]
    props = schema["parameters"]["properties"]
    assert "query" in props
    assert "top_k" in props


def test_get_tools_handler_callable():
    tools = get_tools()
    assert callable(tools[0].handler)
