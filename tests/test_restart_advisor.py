import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def test_advise_restart_normalizes_json(monkeypatch):
    from supervisor.restart_advisor import advise_restart

    class DummyClient:
        def chat(self, **kwargs):
            return ({
                "content": '{"verdict":"hard_restart_recommended","confidence":0.82,"summary":"worker appears stuck","signals":["resume_needed=true"],"risks":["possible duplicate processing after crash"]}'
            }, {"cost": 0.01})

    monkeypatch.setattr("supervisor.restart_advisor.LLMClient", lambda: DummyClient())

    result = advise_restart(
        reason="task appears stuck",
        state={"resume_needed": True, "no_commit_streak": 2},
        pending_count=1,
        running_count=1,
    )

    assert result["ok"] is True
    assert result["verdict"] == "hard_restart_recommended"
    assert result["confidence"] == 0.82
    assert result["signals"] == ["resume_needed=true"]
    assert result["payload"]["contract_version"] == 2
    assert result["payload"]["signals"]["interrupted_work"] is True


def test_evaluate_restart_policy_blocks_hard_restart_on_active_work():
    from supervisor.restart_advisor import evaluate_restart_policy

    decision = evaluate_restart_policy(
        reason="manual restart",
        state={"resume_needed": False, "no_commit_streak": 0, "recent_restart_count": 0},
        pending_count=0,
        running_count=2,
        advisor_result={"verdict": "hard_restart_recommended", "confidence": 0.9},
    )

    assert decision["supervisor_action"] == "skip_restart"
    assert decision["policy"] == "active_work_guard"
    assert decision["blocked_by_active_work"] is True


def test_evaluate_restart_policy_allows_hard_restart_for_interrupted_work():
    from supervisor.restart_advisor import evaluate_restart_policy

    decision = evaluate_restart_policy(
        reason="resume after crash",
        state={"resume_needed": True, "resume_snapshot_pending_count": 1},
        pending_count=1,
        running_count=0,
        advisor_result={"verdict": "hard_restart_recommended", "confidence": 0.9},
    )

    assert decision["supervisor_action"] == "restart_now"
    assert decision["policy"] == "hard_restart_guard_pass"
    assert decision["hard_restart_allowed"] is True


def test_restart_request_fail_open_when_advisor_errors(tmp_path, monkeypatch):
    from supervisor.events import _handle_restart_request

    logs = []
    sent = []
    killed = []
    saved = []
    persisted = []

    monkeypatch.setattr("supervisor.restart_advisor.advise_restart", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("advisor boom")))
    monkeypatch.setattr("os.execv", lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit(0)))

    state = {"owner_chat_id": 123, "tg_offset": 7}
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        PENDING=[],
        RUNNING={},
        WORKERS={},
        load_state=lambda: dict(state),
        save_state=lambda st: saved.append(dict(st)),
        append_jsonl=lambda path, row: logs.append(row),
        send_with_budget=lambda chat_id, text, **kw: sent.append(text),
        safe_restart=lambda **kw: (True, "OK: veles"),
        kill_workers=lambda: killed.append(True),
        persist_queue_snapshot=lambda **kw: persisted.append(kw),
    )

    try:
        _handle_restart_request({"reason": "manual restart test"}, ctx)
    except SystemExit:
        pass

    assert any(row.get("type") == "restart_advisor_error" for row in logs)
    assert any("Restart requested by agent" in msg for msg in sent)
    assert killed == [True]
    assert persisted and persisted[-1].get("reason") == "pre_restart_exit"


