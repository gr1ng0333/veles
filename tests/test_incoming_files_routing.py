import pathlib
import tempfile
import time
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

from ouroboros.artifacts import _INBOX_CONFIRMATION_STATE, list_incoming_artifacts, save_incoming_artifact, schedule_inbox_confirmation


BOOTSTRAP_MARKER = '# ----------------------------\n# 5) Bootstrap repo'


source = pathlib.Path('/opt/veles/colab_launcher.py').read_text(encoding='utf-8')
start = source.index('def _document_to_text_payload')
end = source.index(BOOTSTRAP_MARKER)
snippet = source[start:end]
send_calls = []
ns = {
    'Any': Any,
    'Dict': Dict,
    'Optional': Optional,
    'Tuple': Tuple,
    'pathlib': pathlib,
    'save_incoming_artifact': save_incoming_artifact,
    'os': __import__('os'),
    'send_calls': send_calls,
    'send_with_budget': lambda chat_id, text: send_calls.append((chat_id, text)),
    'schedule_inbox_confirmation': lambda chat_id, file_name, sender: schedule_inbox_confirmation(chat_id, file_name, sender, window_sec=0.05),
    'TelegramClient': object,
}
exec(snippet, ns)
DOCUMENT_TO_TEXT = ns['_document_to_text_payload']


def test_incoming_files_routing_archives_deferred_batches_and_immediate_captioned_files():
    send_calls.clear()
    _INBOX_CONFIRMATION_STATE.clear()
    tmp = pathlib.Path(tempfile.mkdtemp())
    raw = 'cHJpbnQoMSkK'
    tg = SimpleNamespace(download_file_base64=lambda file_id, max_bytes=10_000_000: {
        'f1': (raw, 'text/x-python'),
        'f2': (raw, 'text/x-python'),
        'f3': (raw, 'text/x-python'),
    }.get(file_id, (None, '')))

    payload, image_data, handled = DOCUMENT_TO_TEXT(
        {'file_id': 'f1', 'file_name': 'one.py', 'mime_type': 'text/x-python'},
        '',
        tg,
        42,
        tmp,
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
        1003,
    )
    assert handled is True
    assert image_data is None
    assert '📎 Файл: solution.py' in payload
    inbox = list_incoming_artifacts(tmp, limit=10, chat_id=43)
    assert inbox['count'] == 1
    assert inbox['items'][0]['filename'] == 'solution.py'
    assert inbox['items'][0]['metadata']['activation_mode'] == 'immediate'
