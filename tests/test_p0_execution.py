"""Failures and actual checkpoint continuation, with no provider/network calls."""
import json
import queue
from pathlib import Path
from unittest.mock import Mock

import pytest

from ouroboros.task_execution import TaskCheckpoint, outcome, recoverable_tasks, stop_execution, settle_execution
from ouroboros.tools.registry import ToolContext, ToolEntry, ToolRegistry


class ProcessLost(BaseException):
    pass


TASK = {"id": "p0task", "type": "task", "chat_id": 42, "text": "Read once, report the result"}


def make_tools(root, checkpoint, effects):
    tools = ToolRegistry.__new__(ToolRegistry)
    tools._whitelist = None
    tools._ctx = ToolContext(repo_dir=root, drive_root=root, pending_events=[])
    tools._ctx.checkpoint = checkpoint
    def read(ctx):
        effects.append("read")
        return "durable-tool-result"
    tools._entries = {"repo_read": ToolEntry("repo_read", {"name": "repo_read", "description": "Read",
                       "parameters": {"type": "object", "properties": {}}}, read)}
    return tools


def run_loop(root, checkpoint, effects, responses, monkeypatch):
    from ouroboros import loop_runtime as rt
    monkeypatch.setattr(rt, "get_runtime_diagnostics", lambda: {})
    monkeypatch.setattr(rt, "execution_style_for_active_mode", lambda: "loop")
    monkeypatch.setattr(rt, "tools_enabled_for_active_mode", lambda: True)
    monkeypatch.setattr("ouroboros.pricing.get_pricing", lambda: {})
    llm = Mock()
    llm.default_model.return_value = "codex/gpt-6.1-sol"
    llm.chat.side_effect = [(r, {"prompt_tokens": 20, "completion_tokens": 100}) for r in responses]
    tools = make_tools(root, checkpoint, effects)
    checkpoint.begin()
    value = rt.run_llm_loop_impl(
        [{"role": "user", "content": TASK["text"]}], tools, llm, root / "logs",
        lambda text: None, queue.Queue(), task_type="task", task_id=TASK["id"], drive_root=root,
    )
    return value, llm


CALL = {"role": "assistant", "content": "", "tool_calls": [
    {"id": "call_p0", "type": "function", "function": {"name": "repo_read", "arguments": "{}"}}]}
FINAL = {"role": "assistant", "content": "Read done"}


@pytest.mark.parametrize("boundary", ["ready", "response_ready"])
def test_resume_preserves_tool_results_rounds_and_does_not_restart(tmp_path, monkeypatch, boundary):
    effects = []
    original = TaskCheckpoint.save
    def die(self, phase, state, messages, ctx, seen, response=None):
        original(self, phase, state, messages, ctx, seen, response)
        if phase == boundary and state["round_idx"] == 1:
            raise ProcessLost()
    monkeypatch.setattr(TaskCheckpoint, "save", die)
    with pytest.raises(ProcessLost):
        run_loop(tmp_path, TaskCheckpoint(tmp_path, TASK), effects, [CALL, FINAL], monkeypatch)
    assert effects == (["read"] if boundary == "ready" else [])
    monkeypatch.setattr(TaskCheckpoint, "save", original)
    restored = TaskCheckpoint(tmp_path, {**TASK, "_resume_execution": True})
    assert restored.recovery_problem() == ""
    value, llm = run_loop(tmp_path, restored, effects, [FINAL], monkeypatch)
    assert effects == ["read"]
    assert llm.chat.call_count == 1
    sent = llm.chat.call_args.kwargs["messages"]
    assert any(m.get("content") == "durable-tool-result" for m in sent)
    assert value[1]["rounds"] == 2
    assert value[0] == "Read done"


def make_agent(root, monkeypatch):
    from ouroboros import agent as mod
    monkeypatch.setattr(mod.OuroborosAgent, "_log_worker_boot_once", lambda self: None)
    monkeypatch.setattr(mod, "sync_mode_env_from_state", lambda: None)
    monkeypatch.setattr(mod, "get_runtime_diagnostics", lambda: {})
    monkeypatch.setattr("supervisor.state.load_state", lambda: {})
    monkeypatch.setattr("supervisor.state.save_state", lambda st: None)
    a = mod.OuroborosAgent(mod.Env(root, root))
    def prepare(task):
        ctx = ToolContext(repo_dir=root, drive_root=root, pending_events=a._pending_events)
        a.tools.set_context(ctx)
        return ctx, [{"role": "user", "content": "test"}], {}
    monkeypatch.setattr(a, "_prepare_task_context", prepare)
    return a, mod


