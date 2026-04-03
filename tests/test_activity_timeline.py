"""Tests for activity_timeline tool."""
from __future__ import annotations

import json
import pathlib
import textwrap
from datetime import datetime, timezone, timedelta

import pytest

from ouroboros.tools.activity_timeline import (
    _parse_ts,
    _fmt_ts,
    _fmt_dur,
    _normalise_event,
    _normalise_chat,
    _format_text,
    _build_timeline,
    _activity_timeline,
    get_tools,
)
from ouroboros.tools.registry import ToolContext


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_ctx(tmp_path: pathlib.Path) -> ToolContext:
    """Create a minimal ToolContext pointing at a tmp drive root."""
    ctx = ToolContext.__new__(ToolContext)
    ctx.drive_root = tmp_path
    ctx.repo_dir = "/opt/veles"
    return ctx


def _write_jsonl(path: pathlib.Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ── Unit tests ────────────────────────────────────────────────────────────────

def test_parse_ts_valid():
    ts = "2026-04-03T21:00:00+00:00"
    dt = _parse_ts(ts)
    assert dt is not None
    assert dt.year == 2026
    assert dt.tzinfo is not None


def test_parse_ts_invalid():
    assert _parse_ts("") is None
    assert _parse_ts("not-a-date") is None
    assert _parse_ts(None) is None


def test_fmt_dur():
    assert _fmt_dur(30) == "30s"
    assert _fmt_dur(90) == "2m"
    assert _fmt_dur(7200) == "2.0h"


def test_normalise_event_task_lifecycle():
    now = datetime.now(timezone.utc)
    rec = {"ts": now.isoformat(), "type": "task_done",
           "task_id": "abc12345", "task_type": "evolution", "rounds": 15}
    ev = _normalise_event(rec, "events")
    assert ev is not None
    assert ev["kind"] == "task_done"
    assert "done" in ev["label"]


def test_normalise_event_skips_llm_round():
    now = datetime.now(timezone.utc)
    rec = {"ts": now.isoformat(), "type": "llm_round", "task_id": "x", "round": 1}
    assert _normalise_event(rec, "events") is None


def test_normalise_event_restart():
    now = datetime.now(timezone.utc)
    rec = {"ts": now.isoformat(), "type": "startup_verification",
           "sha": "abc1234", "source": "agent_restart_request"}
    ev = _normalise_event(rec, "supervisor")
    assert ev is not None
    assert ev["kind"] == "restart"
    assert "restart" in ev["label"]


def test_normalise_chat_incoming():
    now = datetime.now(timezone.utc)
    rec = {"ts": now.isoformat(), "direction": "in", "text": "Hello Veles!"}
    ev = _normalise_chat(rec)
    assert ev is not None
    assert ev["kind"] == "chat_in"
    assert "Hello" in ev["detail"]


def test_normalise_chat_empty_text():
    now = datetime.now(timezone.utc)
    rec = {"ts": now.isoformat(), "direction": "in", "text": ""}
    assert _normalise_chat(rec) is None


def test_format_text_no_events():
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=1)
    result = _format_text([], since, now)
    assert "No notable events" in result


def test_activity_timeline_text(tmp_path: pathlib.Path):
    """End-to-end: write synthetic logs, call tool, check output."""
    drive = tmp_path
    now = datetime.now(timezone.utc)

    # events.jsonl with one task_done
    events_records = [
        {
            "ts": (now - timedelta(minutes=10)).isoformat(),
            "type": "task_received",
            "task_id": "aabbccdd",
            "task_type": "evolution",
            "text": "EVOLUTION #160",
        },
        {
            "ts": (now - timedelta(minutes=2)).isoformat(),
            "type": "task_done",
            "task_id": "aabbccdd",
            "task_type": "evolution",
            "rounds": 12,
        },
    ]
    _write_jsonl(drive / "logs" / "events.jsonl", events_records)

    # chat.jsonl with one owner message
    chat_records = [
        {
            "ts": (now - timedelta(minutes=8)).isoformat(),
            "direction": "in",
            "text": "What's the status?",
        }
    ]
    _write_jsonl(drive / "logs" / "chat.jsonl", chat_records)

    ctx = _make_ctx(drive)
    result = _activity_timeline(ctx, hours=1.0)

    assert "Activity timeline" in result
    assert "task" in result.lower() or "evolution" in result.lower()
    assert "Summary:" in result
