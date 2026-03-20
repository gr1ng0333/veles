"""Tests for Memory.summarize_chat / chat_history truncation logic."""

import json
import pathlib
import tempfile

import pytest

from ouroboros.memory import Memory, RECENT_FULL_REPLIES


def _make_entry(direction: str, text: str, ts: str = "2026-03-20T12:00:00Z") -> dict:
    return {"ts": ts, "session_id": "s1", "direction": direction, "chat_id": 1, "user_id": 1, "text": text}


LONG_TEXT = "A" * 2000  # well above the 800-char cutoff
SHORT_TEXT = "Hello"


class TestSummarizeChat:
    """Tests for Memory.summarize_chat()."""

    def _mem(self) -> Memory:
        return Memory(drive_root=pathlib.Path(tempfile.mkdtemp()))

    def test_recent_replies_not_truncated(self):
        """Last RECENT_FULL_REPLIES outgoing messages are included in full."""
        entries = []
        for i in range(5):
            entries.append(_make_entry("out", LONG_TEXT, f"2026-03-20T12:{i:02d}:00Z"))

        result = self._mem().summarize_chat(entries)
        lines = result.strip().split("\n")

        # All 5 lines are outgoing
        assert len(lines) == 5

        # First 2 (older) must be truncated — no full LONG_TEXT
        for line in lines[:2]:
            assert LONG_TEXT not in line
            assert "..." in line

        # Last 3 (recent) must contain full text
        for line in lines[2:]:
            assert LONG_TEXT in line

    def test_owner_messages_never_truncated(self):
        """Incoming (owner) messages are never truncated regardless of position."""
        entries = [_make_entry("in", LONG_TEXT, f"2026-03-20T12:{i:02d}:00Z") for i in range(5)]
        result = self._mem().summarize_chat(entries)

        for line in result.strip().split("\n"):
            assert LONG_TEXT in line
            assert "..." not in line

    def test_short_replies_unaffected(self):
        """Outgoing replies shorter than 800 chars remain unchanged."""
        entries = [_make_entry("out", SHORT_TEXT, f"2026-03-20T12:{i:02d}:00Z") for i in range(6)]
        result = self._mem().summarize_chat(entries)

        for line in result.strip().split("\n"):
            assert SHORT_TEXT in line
            assert "..." not in line

    def test_mixed_directions(self):
        """In a mixed conversation only old outgoing are truncated."""
        entries = [
            _make_entry("in", "owner msg 1", "2026-03-20T12:00:00Z"),
            _make_entry("out", LONG_TEXT, "2026-03-20T12:01:00Z"),   # old out #1
            _make_entry("in", "owner msg 2", "2026-03-20T12:02:00Z"),
            _make_entry("out", LONG_TEXT, "2026-03-20T12:03:00Z"),   # old out #2
            _make_entry("in", "owner msg 3", "2026-03-20T12:04:00Z"),
            _make_entry("out", LONG_TEXT, "2026-03-20T12:05:00Z"),   # recent out #1
            _make_entry("out", LONG_TEXT, "2026-03-20T12:06:00Z"),   # recent out #2
            _make_entry("in", LONG_TEXT, "2026-03-20T12:07:00Z"),
            _make_entry("out", LONG_TEXT, "2026-03-20T12:08:00Z"),   # recent out #3
        ]
        result = self._mem().summarize_chat(entries)
        lines = result.strip().split("\n")

        assert len(lines) == 9

        # lines[1] = old out #1 (truncated), lines[3] = old out #2 (truncated)
        assert LONG_TEXT not in lines[1]
        assert "..." in lines[1]
        assert LONG_TEXT not in lines[3]
        assert "..." in lines[3]

        # lines[5], lines[6], lines[8] = recent outs (full)
        assert LONG_TEXT in lines[5]
        assert LONG_TEXT in lines[6]
        assert LONG_TEXT in lines[8]

        # All incoming lines (0, 2, 4, 7) have full text
        assert "owner msg 1" in lines[0]
        assert "owner msg 2" in lines[2]
        assert "owner msg 3" in lines[4]
        assert LONG_TEXT in lines[7]

    def test_constant_value(self):
        """RECENT_FULL_REPLIES is 3."""
        assert RECENT_FULL_REPLIES == 3

    def test_empty_entries(self):
        """Empty input returns empty string."""
        assert self._mem().summarize_chat([]) == ""


class TestChatHistory:
    """Tests for Memory.chat_history() truncation logic."""

    def _mem_with_chat(self, entries: list) -> Memory:
        tmp = pathlib.Path(tempfile.mkdtemp())
        logs = tmp / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        chat_file = logs / "chat.jsonl"
        chat_file.write_text(
            "\n".join(json.dumps(e) for e in entries),
            encoding="utf-8",
        )
        return Memory(drive_root=tmp)

    def test_recent_replies_not_truncated(self):
        """Last 3 outgoing messages in chat_history() are not truncated."""
        entries = [_make_entry("out", LONG_TEXT, f"2026-03-20T12:{i:02d}:00Z") for i in range(5)]
        mem = self._mem_with_chat(entries)
        result = mem.chat_history()

        lines = [l for l in result.strip().split("\n") if l.startswith("→")]
        assert len(lines) == 5

        # First 2 truncated
        for line in lines[:2]:
            assert LONG_TEXT not in line
            assert "..." in line

        # Last 3 full
        for line in lines[2:]:
            assert LONG_TEXT in line

    def test_owner_messages_never_truncated(self):
        """Incoming messages in chat_history() are never truncated."""
        entries = [_make_entry("in", LONG_TEXT, f"2026-03-20T12:{i:02d}:00Z") for i in range(4)]
        mem = self._mem_with_chat(entries)
        result = mem.chat_history()

        for line in result.strip().split("\n"):
            if line.startswith("←"):
                assert LONG_TEXT in line