@pytest.mark.parametrize("phase", ["preparing", "model_inflight", "tools_inflight", "missing"])
def test_unknown_outcome_is_interrupted_without_any_model_or_tool_call(tmp_path, monkeypatch, phase):
    if phase != "missing":
        cp = TaskCheckpoint(tmp_path, TASK)
        cp.begin()
        cp.data["phase"] = phase
        cp.write()
    a, mod = make_agent(tmp_path, monkeypatch)
    loop = Mock(side_effect=AssertionError("must not replay"))
    monkeypatch.setattr(mod, "run_llm_loop", loop)
    events = a.handle_task({**TASK, "_resume_execution": True})
    done = next(e for e in events if e["type"] == "task_done")
    assert done["ok"] is False and done["status"] == "interrupted"
    assert "recovery_unknown_outcome" in done["reason"]
    loop.assert_not_called()
    assert json.loads((tmp_path / "task_results/p0task.json").read_text())["status"] == "interrupted"


@pytest.mark.parametrize("mode", ["exception", "empty", "model_failure", "tool_error", "limit", "success"])
def test_result_event_eval_and_parent_file_agree(tmp_path, monkeypatch, mode):
    a, mod = make_agent(tmp_path, monkeypatch)
    usage = {"execution_status": "failed", "execution_reason": "model_unavailable"} if mode == "model_failure" else {}
    if mode == "limit":
        usage.update(execution_status="interrupted", execution_reason="round_limit")
    loop = Mock(side_effect=RuntimeError("boom")) if mode == "exception" else Mock(
        return_value=("" if mode == "empty" else "answer", usage,
                      {"tool_calls": [{"is_error": True}] if mode == "tool_error" else []}))
    monkeypatch.setattr(mod, "run_llm_loop", loop)
    events = a.handle_task(TASK)
    done = next(e for e in events if e["type"] == "task_done")
    expected = "completed" if mode == "success" else "interrupted" if mode == "limit" else "failed"
    assert done["status"] == expected and done["ok"] == (expected == "completed")
    stored = json.loads((tmp_path / "task_results/p0task.json").read_text())
    assert stored["status"] == expected
    rows = [json.loads(l) for l in (tmp_path / "logs/events.jsonl").read_text().splitlines()]
    for r in rows:
        if r["type"] in ("task_done", "task_eval"):
            assert r["status"] == expected and r["ok"] == done["ok"]
    # Redelivery of a finished task must never re-execute the model or tools.
    a.handle_task(TASK)
    assert loop.call_count == 1


def test_preparation_error_emits_failed_completion(tmp_path, monkeypatch):
    a, mod = make_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(a, "_prepare_task_context", Mock(side_effect=RuntimeError("context failed")))
    loop = Mock()
    monkeypatch.setattr(mod, "run_llm_loop", loop)
    events = a.handle_task(TASK)
    done = next(e for e in events if e["type"] == "task_done")
    assert done["status"] == "failed" and done["ok"] is False
    loop.assert_not_called()


def test_finished_response_recovered_until_delivery_settled(tmp_path):
    cp = TaskCheckpoint(tmp_path, TASK)
    cp.begin()
    cp.finish("cached", {}, {}, [])
    assert recoverable_tasks(tmp_path)[0]["id"] == TASK["id"]
    settle_execution(tmp_path, TASK["id"])
    assert recoverable_tasks(tmp_path) == []


def test_unknown_control_event_delivery_is_not_replayed(tmp_path, monkeypatch):
    cp = TaskCheckpoint(tmp_path, TASK)
    cp.begin()
    cp.finish("done", {}, {}, [{"type": "schedule_task", "task_id": "child"}])
    a, mod = make_agent(tmp_path, monkeypatch)
    loop = Mock()
    monkeypatch.setattr(mod, "run_llm_loop", loop)
    events = a.handle_task({**TASK, "_resume_execution": True})
    assert not any(e["type"] == "schedule_task" for e in events)
    done = next(e for e in events if e["type"] == "task_done")
    assert done["status"] == "interrupted" and done["reason"].endswith("event_delivery")
    loop.assert_not_called()


def test_restart_request_is_delivered_after_task_done(tmp_path, monkeypatch):
    a, mod = make_agent(tmp_path, monkeypatch)
    def run(**kwargs):
        a._pending_events.append({"type": "restart_request", "reason": "test"})
        return "Update checked", {}, {}
    monkeypatch.setattr(mod, "run_llm_loop", run)
    events = a.handle_task(TASK)
    kinds = [event["type"] for event in events]
    assert kinds[-1] == "restart_request" and kinds.index("task_done") < kinds.index("restart_request")
    assert TaskCheckpoint(tmp_path, TASK).data["phase"] == "finished"


