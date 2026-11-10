"""Transcribe Telegram voice, audio, and video notes through Google Web Speech."""

from __future__ import annotations

import base64
import binascii
import datetime
import logging
import mimetypes
import pathlib
import subprocess
import sys
import tempfile
import time
import wave
from array import array
from http.client import HTTPException
from typing import Any, Dict, Iterator

import speech_recognition as sr

from supervisor.state import append_jsonl

log = logging.getLogger(__name__)

_PROVIDER = "google-web-speech"
_GOOGLE_ENDPOINT = "https://www.google.com/speech-api/v2/recognize"
_REQUEST_TIMEOUT = 12
_RECOGNITION_BUDGET = 90
_CONVERSION_TIMEOUT = 30


class AudioTranscriptionError(RuntimeError):
    """Raised when Telegram audio cannot be transcribed."""


def _guess_suffix(kind: str, mime_type: str, file_name: str = "") -> str:
    supported = {".ogg", ".mp3", ".m4a", ".mp4", ".oga", ".wav", ".opus", ".aac"}
    suffix = pathlib.Path(file_name.strip().lower()).suffix
    if suffix in supported:
        return suffix
    if kind == "voice":
        return ".ogg"
    guessed = mimetypes.guess_extension(mime_type or "")
    return guessed if guessed in supported else ".bin"


def _convert_to_wav(src_path: pathlib.Path, dst_path: pathlib.Path) -> None:
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(src_path), "-vn", "-ar", "16000", "-ac", "1",
                "-c:a", "pcm_s16le", str(dst_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_CONVERSION_TIMEOUT,
        )
    except FileNotFoundError as e:
        raise AudioTranscriptionError("ffmpeg is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise AudioTranscriptionError("audio conversion timed out") from e
    if proc.returncode != 0:
        raise AudioTranscriptionError("ffmpeg could not decode the audio")


def _wav_chunks(wav_path: pathlib.Path) -> Iterator[sr.AudioData]:
    """Adapt the STT Pipeline's lowest-energy boundary search to streamed PCM."""
    with wave.open(str(wav_path), "rb") as source:
        rate = source.getframerate()
        width = source.getsampwidth()
        max_frames = 35 * rate
        window = rate // 5  # 200 ms
        step = rate // 10  # 100 ms
        total = source.getnframes()
        while (start := source.tell()) < total:
            remaining = total - start
            frames = min(remaining, max_frames)
            pcm = source.readframes(frames)
            if len(pcm) != frames * width:
                raise AudioTranscriptionError("decoded audio is incomplete")
            if remaining > max_frames:
                samples = array("h", pcm)
                if sys.byteorder == "big":
                    samples.byteswap()
                # Leave at least 10 seconds for the last request where possible.
                search_end = min(max_frames, remaining - 10 * rate)
                quietest = min(
                    range(20 * rate, search_end - window + 1, step),
                    key=lambda at: sum(map(abs, samples[at:at + window])),
                )
                cut = quietest + window // 2
                source.setpos(start + cut)
                pcm = pcm[:cut * width]
            yield sr.AudioData(pcm, rate, width)


def _recognize_chunk(
    recognizer: sr.Recognizer, audio: sr.AudioData, language: str, deadline: float, index: int,
) -> str:
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AudioTranscriptionError("Google Web Speech timed out")
        recognizer.operation_timeout = min(_REQUEST_TIMEOUT, remaining)
        try:
            return recognizer.recognize_google(
                audio, language=language, endpoint=_GOOGLE_ENDPOINT,
            ).strip()
        except sr.UnknownValueError:
            return ""
        except (sr.RequestError, TimeoutError, HTTPException) as e:
            if attempt or deadline - time.monotonic() <= 0.5:
                raise AudioTranscriptionError(
                    f"Google Web Speech request failed for audio chunk {index} ({type(e).__name__})"
                ) from e
            time.sleep(0.5)
    raise AssertionError("unreachable")


def _transcribe_wav_google(wav_path: pathlib.Path, language: str) -> list[str]:
    recognizer = sr.Recognizer()
    deadline = time.monotonic() + _RECOGNITION_BUDGET
    chunks = [
        _recognize_chunk(recognizer, audio, language, deadline, index)
        for index, audio in enumerate(_wav_chunks(wav_path), 1)
    ]
    if not any(chunks):
        raise AudioTranscriptionError("Google Web Speech could not recognize speech")
    return chunks


def transcribe_telegram_audio(
    *,
    drive_root: pathlib.Path,
    audio_b64: str,
    mime_type: str,
    kind: str,
    file_name: str = "",
    language: str = "ru-RU",
) -> Dict[str, Any]:
    """Return a single ordered transcript for the submitted Telegram recording."""
    metadata = {"provider": _PROVIDER, "kind": kind, "mime_type": mime_type}
    event = {**metadata, "ts": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    try:
        try:
            audio_bytes = base64.b64decode(audio_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise AudioTranscriptionError("invalid Telegram audio encoding") from e
        if not audio_bytes:
            raise AudioTranscriptionError("Telegram audio is empty")
        with tempfile.TemporaryDirectory(prefix="veles-stt-") as directory:
            media_dir = pathlib.Path(directory)
            src_path = media_dir / ("input" + _guess_suffix(kind, mime_type, file_name))
            wav_path = media_dir / "normalized.wav"
            src_path.write_bytes(audio_bytes)
            _convert_to_wav(src_path, wav_path)
            chunks = _transcribe_wav_google(wav_path, language)
        text = "\n".join(chunk or "[Фрагмент не распознан]" for chunk in chunks)
        event.update(
            type="voice_transcribed", text_len=len(text), chunks=len(chunks),
            unrecognized_chunks=chunks.count(""),
        )
        return {"ok": True, "text": text, **metadata}
    except Exception as e:
        error = e if isinstance(e, AudioTranscriptionError) else AudioTranscriptionError(
            f"audio transcription failed ({type(e).__name__})"
        )
        event.update(type="voice_transcription_error", error=str(error))
        if error is e:
            raise
        raise error from e
    finally:
        try:
            append_jsonl(drive_root / "logs" / "supervisor.jsonl", event)
        except OSError:
            log.warning("Could not write audio transcription event")
