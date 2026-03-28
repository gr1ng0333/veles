from __future__ import annotations

import queue
from datetime import datetime, timezone
from pathlib import Path

from ouroboros.fitness_consciousness import FitnessConsciousness, _seconds_until_next_slot


def test_seconds_until_next_slot_uses_utc_plus_3_schedule() -> None:
    now = datetime(2026, 3, 28, 5, 30, tzinfo=timezone.utc)  # 08:30 UTC+3
    assert _seconds_until_next_slot(now) == 30 * 60

    later = datetime(2026, 3, 28, 19, 30, tzinfo=timezone.utc)  # 22:30 UTC+3
    assert _seconds_until_next_slot(later) == 10.5 * 3600


def test_quiet_check_delays_and_then_cancels_after_three_retries(tmp_path: Path, monkeypatch) -> None:
    shared_state = {
        "last_owner_message_at": "2026-03-28T10:00:00Z",
        "last_outgoing_at": "2026-03-28T10:10:00Z",
    }
    monkeypatch.setattr("ouroboros.fitness_consciousness.load_state", lambda: dict(shared_state))
    monkeypatch.setattr("ouroboros.fitness_consciousness.save_state", lambda st: shared_state.update(st))

    daemon = FitnessConsciousness(tmp_path, Path("/opt/veles"), queue.Queue(), lambda: 123)
    now = datetime(2026, 3, 28, 10, 20, tzinfo=timezone.utc)

    first = daemon._delay_for_quiet(now)
    assert first["delayed"] is True
    assert first["reason"] == "recent_activity"
    assert daemon._monitor_state["quiet_retry_count"] == 1
    assert daemon._next_wakeup_sec == 20 * 60

    daemon._monitor_state["quiet_retry_count"] = 3
    cancelled = daemon._delay_for_quiet(now)
    assert cancelled["delayed"] is True
    assert cancelled["reason"] == "cancelled_after_retries"
    assert daemon._monitor_state["quiet_retry_count"] == 0
    assert daemon._next_wakeup_sec > 20 * 60


def test_build_tools_is_limited_and_drive_io_is_scoped_to_fitness(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("ouroboros.fitness_consciousness.load_state", lambda: {})
    monkeypatch.setattr("ouroboros.fitness_consciousness.save_state", lambda st: None)

    daemon = FitnessConsciousness(tmp_path, Path("/opt/veles"), queue.Queue(), lambda: 123)
    registry = daemon._build_tools()
    names = set(registry.available_tools())

    assert "fitness_log_meal" in names
    assert "fatsecret_search" in names
    assert "send_owner_message" in names
    assert "repo_read" not in names
    assert "update_scratchpad" not in names

    write_result = registry.execute(
        "drive_write",
        {"path": "notes/day1.txt", "content": "hello", "mode": "overwrite"},
    )
    assert "OK: wrote" in write_result
    assert (tmp_path / "fitness" / "notes" / "day1.txt").read_text(encoding="utf-8") == "hello"
    assert "hello" == registry.execute("drive_read", {"path": "notes/day1.txt"})


def test_send_owner_message_sets_awaiting_reply_only_for_questions(tmp_path: Path, monkeypatch) -> None:
    shared_state = {}

    def fake_load_state():
        return dict(shared_state)

    def fake_save_state(st):
        shared_state.clear()
        shared_state.update(st)

    monkeypatch.setattr("ouroboros.fitness_consciousness.load_state", fake_load_state)
    monkeypatch.setattr("ouroboros.fitness_consciousness.save_state", fake_save_state)

    events = queue.Queue()
    daemon = FitnessConsciousness(tmp_path, Path("/opt/veles"), events, lambda: 777)
    ok = daemon._queue_owner_message("Как тренировка сегодня?", reason="checkin")
    assert "queued" in ok
    assert shared_state["fitness_awaiting_reply"] is True
    evt = events.get_nowait()
    assert evt["chat_id"] == 777
    assert (tmp_path / "fitness" / "logs" / "events.jsonl").exists()

    shared_state.clear()
    daemon._queue_owner_message("Сегодня цель — 15 минут разминки.", reason="plan")
    assert "fitness_awaiting_reply" not in shared_state


def test_context_mentions_program_bootstrap_when_profile_missing_program(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("ouroboros.fitness_consciousness.load_state", lambda: {})
    monkeypatch.setattr("ouroboros.fitness_consciousness.save_state", lambda st: None)

    fitness_root = tmp_path / "fitness"
    fitness_root.mkdir(parents=True, exist_ok=True)
    (fitness_root / "profile.json").write_text('{"weight_kg": 84, "height_cm": 173}', encoding="utf-8")

    daemon = FitnessConsciousness(tmp_path, Path("/opt/veles"), queue.Queue(), lambda: 123)
    context = daemon._build_context()
    assert "нет active_program" in context
    assert "3 дня" in context
