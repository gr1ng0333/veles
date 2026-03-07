import queue
from unittest.mock import patch


def test_enqueue_chat_direct_uses_single_sticky_worker_thread(monkeypatch):
    from supervisor import workers

    seen = []

    class FakeThread:
        started = 0

        def __init__(self, target=None, name=None, daemon=None):
            self._target = target
            self._name = name
            self._daemon = daemon
            self._alive = False

        def start(self):
            FakeThread.started += 1
            self._alive = True

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(workers, "_direct_chat_queue", None)
    monkeypatch.setattr(workers, "_direct_chat_thread", None)
    monkeypatch.setattr(workers.threading, "Thread", FakeThread)

    task1 = workers.enqueue_chat_direct(1, "first")
    q1 = workers._direct_chat_queue
    t1 = workers._direct_chat_thread
    task2 = workers.enqueue_chat_direct(1, "second")

    assert isinstance(q1, queue.Queue)
    assert workers._direct_chat_queue is q1
    assert workers._direct_chat_thread is t1
    assert FakeThread.started == 1
    assert q1.qsize() == 2
    assert task1["_is_direct_chat"] is True
    assert task2["text"] == "second"


def test_auto_resume_enqueues_instead_of_spawning_raw_thread(tmp_path, monkeypatch):
    from supervisor import workers

    drive_root = tmp_path
    (drive_root / "logs").mkdir(parents=True, exist_ok=True)
    (drive_root / "memory").mkdir(parents=True, exist_ok=True)
    (drive_root / "state").mkdir(parents=True, exist_ok=True)
    (drive_root / "memory" / "scratchpad.md").write_text("# Scratchpad\nresume me\n", encoding="utf-8")

    monkeypatch.setattr(workers, "DRIVE_ROOT", drive_root)
    monkeypatch.setattr(workers.time, "sleep", lambda *_: None)

    state = {
        "owner_chat_id": 123,
        "launcher_session_id": "sess-1",
        "auto_resume_consumed_session_id": "",
        "resume_needed": True,
        "resume_reason": "interrupted_work",
        "suppress_auto_resume_until_owner_message": False,
    }

    saved = {}
    enqueued = []

    class BusyAgent:
        _busy = False

    monkeypatch.setattr(workers, "load_state", lambda: dict(state))
    monkeypatch.setattr(workers, "save_state", lambda st: saved.update(st))
    monkeypatch.setattr(workers, "_get_chat_agent", lambda: BusyAgent())
    monkeypatch.setattr(workers, "enqueue_chat_direct", lambda cid, txt, img=None: enqueued.append((cid, txt, img)))

    workers.auto_resume_after_restart()

    assert enqueued, "auto-resume must enqueue direct chat work"
    assert enqueued[0][0] == 123
    assert "auto-resume after restart" in enqueued[0][1]
    assert saved["resume_needed"] is False
    assert saved["auto_resume_consumed_session_id"] == "sess-1"
