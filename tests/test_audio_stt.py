import base64
import json
import time
import wave

import pytest
import speech_recognition as sr

from supervisor import audio_stt


def _write_wav(path, pcm, rate=16_000):
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(pcm)


def test_long_wav_splits_at_quiet_points_without_losing_samples(tmp_path):
    rate = 16_000
    loud = b"\x00\x20" * rate
    quiet = b"\x00\x00" * rate
    pcm = (loud * 29 + quiet) * 4
    wav = tmp_path / "long.wav"
    _write_wav(wav, pcm)

    chunks = list(audio_stt._wav_chunks(wav))

    assert len(chunks) == 4
    assert all(20 * rate * 2 <= len(chunk.frame_data) <= 35 * rate * 2 for chunk in chunks)
    assert chunks[0].frame_data.endswith(quiet[: rate // 5])
    assert b"".join(chunk.frame_data for chunk in chunks) == pcm


@pytest.mark.parametrize("failed", [False, True])
def test_telegram_transcript_marks_missing_speech_or_fails_and_cleans_up(tmp_path, monkeypatch, failed):
    monkeypatch.setattr(audio_stt.tempfile, "tempdir", str(tmp_path))

    def convert(src, dst):
        assert src.name == "input.wav"
        assert src != dst
        _write_wav(dst, b"\x01\x00" * 16_000 * 60)

    calls = []

    def recognize(self, audio, *, language, endpoint):
        assert language == "ru-RU"
        assert endpoint == "https://www.google.com/speech-api/v2/recognize"
        assert 0 < self.operation_timeout <= 12
        calls.append(audio)
        if len(calls) == 1:
            return "Начало"
        if failed:
            raise sr.RequestError("detail omitted from public error")
        if len(calls) == 2:
            raise sr.UnknownValueError()
        return "Конец"

    monkeypatch.setattr(audio_stt, "_convert_to_wav", convert)
    monkeypatch.setattr(sr.Recognizer, "recognize_google", recognize)
    monkeypatch.setattr(audio_stt.time, "sleep", lambda seconds: None)

    def transcribe():
        return audio_stt.transcribe_telegram_audio(
            drive_root=tmp_path, audio_b64=base64.b64encode(b"fixture").decode(),
            mime_type="audio/wav", kind="audio", file_name="sample.wav",
        )

    if failed:
        with pytest.raises(audio_stt.AudioTranscriptionError, match="chunk 2"):
            transcribe()
        assert calls[1] is calls[2]
    else:
        assert transcribe()["text"] == "Начало\n[Фрагмент не распознан]\nКонец"
    assert len(calls) == 3
    assert not list(tmp_path.glob("veles-stt-*"))
    event = json.loads((tmp_path / "logs" / "supervisor.jsonl").read_text(encoding="utf-8"))
    assert event["type"] == ("voice_transcription_error" if failed else "voice_transcribed")
    assert "detail omitted" not in str(event)


def test_expired_recognition_budget_does_not_call_provider():
    audio = sr.AudioData(b"", 16_000, 2)
    with pytest.raises(audio_stt.AudioTranscriptionError, match="timed out"):
        audio_stt._recognize_chunk(None, audio, "ru-RU", time.monotonic() - 1, 1)
