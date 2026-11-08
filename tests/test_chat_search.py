import json
from collections import Counter

import numpy as np

from ouroboros import chat_search
from ouroboros.chat_records import chat_message_id
from ouroboros.loop import _truncate_tool_result
from ouroboros.tools.registry import ToolContext, ToolRegistry


def _registry(root):
    registry = ToolRegistry(root, root)
    registry.set_context(ToolContext(repo_dir=root, drive_root=root, current_chat_id=42))
    return registry


def _write_rows(root, rows, mode="w"):
    (root / "logs").mkdir(exist_ok=True)
    with (root / "logs/chat.jsonl").open(mode, encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_search_window_scopes_results_and_reuses_vectors(tmp_path, monkeypatch):
    calls = Counter()

    class LocalEncoder:
        def __init__(self, directory):
            pass

        def encode(self, texts, *, query=False):
            vectors = np.zeros((len(texts), chat_search.DIMENSIONS), dtype="<f4")
            for index, text in enumerate(texts):
                calls[text] += 1
                vectors[index, 0 if query or "snapshot" in text else 1] = 1
            return vectors

    monkeypatch.setattr(chat_search, "E5Encoder", LocalEncoder)
    old = {"role": "in", "chat_id": 42, "text": "old snapshot"}
    original = {"direction": "outgoing", "ts": "2026-04-21T12:00:00Z",
                "text": "snapshot for seven days\u2028after update"}
    filler = {"role": "in", "chat_id": 42, "text": "unrelated"}
    foreign = {"role": "in", "chat_id": 7, "text": "foreign snapshot"}
    _write_rows(tmp_path, [old, original, *([filler] * 999), foreign])
    registry = _registry(tmp_path)

    def search():
        return registry.execute("memory_search", {"query": "backup retention", "scope": "chat", "top_k": 1})

    result = search()
    assert "1000 recent records" in result
    assert f"{chat_message_id(original)} | Veles | 2026-04-21" in result
    assert "old snapshot" not in calls and "foreign snapshot" not in calls
    assert calls[original["text"]] == calls["unrelated"] == 1
    assert search() == result
    assert calls[original["text"]] == 1

    updated = {"role": "in", "chat_id": 42, "text": "new snapshot retention policy"}
    _write_rows(tmp_path, [updated], mode="a")
    assert chat_message_id(updated) in search()
    assert calls[updated["text"]] == 1 and calls["unrelated"] == 1
    recovered = registry.execute("chat_history", {"message_id": chat_message_id(original)})
    assert original["text"] in recovered


def test_read_original_in_pages_with_chat_scoped_neighbors(tmp_path):
    target = {"chat_id": 42, "text": "первая строка\n" + "слово " * 430}
    _write_rows(tmp_path, [
        {"role": "in", "chat_id": 42, "text": "before"},
        {"role": "in", "chat_id": 8, "text": "private other chat"},
        target,
        {"role": "out", "chat_id": 42, "text": "after"},
    ])
    registry = _registry(tmp_path)
    args = {"message_id": chat_message_id(target)}
    first = registry.execute("chat_history", args)
    second = registry.execute("chat_history", {**args, "text_offset": 1400})
    assert "Unknown speaker" in first and "before" in first and "after" in first
    assert "private other chat" not in first
    assert first.split("\n\n", 1)[1] + second.split("\n\n", 1)[1] == target["text"]
    assert first == _truncate_tool_result(first, "chat_history", "codex")


def test_missing_model_does_not_label_lexical_search_semantic(tmp_path):
    _write_rows(tmp_path, [{"chat_id": 42, "role": "in", "text": "database snapshot retention policy"}])
    registry = _registry(tmp_path)
    query = {"query": "retention policy"}
    assert "Semantic chat search is unavailable" in registry.execute("memory_search", {**query, "scope": "chat"})
    assert "database snapshot retention policy" in registry.execute("memory_search", query)
