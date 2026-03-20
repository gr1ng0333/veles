"""Tests for worker task dispatch — assign_tasks, ensure_workers_healthy, queue restore."""

import json
import pathlib
import time
import types
from unittest.mock import MagicMock, patch

import pytest

import supervisor.workers as workers
import supervisor.queue as queue_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_proc(alive: bool = True, exitcode: int = 0):
    p = MagicMock()
    p.is_alive.return_value = alive
    p.exitcode = exitcode
    p.terminate = MagicMock()
    p.join = MagicMock()
    return p


def _make_worker(wid: int, alive: bool = True, busy_task_id=None):
    w = workers.Worker(wid=wid, proc=_fake_proc(alive=alive), in_q=MagicMock(), busy_task_id=busy_task_id)
    return w


def _review_task(tid: str = "rev01", chat_id: int = 111):
    return {"id": tid, "type": "review", "chat_id": chat_id, "text": "REVIEW: test"}


def _setup_workers_module(tmp_path):
    """Minimal setup for workers module globals for testing."""
    workers.WORKERS.clear()
    workers.PENDING.clear()
    workers.RUNNING.clear()
    workers.CRASH_TS.clear()
    workers.QUEUE_SEQ_COUNTER_REF["value"] = 0
    workers.DRIVE_ROOT = tmp_path
    workers.MAX_WORKERS = 1
    workers.QUEUE_MAX_RETRIES = 1
    workers._LAST_SPAWN_TIME = 0.0
    workers._SPAWN_GRACE_SEC = 90.0
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)

    # Also set up queue module references
    queue_mod.PENDING = workers.PENDING
    queue_mod.RUNNING = workers.RUNNING
    queue_mod.QUEUE_SEQ_COUNTER_REF = workers.QUEUE_SEQ_COUNTER_REF
    queue_mod.DRIVE_ROOT = tmp_path


# ---------------------------------------------------------------------------
# assign_tasks tests
# ---------------------------------------------------------------------------

class TestAssignTasks:
    """assign_tasks should only assign to alive workers."""

    def test_review_task_assigned_to_alive_worker(self, tmp_path, monkeypatch):
        _setup_workers_module(tmp_path)
        monkeypatch.setattr("supervisor.state.load_state", lambda: {"owner_chat_id": 111, "spent_usd": 0})
        monkeypatch.setattr("supervisor.state.budget_remaining", lambda st: 999.0)
        monkeypatch.setattr(workers, "send_with_budget", lambda *a, **kw: None)

        w = _make_worker(0, alive=True)
        workers.WORKERS[0] = w
        task = _review_task()
        workers.PENDING.append(task)

        workers.assign_tasks()

        assert w.busy_task_id == "rev01"
        assert len(workers.PENDING) == 0
        assert "rev01" in workers.RUNNING
        w.in_q.put.assert_called_once()

    def test_review_task_NOT_assigned_to_dead_worker(self, tmp_path, monkeypatch):
        _setup_workers_module(tmp_path)
        monkeypatch.setattr("supervisor.state.load_state", lambda: {"owner_chat_id": 111, "spent_usd": 0})
        monkeypatch.setattr("supervisor.state.budget_remaining", lambda st: 999.0)

        w = _make_worker(0, alive=False)
        workers.WORKERS[0] = w
        task = _review_task()
        workers.PENDING.append(task)

        workers.assign_tasks()

        # Task must remain in PENDING — dead worker must not receive it
        assert w.busy_task_id is None
        assert len(workers.PENDING) == 1
        assert len(workers.RUNNING) == 0
        w.in_q.put.assert_not_called()

    def test_evolution_task_skipped_when_over_budget(self, tmp_path, monkeypatch):
        _setup_workers_module(tmp_path)
        monkeypatch.setattr("supervisor.state.load_state", lambda: {"owner_chat_id": 111, "spent_usd": 999})
        monkeypatch.setattr("supervisor.state.budget_remaining", lambda st: 10.0)
        monkeypatch.setattr("supervisor.state.EVOLUTION_BUDGET_RESERVE", 50.0)

        w = _make_worker(0, alive=True)
        workers.WORKERS[0] = w
        workers.PENDING.append({"id": "evo01", "type": "evolution", "chat_id": 111, "text": "evolve"})

        workers.assign_tasks()

        # Evolution dropped due to budget; worker stays free
        assert w.busy_task_id is None
        assert len(workers.PENDING) == 0  # cleaned out