def test_restart_request_policy_can_suppress_restart(tmp_path, monkeypatch):
    from supervisor.events import _handle_restart_request

    logs = []
    sent = []
    safe_restart_calls = []

    monkeypatch.setattr("supervisor.restart_advisor.advise_restart", lambda **kwargs: {
        "ok": True,
        "verdict": "hard_restart_recommended",
        "confidence": 0.91,
    })

    state = {"owner_chat_id": 123, "tg_offset": 7, "resume_needed": False}
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        PENDING=[],
        RUNNING={"t1": {"task": {"text": "still working"}}},
        WORKERS={},
        load_state=lambda: dict(state),
        save_state=lambda st: None,
        append_jsonl=lambda path, row: logs.append(row),
        send_with_budget=lambda chat_id, text, **kw: sent.append(text),
        safe_restart=lambda **kw: safe_restart_calls.append(kw) or (True, "OK: veles"),
        kill_workers=lambda: (_ for _ in ()).throw(AssertionError("kill_workers should not be called")),
        persist_queue_snapshot=lambda **kw: (_ for _ in ()).throw(AssertionError("persist_queue_snapshot should not be called")),
    )

    _handle_restart_request({"reason": "manual restart test"}, ctx)

    assert safe_restart_calls == []
    assert any(row.get("type") == "restart_advisor_verdict" for row in logs)
    assert any(row.get("type") == "restart_advisor_policy_decision" for row in logs)
    assert any("Restart suppressed by policy" in msg for msg in sent)


def test_events_restart_entrypoint_delegates_to_live_restart_flow(monkeypatch):
    from supervisor.events import _handle_restart_request

    called = []

    monkeypatch.setattr(
        "supervisor.restart_flow.handle_restart_request",
        lambda evt, ctx: called.append((evt, ctx)),
    )

    evt = {"reason": "delegation test"}
    ctx = object()
    _handle_restart_request(evt, ctx)

    assert called == [(evt, ctx)]


def test_restart_request_persists_post_restart_notification_handoff(tmp_path, monkeypatch):
    from supervisor.events import _handle_restart_request

    logs = []
    sent = []
    killed = []
    persisted = []
    saved_states = []

    monkeypatch.setattr("supervisor.restart_advisor.advise_restart", lambda **kwargs: {
        "ok": True,
        "verdict": "soft_restart_recommended",
        "confidence": 0.77,
    })
    monkeypatch.setattr("os.execv", lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit(0)))

    state = {"owner_chat_id": 123, "tg_offset": 7}

    def _load_state():
        return dict(state)

    def _save_state(st):
        state.update(dict(st))
        saved_states.append(dict(st))

    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        PENDING=[],
        RUNNING={},
        WORKERS={},
        load_state=_load_state,
        save_state=_save_state,
        append_jsonl=lambda path, row: logs.append(row),
        send_with_budget=lambda chat_id, text, **kw: sent.append(text),
        safe_restart=lambda **kw: (True, "OK: veles"),
        kill_workers=lambda: killed.append(True),
        persist_queue_snapshot=lambda **kw: persisted.append(kw),
    )

    try:
        _handle_restart_request({"reason": "deploy post-restart ack"}, ctx)
    except SystemExit:
        pass

    assert killed == [True]
    assert persisted and persisted[-1].get("reason") == "pre_restart_exit"
    assert saved_states, "restart flow must persist handoff state before execv"
    assert state["restart_notify_pending"] is True
    assert state["restart_notify_reason"] == "deploy post-restart ack"
    assert state["restart_notify_source"] == "agent_restart_request"
    assert state["tg_offset"] == 7
    assert state.get("session_id")


def test_send_photo_event_logs_success(tmp_path):
    from supervisor.events import _handle_send_photo

    logs = []
    sent = []

    class DummyTG:
        def send_photo(self, chat_id, photo_bytes, caption=""):
            sent.append({"chat_id": chat_id, "bytes": photo_bytes, "caption": caption})
            return True, None

    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        TG=DummyTG(),
        append_jsonl=lambda path, row: logs.append(row),
    )

    evt = {
        "chat_id": 123,
        "image_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
        "caption": "captcha",
        "source": "browser_last_screenshot",
        "task_id": "task-123",
        "task_type": "task",
        "is_direct_chat": True,
    }

    _handle_send_photo(evt, ctx)

    assert len(sent) == 1
    assert sent[0]["chat_id"] == 123
    assert sent[0]["caption"] == "captcha"
    assert any(row.get("type") == "send_photo_delivered" for row in logs)
    delivered = next(row for row in logs if row.get("type") == "send_photo_delivered")
    assert delivered["source"] == "browser_last_screenshot"
    assert delivered["task_id"] == "task-123"
    assert delivered["task_type"] == "task"
    assert delivered["is_direct_chat"] is True
