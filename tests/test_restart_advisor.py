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

    evt = {"reason": "manual restart test"}
    try:
        _handle_restart_request(evt, ctx)
    except SystemExit:
        pass

    assert any(row.get("type") == "restart_advisor_error" for row in logs)
    assert any("Restart requested by agent" in msg for msg in sent)
    assert killed == [True]
    assert persisted and persisted[-1].get("reason") == "pre_restart_exit"
