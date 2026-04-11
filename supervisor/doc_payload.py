"""
Document-to-text payload normalization for incoming Telegram files.

Extracted from colab_launcher.py to keep the launcher under the 1000-line limit.
"""
from __future__ import annotations

import os
import pathlib
from typing import Any, Callable, Dict, Optional, Tuple

from ouroboros.artifacts import save_incoming_artifact, schedule_inbox_confirmation
from ouroboros.doc_ingest import ingest_legacy_word_document


def document_to_text_payload(
    doc: Dict[str, Any],
    caption: str,
    tg: Any,  # TelegramClient
    chat_id: int,
    drive_root: pathlib.Path,
    send_with_budget: Callable,
    message_id: int = 0,
) -> Tuple[Optional[str], Optional[Tuple[str, str, str]], bool]:
    """Normalize Telegram document into either text augmentation or image payload.

    New rule: every incoming file is archived to artifacts/inbox first.
    Files without caption stay deferred in inbox and are not injected into LLM context.

    Returns: (text_override, image_data, handled)
    handled=False means unsupported and caller should stop processing.
    """
    mime_type = str(doc.get('mime_type') or '')
    file_name = str(doc.get('file_name') or 'file')
    file_ext = file_name.rsplit('.', 1)[-1].lower() if '.' in file_name else ''
    file_id = doc.get('file_id')
    has_caption = bool((caption or '').strip())
    text_extensions = {
        'py', 'txt', 'md', 'json', 'csv', 'yaml', 'yml', 'toml',
        'cfg', 'ini', 'sh', 'bash', 'js', 'ts', 'html', 'css',
        'xml', 'sql', 'log', 'env', 'gitignore', 'dockerfile',
    }
    image_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'}
    archive = lambda raw_b64, detected_mime, kind: save_incoming_artifact(
        drive_root, filename=file_name, file_base64=raw_b64, content_kind=kind,
        mime_type=detected_mime or mime_type or 'application/octet-stream',
        chat_id=chat_id, caption=caption, metadata={
            'message_id': int(message_id or 0),
            'telegram_file_id': file_id or '',
            'activation_mode': 'immediate' if has_caption else 'deferred',
        },
    )

    if ((mime_type or '').strip().lower().startswith('image/') or file_ext in image_extensions) and file_id:
        b64, detected_mime = tg.download_file_base64(file_id)
        if not b64:
            return None, None, False
        meta = archive(b64, detected_mime, 'image')
        if isinstance(meta, dict) and not has_caption:
            schedule_inbox_confirmation(chat_id, file_name, send_with_budget)
        if has_caption:
            return None, (b64, detected_mime, caption), True
        return None, None, True

    if (file_ext in text_extensions or mime_type.startswith('text/')) and file_id:
        raw_b64, detected_mime = tg.download_file_base64(file_id)
        if not raw_b64:
            return None, None, False
        meta = archive(raw_b64, detected_mime, 'incoming')
        if isinstance(meta, dict) and not has_caption:
            schedule_inbox_confirmation(chat_id, file_name, send_with_budget)
        if not has_caption:
            return None, None, True
        import base64 as _b64mod
        file_bytes = _b64mod.b64decode(raw_b64)
        try:
            text_content = file_bytes.decode('utf-8')
        except UnicodeDecodeError:
            text_content = file_bytes.decode('latin-1')
        max_file_content = 80000
        full_len = len(text_content)
        if full_len > max_file_content:
            text_content = text_content[:max_file_content] + f'\n\n... (обрезано, всего {full_len} символов)'
        user_text = caption or ''
        payload = f"{user_text}\n\n📎 Файл: {file_name}\n```{file_ext}\n{text_content}\n```"
        return payload.strip(), None, True

    if file_ext == 'doc' and file_id:
        raw_b64, detected_mime = tg.download_file_base64(file_id)
        if not raw_b64:
            return None, None, False
        import base64 as _b64mod
        file_bytes = _b64mod.b64decode(raw_b64)
        ingest_result = ingest_legacy_word_document(
            drive_root=drive_root,
            file_name=file_name,
            file_bytes=file_bytes,
            chat_id=chat_id,
            caption=caption,
            message_id=int(message_id or 0),
            telegram_file_id=str(file_id or ''),
            activation_mode='immediate' if has_caption else 'deferred',
        )
        if not has_caption:
            schedule_inbox_confirmation(chat_id, file_name, send_with_budget)
            return None, None, True
        extracted = str(ingest_result.get('extracted_text') or '').strip()
        status = str(ingest_result.get('status') or 'archived')
        metadata = ingest_result.get('metadata') or {}
        if extracted:
            if len(extracted) > 80000:
                extracted = extracted[:80000] + '\n\n... (обрезано)'
            payload = f"{caption or ''}\n\n📎 DOC: {file_name}\nСтатус ingest: {status}\n\n{extracted}"
            return payload.strip(), None, True
        meta_path = metadata.get('relative_path') if isinstance(metadata, dict) else ''
        payload = f"{caption or ''}\n\n📎 DOC: {file_name}\nСтатус ingest: {status}. Текст автоматически не извлечён.\nМетаданные: {meta_path or 'не сохранены'}"
        return payload.strip(), None, True

    if file_ext == 'pdf' and file_id:
        raw_b64, detected_mime = tg.download_file_base64(file_id)
        if not raw_b64:
            return None, None, False
        meta = archive(raw_b64, detected_mime or 'application/pdf', 'pdf')
        if isinstance(meta, dict) and not has_caption:
            schedule_inbox_confirmation(chat_id, file_name, send_with_budget)
        if not has_caption:
            return None, None, True
        import base64 as _b64mod
        import tempfile as _tmpmod
        file_bytes = _b64mod.b64decode(raw_b64)
        pdf_text = None
        tmp_path = None
        try:
            with _tmpmod.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                tmp.write(file_bytes)
                tmp_path = tmp.name
            try:
                import pdfplumber
                with pdfplumber.open(tmp_path) as pdf:
                    pdf_text = '\n\n'.join(page.extract_text() or '' for page in pdf.pages)
            except ImportError:
                try:
                    from PyPDF2 import PdfReader
                    reader = PdfReader(tmp_path)
                    pdf_text = '\n\n'.join(page.extract_text() or '' for page in reader.pages)
                except ImportError:
                    pdf_text = None
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        if pdf_text:
            max_file_content = 80000
            if len(pdf_text) > max_file_content:
                pdf_text = pdf_text[:max_file_content] + '\n\n... (обрезано)'
            user_text = caption or ''
            payload = f"{user_text}\n\n📎 PDF: {file_name}\n{pdf_text}"
            return payload.strip(), None, True
        send_with_budget(chat_id, '⚠️ Не удалось извлечь текст из PDF. Установите pdfplumber или PyPDF2.')
        return None, None, False

    archive_extensions = {'zip', 'rar', '7z', 'tar', 'gz', 'tgz', 'bz2', 'xz'}
    is_archive = file_ext in archive_extensions
    max_download_bytes = 100_000_000 if is_archive else 10_000_000
    raw_b64, detected_mime = tg.download_file_base64(file_id, max_bytes=max_download_bytes) if file_id else (None, '')
    if raw_b64:
        meta = archive(raw_b64, detected_mime, 'archive' if is_archive else 'binary')
        if not has_caption:
            schedule_inbox_confirmation(chat_id, file_name, send_with_budget)
            return None, None, True
        if is_archive:
            relative_path = meta.get('relative_path', '') if isinstance(meta, dict) else ''
            size_hint = int(meta.get('bytes') or 0) if isinstance(meta, dict) else 0
            payload_lines = [caption or '', f'📦 Архив: {file_name}']
            if relative_path:
                payload_lines.append(f'Сохранён: {relative_path}')
            if size_hint:
                payload_lines.append(f'Размер: {size_hint} байт')
            return "\n\n".join(line for line in payload_lines if line).strip(), None, True
    if is_archive:
        send_with_budget(chat_id, f'⚠️ Не удалось принять архив .{file_ext}: Telegram не отдал содержимое или файл превышает лимит {max_download_bytes // 1_000_000} MB.')
        return None, None, True
    send_with_budget(chat_id, f'⚠️ Формат .{file_ext or "bin"} не поддерживается для немедленной обработки. Файл сохранён во входящий архив.')
    return None, None, True

# ----------------------------
# 5) Bootstrap repo
# ----------------------------
