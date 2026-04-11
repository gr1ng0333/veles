import pathlib
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

from ouroboros.artifacts import _INBOX_CONFIRMATION_STATE, list_incoming_artifacts
from supervisor.doc_payload import document_to_text_payload as DOCUMENT_TO_TEXT


def _make_send_and_tg(raw='cHJpbnQoMSkK', file_map=None):
    send_calls = []
    send_budget = lambda chat_id, text: send_calls.append((chat_id, text))
    if file_map is None:
        file_map = {'f1': (raw, 'text/x-python'), 'f2': (raw, 'text/x-python'), 'f3': (raw, 'text/x-python')}
    tg = SimpleNamespace(download_file_base64=lambda file_id, max_bytes=10_000_000: file_map.get(file_id, (None, '')))
    return send_calls, send_budget, tg


def test_incoming_files_routing_archives_deferred_batches_and_immediate_captioned_files():
    _INBOX_CONFIRMATION_STATE.clear()
    send_calls, send_budget, tg = _make_send_and_tg()
    tmp = pathlib.Path(tempfile.mkdtemp())

    # Deferred files (no caption) - use fast window via mock
    with mock.patch('supervisor.doc_payload.schedule_inbox_confirmation',
                    side_effect=lambda chat_id, file_name, sender: __import__('ouroboros.artifacts', fromlist=['schedule_inbox_confirmation']).schedule_inbox_confirmation(
                        chat_id, file_name, sender, window_sec=0.05)):

        payload, image_data, handled = DOCUMENT_TO_TEXT(
            {'file_id': 'f1', 'file_name': 'one.py', 'mime_type': 'text/x-python'},
            '', tg, 42, tmp, send_budget, 1001,
        )
        assert handled is True
        assert payload is None
        assert image_data is None

        DOCUMENT_TO_TEXT(
            {'file_id': 'f2', 'file_name': 'two.py', 'mime_type': 'text/x-python'},
            '', tg, 42, tmp, send_budget, 1002,
        )

    time.sleep(0.12)
    recent = [item for item in send_calls if item[0] == 42]
    assert len(recent) == 1
    assert 'one.py' in recent[0][1]
    assert 'two.py' in recent[0][1]
    assert '(2)' in recent[0][1]

    inbox = list_incoming_artifacts(tmp, limit=10, chat_id=42)
    assert inbox['count'] == 2
    assert {item['filename'] for item in inbox['items']} == {'one.py', 'two.py'}
    assert {item['metadata']['activation_mode'] for item in inbox['items']} == {'deferred'}

    # Immediate file (with caption)
    payload, image_data, handled = DOCUMENT_TO_TEXT(
        {'file_id': 'f3', 'file_name': 'solution.py', 'mime_type': 'text/x-python'},
        'проверь код', tg, 43, tmp, send_budget, 1003,
    )
    assert handled is True
    assert image_data is None
    assert '📎 Файл: solution.py' in payload
    inbox = list_incoming_artifacts(tmp, limit=10, chat_id=43)
    assert inbox['count'] == 1
    assert inbox['items'][0]['filename'] == 'solution.py'
    assert inbox['items'][0]['metadata']['activation_mode'] == 'immediate'


def test_incoming_zip_with_caption_is_archived_and_exposed_as_payload():
    send_calls = []
    captured_max_bytes = []

    def send_budget(chat_id, text):
        send_calls.append((chat_id, text))

    def download_file_base64(file_id, max_bytes=10_000_000):
        captured_max_bytes.append(max_bytes)
        return ('UEsDBAoAAAAAAA==', 'application/zip')

    tg = SimpleNamespace(download_file_base64=download_file_base64)
    tmp = pathlib.Path(tempfile.mkdtemp())

    payload, image_data, handled = DOCUMENT_TO_TEXT(
        {'file_id': 'zip1', 'file_name': 'bank.zip', 'mime_type': 'application/zip'},
        'импортируй архив', tg, 44, tmp, send_budget, 1004,
    )

    assert handled is True
    assert image_data is None
    assert '📦 Архив: bank.zip' in payload
    assert 'Сохранён:' in payload
    assert captured_max_bytes == [100_000_000]
    assert send_calls == []

    inbox = list_incoming_artifacts(tmp, limit=10, chat_id=44)
    assert inbox['count'] == 1
    assert inbox['items'][0]['filename'] == 'bank.zip'
    assert inbox['items'][0]['metadata']['activation_mode'] == 'immediate'


def test_incoming_zip_download_failure_reports_real_error():
    send_calls = []
    send_budget = lambda chat_id, text: send_calls.append((chat_id, text))
    tg = SimpleNamespace(download_file_base64=lambda file_id, max_bytes=10_000_000: (None, ''))
    tmp = pathlib.Path(tempfile.mkdtemp())

    payload, image_data, handled = DOCUMENT_TO_TEXT(
        {'file_id': 'zip2', 'file_name': 'broken.zip', 'mime_type': 'application/zip'},
        'обработай архив', tg, 45, tmp, send_budget, 1005,
    )

    assert handled is True
    assert payload is None
    assert image_data is None
    assert len(send_calls) == 1
    assert 'Не удалось принять архив .zip' in send_calls[0][1]

    inbox = list_incoming_artifacts(tmp, limit=10, chat_id=45)
    assert inbox['count'] == 0
