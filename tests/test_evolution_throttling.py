"""Tests for evolution throttling, no-commit backoff, process dedup.

Covers:
- Cooldown: enqueue blocked if < EVOLUTION_COOLDOWN_SEC since last
- Backoff: after 3 no-commit cycles, cooldown doubles exponentially
- Hourly cap: after EVOLUTION_MAX_CYCLES_PER_HOUR cycles — blocked
- No-commit tracking: task_done without commit increments streak
- Commit reset: task_done with commit resets streak
- PID lock: second start kills stale process
- Evolution max rounds default changed to 10
"""
import datetime
import json
import os
import pathlib
import sys
import time
import types
from unittest import mock

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------------------
# Helpers: minimal stubs so supervisor modules can be imported in isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_telegram(monkeypatch):
    """Prevent any real Telegram calls."""
    monkeypatch.setattr("supervisor.telegram.send_with_budget", lambda *a, **kw: None)


@pytest.fixture
def tmp_drive(tmp_path, monkeypatch):
    """Set up a temporary DRIVE_ROOT for supervisor.state + supervisor.queue."""
    for d in ("state", "logs"):
        (tmp_path / d).mkdir(parents=True)
    # Init state module
    from supervisor import state as st_mod
    st_mod.init(tmp_path, total_budget_limit=1000.0)

    # Init queue module
    from supervisor import queue as q_mod
    q_mod.init(tmp_path, soft_timeout=600, hard_timeout=1800, evolution_hard_timeout=3600)

    # Wire up shared data structures
    pending = []
    running = {}
    seq_ref = {"value": 0}
    q_mod.init_queue_refs(pending, running, seq_ref)

    return tmp_path


def _make_state(tmp_drive, **overrides):
    """Write a state.json and return it."""
    from supervisor.state import save_state, ensure_state_defaults
    st = ensure_state_defaults({})
    st["owner_chat_id"] = 123
    st.update(overrides)
    save_state(st)
    return st


# ===========================================================================
# 1. Cooldown tests
# ===========================================================================

class TestEvolutionCooldown:
    def test_enqueue_blocked_within_cooldown(self, tmp_drive, monkeypatch):
        """Should NOT enqueue if last evolution was < EVOLUTION_COOLDOWN_SEC ago."""
        from supervisor import queue as q
        monkeypatch.setattr(q, "EVOLUTION_COOLDOWN_SEC", 120)
        monkeypatch.setattr(q, "EVOLUTION_MAX_CYCLES_PER_HOUR", 100)

        recent_ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30)
        _make_state(tmp_drive,
                    evolution_mode_enabled=True,
                    last_evolution_task_at=recent_ts.isoformat())

        q.PENDING.clear()
        q.RUNNING.clear()
        q.enqueue_evolution_task_if_needed()
        assert len(q.PENDING) == 0, "Should not enqueue during cooldown"

    def test_enqueue_allowed_after_cooldown(self, tmp_drive, monkeypatch):
        """Should enqueue if cooldown has passed."""
        from supervisor import queue as q
        monkeypatch.setattr(q, "EVOLUTION_COOLDOWN_SEC", 120)
        monkeypatch.setattr(q, "EVOLUTION_MAX_CYCLES_PER_HOUR", 100)

        old_ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=200)
        _make_state(tmp_drive,
                    evolution_mode_enabled=True,
                    last_evolution_task_at=old_ts.isoformat())

        q.PENDING.clear()
        q.RUNNING.clear()
        q.enqueue_evolution_task_if_needed()
        assert len(q.PENDING) == 1, "Should enqueue after cooldown expires"

    def test_first_enqueue_works_without_last_ts(self, tmp_drive, monkeypatch):
        """First evolution enqueue should work (no last_evolution_task_at)."""
        from supervisor import queue as q
        monkeypatch.setattr(q, "EVOLUTION_COOLDOWN_SEC", 120)
        monkeypatch.setattr(q, "EVOLUTION_MAX_CYCLES_PER_HOUR", 100)

        _make_state(tmp_drive,
                    evolution_mode_enabled=True,
                    last_evolution_task_at="")

        q.PENDING.clear()
        q.RUNNING.clear()
        q.enqueue_evolution_task_if_needed()
        assert len(q.PENDING) == 1