def test_queued_personal_chat_uses_xhigh_and_recovers_without_working_copy(tmp_path, monkeypatch):
    a, mod = make_agent(tmp_path, monkeypatch)
    a._prepare_task_context = lambda task: mod.OuroborosAgent._prepare_task_context(a, task)
    monkeypatch.setenv("OUROBOROS_REASONING_EFFORT", "medium")
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return "Личный ответ", {}, {}

    monkeypatch.setattr(mod, "run_llm_loop", run)
    task = {**TASK, "conversation_mode": "personal"}
    TaskCheckpoint(tmp_path, task).queue()
    recovered, = recoverable_tasks(tmp_path)
    assert recovered["_resume_execution"]
    events = a.handle_task(recovered)
    assert calls[0]["initial_effort"] == "xhigh"
    assert calls[0]["messages"][-1]["content"] == TASK["text"]
    assert calls[0]["tools"].get_schema_by_name("run_shell")
    assert all(event["conversation_mode"] == "personal" for event in events)
    assert any(event["type"] == "task_done" and event["ok"] for event in events)
    assert (tmp_path / "personal/logs/events.jsonl").exists()
    assert (tmp_path / "personal/task_results/p0task.json").exists()
    assert not (tmp_path / "task_results/p0task.json").exists()

    a.handle_task({**TASK, "id": "back-to-work"})
    assert calls[1]["initial_effort"] == "medium"


