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
    _load_task_reflections,
    _load_dialogue_blocks,
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


def _write_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


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


# ── Task reflections loader ───────────────────────────────────────────────────


def test_load_task_reflections_basic(tmp_path):
    ref_path = tmp_path / "logs" / "task_reflections.jsonl"
    records = [
        {
            "ts": "2026-04-01T10:00:00+00:00",
            "task_id": "abc123",
            "goal": "Fix the SSH key deploy timeout issue on remote server",
            "rounds": 15,
            "max_rounds": 30,
            "error_count": 2,
            "key_markers": ["TOOL_TIMEOUT"],
            "reflection": "The root cause was a hardcoded 30s timeout in registry.py "
                          "for repo_write_commit. Fixed by increasing to 90s.",
        },
        {
            "ts": "2026-04-02T10:00:00+00:00",
            "task_id": "def456",
            "goal": "Implement skills system for task-scoped context injection",
            "rounds": 7,
            "max_rounds": 30,
            "error_count": 0,
            "key_markers": [],
            "reflection": "Created skill_load tool, context.py injection, and _map.md. "
                          "All 30 tests green on first run.",
        },
    ]
    _write_jsonl(ref_path, records)
    chunks = _load_task_reflections(tmp_path)
    assert len(chunks) >= 2
    texts = " ".join(c.text for c in chunks)
    assert "SSH" in texts or "ssh" in texts.lower()
    assert "skill" in texts.lower()


def test_load_task_reflections_skips_failed(tmp_path):
    ref_path = tmp_path / "logs" / "task_reflections.jsonl"
    records = [
        {
            "ts": "2026-03-20T10:00:00+00:00",
            "task_id": "fail001",
            "goal": "Some task that failed reflection generation",
            "rounds": 10,
            "max_rounds": 30,
            "error_count": 1,
            "key_markers": [],
            "reflection": "(reflection generation failed: Error code: 401 - auth error)",
        },
        {
            "ts": "2026-03-21T10:00:00+00:00",
            "task_id": "ok002",
            "goal": "Another task with valid reflection about copilot billing",
            "rounds": 5,
            "max_rounds": 30,
            "error_count": 0,
            "key_markers": [],
            "reflection": "Discovered that session reset billing used wrong initiator header.",
        },
    ]
    _write_jsonl(ref_path, records)
    chunks = _load_task_reflections(tmp_path)
    # Failed reflection's content should not appear but goal still indexed
    texts = " ".join(c.text for c in chunks)
    assert "reflection generation failed" not in texts
    assert "copilot" in texts.lower()


def test_load_task_reflections_missing_file(tmp_path):
    chunks = _load_task_reflections(tmp_path)
    assert chunks == []


def test_load_task_reflections_combined_text(tmp_path):
    """Goal and reflection should both be searchable in the combined chunk."""
    ref_path = tmp_path / "logs" / "task_reflections.jsonl"
    _write_jsonl(ref_path, [{
        "ts": "2026-04-01T10:00:00+00:00",
        "task_id": "combo1",
        "goal": "Understand copilot interaction_id rollover mechanism",
        "rounds": 28,
        "max_rounds": 30,
        "error_count": 0,
        "key_markers": [],
        "reflection": "Found that new thread created every 28 rounds via uuid4.",
    }])
    chunks = _load_task_reflections(tmp_path)
    assert len(chunks) >= 1
    combined = " ".join(c.text for c in chunks)
    # Both goal and reflection text should be present
    assert "copilot" in combined.lower()
    assert "uuid4" in combined.lower()


# ── Dialogue blocks loader ────────────────────────────────────────────────────


def test_load_dialogue_blocks_basic(tmp_path):
    blocks_path = tmp_path / "memory" / "dialogue_blocks.json"
    blocks = [
        {
            "ts": "2026-04-03T19:10:00+00:00",
            "type": "era",
            "range": "2026-03-25 to 2026-03-28",
            "message_count": 700,
            "content": (
                "In this period I fixed the Copilot accounting bug where backend rounds "
                "were counted instead of premium user requests. The interaction_id "
                "normalization resolved duplicate billing entries across sessions."
            ),
        },
        {
            "ts": "2026-04-03T19:15:00+00:00",
            "type": "episode",
            "range": "2026-03-28 to 2026-03-29",
            "message_count": 100,
            "content": (
                "Implemented fitness bot as separate repository. "
                "Removed built-in fitness contour from main Veles codebase. "
                "Discovered Telegram token leak in scratchpad."
            ),
        },
    ]
    _write_json(blocks_path, blocks)
    chunks = _load_dialogue_blocks(tmp_path)
    assert len(chunks) >= 2
    texts = " ".join(c.text for c in chunks)
    assert "copilot" in texts.lower()
    assert "fitness" in texts.lower()
    # Source labels should include range
    sources = {c.source for c in chunks}
    assert any("2026-03-25" in s for s in sources)


