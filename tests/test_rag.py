"""Tests for RAG (Retrieval-Augmented Generation) tools."""
from __future__ import annotations

import json
import pathlib
import tempfile
import textwrap

import pytest

from ouroboros.tools.rag import (
    _tokenize,
    _chunk_text,
    _parse_file,
    _validate_collection_name,
    _build_bm25_index,
    _bm25_score,
    _rag_index,
    _rag_query,
    _rag_list,
    _rag_delete,
    _load_chunks,
    _load_meta,
    get_tools,
)
from ouroboros.tools.registry import ToolContext, ToolEntry


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ctx(drive_root: pathlib.Path) -> ToolContext:
    return ToolContext(
        repo_dir=pathlib.Path("/opt/veles"),
        drive_root=drive_root,
    )


def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ── Tokenizer ─────────────────────────────────────────────────────────────────


def test_tokenize_basic():
    tokens = _tokenize("Hello world, это тест 123")
    assert "hello" in tokens
    assert "world" in tokens
    assert "тест" in tokens


def test_tokenize_stop_words_removed():
    tokens = _tokenize("the cat is on the mat with some stuff")
    assert "the" not in tokens
    assert "is" not in tokens
    assert "cat" in tokens
    assert "mat" in tokens


def test_tokenize_empty():
    assert _tokenize("") == []
    assert _tokenize("   ") == []


def test_tokenize_min_length():
    tokens = _tokenize("a b c hello")
    assert "a" not in tokens
    assert "hello" in tokens


# ── Chunk text ────────────────────────────────────────────────────────────────


def test_chunk_text_short():
    chunks = _chunk_text("Short paragraph with enough words to pass token gate.", "file.txt")
    assert len(chunks) >= 1
    assert chunks[0]["source"] == "file.txt"
    assert chunks[0]["chunk_idx"] == 0


def test_chunk_text_long():
    # ~2000 chars — must produce multiple chunks
    text = ("word_token " * 200).strip()
    chunks = _chunk_text(text, "big.txt", chunk_size=300, overlap=50)
    assert len(chunks) >= 3
    # All chunks have required keys
    for c in chunks:
        assert "text" in c
        assert "source" in c
        assert "page" in c
        assert "chunk_idx" in c


def test_chunk_text_skips_empty():
    chunks = _chunk_text("", "empty.txt")
    assert chunks == []

    chunks = _chunk_text("   \n\n   ", "whitespace.txt")
    assert chunks == []


def test_chunk_text_skips_tiny_fragments():
    # Only punctuation / stop-words → fewer than MIN_CHUNK_TOKENS useful tokens
    chunks = _chunk_text("a b c d", "tiny.txt")
    assert chunks == []


def test_chunk_text_paragraph_split():
    text = (
        "First paragraph about dynamic programming algorithms and optimization.\n\n"
        "Second paragraph about graph theory and shortest path algorithms.\n\n"
        "Third paragraph about machine learning and neural networks training."
    )
    chunks = _chunk_text(text, "essay.txt", chunk_size=200)
    # Should produce at least 2 chunks
    assert len(chunks) >= 2


def test_chunk_text_huge_paragraph():
    # A single paragraph > chunk_size must still be sliced
    para = "word " * 300  # ~1500 chars, one paragraph
    chunks = _chunk_text(para, "huge.txt", chunk_size=200, overlap=30)
    assert len(chunks) >= 3


# ── File parser ───────────────────────────────────────────────────────────────


def test_parse_text_file(tmp_path):
    f = tmp_path / "readme.md"
    _write(f, "# Title\n\nSome content about dynamic programming.")
    text = _parse_file(f)
    assert "dynamic programming" in text


def test_parse_nonexistent_file(tmp_path):
    text = _parse_file(tmp_path / "nonexistent.txt")
    assert text == ""


def test_parse_json_file(tmp_path):
    f = tmp_path / "data.json"
    _write(f, '{"key": "value", "list": [1, 2, 3]}')
    text = _parse_file(f)
    assert "value" in text


def test_parse_py_file(tmp_path):
    f = tmp_path / "module.py"
    _write(f, "def hello():\n    return 'world'\n")
    text = _parse_file(f)
    assert "def hello" in text


def test_parse_unknown_extension_as_text(tmp_path):
    f = tmp_path / "weird.xyz"
    _write(f, "content with some words for indexing")
    text = _parse_file(f)
    assert "content" in text


# ── Collection name validation ────────────────────────────────────────────────


def test_validate_collection_name_valid():
    for name in ["tasks", "my-docs", "project_v2", "A1B2"]:
        _validate_collection_name(name)  # should not raise