def test_stale_running_snapshot_cannot_revive_settled_task(tmp_path, monkeypatch):
    from supervisor import queue as q
    cp = TaskCheckpoint(tmp_path, TASK)
    cp.begin()
    cp.finish("done", {}, {}, [{"type": "restart_request"}])
    settle_execution(tmp_path, TASK["id"])
    snapshot = tmp_path / "state/queue_snapshot.json"
    snapshot.write_text(json.dumps({
        "ts": "2026-11-08T21:24:41+00:00", "running": [{"task": TASK}],
        "pending": [{"task": TASK}],
    }), encoding="utf-8")
    monkeypatch.setattr(q, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(q, "QUEUE_SNAPSHOT_PATH", snapshot)
    monkeypatch.setattr(q, "PENDING", [])
    assert q.restore_pending_from_snapshot() == 0
    assert q.PENDING == [] and TaskCheckpoint(tmp_path, TASK).data["delivery_settled"]


@pytest.mark.parametrize("mode", ["exception", "empty", "tool_error", "limit", "success"])
def test_focused_worker_propagates_same_outcomes(tmp_path, monkeypatch, mode):
    from ouroboros import focused_agent as mod
    monkeypatch.setattr(mod.FocusedAgent, "_log_boot", lambda self: None)
    monkeypatch.setattr(mod, "sync_mode_env_from_state", lambda: None)
    a = mod.FocusedAgent(str(tmp_path), str(tmp_path))
    usage = {"execution_status": "interrupted", "execution_reason": "round_limit"} if mode == "limit" else {}
    loop = Mock(side_effect=RuntimeError("boom")) if mode == "exception" else Mock(return_value=(
        "" if mode == "empty" else "answer", usage,
        {"tool_calls": [{"is_error": True}] if mode == "tool_error" else []}))
    monkeypatch.setattr(mod, "run_llm_loop", loop)
    events = a.handle_task({**TASK, "type": "focused"})
    done = next(e for e in events if e["type"] == "task_done")
    expected = "completed" if mode == "success" else "interrupted" if mode == "limit" else "failed"
    assert done["status"] == expected and done["ok"] == (expected == "completed")
    assert json.loads((tmp_path / "task_results/p0task.json").read_text())["status"] == expected
    a.handle_task({**TASK, "type": "focused"})
    assert loop.call_count == 1


@pytest.mark.parametrize("response,reason", [(None, "model_unavailable"),
    ({**CALL, "_execution_incomplete": True}, "provider_incomplete")])
def test_real_loop_failure_paths_cannot_return_success(tmp_path, monkeypatch, response, reason):
    from ouroboros import loop_runtime as rt
    monkeypatch.setattr(rt, "_call_llm_with_fallback", lambda **kwargs: response)
    effects = []
    value, llm = run_loop(tmp_path, TaskCheckpoint(tmp_path, TASK), effects, [], monkeypatch)
    assert not outcome(value[1])["ok"] and outcome(value[1])["reason"] == reason
    assert effects == []


def test_claudexor_incomplete_signal_is_preserved():
    from ouroboros.claudexor_transport import _to_chat_message
    msg = _to_chat_message({"outcome": "incomplete", "message": FINAL}, "op-test")
    assert msg["_execution_incomplete"] is True


def test_supervisor_fallback_result_and_notification_keep_failure(tmp_path):
    from types import SimpleNamespace
    from supervisor.events import _handle_task_done
    cp = TaskCheckpoint(tmp_path, TASK)
    cp.begin()
    cp.finish("failed", {"execution_status": "failed"}, {}, [])
    sent = []
    ctx = SimpleNamespace(DRIVE_ROOT=tmp_path, RUNNING={}, WORKERS={},
        load_state=lambda: {"owner_chat_id": 42}, save_state=lambda st: None,
        append_jsonl=lambda *args: None, persist_queue_snapshot=lambda **kw: None,
        send_with_budget=lambda *args, **kw: sent.append(args))
    _handle_task_done({"task_id": TASK["id"], "task_type": "task", "ok": False,
                      "status": "failed", "reason": "model_unavailable", "total_rounds": 1}, ctx)
    result = json.loads((tmp_path / "task_results/p0task.json").read_text())
    assert result["status"] == "failed" and result["reason"] == "model_unavailable"
    assert TaskCheckpoint(tmp_path, TASK).data["delivery_settled"] is True
    assert not any("✅ Done" in str(args) for args in sent)


def test_corrupt_checkpoint_blocks_replay_and_keeps_evidence(tmp_path):
    cp = TaskCheckpoint(tmp_path, TASK)
    cp.begin()
    cp.path.write_text("{broken", encoding="utf-8")
    loaded = TaskCheckpoint(tmp_path, TASK)
    assert loaded.recovery_problem().endswith("invalid_checkpoint")
    assert loaded.path.with_suffix(".invalid").read_text() == "{broken"


def test_cancelled_tasks_are_not_recovered_from_checkpoint_scan(tmp_path):
    cp = TaskCheckpoint(tmp_path, TASK)
    cp.begin()
    assert recoverable_tasks(tmp_path)[0]["id"] == TASK["id"]
    stop_execution(tmp_path, TASK, "cancelled", "owner_cancelled")
    assert recoverable_tasks(tmp_path) == []
    assert outcome(TaskCheckpoint(tmp_path, TASK).data["result"]["usage"])["status"] == "cancelled"


def test_long_outage_preserves_full_queued_payload_and_marks_running_as_resume(tmp_path, monkeypatch):
    from supervisor import queue as q
    pending = []
    monkeypatch.setattr(q, "PENDING", pending)
    monkeypatch.setattr(q, "RUNNING", {})
    monkeypatch.setattr(q, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(q, "QUEUE_SEQ_COUNTER_REF", {"value": 0})
    monkeypatch.setattr(q, "QUEUE_SNAPSHOT_PATH", tmp_path / "state/queue_snapshot.json")
    monkeypatch.setattr(q, "load_state", lambda: {})
    monkeypatch.setattr(q, "save_state", lambda st: None)
    q.QUEUE_SNAPSHOT_PATH.parent.mkdir()
    focused = {**TASK, "id": "queued", "model": "codex/custom", "tool_whitelist": "CODE", "system_prompt": "keep me"}
    q.QUEUE_SNAPSHOT_PATH.write_text(json.dumps({"ts": "2020-01-01T00:00:00+00:00",
        "pending": [{"task": focused}], "running": [{"task": TASK}]}))
    assert q.restore_pending_from_snapshot() == 2
    assert next(t for t in pending if t["id"] == "queued")["system_prompt"] == "keep me"
    assert next(t for t in pending if t["id"] == TASK["id"])["_resume_execution"] is True
    q.persist_queue_snapshot()
    assert json.loads(q.QUEUE_SNAPSHOT_PATH.read_text())["pending"][1]["task"]["system_prompt"] == "keep me"


def test_direct_chat_checkpoint_is_found_without_queue_snapshot(tmp_path, monkeypatch):
    from supervisor import queue as q
    cp = TaskCheckpoint(tmp_path, {**TASK, "_is_direct_chat": True})
    cp.begin()
    monkeypatch.setattr(q, "PENDING", [])
    monkeypatch.setattr(q, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(q, "QUEUE_SNAPSHOT_PATH", tmp_path / "no-snapshot.json")
    monkeypatch.setattr(q, "QUEUE_SEQ_COUNTER_REF", {"value": 0})
    monkeypatch.setattr(q, "load_state", lambda: {})
    monkeypatch.setattr(q, "save_state", lambda st: None)
    assert q.restore_pending_from_snapshot() == 1
    assert q.PENDING[0]["_is_direct_chat"] and q.PENDING[0]["_resume_execution"]