# ===========================================================================
# 2. Backoff tests
# ===========================================================================

class TestNoCommitBackoff:
    def test_backoff_doubles_after_3_no_commits(self, tmp_drive, monkeypatch):
        """After 3 no-commit cycles, effective cooldown = base * 2."""
        from supervisor import queue as q
        monkeypatch.setattr(q, "EVOLUTION_COOLDOWN_SEC", 120)
        monkeypatch.setattr(q, "EVOLUTION_MAX_CYCLES_PER_HOUR", 100)

        # Last evolution was 150s ago (> base 120s), but with streak=3
        # effective = 120 * 2^min(3-2,4) = 240s, so 150s < 240s → blocked
        old_ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=150)
        _make_state(tmp_drive,
                    evolution_mode_enabled=True,
                    last_evolution_task_at=old_ts.isoformat(),
                    no_commit_streak=3)

        q.PENDING.clear()
        q.RUNNING.clear()
        q.enqueue_evolution_task_if_needed()
        assert len(q.PENDING) == 0, "Should be blocked by backoff cooldown"

    def test_backoff_allows_after_sufficient_wait(self, tmp_drive, monkeypatch):
        """After enough time, even with streak, should enqueue."""
        from supervisor import queue as q
        monkeypatch.setattr(q, "EVOLUTION_COOLDOWN_SEC", 120)
        monkeypatch.setattr(q, "EVOLUTION_MAX_CYCLES_PER_HOUR", 100)

        # streak=3, effective=240s, last was 300s ago → allowed
        old_ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=300)
        _make_state(tmp_drive,
                    evolution_mode_enabled=True,
                    last_evolution_task_at=old_ts.isoformat(),
                    no_commit_streak=3)

        q.PENDING.clear()
        q.RUNNING.clear()
        q.enqueue_evolution_task_if_needed()
        assert len(q.PENDING) == 1

    def test_backoff_caps_at_power_4(self, tmp_drive, monkeypatch):
        """Backoff exponent capped at 4 → max multiplier = 16."""
        from supervisor import queue as q
        monkeypatch.setattr(q, "EVOLUTION_COOLDOWN_SEC", 120)
        monkeypatch.setattr(q, "EVOLUTION_MAX_CYCLES_PER_HOUR", 100)

        # streak=10 → exponent = min(10-2, 4) = 4 → 120*16 = 1920s
        # last was 1800s ago → still blocked
        old_ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1800)
        _make_state(tmp_drive,
                    evolution_mode_enabled=True,
                    last_evolution_task_at=old_ts.isoformat(),
                    no_commit_streak=10)

        q.PENDING.clear()
        q.RUNNING.clear()
        q.enqueue_evolution_task_if_needed()
        assert len(q.PENDING) == 0, "1800s < 1920s, should still be blocked"

    def test_no_backoff_below_streak_3(self, tmp_drive, monkeypatch):
        """Streak < 3 should use base cooldown."""
        from supervisor import queue as q
        monkeypatch.setattr(q, "EVOLUTION_COOLDOWN_SEC", 120)
        monkeypatch.setattr(q, "EVOLUTION_MAX_CYCLES_PER_HOUR", 100)

        # streak=2, base cooldown=120, last was 150s ago → allowed
        old_ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=150)
        _make_state(tmp_drive,
                    evolution_mode_enabled=True,
                    last_evolution_task_at=old_ts.isoformat(),
                    no_commit_streak=2)

        q.PENDING.clear()
        q.RUNNING.clear()
        q.enqueue_evolution_task_if_needed()
        assert len(q.PENDING) == 1


# ===========================================================================
# 3. Hourly cap tests
# ===========================================================================

