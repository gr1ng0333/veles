import json
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Barrier

import pytest

from ouroboros.loop import _truncate_tool_result
from ouroboros.memory_publication import journal_path
from ouroboros.tools.registry import ToolContext, ToolRegistry


@pytest.fixture
def memory_tools(tmp_path):
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry.set_context(ToolContext(repo_dir=tmp_path, drive_root=tmp_path, task_id="memory-test"))
    (tmp_path / "memory").mkdir(exist_ok=True)
    return registry.execute


@pytest.mark.parametrize("name", ["identity", "scratchpad"])
def test_memory_roundtrip_through_tools(tmp_path, memory_tools, name):
    path = tmp_path / "memory" / f"{name}.md"
    old = ("Сохраняем точный текст и переводы строк.\r\n" * 80).encode("utf-8")
    path.write_bytes(old)
    history = journal_path(path)
    # The scratchpad can already have preview-only rows from older releases.
    legacy_row = {"content_preview": "previous preview", "content_len": 100}
    if name == "scratchpad":
        history.write_bytes(json.dumps(legacy_row).encode("utf-8"))
    relative = f"memory/{path.name}"
    revision = sha256(old).hexdigest()
    first = memory_tools("drive_read", {"path": relative})
    assert first == _truncate_tool_result(first, "drive_read", "codex")
    assert first.startswith(f"revision={revision}\n")
    assert "next_offset=2000" in first
    second = memory_tools("drive_read", {"path": relative, "offset": 2000, "revision": revision})
    assert first.split("\n\n", 1)[1] + second.split("\n\n", 1)[1] == old.decode("utf-8")

    tool = f"update_{name}"
    new = "Revised memory " * 240
    assert "Revision required" in memory_tools(tool, {"content": new})
    assert "Protected memory" in memory_tools("drive_write", {"path": relative, "content": new})
    assert path.read_bytes() == old
    result = memory_tools(tool, {"content": new, "expected_revision": revision})
    assert result.startswith("OK")
    assert path.read_bytes() == new.encode("utf-8")
    assert "Revision conflict" in memory_tools(tool, {"content": new, "expected_revision": revision})
    assert "Revision conflict" in memory_tools(
        "drive_read", {"path": relative, "offset": 2000, "revision": revision},
    )
    assert "Protected memory" in memory_tools(
        "drive_write", {"path": f"memory/{history.name}", "content": "", "mode": "overwrite"},
    )
    rows = [json.loads(row) for row in history.read_text(encoding="utf-8").splitlines()]
    if name == "scratchpad":
        assert rows.pop(0) == legacy_row
    row, = rows
    assert row["old_sha256"] == revision
    assert row["old_content"].encode("utf-8") == old
    assert row["new_content"] == new
    assert row["new_sha256"] == sha256(path.read_bytes()).hexdigest()
    assert memory_tools(tool, {
        "content": row["old_content"], "expected_revision": row["new_sha256"],
    }).startswith("OK")
    assert path.read_bytes() == old


def test_concurrent_memory_writers_preserve_the_winner(tmp_path, memory_tools):
    path = tmp_path / "memory" / "scratchpad.md"
    path.write_bytes(b"working memory")
    revision = sha256(path.read_bytes()).hexdigest()
    ready = Barrier(2)

    def write(content):
        ready.wait(timeout=5)
        return memory_tools("update_scratchpad", {"content": content, "expected_revision": revision})

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(write, ["first", "second"]))
    assert sum(r.startswith("OK") for r in results) == 1
    assert sum("Revision conflict" in r for r in results) == 1
    row = json.loads(journal_path(path).read_text(encoding="utf-8"))
    assert path.read_bytes() == row["new_content"].encode("utf-8")


def test_identity_preserved_when_journal_cannot_be_written(tmp_path, memory_tools):
    path = tmp_path / "memory" / "identity.md"
    old = b"The existing identity. " * 30
    path.write_bytes(old)
    journal_path(path).mkdir()
    result = memory_tools("update_identity", {
        "content": "A substantial replacement text. " * 30,
        "expected_revision": sha256(old).hexdigest(),
    })
    assert result.startswith("⚠️")
    assert path.read_bytes() == old


def test_identity_shrink_warns_with_history_available(tmp_path, memory_tools):
    path = tmp_path / "memory" / "identity.md"
    old = b"The previous identity. " * 30
    path.write_bytes(old)
    revision = sha256(old).hexdigest()
    assert "at least 50" in memory_tools("update_identity", {"content": "", "expected_revision": revision})
    assert path.read_bytes() == old
    result = memory_tools("update_identity", {
        "content": "A deliberately shorter but meaningful statement of identity.",
        "expected_revision": revision,
    })
    assert result.startswith("OK")
    assert "SELF_OVERWRITE_NOTICE" in result
    assert json.loads(journal_path(path).read_text(encoding="utf-8"))["old_content"].encode("utf-8") == old
