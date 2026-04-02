import pathlib
import tempfile
import time
from types import SimpleNamespace

from ouroboros.artifacts import _INBOX_CONFIRMATION_STATE, list_incoming_artifacts, schedule_inbox_confirmation
from supervisor.doc_payload import document_to_text_payload as DOCUMENT_TO_TEXT


def test_incoming_files_routing_archives_deferred_batches_and_immediate_captioned_files():
    send_calls = []
    _INBOX_CONFIRMATION_STATE.clear()
    tmp = pathlib.Path(tempfile.mkdtemp())
    raw = 'cHJpbnQoMSkK'
    tg = SimpleNamespace(download_file_base64=lambda file_id, max_bytes=10_000_000: {
        'f1': (raw, 'text/x-python'),
        'f2': (raw, 'text/x-python'),
        'f3': (raw, 'text/x-python'),
    }.get(file_id, (None, '')))
    send_budget = lambda chat_id, text: send_calls.append((chat_id, text))

    payload, image_data, handled = DOCUMENT_TO_TEXT(
        {'file_id': 'f1', 'file_name': 'one.py', 'mime_type': 'text/x-python'},
        '',
        tg,
        42,
        tmp,
        send_budget,
        1001,
    )
    assert handled is True
    assert payload is None
    assert image_data is None

    DOCUMENT_TO_TEXT(
        {'file_id': 'f2', 'file_name': 'two.py', 'mime_type': 'text/x-python'},
        '',
        tg,
        42,
        tmp,
        send_budget,
        1002,
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

    payload, image_data, handled = DOCUMENT_TO_TEXT(
        {'file_id': 'f3', 'file_name': 'solution.py', 'mime_type': 'text/x-python'},
        'проверь код',
        tg,
        43,
        tmp,
        send_budget,
        1003,
    )
    assert handled is True
    assert image_data is None
    assert '📎 Файл: solution.py' in payload
    inbox = list_incoming_artifacts(tmp, limit=10, chat_id=43)
    assert inbox['count'] == 1
    assert inbox['items'][0]['filename'] == 'solution.py'
    assert inbox['items'][0]['metadata']['activation_mode'] == 'immediate'
