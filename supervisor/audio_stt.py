"""
Supervisor — Telegram audio transcription (voice/audio/video_note -> text).

MVP pipeline:
- save Telegram media locally
- convert to wav via ffmpeg
- transcribe via OpenAI speech API

If transcription fails, caller gets a structured error instead of silent drop.
"""

from __future__ import annotations

import base64
import datetime
import mimetypes
import os
import pathlib
import subprocess
import tempfile
from typing import Any, Dict, Optional

from openai import OpenAI

from supervisor.state import append_jsonl


class AudioTranscriptionError(RuntimeError):
    """Raised when Telegram audio cannot be transcribed."""


def _media_dir(drive_root: pathlib.Path) -> pathlib.Path:
    path = drive_root / "media" / "tg_voice"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _guess_suffix(kind: str, mime_type: str, file_name: str = "") -> str:
    name = str(file_name or "").strip().lower()
    if name.endswith(".ogg"):
        return ".ogg"
    if name.endswith(".mp3"):
        return ".mp3"
    if name.endswith(".m4a"):
        return ".m4a"
    if name.endswith(".mp4"):
        return ".mp4"
    if name.endswith(".oga"):
        return ".oga"
    if name.endswith(".wav"):
        return ".wav"
    if kind == "voice":
        return ".ogg"
    guessed = mimetypes.guess_extension(mime_type or "") or ""
    if guessed in {".ogg", ".mp3", ".m4a", ".mp4", ".oga", ".wav"}:
        return guessed
    return ".bin"


def _convert_to_wav(src_path: pathlib.Path, dst_path: pathlib.Path) -> None:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            str(dst_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise AudioTranscriptionError(
            f"ffmpeg conversion failed: {proc.stderr.strip()[:400]}"
        )


def _transcribe_wav_openai(wav_path: pathlib.Path, language: str = "ru") -> str:
    api_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise AudioTranscriptionError("OPENAI_API_KEY is not configured for speech transcription")

    client = OpenAI(api_key=api_key)
    with wav_path.open("rb") as audio_file:
        resp = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=audio_file,
            language=language,
        )
    text = getattr(resp, "text", None) or ""
    text = str(text).strip()
    if not text:
        raise AudioTranscriptionError("speech API returned empty transcription")
    return text


def transcribe_telegram_audio(
    *,
    drive_root: pathlib.Path,
    audio_b64: str,
    mime_type: str,
    kind: str,
    file_name: str = "",
    language: str = "ru",
) -> Dict[str, Any]:
    """Decode Telegram audio bytes, convert to wav, transcribe, return metadata + text."""
    media_dir = _media_dir(drive_root)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = _guess_suffix(kind, mime_type, file_name=file_name)
    src_path = media_dir / f"{ts}_{kind}{suffix}"
    wav_path = media_dir / f"{ts}_{kind}.wav"

    try:
        audio_bytes = base64.b64decode(audio_b64)
    except Exception as e:
        raise AudioTranscriptionError(f"invalid telegram audio base64: {e}") from e

    src_path.write_bytes(audio_bytes)

    try:
        _convert_to_wav(src_path, wav_path)
        text = _transcribe_wav_openai(wav_path, language=language)
        result = {
            "ok": True,
            "text": text,
            "kind": kind,
            "mime_type": mime_type,
            "src_path": str(src_path),
            "wav_path": str(wav_path),
        }
        append_jsonl(
            drive_root / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "voice_transcribed",
                "kind": kind,
                "mime_type": mime_type,
                "src_path": str(src_path),
                "wav_path": str(wav_path),
                "text_len": len(text),
            },
        )
        return result
    except Exception as e:
        append_jsonl(
            drive_root / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "voice_transcription_error",
                "kind": kind,
                "mime_type": mime_type,
                "src_path": str(src_path),
                "wav_path": str(wav_path),
                "error": repr(e),
            },
        )
        if isinstance(e, AudioTranscriptionError):
            raise
        raise AudioTranscriptionError(str(e)) from e
