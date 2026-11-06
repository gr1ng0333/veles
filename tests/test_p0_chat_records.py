import json

from ouroboros.chat_records import chat_direction
from ouroboros.consolidator import DialogueConsolidator
from ouroboros.memory import Memory


def test_deployed_and_legacy_rows_have_the_same_authorship(tmp_path):
    rows = [{"role": "in", "text": "owner instruction"},
            {"role": "out", "text": "agent answer"},
            {"direction": "incoming", "text": "legacy owner"},
            {"direction": "outgoing", "text": "legacy agent"}]
    text = Memory(tmp_path).summarize_chat(rows)
    assert "←  owner instruction" in text and "→  agent answer" in text
    formatted = DialogueConsolidator(tmp_path, None)._format_entries(rows)
    assert "Creator: owner instruction" in formatted
    assert "Veles: agent answer" in formatted
    assert "Creator: legacy owner" in formatted
    assert "Veles: legacy agent" in formatted
    assert chat_direction({"role": "out", "direction": "in"}) == "out"
    assert chat_direction({}) == "unknown"


def test_writer_keeps_full_owner_message_and_new_reader_reads_old_rows(tmp_path, monkeypatch):
    from supervisor import telegram as tg
    monkeypatch.setattr(tg, "DRIVE_ROOT", tmp_path)
    long_text = "Длинное ТЗ: " + "а" * 6000
    tg.log_chat("in", 42, 42, long_text)
    tg.log_chat("out", 42, 0, "мой ответ")
    path = tmp_path / "logs/chat.jsonl"
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["text"] == long_text
    assert rows[0]["role"] == rows[0]["direction"] == "in"
    # Existing deployed rows are read without migration or rewriting.
    rows[1].pop("direction")
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    rendered = Memory(tmp_path).chat_history()
    assert long_text in rendered and "→" in rendered


def test_legacy_summary_attribution_is_disclosed_not_silently_rewritten(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    original = [{"type": "episode", "content": "legacy content"}]
    p = mem / "dialogue_blocks.json"
    p.write_text(json.dumps(original))
    rendered = DialogueConsolidator(tmp_path, None).render_for_context()
    assert "speaker attribution was not verified" in rendered
    assert "legacy content" in rendered
    assert json.loads(p.read_text()) == original
