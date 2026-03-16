import pathlib
import tempfile
from typing import Any, Dict, Optional, Tuple

from ouroboros.artifacts import list_incoming_artifacts, save_incoming_artifact


BOOTSTRAP_MARKER = '# ----------------------------\n# 5) Bootstrap repo'


def _load_document_helpers():
    source = pathlib.Path('/opt/veles/colab_launcher.py').read_text(encoding='utf-8')
    start = source.index('def _is_supported_image_mime')
    end = source.index(BOOTSTRAP_MARKER)
    snippet = source[start:end]
    ns = {
        'Any': Any,
        'Dict': Dict,
        'Optional': Optional,
        'Tuple': Tuple,
        'pathlib': pathlib,
        'save_incoming_artifact': save_incoming_artifact,
        'os': __import__('os'),
        'send_with_budget': lambda chat_id, text: None,
        'TelegramClient': object,
    }
    exec(snippet, ns)
    return ns['_document_to_text_payload']


class DummyTG:
    def __init__(self, payload_map):
        self.payload_map = payload_map

    def download_file_base64(self, file_id, max_bytes=10_000_000):
        return self.payload_map.get(file_id, (None, ''))


DOCUMENT_TO_TEXT = _load_document_helpers()


def test_document_without_caption_goes_only_to_inbox():
    tmp = pathlib.Path(tempfile.mkdtemp())
    raw = 'cHJpbnQoMSkK'
    tg = DummyTG({'f1': (raw, 'text/x-python')})
    payload, image_data, handled = DOCUMENT_TO_TEXT(
        {'file_id': 'f1', 'file_name': 'solution.py', 'mime_type': 'text/x-python'},
        '',
        tg,
        42,
        tmp,
        1001,
    )
    assert handled is True
    assert payload is None
    assert image_data is None
    inbox = list_incoming_artifacts(tmp, limit=5, chat_id=42)
    assert inbox['count'] == 1
    assert inbox['items'][0]['filename'] == 'solution.py'
    assert inbox['items'][0]['metadata']['activation_mode'] == 'deferred'


def test_document_with_caption_is_archived_and_activated():
    tmp = pathlib.Path(tempfile.mkdtemp())
    raw = 'cHJpbnQoMSkK'
    tg = DummyTG({'f1': (raw, 'text/x-python')})
    payload, image_data, handled = DOCUMENT_TO_TEXT(
        {'file_id': 'f1', 'file_name': 'solution.py', 'mime_type': 'text/x-python'},
        'проверь код',
        tg,
        42,
        tmp,
        1002,
    )
    assert handled is True
    assert image_data is None
    assert '📎 Файл: solution.py' in payload
    inbox = list_incoming_artifacts(tmp, limit=5, chat_id=42)
    assert inbox['count'] == 1
    assert inbox['items'][0]['metadata']['activation_mode'] == 'immediate'