def test_load_dialogue_blocks_missing_file(tmp_path):
    chunks = _load_dialogue_blocks(tmp_path)
    assert chunks == []


def test_load_dialogue_blocks_wrong_format(tmp_path):
    """Non-list JSON should return empty without crashing."""
    blocks_path = tmp_path / "memory" / "dialogue_blocks.json"
    blocks_path.parent.mkdir(parents=True, exist_ok=True)
    blocks_path.write_text('{"key": "value"}', encoding="utf-8")
    chunks = _load_dialogue_blocks(tmp_path)
    assert chunks == []


def test_load_dialogue_blocks_skips_empty_content(tmp_path):
    blocks_path = tmp_path / "memory" / "dialogue_blocks.json"
    blocks = [
        {"ts": "2026-04-01T10:00:00+00:00", "range": "test", "content": ""},
        {"ts": "2026-04-01T10:01:00+00:00", "range": "test2",
         "content": "Valid block content about SSH deployment and key management."},
    ]
    _write_json(blocks_path, blocks)
    chunks = _load_dialogue_blocks(tmp_path)
    assert len(chunks) >= 1
    assert all(c.text.strip() for c in chunks)


# ── Full corpus ───────────────────────────────────────────────────────────────


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


def test_build_corpus_includes_reflections_and_dialogue(tmp_path):
    """Reflections and dialogue blocks must appear in the corpus."""
    # reflections
    ref_path = tmp_path / "logs" / "task_reflections.jsonl"
    _write_jsonl(ref_path, [{
        "ts": "2026-04-01T10:00:00+00:00",
        "task_id": "r001",
        "goal": "Fix Copilot timeout issues in registry TOOL_TIMEOUT_OVERRIDES",
        "rounds": 20,
        "max_rounds": 30,
        "error_count": 3,
        "key_markers": ["TOOL_TIMEOUT"],
        "reflection": "Increased timeout for repo_write_commit from 30s to 90s.",
    }])
    # dialogue blocks
    blocks_path = tmp_path / "memory" / "dialogue_blocks.json"
    _write_json(blocks_path, [{
        "ts": "2026-04-03T10:00:00+00:00",
        "type": "episode",
        "range": "2026-04-01 to 2026-04-02",
        "message_count": 50,
        "content": "Skills system was implemented: skill_load tool, context injection, _map.md.",
    }])

    corpus = _build_corpus(tmp_path)
    sources = {c.source for c in corpus}
    assert any("reflection" in s for s in sources), f"No reflection sources in: {sources}"
    assert any("dialogue" in s for s in sources), f"No dialogue sources in: {sources}"


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


def test_memory_search_finds_reflections(tmp_path):
    """End-to-end: reflection data is searchable via tool API."""
    ref_path = tmp_path / "logs" / "task_reflections.jsonl"
    _write_jsonl(ref_path, [{
        "ts": "2026-04-02T10:00:00+00:00",
        "task_id": "e2e001",
        "goal": "Fix copilot push timeout in registry timeout overrides",
        "rounds": 25,
        "max_rounds": 30,
        "error_count": 2,
        "key_markers": ["TOOL_TIMEOUT"],
        "reflection": "Increased repo_write_commit timeout from 30s to 90s in TOOL_TIMEOUT_OVERRIDES.",
    }])
    ctx = _make_ctx(tmp_path)
    result = _memory_search(ctx, "copilot timeout registry")
    assert "reflection" in result
    assert "score=" in result


def test_memory_search_finds_dialogue_blocks(tmp_path):
    """End-to-end: dialogue block data is searchable via tool API."""
    blocks_path = tmp_path / "memory" / "dialogue_blocks.json"
    _write_json(blocks_path, [{
        "ts": "2026-04-03T10:00:00+00:00",
        "type": "era",
        "range": "2026-03-25 to 2026-03-28",
        "message_count": 700,
        "content": (
            "Copilot accounting fix: premium request counting was wrong. "
            "interaction_id normalization resolved billing duplicates."
        ),
    }])
    ctx = _make_ctx(tmp_path)
    result = _memory_search(ctx, "copilot accounting billing")
    assert "dialogue" in result
    assert "score=" in result


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
    # description should mention the new sources
    assert "reflections" in schema["description"] or "reflection" in schema["description"]
    assert "dialogue" in schema["description"]


def test_get_tools_handler_callable():
    tools = get_tools()
    assert callable(tools[0].handler)

# ── Notes loader tests ────────────────────────────────────────────────────────

from ouroboros.tools.memory_search import _load_notes