# ---------------------------------------------------------------------------
# ensure_workers_healthy tests
# ---------------------------------------------------------------------------

class TestEnsureWorkersHealthy:
    """Health checks should detect dead busy workers even during grace period."""

    def test_dead_busy_worker_detected_during_grace(self, tmp_path, monkeypatch):
        """Dead worker with assigned task must be detected even during grace period."""
        _setup_workers_module(tmp_path)
        monkeypatch.setattr("supervisor.state.load_state", lambda: {"owner_chat_id": 111})
        monkeypatch.setattr(workers, "send_with_budget", lambda *a, **kw: None)

        w = _make_worker(0, alive=False, busy_task_id="rev01")
        workers.WORKERS[0] = w
        task = _review_task()
        workers.RUNNING["rev01"] = {"task": dict(task), "worker_id": 0, "started_at": time.time()}

        # Set grace period active
        workers._LAST_SPAWN_TIME = time.time()

        # Mock respawn_worker to avoid real process creation
        respawned = []
        monkeypatch.setattr(workers, "respawn_worker", lambda wid: respawned.append(wid))

        workers.ensure_workers_healthy()

        # Dead busy worker must be detected, task re-queued, worker respawned
        assert 0 in respawned
        assert "rev01" not in workers.RUNNING
        # Task re-queued with incremented attempt
        assert len(workers.PENDING) == 1
        assert workers.PENDING[0]["_attempt"] == 2

    def test_idle_dead_worker_skipped_during_grace(self, tmp_path, monkeypatch):
        """Idle dead workers should be skipped during grace period."""
        _setup_workers_module(tmp_path)
        monkeypatch.setattr("supervisor.state.load_state", lambda: {"owner_chat_id": 111})

        w = _make_worker(0, alive=False)  # idle, no busy_task_id
        workers.WORKERS[0] = w

        workers._LAST_SPAWN_TIME = time.time()  # grace active

        respawned = []
        monkeypatch.setattr(workers, "respawn_worker", lambda wid: respawned.append(wid))

        workers.ensure_workers_healthy()

        # Should NOT be respawned during grace (still initializing)
        assert len(respawned) == 0

    def test_crash_requeue_respects_retry_limit(self, tmp_path, monkeypatch):
        """After max retries, task should be dropped (not re-queued)."""
        _setup_workers_module(tmp_path)
        monkeypatch.setattr("supervisor.state.load_state", lambda: {"owner_chat_id": 111})
        monkeypatch.setattr(workers, "send_with_budget", lambda *a, **kw: None)
        workers._LAST_SPAWN_TIME = 0.0  # no grace

        task = _review_task()
        task["_attempt"] = 2  # already retried once; QUEUE_MAX_RETRIES=1 → should drop

        w = _make_worker(0, alive=False, busy_task_id="rev01")
        workers.WORKERS[0] = w
        workers.RUNNING["rev01"] = {"task": dict(task), "worker_id": 0, "started_at": time.time()}

        respawned = []
        monkeypatch.setattr(workers, "respawn_worker", lambda wid: respawned.append(wid))

        workers.ensure_workers_healthy()

        # Task should NOT be re-queued (retry limit exhausted)
        assert len(workers.PENDING) == 0
        assert 0 in respawned

    def test_crash_requeue_increments_attempt(self, tmp_path, monkeypatch):
        """First crash should re-queue with incremented _attempt."""
        _setup_workers_module(tmp_path)
        monkeypatch.setattr("supervisor.state.load_state", lambda: {"owner_chat_id": 111})
        monkeypatch.setattr(workers, "send_with_budget", lambda *a, **kw: None)
        workers._LAST_SPAWN_TIME = 0.0

        task = _review_task()
        task["_attempt"] = 1

        w = _make_worker(0, alive=False, busy_task_id="rev01")
        workers.WORKERS[0] = w
        workers.RUNNING["rev01"] = {"task": dict(task), "worker_id": 0, "started_at": time.time()}

        respawned = []
        monkeypatch.setattr(workers, "respawn_worker", lambda wid: respawned.append(wid))

        workers.ensure_workers_healthy()

        assert len(workers.PENDING) == 1
        assert workers.PENDING[0]["_attempt"] == 2


