"""Telegram media extraction — photo, audio, document, PDF processing."""
from __future__ import annotations

import base64
import logging
import os
import tempfile
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger(__name__)

_TEXT_EXTENSIONS = {
    "py", "txt", "md", "json", "csv", "yaml", "yml", "toml",
    "cfg", "ini", "sh", "bash", "js", "ts", "html", "css",
    "xml", "sql", "log", "env", "gitignore", "dockerfile",
}
_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
_MAX_FILE_CONTENT = 80000


def extract_media(
    msg: Dict[str, Any],
    caption: str,
    text: str,
    tg_client: Any,
    drive_root: Any,
) -> Tuple[Optional[Tuple[str, str, str]], str, Optional[str]]:
    """Extract media from a Telegram message.

    Returns (image_data, text, error).
    error is a user-facing string if processing should stop, else None.
    """
    image_data = None
    error = None

    if msg.get("photo"):
        best_photo = msg["photo"][-1]
        file_id = best_photo.get("file_id")
        if file_id:
            b64, mime = tg_client.download_file_base64(file_id)
            if b64:
                image_data = (b64, mime, caption)

    elif msg.get("voice") or msg.get("audio") or msg.get("video_note"):
        text, error = _handle_audio(msg, caption, text, tg_client, drive_root)

    elif msg.get("document"):
        image_data, text, error = _handle_document(msg, caption, text, tg_client)

    return image_data, text, error


def _handle_audio(
    msg: Dict[str, Any], caption: str, text: str,
    tg_client: Any, drive_root: Any,
) -> Tuple[str, Optional[str]]:
    from supervisor.audio_stt import transcribe_telegram_audio, AudioTranscriptionError

    audio_obj = msg.get("voice") or msg.get("audio") or msg.get("video_note") or {}
    audio_kind = "voice" if msg.get("voice") else ("audio" if msg.get("audio") else "video_note")
    file_id = str(audio_obj.get("file_id") or "")
    mime_type = str(audio_obj.get("mime_type") or "")
    file_name = str(audio_obj.get("file_name") or audio_kind)
    if not file_id:
        return text, None
    audio_b64, audio_mime = tg_client.download_file_base64(file_id, max_bytes=25_000_000)
    if not audio_b64:
        return text, "⚠️ Не удалось скачать голосовое из Telegram."
    try:
        tr = transcribe_telegram_audio(
            drive_root=drive_root, audio_b64=audio_b64,
            mime_type=mime_type or audio_mime, kind=audio_kind,
            file_name=file_name, language="ru-RU",
        )
        transcribed = str(tr.get("text") or "").strip()
        prefix_map = {"voice": "[Голосовое сообщение]", "audio": "[Аудио]", "video_note": "[Кружок]"}
        prefix = prefix_map.get(audio_kind, "[Аудио]")
        text = f"{prefix}\n{transcribed}" if transcribed else prefix
        if caption:
            text = f"{caption}\n\n{text}" if text else caption
        return text, None
    except AudioTranscriptionError as e:
        return text, f"⚠️ Не удалось распознать голосовое: {e}"


def _handle_document(
    msg: Dict[str, Any], caption: str, text: str, tg_client: Any,
) -> Tuple[Optional[Tuple[str, str, str]], str, Optional[str]]:
    doc = msg["document"]
    mime_type = str(doc.get("mime_type") or "")
    file_name = str(doc.get("file_name") or "file")
    file_ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""

    if mime_type.startswith("image/") or file_ext in _IMAGE_EXTENSIONS:
        file_id = doc.get("file_id")
        if file_id:
            b64, mime = tg_client.download_file_base64(file_id)
            if b64:
                return (b64, mime, caption), text, None
        return None, text, None

    if file_ext in _TEXT_EXTENSIONS or mime_type.startswith("text/"):
        return None, _extract_text_file(doc, file_name, file_ext, caption, tg_client), None

    if file_ext == "pdf":
        result_text, err = _extract_pdf(doc, file_name, caption, tg_client)
        return None, result_text or text, err

    return None, text, f"⚠️ Формат .{file_ext} не поддерживается. Поддерживаются: текст, PDF, картинки."


def _extract_text_file(doc: dict, file_name: str, file_ext: str, caption: str, tg_client: Any) -> str:
    file_id = doc.get("file_id")
    if not file_id:
        return caption or ""
    raw_b64, _ = tg_client.download_file_base64(file_id)
    if not raw_b64:
        return caption or ""
    file_bytes = base64.b64decode(raw_b64)
    try:
        content = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content = file_bytes.decode("latin-1")
    if len(content) > _MAX_FILE_CONTENT:
        content = content[:_MAX_FILE_CONTENT] + f"\n\n... (обрезано, всего {len(content)} символов)"
    user_text = caption or ""
    return f"{user_text}\n\n📎 Файл: {file_name}\n```{file_ext}\n{content}\n```"


def _extract_pdf(doc: dict, file_name: str, caption: str, tg_client: Any) -> Tuple[Optional[str], Optional[str]]:
    file_id = doc.get("file_id")
    if not file_id:
        return None, None
    raw_b64, _ = tg_client.download_file_base64(file_id)
    if not raw_b64:
        return None, None
    file_bytes = base64.b64decode(raw_b64)
    pdf_text = None
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        try:
            import pdfplumber
            with pdfplumber.open(tmp_path) as pdf:
                pdf_text = "\n\n".join(page.extract_text() or "" for page in pdf.pages)
        except ImportError:
            try:
                from PyPDF2 import PdfReader
                reader = PdfReader(tmp_path)
                pdf_text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
            except ImportError:
                pdf_text = None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    if pdf_text:
        if len(pdf_text) > _MAX_FILE_CONTENT:
            pdf_text = pdf_text[:_MAX_FILE_CONTENT] + "\n\n... (обрезано)"
        user_text = caption or ""
        return f"{user_text}\n\n📎 PDF: {file_name}\n{pdf_text}", None
    return None, "⚠️ Не удалось извлечь текст из PDF. Установите pdfplumber или PyPDF2."
