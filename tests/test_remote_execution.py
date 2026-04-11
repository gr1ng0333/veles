import json
import pathlib
import subprocess

from ouroboros.tools.registry import ToolContext
from ouroboros.tools import remote_execution


def _ctx(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path, pending_events=[])


def test_remote_command_exec_read_only_success(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(remote_execution, "_get_target_record", lambda _ctx, alias: {"alias": alias, "host": "example", "port": 22, "user": "root", "auth_mode": "key", "ssh_key_path": "/tmp/key"})
    monkeypatch.setattr(remote_execution, "_bootstrap_session", lambda _ctx, alias: {"status": "ok"})
    monkeypatch.setattr(remote_execution, "_base_ssh_command", lambda _ctx, record: ["ssh", f"{record['user']}@{record['host']}"])

    def fake_run(cmd, cwd, capture_output, text, timeout):
        assert cmd[-1] == "cd /srv/app && ls -la"
        return subprocess.CompletedProcess(cmd, 0, stdout="file1\nfile2\n", stderr="")

    monkeypatch.setattr(remote_execution.subprocess, "run", fake_run)
    result = json.loads(remote_execution.remote_command_exec(ctx, alias="prod", command="ls -la", cwd="/srv/app"))
    assert result["status"] == "ok"
    assert result["mutation_risk_level"] == "read_only"
    assert ctx.pending_events[-1]["type"] == "remote_execution"
    assert ctx.pending_events[-1]["exit_code"] == 0


def test_remote_command_exec_denies_mutation_in_read_only(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(remote_execution, "_get_target_record", lambda _ctx, alias: {"alias": alias, "host": "example", "port": 22, "user": "root", "auth_mode": "key", "ssh_key_path": "/tmp/key"})
    result = json.loads(remote_execution.remote_command_exec(ctx, alias="prod", command="mkdir build", cwd="/srv/app"))
    assert result["status"] == "error"
    assert result["kind"] == "policy_deny"
    assert "not allowed" in result["error"]
    assert ctx.pending_events[-1]["error_kind"] == "policy_deny"


def test_remote_command_exec_allows_mutating_mode(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(remote_execution, "_get_target_record", lambda _ctx, alias: {"alias": alias, "host": "example", "port": 22, "user": "root", "auth_mode": "key", "ssh_key_path": "/tmp/key"})
    monkeypatch.setattr(remote_execution, "_bootstrap_session", lambda _ctx, alias: {"status": "ok"})
    monkeypatch.setattr(remote_execution, "_base_ssh_command", lambda _ctx, record: ["ssh", f"{record['user']}@{record['host']}"])
    monkeypatch.setattr(remote_execution.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="ok", stderr=""))
    result = json.loads(remote_execution.remote_command_exec(ctx, alias="prod", command="mkdir build", execution_mode="mutating"))
    assert result["status"] == "ok"
    assert result["mutation_risk_level"] == "mutating"


def test_remote_command_exec_normalizes_missing_cwd(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(remote_execution, "_get_target_record", lambda _ctx, alias: {"alias": alias, "host": "example", "port": 22, "user": "root", "auth_mode": "key", "ssh_key_path": "/tmp/key"})
    monkeypatch.setattr(remote_execution, "_bootstrap_session", lambda _ctx, alias: {"status": "ok"})
    monkeypatch.setattr(remote_execution, "_base_ssh_command", lambda _ctx, record: ["ssh", f"{record['user']}@{record['host']}"])
    monkeypatch.setattr(remote_execution.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout="", stderr="sh: line 1: cd: /missing: No such file or directory"))
    result = json.loads(remote_execution.remote_command_exec(ctx, alias="prod", command="ls", cwd="/missing"))
    assert result["status"] == "error"
    assert result["kind"] == "cwd_missing"
    assert ctx.pending_events[-1]["error_kind"] == "cwd_missing"


def test_remote_command_exec_timeout(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(remote_execution, "_get_target_record", lambda _ctx, alias: {"alias": alias, "host": "example", "port": 22, "user": "root", "auth_mode": "key", "ssh_key_path": "/tmp/key"})
    monkeypatch.setattr(remote_execution, "_bootstrap_session", lambda _ctx, alias: {"status": "ok"})
    monkeypatch.setattr(remote_execution, "_base_ssh_command", lambda _ctx, record: ["ssh", f"{record['user']}@{record['host']}"])

    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 20))

    monkeypatch.setattr(remote_execution.subprocess, "run", raise_timeout)
    result = json.loads(remote_execution.remote_command_exec(ctx, alias="prod", command="find /", execution_mode="mutating", timeout_sec=3))
    assert result["status"] == "error"
    assert result["kind"] == "timeout"
    assert ctx.pending_events[-1]["error_kind"] == "timeout"


def test_remote_command_exec_allows_read_only_diagnostics(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(remote_execution, "_get_target_record", lambda _ctx, alias: {"alias": alias, "host": "example", "port": 22, "user": "root", "auth_mode": "key", "ssh_key_path": "/tmp/key"})
    monkeypatch.setattr(remote_execution, "_bootstrap_session", lambda _ctx, alias: {"status": "ok"})
    monkeypatch.setattr(remote_execution, "_base_ssh_command", lambda _ctx, record: ["ssh", f"{record['user']}@{record['host']}"])

    def fake_run(cmd, cwd, capture_output, text, timeout):
        assert cmd[-1] == "cd . && systemctl show x-ui.service"
        return subprocess.CompletedProcess(cmd, 0, stdout="ActiveState=active\n", stderr="")

    monkeypatch.setattr(remote_execution.subprocess, "run", fake_run)
    result = json.loads(remote_execution.remote_command_exec(ctx, alias="prod", command="systemctl show x-ui.service"))
    assert result["status"] == "ok"
    assert result["mutation_risk_level"] == "read_only"


def test_remote_command_exec_denies_mutating_systemctl_in_read_only(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(remote_execution, "_get_target_record", lambda _ctx, alias: {"alias": alias, "host": "example", "port": 22, "user": "root", "auth_mode": "key", "ssh_key_path": "/tmp/key"})
    result = json.loads(remote_execution.remote_command_exec(ctx, alias="prod", command="systemctl restart x-ui.service"))
    assert result["status"] == "error"
    assert result["kind"] == "policy_deny"