# ---------------------------------------------------------------------------
# restore_pending_from_snapshot tests
# ---------------------------------------------------------------------------

class TestRestorePendingFromSnapshot:
    """Snapshot restore must recover both PENDING and RUNNING tasks."""

    def test_running_tasks_restored_as_pending(self, tmp_path, monkeypatch):
        """RUNNING tasks in snapshot should be restored to PENDING on restart."""
        _setup_workers_module(tmp_path)
        monkeypatch.setattr(queue_mod, "QUEUE_SNAPSHOT_PATH", tmp_path / "state" / "queue_snapshot.json")
        monkeypatch.setattr("supervisor.state.load_state", lambda: {})
        monkeypatch.setattr("supervisor.state.save_state", lambda st: None)

        snap = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "pending_count": 0,
            "running_count": 1,
            "pending": [],
            "running": [
                {
                    "id": "rev01",
                    "type": "review",
                    "task": {"id": "rev01", "type": "review", "chat_id": 111, "text": "REVIEW: test"},
                },
            ],
        }
        (tmp_path / "state" / "queue_snapshot.json").write_text(
            json.dumps(snap), encoding="utf-8"
        )

        restored = queue_mod.restore_pending_from_snapshot()

        assert restored == 1
        assert len(workers.PENDING) == 1
        assert workers.PENDING[0]["type"] == "review"

    def test_pending_and_running_both_restored(self, tmp_path, monkeypatch):
        _setup_workers_module(tmp_path)
        monkeypatch.setattr(queue_mod, "QUEUE_SNAPSHOT_PATH", tmp_path / "state" / "queue_snapshot.json")
        monkeypatch.setattr("supervisor.state.load_state", lambda: {})
        monkeypatch.setattr("supervisor.state.save_state", lambda st: None)

        snap = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "pending_count": 1,
            "running_count": 1,
            "pending": [
                {"id": "evo01", "task": {"id": "evo01", "type": "evolution", "chat_id": 111, "text": "evolve"}},
            ],
            "running": [
                {"id": "rev01", "task": {"id": "rev01", "type": "review", "chat_id": 111, "text": "REVIEW: test"}},
            ],
        }
        (tmp_path / "state" / "queue_snapshot.json").write_text(
            json.dumps(snap), encoding="utf-8"
        )

        restored = queue_mod.restore_pending_from_snapshot()

        assert restored == 2
        ids = {t["id"] for t in workers.PENDING}
        assert "evo01" in ids
        assert "rev01" in ids

    def test_stale_snapshot_not_restored(self, tmp_path, monkeypatch):
        _setup_workers_module(tmp_path)
        monkeypatch.setattr(queue_mod, "QUEUE_SNAPSHOT_PATH", tmp_path / "state" / "queue_snapshot.json")

        old_ts = "2020-01-01T00:00:00+00:00"
        snap = {
            "ts": old_ts,
            "pending": [{"id": "old", "task": {"id": "old", "type": "task", "chat_id": 1, "text": "old"}}],
            "running": [],
        }
        (tmp_path / "state" / "queue_snapshot.json").write_text(
            json.dumps(snap), encoding="utf-8"
        )

        restored = queue_mod.restore_pending_from_snapshot()

        assert restored == 0
        assert len(workers.PENDING) == 0
