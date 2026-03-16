from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import re
import threading
from datetime import datetime, timezone
from functools import partial
from typing import Any, Callable, Dict, List, Optional


_OUTBOX_ROOT = "artifacts/outbox"
_INBOX_ROOT = "artifacts/inbox"


def _write_artifact(
    drive_root: pathlib.Path,
    *,
    root_dir: str,
    filename: str,
    payload: bytes,
    content_kind: str,
    source: str,
    task_id: str,
    chat_id: Optional[int],
    mime_type: str,
    caption: str,
    related_message: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    base_dir = drive_root / root_dir / now.strftime('%Y/%m/%d')
    task_part = re.sub(r'-+', '-', re.sub(r'[^a-z0-9._-]+', '-', (task_id or 'direct-chat').strip().lower())).strip('-._') or 'direct-chat'
    if content_kind:
        kind_part = re.sub(r'-+', '-', re.sub(r'[^a-z0-9._-]+', '-', (content_kind or '').strip().lower())).strip('-._') or 'generic'
    else:
        suffix_hint = pathlib.Path(filename or '').suffix.lower()
        if suffix_hint == '.py':
            kind_part = 'python'
        elif suffix_hint in {'.html', '.htm'}:
            kind_part = 'html'
        elif suffix_hint in {'.json', '.yaml', '.yml', '.toml', '.ini', '.cfg'}:
            kind_part = 'config'
        elif suffix_hint in {'.csv', '.tsv'}:
            kind_part = 'table'
        elif suffix_hint in {'.txt', '.md', '.rst'} or (mime_type or '').startswith('text/'):
            kind_part = 'text'
        else:
            kind_part = 'generic'
    target_dir = base_dir / task_part / kind_part
    target_dir.mkdir(parents=True, exist_ok=True)

    path = pathlib.Path((filename or '').strip())
    stem = re.sub(r'-+', '-', re.sub(r'[^a-z0-9._-]+', '-', ((path.stem or kind_part) or '').strip().lower())).strip('-._') or kind_part
    suffix = path.suffix[:20].lower()
    if not suffix or not re.fullmatch(r"\.[A-Za-z0-9._-]{1,19}", suffix):
        suffix = '.bin'
    file_path = target_dir / f'{stem}{suffix}'
    if file_path.exists():
        digest = hashlib.sha256(payload).hexdigest()[:10]
        file_path = target_dir / f'{stem}-{digest}{suffix}'

    file_path.write_bytes(payload)
    meta = {
        'ts': now.isoformat(),
        'filename': file_path.name,
        'relative_path': str(file_path.relative_to(drive_root)),
        'bytes': len(payload),
        'sha256': hashlib.sha256(payload).hexdigest(),
        'mime_type': mime_type,
        'content_kind': content_kind,
        'source': source,
        'task_id': task_id or '',
        'chat_id': int(chat_id or 0),
        'caption': caption or '',
        'related_message': related_message or '',
    }
    if metadata:
        meta['metadata'] = metadata
    file_path.with_suffix(file_path.suffix + '.meta.json').write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    return meta


def save_artifact(
    ctx_or_root,
    *,
    filename: str,
    data: bytes | None = None,
    content: str = '',
    file_base64: str = '',
    content_kind: str = 'generic',
    source: str = 'tool',
    archive_root: str = _OUTBOX_ROOT,
    task_id: str = '',
    chat_id: Optional[int] = None,
    mime_type: str = 'application/octet-stream',
    caption: str = '',
    related_message: str = '',
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any] | str:
    payload = data if data is not None else (base64.b64decode(file_base64) if file_base64 else content.encode('utf-8') if content else None)
    if payload is None:
        return '⚠️ save_artifact requires non-empty content.'
    drive_root = pathlib.Path(getattr(ctx_or_root, 'drive_root', ctx_or_root))
    return _write_artifact(
        drive_root,
        root_dir=archive_root,
        filename=filename,
        payload=payload,
        content_kind=content_kind,
        source=source,
        task_id=task_id or getattr(ctx_or_root, 'task_id', '') or '',
        chat_id=chat_id if chat_id is not None else getattr(ctx_or_root, 'current_chat_id', None),
        mime_type=mime_type,
        caption=caption,
        related_message=related_message,
        metadata=metadata,
    )


save_incoming_artifact = partial(save_artifact, source='telegram_inbox', archive_root=_INBOX_ROOT)


def list_incoming_artifacts(
    ctx_or_root,
    *,
    limit: int = 10,
    chat_id: Optional[int] = None,
    content_kind: str = '',
    filename_contains: str = '',
) -> Dict[str, Any]:
    drive_root = pathlib.Path(getattr(ctx_or_root, 'drive_root', ctx_or_root))
    inbox_root = drive_root / _INBOX_ROOT
    target_chat = chat_id if chat_id is not None else getattr(ctx_or_root, 'current_chat_id', None)
    limit = max(1, min(int(limit or 10), 100))
    kind_filter = (content_kind or '').strip().lower()
    name_filter = (filename_contains or '').strip().lower()
    items = []
    if inbox_root.exists():
        for meta_path in inbox_root.rglob('*.meta.json'):
            try:
                meta = json.loads(meta_path.read_text(encoding='utf-8'))
            except Exception:
                continue
            if target_chat is not None and int(meta.get('chat_id') or 0) != int(target_chat):
                continue
            if kind_filter and str(meta.get('content_kind') or '').strip().lower() != kind_filter:
                continue
            if name_filter and name_filter not in str(meta.get('filename') or '').strip().lower():
                continue
            items.append(meta)
    items.sort(key=lambda item: str(item.get('ts') or ''), reverse=True)
    result = items[:limit]
    return {
        'status': 'ok',
        'count': len(result),
        'total_matches': len(items),
        'items': result,
    }



_INBOX_CONFIRMATION_WINDOW_SEC = 15.0
_INBOX_CONFIRMATION_LOCK = threading.Lock()
_INBOX_CONFIRMATION_STATE: Dict[int, Dict[str, Any]] = {}


def schedule_inbox_confirmation(
    chat_id: int,
    file_name: str,
    sender: Callable[[int, str], None],
    window_sec: float = _INBOX_CONFIRMATION_WINDOW_SEC,
) -> None:
    chat_id = int(chat_id)
    file_name = str(file_name or 'file')
    with _INBOX_CONFIRMATION_LOCK:
        state = _INBOX_CONFIRMATION_STATE.get(chat_id)
        if state is None:
            def _send_summary() -> None:
                with _INBOX_CONFIRMATION_LOCK:
                    current = _INBOX_CONFIRMATION_STATE.pop(chat_id, None)
                if not current:
                    return
                names = [
                    str(name or 'file')
                    for name in (current.get('file_names') or [])
                    if str(name or '').strip()
                ]
                if not names:
                    sender(
                        chat_id,
                        '📥 Файлы сохранены во входящий архив и пока не отправлены в обработку.\n'
                        'Можешь потом сказать: посмотри 10 последних загруженных файлов.',
                    )
                    return
                total = len(names)
                preview = names[:6]
                lines = [f'• {name}' for name in preview]
                if total > len(preview):
                    lines.append(f'• … и ещё {total - len(preview)}')
                noun = 'Файл сохранён' if total == 1 else 'Файлы сохранены'
                suffix = 'не отправлен' if total == 1 else 'не отправлены'
                sender(
                    chat_id,
                    f'📥 {noun} во входящий архив и пока {suffix} в обработку ({total}):\n'
                    + '\n'.join(lines)
                    + '\nМожешь потом сказать: посмотри 10 последних загруженных файлов.',
                )

            timer = threading.Timer(float(window_sec), _send_summary)
            timer.daemon = True
            _INBOX_CONFIRMATION_STATE[chat_id] = {
                'file_names': [file_name],
                'timer': timer,
            }
            timer.start()
            return
        state.setdefault('file_names', []).append(file_name)
