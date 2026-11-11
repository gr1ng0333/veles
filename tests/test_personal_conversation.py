import json
from types import SimpleNamespace
from unittest.mock import Mock

from ouroboros.context import build_startup_sections
from ouroboros.memory import Memory
from ouroboros.tools.control import _chat_history
from ouroboros.tools.memory_search import _memory_search
from ouroboros.tools.registry import ToolContext
from ouroboros.utils import append_jsonl
from supervisor import conversation_mode, queue, telegram
from supervisor.events import _find_duplicate_task, _handle_send_message


def test_personal_history_is_deep_verbatim_and_scoped(tmp_path):
    work = Memory(tmp_path)
    work.ensure_files()
    work.identity_path().write_text("Shared identity", encoding="utf-8")
    work.scratchpad_path().write_text("Shared scratchpad", encoding="utf-8")
    private = Memory(tmp_path / "personal")
    for index in range(12):
        append_jsonl(work.logs_path("chat.jsonl"), {
            "chat_id": 42, "role": "in", "text": f"Рабочий случай {index}",
        })
    for index in range(130):
        append_jsonl(private.logs_path("chat.jsonl"), {
            "chat_id": 42, "role": "out", "ts": "2026-11-10T12:00:00+00:00",
            "text": f"Личный разговор {index} уникальная тема " + ("д" * 900 if index == 0 else ""),
        })
    append_jsonl(private.logs_path("chat.jsonl"), {
        "chat_id": 42, "role": "in", "ts": "2026-11-11T12:00:01+00:00", "text": "Текущая реплика",
    })
    env = SimpleNamespace(repo_dir=tmp_path, drive_root=tmp_path,
                          repo_path=lambda rel: tmp_path / rel, drive_path=lambda rel: tmp_path / rel)
    task = {"chat_id": 42, "conversation_mode": "personal", "received_at": "2026-11-11T12:00:00+00:00"}

    def startup(task):
        return "\n".join(text for block in build_startup_sections(env, work, task) for _, text in block)

    personal_prompt = startup(task)
    assert "Shared identity" in personal_prompt and "Shared scratchpad" in personal_prompt
    assert "Личный разговор 0 уникальная тема " + "д" * 900 in personal_prompt
    assert "Личный разговор 129" in personal_prompt and "Текущая реплика" not in personal_prompt
    assert "Рабочий случай 0\n" not in personal_prompt and "Рабочий случай 11" in personal_prompt
    assert "Личный разговор" not in startup({"chat_id": 42})

    working_tools = ToolContext(tmp_path, tmp_path, current_chat_id=42)
    personal_tools = ToolContext(tmp_path, tmp_path, current_chat_id=42, conversation_mode="personal")
    assert "Личный разговор" not in _chat_history(working_tools)
    assert "Личный разговор" in _chat_history(personal_tools)
    assert "no results" in _memory_search(working_tools, "уникальная тема")
    assert "Личный разговор" in _memory_search(personal_tools, "уникальная тема")


def test_personal_queue_payload_stays_in_its_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(queue, "PENDING", [])
    monkeypatch.setattr(queue, "RUNNING", {})
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "QUEUE_SEQ_COUNTER_REF", {"value": 0})
    monkeypatch.setattr(queue, "QUEUE_SNAPSHOT_PATH", tmp_path / "state/queue_snapshot.json")
    monkeypatch.setattr(queue, "load_state", dict)
    monkeypatch.setattr(queue, "save_state", lambda state: None)
    for ident, text in (("z-first", "первая личная реплика"), ("a-second", "вторая личная реплика")):
        queue.enqueue_personal_chat({"id": ident, "chat_id": 42, "type": "task", "text": text})
    queue.persist_queue_snapshot()
    assert "личная реплика" not in queue.QUEUE_SNAPSHOT_PATH.read_text(encoding="utf-8")
    queue.PENDING.clear()
    assert queue.restore_pending_from_snapshot() == 2
    assert [t["id"] for t in queue.PENDING] == ["z-first", "a-second"]


def test_personal_reply_uses_its_task_mode_after_the_owner_returns_to_work(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(telegram, "TOTAL_BUDGET_LIMIT", 1)
    monkeypatch.setattr(telegram, "get_tg", Mock())
    monkeypatch.setattr(telegram, "load_state", lambda: {"personal_chat_active": False})
    monkeypatch.setattr(telegram, "save_state", Mock())
    monkeypatch.setattr(telegram, "_send_markdown_telegram", lambda *args: (True, "ok"))
    ctx = SimpleNamespace(send_with_budget=telegram.send_with_budget, DRIVE_ROOT=tmp_path)
    _handle_send_message({"chat_id": 42, "text": "Личный ответ", "conversation_mode": "personal"}, ctx)
    assert "Личный ответ" in (tmp_path / "personal/logs/chat.jsonl").read_text(encoding="utf-8")
    assert not (tmp_path / "logs/chat.jsonl").exists()


def test_work_deduplication_does_not_send_personal_tasks_to_the_model(monkeypatch):
    from ouroboros import llm

    client = Mock()
    client.chat.return_value = ({"content": "NONE"}, {})
    monkeypatch.setattr(llm, "LLMClient", lambda: client)
    private = {"id": "private", "text": "личная реплика", "conversation_mode": "personal"}
    work = {"id": "work", "text": "рабочая задача"}
    _find_duplicate_task("новая задача", [private, work], {"private": {"task": private}})
    prompt = str(client.chat.call_args.kwargs["messages"])
    assert "рабочая задача" in prompt and "личная реплика" not in prompt


def test_selector_uses_separate_model_request_and_respects_current_mode(monkeypatch):
    client = Mock()
    client.default_model.return_value = "codex/test"
    client.chat.return_value = ({"content": "personal"}, {})
    monkeypatch.setattr(conversation_mode, "LLMClient", lambda: client)
    assert conversation_mode._classify("personal", "Спасибо, что выслушал.", None) == "personal"
    args = client.chat.call_args.kwargs
    assert args["reasoning_effort"] == "low"
    assert "tools" not in args
    assert json.loads(args["messages"][-1]["content"]) == {
        "mode": "personal", "message": "Спасибо, что выслушал.",
    }
