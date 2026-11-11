import ast
import datetime
import logging
import queue
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from supervisor.audio_stt import AudioTranscriptionError
from supervisor.workers import make_chat_task
from ouroboros.conversation import PERSONAL, WORK, conversation_root


def _run_launcher_poll(tmp_path, tg, owner_id, transcribe, *, selector=None, logged=None, personal=None):
    # Importing the launcher starts the supervisor. Run its actual poll loop once with fake I/O.
    launcher = Path(__file__).resolve().parents[1] / "colab_launcher.py"
    tree = ast.parse(launcher.read_text(encoding="utf-8"))
    loop = next(node for node in tree.body if isinstance(node, ast.While))
    prefixes = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "_TELEGRAM_AUDIO_PREFIXES"
    )
    loop.test = ast.Call(func=ast.Name(id="run_once", ctx=ast.Load()), args=[], keywords=[])
    program = ast.fix_missing_locations(ast.Module(body=[prefixes, loop], type_ignores=[]))

    state = {"owner_id": owner_id, "owner_chat_id": owner_id}
    clock = [100.0]
    thread = Mock()
    send = Mock()
    context = dict(
        datetime=datetime, deque=deque, time=SimpleNamespace(
            time=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ),
        log=logging.getLogger(__name__), _queue_mod=queue, _last_message_ts=100.0,
        run_once=Mock(side_effect=[True, False]), offset=1, _ACTIVE_MODE_SEC=300,
        DIAG_SLOW_CYCLE_SEC=0, DIAG_HEARTBEAT_SEC=0, TG=tg, DRIVE_ROOT=tmp_path,
        threading=SimpleNamespace(Thread=thread), AudioTranscriptionError=AudioTranscriptionError,
        rotate_chat_log_if_needed=Mock(), ensure_workers_healthy=Mock(), get_event_q=queue.Queue,
        enforce_task_timeouts=Mock(), enqueue_evolution_task_if_needed=Mock(), assign_tasks=Mock(),
        persist_queue_snapshot=Mock(), load_state=state.copy, save_state=state.update,
        log_chat=logged or Mock(), _consciousness=Mock(), _get_chat_agent=lambda: SimpleNamespace(_busy=False),
        send_with_budget=send, transcribe_telegram_audio=transcribe,
        PERSONAL=PERSONAL, WORK=WORK, conversation_root=conversation_root,
        select_conversation_mode=selector or Mock(return_value=WORK),
        make_chat_task=make_chat_task, enqueue_personal_chat=personal or Mock(),
    )
    exec(compile(program, str(launcher), "exec"), context)
    dispatched = [call.kwargs["args"][1] for call in thread.call_args_list]
    return state, dispatched, send


def _media_update(update_id, kind, user_id=10, caption="", *, edited=False):
    msg = {
        "chat": {"id": user_id}, "from": {"id": user_id}, "caption": caption,
        kind: {"file_id": f"file-{update_id}"},
    }
    return {"update_id": update_id, "edited_message" if edited else "message": msg}


@pytest.mark.parametrize("in_initial_poll", [True, False])
def test_burst_audio_updates_use_authorized_main_ingress_in_order(tmp_path, in_initial_poll):
    first = _media_update(1, "voice")
    following = [
        _media_update(2, "voice"),
        _media_update(3, "voice", user_id=99),
        _media_update(4, "audio", caption="Подпись"),
        _media_update(5, "video_note", edited=True),
    ]
    replies = {1: [first, *following] if in_initial_poll else [first], 6: []}
    if not in_initial_poll:
        replies[2] = following
    tg = SimpleNamespace(
        get_updates=Mock(side_effect=lambda *, offset, timeout: replies[offset]),
        download_file_base64=Mock(side_effect=lambda file_id, **kw: (file_id, "audio/ogg")),
    )
    transcribe = Mock(side_effect=lambda **kw: {"text": kw["audio_b64"]})

    state, dispatched, send = _run_launcher_poll(tmp_path, tg, 10, transcribe)

    assert state["tg_offset"] == 6
    assert [call.kwargs["offset"] for call in tg.get_updates.call_args_list] == (
        [1, 6] if in_initial_poll else [1, 2, 6]
    )
    assert [call.args[0] for call in tg.download_file_base64.call_args_list] == [
        "file-1", "file-2", "file-4", "file-5",
    ]
    assert [call.kwargs["kind"] for call in transcribe.call_args_list] == [
        "voice", "voice", "audio", "video_note",
    ]
    assert dispatched == [
        "[Голосовое сообщение]\nfile-1", "[Голосовое сообщение]\nfile-2",
        "Подпись\n\n[Аудио]\nfile-4", "[Кружок]\nfile-5",
    ]
    send.assert_not_called()


