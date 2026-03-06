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
