import json

from ouroboros.codex_recovery import _try_extract_tool_calls_from_text
from ouroboros.codex_proxy_format import _output_to_chat_message


def test_recovery_extracts_recipient_name_inside_tool_uses_and_strips_preamble():
    text = """Проверяю полный пакет перед push.

to=multi_tool_use.parallel
```json
{"tool_uses":[{"recipient_name":"functions.run_shell","parameters":{"cmd":["bash","-lc","pytest -q"],"cwd":"/opt/veles"}}]}
```
"""
    tool_calls, cleaned = _try_extract_tool_calls_from_text(text)

    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "run_shell"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {
        "cmd": ["bash", "-lc", "pytest -q"],
        "cwd": "/opt/veles",
    }
    assert "to=multi_tool_use.parallel" not in cleaned
    assert "recipient_name" not in cleaned
    assert "Проверяю полный пакет" in cleaned


def test_output_to_chat_message_recovers_pseudo_tool_call_from_text_when_enabled(monkeypatch):
    monkeypatch.setenv("CODEX_TOOL_RECOVERY_ENABLED", "true")
    output_items = [
        {
            "type": "message",
            "content": [
                {
                    "type": "output_text",
                    "text": "to=multi_tool_use.parallel\n```json\n{\"tool_uses\":[{\"recipient_name\":\"functions.run_shell\",\"parameters\":{\"cmd\":[\"bash\",\"-lc\",\"echo ok\"],\"cwd\":\"/opt/veles\"}}]}\n```",
                }
            ],
        }
    ]

    msg = _output_to_chat_message(output_items)

    assert msg["tool_calls"] is not None
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["function"]["name"] == "run_shell"
    assert "to=multi_tool_use.parallel" not in (msg["content"] or "")
    assert "tool_uses" not in (msg["content"] or "")