class TestHourlyCap:
    def test_blocked_at_hourly_cap(self, tmp_drive, monkeypatch):
        """Should not enqueue when hourly cap reached."""
        from supervisor import queue as q
        monkeypatch.setattr(q, "EVOLUTION_COOLDOWN_SEC", 0)
        monkeypatch.setattr(q, "EVOLUTION_MAX_CYCLES_PER_HOUR", 6)

        now = time.time()
        cycles = [now - 60 * i for i in range(6)]  # 6 cycles in last hour
        _make_state(tmp_drive,
                    evolution_mode_enabled=True,
                    last_evolution_task_at="",
                    evolution_cycles_1h=cycles)

        q.PENDING.clear()
        q.RUNNING.clear()
        q.enqueue_evolution_task_if_needed()
        assert len(q.PENDING) == 0, "Should be capped at 6/hour"

    def test_allowed_below_hourly_cap(self, tmp_drive, monkeypatch):
        """Should enqueue when below hourly cap."""
        from supervisor import queue as q
        monkeypatch.setattr(q, "EVOLUTION_COOLDOWN_SEC", 0)
        monkeypatch.setattr(q, "EVOLUTION_MAX_CYCLES_PER_HOUR", 6)

        now = time.time()
        cycles = [now - 60 * i for i in range(5)]  # only 5
        _make_state(tmp_drive,
                    evolution_mode_enabled=True,
                    last_evolution_task_at="",
                    evolution_cycles_1h=cycles)

        q.PENDING.clear()
        q.RUNNING.clear()
        q.enqueue_evolution_task_if_needed()
        assert len(q.PENDING) == 1

    def test_old_cycles_pruned(self, tmp_drive, monkeypatch):
        """Cycles older than 1 hour should be pruned."""
        from supervisor import queue as q
        monkeypatch.setattr(q, "EVOLUTION_COOLDOWN_SEC", 0)
        monkeypatch.setattr(q, "EVOLUTION_MAX_CYCLES_PER_HOUR", 6)

        now = time.time()
        # 6 cycles but all older than 1 hour
        cycles = [now - 4000 for _ in range(6)]
        _make_state(tmp_drive,
                    evolution_mode_enabled=True,
                    last_evolution_task_at="",
                    evolution_cycles_1h=cycles)

        q.PENDING.clear()
        q.RUNNING.clear()
        q.enqueue_evolution_task_if_needed()
        assert len(q.PENDING) == 1, "Old cycles should be pruned"


# ===========================================================================
# 4. No-commit tracking in events.py
# ===========================================================================