def test_validate_collection_name_invalid():
    with pytest.raises(ValueError):
        _validate_collection_name("")
    with pytest.raises(ValueError):
        _validate_collection_name("bad name!")
    with pytest.raises(ValueError):
        _validate_collection_name("a" * 65)
    with pytest.raises(ValueError):
        _validate_collection_name("path/traversal")


# ── BM25 engine ───────────────────────────────────────────────────────────────


def test_build_bm25_index_basic():
    chunks = [
        {"text": "dynamic programming optimal substructure memoization"},
        {"text": "graph theory shortest path dijkstra algorithm"},
        {"text": "dynamic programming coin change problem solution"},
    ]
    tokenized, idf, avgdl = _build_bm25_index(chunks)
    assert len(tokenized) == 3
    assert "dynamic" in idf
    assert "programming" in idf
    assert "dijkstra" in idf
    # "dynamic" appears in 2/3 docs → lower idf than rare "dijkstra" (1/3)
    assert idf["dijkstra"] > idf["dynamic"]
    assert avgdl > 0


def test_build_bm25_index_empty():
    tokenized, idf, avgdl = _build_bm25_index([])
    assert tokenized == []
    assert idf == {}
    assert avgdl == 0.0


def test_bm25_score_relevant():
    doc_tokens = _tokenize("dynamic programming optimal substructure memoization")
    query_tokens = _tokenize("dynamic programming")
    idf = {"dynamic": 1.5, "programming": 1.5, "optimal": 2.0}
    avgdl = len(doc_tokens)
    score = _bm25_score(query_tokens, doc_tokens, idf, avgdl)
    assert score > 0


def test_bm25_score_irrelevant():
    doc_tokens = _tokenize("graph theory shortest path algorithm")
    query_tokens = _tokenize("dynamic programming")
    idf = {"dynamic": 1.5, "programming": 1.5}
    avgdl = len(doc_tokens)
    score = _bm25_score(query_tokens, doc_tokens, idf, avgdl)
    assert score == 0.0


def test_bm25_score_empty_query():
    doc_tokens = _tokenize("some document content here")
    score = _bm25_score([], doc_tokens, {}, len(doc_tokens))
    assert score == 0.0


def test_bm25_score_empty_doc():
    query_tokens = _tokenize("dynamic programming")
    score = _bm25_score(query_tokens, [], {"dynamic": 1.5}, 10.0)
    assert score == 0.0


def test_bm25_ranking_order():
    """More relevant doc should score higher."""
    chunks = [
        {"text": "dynamic programming is a technique for solving problems with overlapping subproblems"},
        {"text": "graph theory explores nodes and edges without dynamic focus"},
        {"text": "dynamic programming dynamic programming memoization optimal"},
    ]
    tokenized, idf, avgdl = _build_bm25_index(chunks)
    from ouroboros.tools.rag import _bm25_score
    q = _tokenize("dynamic programming")
    scores = [_bm25_score(q, t, idf, avgdl) for t in tokenized]
    # chunks[2] mentions "dynamic programming" twice → highest score
    assert scores[2] >= scores[0] >= scores[1]


# ── rag_index ─────────────────────────────────────────────────────────────────


def test_rag_index_basic(tmp_path):
    ctx = _ctx(tmp_path)
    f1 = tmp_path / "doc1.txt"
    _write(f1, "Dynamic programming is a method for solving complex problems by breaking them into simpler subproblems.")
    f2 = tmp_path / "doc2.txt"
    _write(f2, "Graph theory studies graphs which are mathematical structures used to model pairwise relations.")

    result = _rag_index(ctx, paths=[str(f1), str(f2)], collection="test")
    assert "indexed" in result.lower()
    assert "2" in result  # 2 files

    col_dir = tmp_path / "rag" / "test"
    assert (col_dir / "chunks.json").exists()
    assert (col_dir / "meta.json").exists()

    meta = _load_meta(col_dir)
    assert meta["file_count"] == 2
    assert meta["chunk_count"] >= 2


def test_rag_index_missing_file(tmp_path):
    ctx = _ctx(tmp_path)
    result = _rag_index(ctx, paths=["/nonexistent/file.txt"], collection="test")
    assert "Warning" in result or "not found" in result.lower()


def test_rag_index_empty_paths(tmp_path):
    ctx = _ctx(tmp_path)
    result = _rag_index(ctx, paths=[], collection="test")
    assert "error" in result.lower()


def test_rag_index_overwrite(tmp_path):
    ctx = _ctx(tmp_path)
    f = tmp_path / "doc.txt"
    _write(f, "First version of the document with some content about algorithms.")
    _rag_index(ctx, paths=[str(f)], collection="test")

    # Overwrite with new content
    _write(f, "Second version completely different content about databases.")
    result = _rag_index(ctx, paths=[str(f)], collection="test", overwrite=True)
    assert "indexed" in result.lower()

    chunks = _load_chunks(tmp_path / "rag" / "test")
    texts = " ".join(c["text"] for c in chunks)
    assert "Second version" in texts
    assert "First version" not in texts


