import json
import threading
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from ouroboros.consolidator import DialogueConsolidator, BLOCK_SIZE, MAX_SUMMARY_BLOCKS


@pytest.fixture
def drive_root(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "logs").mkdir()
    return tmp_path


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.chat.return_value = (
        {"role": "assistant", "content": "Episode summary: key decisions were made about architecture."},
        {"prompt_tokens": 500, "completion_tokens": 100, "cost": 0.0},
    )
    return llm


def _write_chat_lines(drive_root, count, start_idx=0):
    """Helper: write N mock chat.jsonl entries."""
    chat_path = drive_root / "logs" / "chat.jsonl"
    lines = []
    for i in range(count):
        entry = {
            "ts": f"2026-03-{20 + i // 100:02d}T{i % 24:02d}:00:00Z",
            "direction": "in" if i % 2 == 0 else "out",
            "text": f"Message {start_idx + i}: {'owner question' if i % 2 == 0 else 'agent response with details about the task'}",
        }
        lines.append(json.dumps(entry, ensure_ascii=False))
    with open(chat_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return len(lines)


def test_no_consolidation_below_threshold(drive_root, mock_llm):
    """Should not consolidate when fewer than 100 new messages."""
    c = DialogueConsolidator(drive_root, mock_llm)
    c._maybe_migrate_legacy()  # offset = 0
    _write_chat_lines(drive_root, 50)
    assert c.maybe_consolidate() is False
    mock_llm.chat.assert_not_called()


def test_consolidation_triggers_at_threshold(drive_root, mock_llm):
    """Should consolidate when 100+ new messages."""
    # Initialize consolidator first (sets offset to 0 since no chat yet)
    c = DialogueConsolidator(drive_root, mock_llm)
    c._maybe_migrate_legacy()  # offset = 0
    # Now write messages after initialization
    _write_chat_lines(drive_root, 110)
    assert c.maybe_consolidate() is True
    mock_llm.chat.assert_called_once()
    # Check blocks file created
    blocks = json.loads((drive_root / "memory" / "dialogue_blocks.json").read_text())
    assert len(blocks) == 1
    assert blocks[0]["type"] == "episode"


def test_multiple_consolidations(drive_root, mock_llm):
    """Multiple batches should create multiple blocks."""
    c = DialogueConsolidator(drive_root, mock_llm)
    c._maybe_migrate_legacy()  # offset = 0
    _write_chat_lines(drive_root, 110)
    c.maybe_consolidate()

    _write_chat_lines(drive_root, 110, start_idx=110)
    c.maybe_consolidate()

    blocks = json.loads((drive_root / "memory" / "dialogue_blocks.json").read_text())
    assert len(blocks) == 2


def test_era_compression_triggers(drive_root, mock_llm):
    """When >10 blocks, oldest 4 should be compressed into era."""
    # Create 11 blocks manually
    blocks_data = [
        {
            "ts": f"2026-03-{i:02d}T00:00:00Z",
            "type": "episode",
            "range": f"2026-03-{i:02d}",
            "message_count": 100,
            "content": f"Episode {i} summary with important details.",
        }
        for i in range(11)
    ]
    (drive_root / "memory" / "dialogue_blocks.json").write_text(
        json.dumps(blocks_data, ensure_ascii=False), encoding="utf-8"
    )
    _write_chat_lines(drive_root, 1200)  # enough lines
    # Set meta so offset matches
    (drive_root / "memory" / "dialogue_meta.json").write_text(
        json.dumps({"last_consolidated_offset": 1200}), encoding="utf-8"
    )

    c = DialogueConsolidator(drive_root, mock_llm)
    c._compress_eras()

    blocks = json.loads((drive_root / "memory" / "dialogue_blocks.json").read_text())
    # 4 oldest compressed into 1 era + 7 remaining = 8 blocks
    assert len(blocks) == 8
    assert blocks[0]["type"] == "era"


def test_render_for_context(drive_root, mock_llm):
    """render_for_context should produce formatted text."""
    blocks_data = [
        {
            "ts": "2026-03-01T00:00:00Z",
            "type": "era",
            "range": "2026-02-15 to 2026-03-01",
            "message_count": 400,
            "content": "Initial setup period.",
        },
        {
            "ts": "2026-03-15T00:00:00Z",
            "type": "episode",
            "range": "2026-03-10 — 2026-03-15",
            "message_count": 100,
            "content": "Copilot proxy work.",
        },
    ]
    (drive_root / "memory" / "dialogue_blocks.json").write_text(
        json.dumps(blocks_data, ensure_ascii=False), encoding="utf-8"
    )
    c = DialogueConsolidator(drive_root, None)
    text = c.render_for_context()
    assert "Era" in text
    assert "Episode" in text
    assert "Copilot" in text
    assert len(text) > 50


def test_render_empty(drive_root):
    """Empty consolidator should return empty string."""
    c = DialogueConsolidator(drive_root, None)
    assert c.render_for_context() == ""


def test_legacy_migration(drive_root, mock_llm):
    """Should migrate dialogue_summary.md to blocks format."""
    (drive_root / "memory" / "dialogue_summary.md").write_text(
        "# Old Summary\nSome legacy dialogue summary content here.",
        encoding="utf-8",
    )
    c = DialogueConsolidator(drive_root, mock_llm)
    c._maybe_migrate_legacy()

    blocks = json.loads((drive_root / "memory" / "dialogue_blocks.json").read_text())
    assert len(blocks) == 1
    assert blocks[0]["type"] == "era"
    assert "Old Summary" in blocks[0]["content"] or "legacy" in blocks[0]["range"]


def test_legacy_migration_structured(drive_root, mock_llm):
    """Should parse structured episode headers from legacy summary."""
    content = (
        "### Episode: 2026-03-01\nFirst episode details.\n\n"
        "### Era: 2026-02-01 to 2026-02-28\nEra summary.\n"
    )
    (drive_root / "memory" / "dialogue_summary.md").write_text(content, encoding="utf-8")
    c = DialogueConsolidator(drive_root, mock_llm)
    c._maybe_migrate_legacy()

    blocks = json.loads((drive_root / "memory" / "dialogue_blocks.json").read_text())
    assert len(blocks) == 2
    assert blocks[0]["type"] == "episode"
    assert blocks[1]["type"] == "era"


def test_force_consolidate(drive_root, mock_llm):
    """force=True should consolidate even below threshold."""
    c = DialogueConsolidator(drive_root, mock_llm)
    c._maybe_migrate_legacy()  # offset = 0
    _write_chat_lines(drive_root, 30)
    assert c.maybe_consolidate(force=True) is True
    mock_llm.chat.assert_called_once()


def test_llm_failure_graceful(drive_root):
    """If LLM fails, consolidation should not crash."""
    llm = MagicMock()
    llm.chat.side_effect = Exception("LLM unavailable")

    c = DialogueConsolidator(drive_root, llm)
    c._maybe_migrate_legacy()  # offset = 0
    _write_chat_lines(drive_root, 110)
    result = c.maybe_consolidate()
    assert result is False


def test_thread_safety(drive_root, mock_llm):
    """Concurrent calls should not corrupt blocks file."""
    c = DialogueConsolidator(drive_root, mock_llm)
    c._maybe_migrate_legacy()  # offset = 0
    _write_chat_lines(drive_root, 500)
    errors = []

    def run():
        try:
            c.maybe_consolidate()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(errors) == 0
    # Blocks file should be valid JSON
    blocks = json.loads((drive_root / "memory" / "dialogue_blocks.json").read_text())
    assert isinstance(blocks, list)


def test_init_fresh_no_chat(drive_root, mock_llm):
    """First run without chat.jsonl should initialize cleanly."""
    c = DialogueConsolidator(drive_root, mock_llm)
    c._maybe_migrate_legacy()

    # Should create empty blocks
    blocks = json.loads((drive_root / "memory" / "dialogue_blocks.json").read_text())
    assert blocks == []
    meta = json.loads((drive_root / "memory" / "dialogue_meta.json").read_text())
    assert meta["last_consolidated_offset"] == 0


def test_init_fresh_with_existing_chat(drive_root, mock_llm):
    """First run with existing chat should set offset to current size."""
    _write_chat_lines(drive_root, 200)
    c = DialogueConsolidator(drive_root, mock_llm)
    c._maybe_migrate_legacy()

    meta = json.loads((drive_root / "memory" / "dialogue_meta.json").read_text())
    assert meta["last_consolidated_offset"] == 200
