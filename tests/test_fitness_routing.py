from __future__ import annotations

import json
import queue
from pathlib import Path

from ouroboros.fitness_consciousness import FitnessConsciousness
from supervisor import events as supervisor_events
from supervisor import telegram


def test_log_chat_scope_fitness_uses_isolated_log(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(telegram, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(telegram, "load_state", lambda: {"session_id": "sess-1"})

    telegram.log_chat("in", 1, 2, "яблоко 200 г", scope="fitness")

    fitness_log = tmp_path / "fitness" / "logs" / "chat.jsonl"
    assert fitness_log.exists()
    payload = json.loads(fitness_log.read_text(encoding="utf-8").strip())
    assert payload["direction"] == "in"
    assert payload["text"] == "яблоко 200 г"
    assert not (tmp_path / "logs" / "chat.jsonl").exists()


def test_handle_send_message_passes_chat_scope() -> None:
    calls = []

    class Ctx:
        def send_with_budget(self, chat_id, text, log_text=None, fmt="", is_progress=False, chat_scope="main"):
            calls.append({
                "chat_id": chat_id,
                "text": text,
                "log_text": log_text,
                "fmt": fmt,
                "is_progress": is_progress,
                "chat_scope": chat_scope,
            })

        DRIVE_ROOT = Path("/tmp")

        @staticmethod
        def append_jsonl(*args, **kwargs):
            raise AssertionError("append_jsonl should not be called on success")

    supervisor_events._handle_send_message(
        {"chat_id": 5, "text": "fit reply", "chat_scope": "fitness", "format": "markdown"},
        Ctx(),
    )

    assert calls == [{
        "chat_id": 5,
        "text": "fit reply",
        "log_text": None,
        "fmt": "markdown",
        "is_progress": False,
        "chat_scope": "fitness",
    }]


def test_handle_owner_message_clears_flags_and_queues_reply(tmp_path: Path, monkeypatch) -> None:
    shared_state = {
        "owner_id": 77,
        "session_id": "sess-2",
        "fitness_awaiting_reply": True,
        "fitness_next_message": True,
    }

    def fake_load_state():
        return dict(shared_state)

    def fake_save_state(st):
        shared_state.clear()
        shared_state.update(st)

    monkeypatch.setattr("ouroboros.fitness_consciousness.load_state", fake_load_state)
    monkeypatch.setattr("ouroboros.fitness_consciousness.save_state", fake_save_state)
    monkeypatch.setattr(
        "ouroboros.fitness_consciousness.run_llm_loop",
        lambda **kwargs: ("Сделай сегодня 20 минут прогулки и 3 подхода приседаний.", {"cost_usd": 0.12}, None),
    )

    daemon = FitnessConsciousness(tmp_path, Path("/opt/veles"), queue.Queue(), lambda: 123)
    captured = []
    monkeypatch.setattr(daemon, "_queue_owner_message", lambda text, reason="": captured.append((text, reason)) or "ok")

    result = daemon.handle_owner_message("что у меня на сегодня по тренировке?")

    assert result == "OK: fitness owner message handled."
    assert shared_state["fitness_awaiting_reply"] is False
    assert shared_state["fitness_next_message"] is False
    assert captured == [("Сделай сегодня 20 минут прогулки и 3 подхода приседаний.", "owner_message_reply_fallback")]

    chat_log = tmp_path / "fitness" / "logs" / "chat.jsonl"
    payload = json.loads(chat_log.read_text(encoding="utf-8").strip())
    assert payload["direction"] == "in"
    assert "сегодня" in payload["text"]