def test_rag_index_append(tmp_path):
    ctx = _ctx(tmp_path)
    f1 = tmp_path / "doc1.txt"
    f2 = tmp_path / "doc2.txt"
    _write(f1, "Document one about dynamic programming and memoization techniques.")
    _write(f2, "Document two about graph algorithms and shortest path solutions.")

    _rag_index(ctx, paths=[str(f1)], collection="test")
    meta_before = _load_meta(tmp_path / "rag" / "test")

    _rag_index(ctx, paths=[str(f2)], collection="test", overwrite=False)
    meta_after = _load_meta(tmp_path / "rag" / "test")

    assert meta_after["file_count"] == 2
    assert meta_after["chunk_count"] >= meta_before["chunk_count"]


def test_rag_index_invalid_collection(tmp_path):
    ctx = _ctx(tmp_path)
    result = _rag_index(ctx, paths=[], collection="bad name!")
    assert "error" in result.lower()


# ── rag_query ─────────────────────────────────────────────────────────────────


def test_rag_query_basic(tmp_path):
    ctx = _ctx(tmp_path)
    f = tmp_path / "docs" / "alg.txt"
    _write(f, textwrap.dedent("""\
        Dynamic programming is an algorithmic technique that solves complex problems
        by breaking them into overlapping subproblems and storing results.

        Graph theory studies mathematical structures called graphs.
        A graph consists of vertices connected by edges.

        Sorting algorithms arrange elements in a specific order.
        Quicksort is a popular divide-and-conquer sorting algorithm.
    """))
    _rag_index(ctx, paths=[str(f)], collection="alg")

    result = _rag_query(ctx, query="dynamic programming subproblems", collection="alg", top_k=3)
    assert "dynamic" in result.lower() or "programming" in result.lower()
    assert "rag_query" in result


def test_rag_query_no_results(tmp_path):
    ctx = _ctx(tmp_path)
    f = tmp_path / "doc.txt"
    _write(f, "This document is about cooking recipes and food ingredients.")
    _rag_index(ctx, paths=[str(f)], collection="food")

    result = _rag_query(ctx, query="quantum mechanics superconductors", collection="food", top_k=5)
    assert "no results" in result.lower()


def test_rag_query_missing_collection(tmp_path):
    ctx = _ctx(tmp_path)
    result = _rag_query(ctx, query="test query", collection="nonexistent")
    assert "not exist" in result.lower() or "empty" in result.lower()


def test_rag_query_empty_query(tmp_path):
    ctx = _ctx(tmp_path)
    _write(tmp_path / "doc.txt", "some content here for testing purposes")
    _rag_index(ctx, paths=[str(tmp_path / "doc.txt")], collection="test")
    result = _rag_query(ctx, query="", collection="test")
    assert "error" in result.lower()


def test_rag_query_top_k_respected(tmp_path):
    ctx = _ctx(tmp_path)
    # Create multiple distinct documents
    for i in range(8):
        f = tmp_path / f"doc{i}.txt"
        _write(f, f"Document {i} about topic{i} with specific content and words for ranking.")
    files = [str(tmp_path / f"doc{i}.txt") for i in range(8)]
    _rag_index(ctx, paths=files, collection="multi")

    result = _rag_query(ctx, query="document topic content", collection="multi", top_k=3)
    # Count "**N.**" result markers
    import re
    matches = re.findall(r"\*\*\d+\.\*\*", result)
    assert len(matches) <= 3


def test_rag_query_show_source_false(tmp_path):
    ctx = _ctx(tmp_path)
    f = tmp_path / "doc.txt"
    _write(f, "Dynamic programming solves problems by storing intermediate results.")
    _rag_index(ctx, paths=[str(f)], collection="test")
    result = _rag_query(ctx, query="dynamic programming", collection="test",
                        show_source=False)
    # With show_source=False, file name should not appear
    assert "doc.txt" not in result
    assert "score=" not in result


def test_rag_query_relevance_ordering(tmp_path):
    """More relevant chunk should appear first."""
    ctx = _ctx(tmp_path)
    f = tmp_path / "mixed.txt"
    _write(f, textwrap.dedent("""\
        Sorting algorithms are fundamental to computer science.
        Quicksort uses divide and conquer strategy.

        Dynamic programming solves optimization problems by breaking them
        into overlapping subproblems with memoization. Dynamic programming
        is extremely useful for dynamic programming challenges.

        Binary trees store data in hierarchical structure.
    """))
    _rag_index(ctx, paths=[str(f)], collection="mixed")
    result = _rag_query(ctx, query="dynamic programming memoization", collection="mixed", top_k=2)
    # First result should contain "dynamic programming"
    lines = result.strip().split("\n")
    first_result_text = "\n".join(lines[2:])  # skip header
    assert "dynamic" in first_result_text.lower()


