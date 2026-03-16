from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import re
from datetime import datetime, timezone
from functools import partial
from typing import Any, Dict, Optional


_OUTBOX_ROOT = "artifacts/outbox"
_INBOX_ROOT = "artifacts/inbox"


def _slug(value: str, fallback: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9._-]+", "-", (value or "").strip().lower())).strip("-._") or fallback


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
    task_part = _slug(task_id or 'direct-chat', 'direct-chat')
    if content_kind:
        kind_part = _slug(content_kind, 'generic')
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
    stem = _slug(path.stem or kind_part, kind_part)
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


save_incoming_artifact = partial(save_artifact, source='telegram_inbox')


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