def test_first_voice_can_register_owner_without_sending_media(tmp_path):
    tg = SimpleNamespace(
        get_updates=Mock(return_value=[_media_update(1, "voice")]),
        download_file_base64=Mock(),
    )
    transcribe = Mock()
    state, dispatched, send = _run_launcher_poll(tmp_path, tg, None, transcribe)

    assert state["owner_id"] == 10
    assert not dispatched
    send.assert_called_once()
    tg.download_file_base64.assert_not_called()
    transcribe.assert_not_called()


def test_voice_after_text_burst_is_handled_even_if_transcription_fails(tmp_path):
    first = {"update_id": 1, "message": {"chat": {"id": 10}, "from": {"id": 10}, "text": "Текст"}}
    replies = {1: [first], 2: [_media_update(2, "voice"), _media_update(3, "voice")], 4: []}
    tg = SimpleNamespace(
        get_updates=Mock(side_effect=lambda *, offset, timeout: replies[offset]),
        download_file_base64=Mock(return_value=("data", "audio/ogg")),
    )
    transcribe = Mock(side_effect=[AudioTranscriptionError("provider timed out"), {"text": "Дальше"}])

    state, dispatched, send = _run_launcher_poll(tmp_path, tg, 10, transcribe)

    assert state["tg_offset"] == 4
    assert dispatched == ["Текст", "[Голосовое сообщение]\nДальше"]
    send.assert_called_once_with(10, "⚠️ Не удалось распознать голосовое: provider timed out")


def test_natural_mode_changes_route_first_and_last_personal_turns(tmp_path):
    working = "Проверь статус ветки."
    opening = "Есть кое-что личное, что хотелось бы с тобой обсудить."
    followup = "Спасибо, ещё хочу рассказать про вчера."
    closing = "На сегодня хватит, вернёмся к обычным делам."
    next_work = "Теперь покажи состояние сборки."
    texts = [working, opening, followup, closing, next_work]
    updates = [
        {"update_id": index, "message": {"text": text, "chat": {"id": 10}, "from": {"id": 10}}}
        for index, text in enumerate(texts, 1)
    ]
    replies = {1: updates[:1], 2: updates[1:], 6: []}
    tg = SimpleNamespace(get_updates=Mock(side_effect=lambda *, offset, timeout: replies[offset]))
    seen = []

    def select(state, chat_id, text, **kwargs):
        seen.append((bool(state.get("personal_chat_active")), text))
        return PERSONAL if text in (opening, followup) else WORK

    logged = Mock()
    personal = Mock()
    state, dispatched, _ = _run_launcher_poll(
        tmp_path, tg, 10, Mock(), selector=select, logged=logged, personal=personal,
    )
    assert [call.args[0]["text"] for call in personal.call_args_list] == [opening, followup, closing]
    assert dispatched == [working, next_work]
    assert [call.kwargs["conversation_mode"] for call in logged.call_args_list] == [
        WORK, PERSONAL, PERSONAL, PERSONAL, WORK,
    ]
    assert seen == [(False, working), (False, opening), (True, followup), (True, closing), (False, next_work)]
    assert state["personal_chat_active"] is False