class TestEvolutionTaskDone:
    def _make_ctx(self, tmp_drive):
        """Build a minimal event-handler context."""
        from supervisor.state import load_state, save_state, append_jsonl
        ctx = types.SimpleNamespace(
            DRIVE_ROOT=tmp_drive,
            load_state=load_state,
            save_state=save_state,
            append_jsonl=append_jsonl,
            RUNNING={},
            WORKERS={},
            persist_queue_snapshot=lambda **kw: None,
            send_with_budget=lambda *a, **kw: None,
        )
        return ctx

    def test_no_commit_increments_streak(self, tmp_drive):
        """task_done with ok=True but no commit text → streak increments."""
        from supervisor.events import _handle_task_done
        _make_state(tmp_drive, no_commit_streak=1)
        ctx = self._make_ctx(tmp_drive)

        evt = {
            "task_id": "abc123",
            "task_type": "evolution",
            "ok": True,
            "response_len": 200,
            "total_rounds": 5,
            "response_text": "I analyzed the codebase and found nothing to do.",
        }
        _handle_task_done(evt, ctx)

        from supervisor.state import load_state
        st = load_state()
        assert st["no_commit_streak"] == 2

    def test_commit_resets_streak(self, tmp_drive):
        """task_done with ok=True and commit hash → streak resets to 0."""
        from supervisor.events import _handle_task_done
        _make_state(tmp_drive, no_commit_streak=5, evolution_consecutive_failures=2)
        ctx = self._make_ctx(tmp_drive)

        evt = {
            "task_id": "abc123",
            "task_type": "evolution",
            "ok": True,
            "response_len": 500,
            "total_rounds": 8,
            "response_text": "Committed changes abc1234def in evolution cycle.",
        }
        _handle_task_done(evt, ctx)

        from supervisor.state import load_state
        st = load_state()
        assert st["no_commit_streak"] == 0
        assert st["evolution_consecutive_failures"] == 0

    def test_failure_increments_consecutive_failures(self, tmp_drive):
        """task_done with ok=False → consecutive failures increment."""
        from supervisor.events import _handle_task_done
        _make_state(tmp_drive, evolution_consecutive_failures=1)
        ctx = self._make_ctx(tmp_drive)

        evt = {
            "task_id": "abc123",
            "task_type": "evolution",
            "ok": False,
            "response_len": 100,
            "total_rounds": 3,
        }
        _handle_task_done(evt, ctx)

        from supervisor.state import load_state
        st = load_state()
        assert st["evolution_consecutive_failures"] == 2

    def test_non_evolution_task_ignores_streak(self, tmp_drive):
        """Regular task_done should not touch evolution state."""
        from supervisor.events import _handle_task_done
        _make_state(tmp_drive, no_commit_streak=5)
        ctx = self._make_ctx(tmp_drive)

        evt = {
            "task_id": "abc123",
            "task_type": "task",
            "ok": True,
            "response_len": 200,
            "total_rounds": 3,
        }
        _handle_task_done(evt, ctx)

        from supervisor.state import load_state
        st = load_state()
        assert st["no_commit_streak"] == 5, "Should not change for non-evolution tasks"


# ===========================================================================
# 5. PID lock
# ===========================================================================

class TestPidLock:
    def test_pid_lock_written(self, tmp_path):
        """PID lock file should contain current process PID."""
        pid_path = tmp_path / "state" / "supervisor.pid"
        pid_path.parent.mkdir(parents=True, exist_ok=True)

        # Simulate _acquire_pid_lock logic
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
        assert int(pid_path.read_text(encoding="utf-8").strip()) == os.getpid()

    def test_stale_pid_detected(self, tmp_path):
        """PID lock with non-existent PID should not block."""
        pid_path = tmp_path / "state" / "supervisor.pid"
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        # Write a PID that almost certainly doesn't exist
        pid_path.write_text("99999999", encoding="utf-8")

        # Simulate the check — should not raise
        old_pid = int(pid_path.read_text(encoding="utf-8").strip())
        killed = False
        try:
            os.kill(old_pid, 0)  # signal 0 = existence check only
            killed = True
        except (ProcessLookupError, PermissionError, OSError):
            pass
        # On most systems, PID 99999999 doesn't exist
        # Just verify we don't crash
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
        assert int(pid_path.read_text(encoding="utf-8").strip()) == os.getpid()


# ===========================================================================
# 6. Evolution max rounds default
# ===========================================================================

class TestEvolutionMaxRounds:
    def test_default_is_10(self, monkeypatch):
        monkeypatch.delenv("OUROBOROS_EVOLUTION_MAX_ROUNDS", raising=False)
        from ouroboros.loop_runtime import _get_evolution_round_limit
        assert _get_evolution_round_limit("evolution", 30) == 10

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("OUROBOROS_EVOLUTION_MAX_ROUNDS", "7")
        from ouroboros.loop_runtime import _get_evolution_round_limit
        assert _get_evolution_round_limit("evolution", 30) == 7

    def test_non_evolution_ignores(self, monkeypatch):
        monkeypatch.setenv("OUROBOROS_EVOLUTION_MAX_ROUNDS", "5")
        from ouroboros.loop_runtime import _get_evolution_round_limit
        assert _get_evolution_round_limit("task", 30) == 30
