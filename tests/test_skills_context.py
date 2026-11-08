"""Skills and tools travel through real model requests and durable task recovery."""

import copy
import json
import queue
from unittest.mock import Mock

import pytest

from ouroboros import loop_runtime as runtime
from ouroboros.task_execution import TaskCheckpoint
from ouroboros.tools.registry import ToolContext, ToolRegistry


TASK = {"id": "skill-test", "chat_id": 42, "type": "task", "text": "Проверь SSH и объясни причину"}
BODY = "# Host runbook\n\n" + "Complete instructions. " * 200
FINAL = {"role": "assistant", "content": "Done"}


class ProcessLost(BaseException):
    pass


def tool_response(*calls):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for i, (name, args) in enumerate(calls)
    ]}


def run_loop(root, task, checkpoint, responses, requests):
    tools = ToolRegistry(root, root)
    ctx = ToolContext(repo_dir=root, drive_root=root)
    ctx.checkpoint = checkpoint
    tools.set_context(ctx)
    llm = Mock()
    llm.default_model.return_value = "codex/gpt-6.1-sol"
    replies = iter(responses)

    def reply(**request):
        requests.append(copy.deepcopy(request))
        return next(replies), {"prompt_tokens": 100, "completion_tokens": 100}

    llm.chat.side_effect = reply
    if checkpoint:
        checkpoint.begin()
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "Base context"}]},
        {"role": "user", "content": task["text"]},
    ]
    text, _, _ = runtime.run_llm_loop_impl(
        messages, tools, llm, root / "logs", lambda text: None, queue.Queue(),
        task_id=task["id"], drive_root=root,
    )
    assert text == "Done"


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(runtime, "get_runtime_diagnostics", lambda: {})
    monkeypatch.setattr(runtime, "execution_style_for_active_mode", lambda: "loop")
    monkeypatch.setattr(runtime, "tools_enabled_for_active_mode", lambda: True)
    monkeypatch.setattr("ouroboros.pricing.get_pricing", lambda: {})


def skill_text(request):
    return "\n".join(part["text"] for part in request["messages"][0]["content"])


def schema_names(request):
    return [schema["function"]["name"] for schema in request["tools"]]


@pytest.mark.parametrize("recover", [False, True])
def test_model_controls_skill_and_tool_loading(tmp_path, monkeypatch, offline, recover):
    source = tmp_path / "prompts/skills/host.md"
    source.parent.mkdir(parents=True)
    source.write_text(BODY, encoding="utf-8")

    checkpoint = TaskCheckpoint(tmp_path, TASK)
    requests = []
    load = tool_response(("skill_load", {"name": "host"}), ("enable_tools", {"tools": "repo_list"}))
    reload = tool_response(("skill_load", {"name": "host"}))
    if recover:
        save = TaskCheckpoint.save

        def interrupt(self, phase, state, messages, ctx, seen, response=None):
            save(self, phase, state, messages, ctx, seen, response)
            if phase == "ready" and state["round_idx"] == 1:
                raise ProcessLost()

        monkeypatch.setattr(TaskCheckpoint, "save", interrupt)
        with pytest.raises(ProcessLost):
            run_loop(tmp_path, TASK, checkpoint, [load], requests)
        monkeypatch.setattr(TaskCheckpoint, "save", save)
        source.write_text("A changed runbook.", encoding="utf-8")
        restored = TaskCheckpoint(tmp_path, {**TASK, "_resume_execution": True})
        assert not restored.recovery_problem()
        run_loop(tmp_path, TASK, restored, [reload, FINAL], requests)
    else:
        run_loop(tmp_path, TASK, checkpoint, [load, reload, FINAL], requests)

    start, following, final = requests
    assert "skill_load" in schema_names(start)
    assert "repo_list" not in schema_names(start) and BODY not in skill_text(start)
    for request in (following, final):
        assert skill_text(request).count(BODY) == 1
        assert "A changed runbook." not in skill_text(request)
        assert schema_names(request).count("repo_list") == 1

    requests.clear()
    run_loop(tmp_path, {**TASK, "id": "new-task"}, None, [FINAL], requests)
    assert "## Task skill:" not in skill_text(requests[0])
    assert "repo_list" not in schema_names(requests[0])


def test_discovery_respects_explicit_allowlist(tmp_path):
    from ouroboros.tools.tool_discovery import bind_tool_discovery

    tools = ToolRegistry(tmp_path, tmp_path)
    tools.set_whitelist({"repo_list", "list_available_tools", "enable_tools"})
    resident = tools.schemas(core_only=True)
    bind_tool_discovery(tools, resident)
    assert "repo_list [loaded]" in tools.execute("list_available_tools", {"query": "repo_list"})
    assert "No callable tools match" in tools.execute("list_available_tools", {"query": "run_shell"})
    result = tools.execute("enable_tools", {"tools": "repo_list,repo_list,run_shell"})
    assert "Already loaded: repo_list" in result
    assert "Unavailable for this task: run_shell" in result
    assert [row['name'] for row in resident].count('repo_list') == 1
    assert 'run_shell' not in {row['name'] for row in resident}
