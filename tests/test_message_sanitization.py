import pathlib
import tempfile

from ouroboros.utils import sanitize_owner_facing_text


def test_sanitize_owner_facing_text_strips_tool_syntax():
    raw = """Сейчас проверяю.

to=multi_tool_use.parallel  
{"tool_uses":[{"recipient_name":"functions.run_shell","parameters":{"cmd":["echo","x"]}}]}

Готово.
"""
    out = sanitize_owner_facing_text(raw)
    assert 'to=multi_tool_use.parallel' not in out
    assert '"tool_uses"' not in out
    assert '"recipient_name"' not in out
    assert out == "Сейчас проверяю.\n\nГотово."


def test_send_with_budget_sanitizes_before_logging_and_send(monkeypatch):
    from supervisor import telegram

    sent = []
    logged = []

    class DummyTG:
        def send_message(self, chat_id, text, parse_mode=""):
            sent.append(text)
            return True, ""

    monkeypatch.setattr(telegram, 'DRIVE_ROOT', pathlib.Path(tempfile.mkdtemp()))
    monkeypatch.setattr(telegram, '_TG', DummyTG())
    monkeypatch.setattr(telegram, 'load_state', lambda: {'owner_id': 1})
    monkeypatch.setattr(telegram, 'budget_line', lambda force=False: '')
    monkeypatch.setattr(telegram, 'split_telegram', lambda text: [text])
    monkeypatch.setattr(telegram, 'log_chat', lambda direction, chat_id, owner_id, text, scope='main': logged.append(text))

    telegram.send_with_budget(1, 'До\n\nto=functions.run_shell {"cmd":["echo","x"]}\n\nПосле')

    assert sent == ['До\n\nПосле']
    assert logged == ['До\n\nПосле']


def test_sanitize_owner_facing_text_normalizes_service_english():
    raw = """Owner should consider проверить контур.
Status: active
Progress: 6/8
⚠️ Task stuck (610s without progress). Restarting agent.
"""
    out = sanitize_owner_facing_text(raw)
    assert "Owner should consider" not in out
    assert "Status:" not in out
    assert "Progress:" not in out
    assert "Task stuck" not in out
    assert "Restarting agent" not in out
    assert "Стоит проверить" in out
    assert "Статус: active" in out
    assert "Прогресс: 6/8" in out
    assert "Задача зависла" in out
    assert "Перезапускаю агент." in out