# ── rag_list ──────────────────────────────────────────────────────────────────


def test_rag_list_empty(tmp_path):
    ctx = _ctx(tmp_path)
    result = _rag_list(ctx)
    assert "no collections" in result.lower()


def test_rag_list_with_collections(tmp_path):
    ctx = _ctx(tmp_path)
    for name, content in [("tasks", "task content about algorithms"),
                           ("notes", "personal notes about learning")]:
        f = tmp_path / f"{name}.txt"
        _write(f, content + " with enough words for tokenization.")
        _rag_index(ctx, paths=[str(f)], collection=name)

    result = _rag_list(ctx)
    assert "tasks" in result
    assert "notes" in result
    assert "chunks" in result.lower() or "chunk" in result.lower()
    assert "2 total" in result


def test_rag_list_shows_stats(tmp_path):
    ctx = _ctx(tmp_path)
    f = tmp_path / "doc.txt"
    _write(f, "Content for indexing with enough tokens to create multiple chunks here.")
    _rag_index(ctx, paths=[str(f)], collection="mytest")
    result = _rag_list(ctx)
    assert "mytest" in result
    assert "1 files" in result


# ── rag_delete ────────────────────────────────────────────────────────────────


def test_rag_delete_existing(tmp_path):
    ctx = _ctx(tmp_path)
    f = tmp_path / "doc.txt"
    _write(f, "Some content for indexing in delete test case.")
    _rag_index(ctx, paths=[str(f)], collection="todel")

    result = _rag_delete(ctx, collection="todel")
    assert "deleted" in result.lower()
    assert not (tmp_path / "rag" / "todel").exists()


def test_rag_delete_nonexistent(tmp_path):
    ctx = _ctx(tmp_path)
    result = _rag_delete(ctx, collection="noexist")
    assert "not found" in result.lower()


def test_rag_delete_invalid_name(tmp_path):
    ctx = _ctx(tmp_path)
    result = _rag_delete(ctx, collection="bad name!")
    assert "error" in result.lower()


def test_rag_delete_cleans_up(tmp_path):
    ctx = _ctx(tmp_path)
    f = tmp_path / "doc.txt"
    _write(f, "Content for collection that will be deleted afterwards.")
    _rag_index(ctx, paths=[str(f)], collection="cleanup")

    _rag_delete(ctx, collection="cleanup")

    # After delete, query should say not found
    result = _rag_query(ctx, query="test", collection="cleanup")
    assert "not exist" in result.lower() or "empty" in result.lower()


# ── get_tools / registry ──────────────────────────────────────────────────────


def test_get_tools_returns_four():
    tools = get_tools()
    assert len(tools) == 4
    names = {t.name for t in tools}
    assert names == {"rag_index", "rag_query", "rag_list", "rag_delete"}


def test_get_tools_are_tool_entries():
    tools = get_tools()
    for t in tools:
        assert isinstance(t, ToolEntry)
        assert callable(t.handler)


def test_get_tools_schemas_valid():
    tools = get_tools()
    for t in tools:
        schema = t.schema
        assert "name" in schema
        assert "description" in schema
        assert "parameters" in schema
        assert schema["parameters"]["type"] == "object"


def test_rag_index_tool_callable(tmp_path):
    ctx = _ctx(tmp_path)
    tools = {t.name: t for t in get_tools()}

    f = tmp_path / "test.txt"
    _write(f, "Test content about dynamic programming and algorithms.")
    result = tools["rag_index"].handler(ctx, paths=[str(f)], collection="tooltest")
    assert "indexed" in result.lower()


def test_rag_query_tool_callable(tmp_path):
    ctx = _ctx(tmp_path)
    tools = {t.name: t for t in get_tools()}

    f = tmp_path / "test.txt"
    _write(f, "Dynamic programming solves problems by storing intermediate results.")
    tools["rag_index"].handler(ctx, paths=[str(f)], collection="tooltest")
    result = tools["rag_query"].handler(ctx, query="dynamic programming", collection="tooltest")
    assert "rag_query" in result


def test_rag_list_tool_callable(tmp_path):
    ctx = _ctx(tmp_path)
    tools = {t.name: t for t in get_tools()}
    result = tools["rag_list"].handler(ctx)
    assert isinstance(result, str)


def test_rag_delete_tool_callable(tmp_path):
    ctx = _ctx(tmp_path)
    tools = {t.name: t for t in get_tools()}
    result = tools["rag_delete"].handler(ctx, collection="noexist")
    assert "not found" in result.lower()