def test_load_notes_basic(tmp_path):
    """_load_notes returns chunks from a valid notes.jsonl."""
    notes_dir = tmp_path / "memory"
    notes_dir.mkdir(parents=True, exist_ok=True)
    note_data = [
        {"id": "20260404_abc1", "timestamp": "2026-04-04T01:00:00Z",
         "text": "Reward model breaks when colon prefix is used in prompt",
         "tags": ["ml", "research"], "source": "https://t.me/abstractDL/402",
         "deleted": False},
        {"id": "20260404_abc2", "timestamp": "2026-04-04T02:00:00Z",
         "text": "3x-ui webBasePath must include trailing slash",
         "tags": ["infra"], "source": "",
         "deleted": False},
    ]
    notes_file = notes_dir / "notes.jsonl"
    notes_file.write_text(
        "\n".join(json.dumps(n) for n in note_data) + "\n",
        encoding="utf-8"
    )
    chunks = _load_notes(tmp_path)
    assert len(chunks) >= 2
    # Source labels must start with 'note/'
    assert all(c.source.startswith("note/") for c in chunks)


def test_load_notes_deleted_skipped(tmp_path):
    """Deleted notes must not appear in chunks."""
    notes_dir = tmp_path / "memory"
    notes_dir.mkdir(parents=True, exist_ok=True)
    note_data = [
        {"id": "n1", "timestamp": "2026-04-04T01:00:00Z",
         "text": "This note is alive and should be indexed",
         "tags": [], "source": "", "deleted": False},
        {"id": "n2", "timestamp": "2026-04-04T01:00:00Z",
         "text": "This note was deleted and must not appear",
         "tags": [], "source": "", "deleted": True},
    ]
    notes_file = notes_dir / "notes.jsonl"
    notes_file.write_text(
        "\n".join(json.dumps(n) for n in note_data) + "\n",
        encoding="utf-8"
    )
    chunks = _load_notes(tmp_path)
    assert len(chunks) == 1
    assert "alive" in chunks[0].text
    assert "deleted" not in chunks[0].text


def test_load_notes_empty_file(tmp_path):
    """Empty notes.jsonl returns empty list without error."""
    notes_dir = tmp_path / "memory"
    notes_dir.mkdir(parents=True, exist_ok=True)
    (notes_dir / "notes.jsonl").write_text("", encoding="utf-8")
    chunks = _load_notes(tmp_path)
    assert chunks == []


def test_load_notes_missing_file(tmp_path):
    """Missing notes.jsonl returns empty list without error."""
    chunks = _load_notes(tmp_path)
    assert chunks == []


def test_load_notes_tags_indexed(tmp_path):
    """Tags are included in the searchable text so they participate in scoring."""
    notes_dir = tmp_path / "memory"
    notes_dir.mkdir(parents=True, exist_ok=True)
    note = {"id": "n1", "timestamp": "2026-04-04T01:00:00Z",
            "text": "XYZ discovery", "tags": ["uniquetag42"], "source": "",
            "deleted": False}
    (notes_dir / "notes.jsonl").write_text(json.dumps(note) + "\n", encoding="utf-8")
    chunks = _load_notes(tmp_path)
    assert len(chunks) == 1
    # The tag must appear in the combined text
    assert "uniquetag42" in chunks[0].text


def test_notes_appear_in_corpus(tmp_path):
    """_build_corpus includes notes chunks when notes.jsonl is present."""
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    note = {"id": "n1", "timestamp": "2026-04-04T01:00:00Z",
            "text": "ssh deploy key generation must verify fingerprint",
            "tags": ["infra"], "source": "", "deleted": False}
    (tmp_path / "memory" / "notes.jsonl").write_text(json.dumps(note) + "\n", encoding="utf-8")
    from ouroboros.tools.memory_search import _build_corpus
    corpus = _build_corpus(tmp_path)
    note_chunks = [c for c in corpus if c.source.startswith("note/")]
    assert len(note_chunks) >= 1


def test_memory_search_finds_note(tmp_path):
    """memory_search returns a note chunk when the query matches."""
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    note = {"id": "n42", "timestamp": "2026-04-04T01:00:00Z",
            "text": "reward model adversarial colon prefix vulnerability paper",
            "tags": ["ml"], "source": "https://arxiv.org/abs/2501.00001",
            "deleted": False}
    (tmp_path / "memory" / "notes.jsonl").write_text(json.dumps(note) + "\n", encoding="utf-8")
    ctx = _make_ctx(tmp_path)
    result = _memory_search(ctx, query="reward model colon", top_k=5)
    assert "note/" in result


def test_schema_description_mentions_notes():
    """Tool description must mention notes so the LLM knows to use it for saved notes."""
    tools = get_tools()
    desc = tools[0].schema["description"]
    assert "note" in desc.lower()
