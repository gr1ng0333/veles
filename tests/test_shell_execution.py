import os
import pathlib
import re
import shutil
import sys
import time

import pytest

from ouroboros.context import compact_tool_history
from ouroboros.loop import _truncate_tool_result
from ouroboros.tools import shell
from ouroboros.tools.registry import ToolContext, ToolRegistry


def _registry(tmp_path):
    registry = ToolRegistry(tmp_path, tmp_path)
    registry.set_context(ToolContext(tmp_path, tmp_path, task_id="shell-test"))
    return registry


def test_shell_uses_runtime_venv_for_subprocess_path(tmp_path, monkeypatch):
    runtime = tmp_path / "venv"
    executable = runtime / "bin/python3"
    monkeypatch.setattr(shell.sys, "prefix", str(runtime))
    monkeypatch.setattr(shell.sys, "base_prefix", str(tmp_path / "system-python"))
    monkeypatch.setattr(shell.sys, "executable", str(executable))
    monkeypatch.setenv("PATH", str(tmp_path / "system-bin"))
    env = shell._command_environment()
    assert env["PATH"].split(os.pathsep)[0] == str(executable.parent)
    assert env["VIRTUAL_ENV"] == str(runtime)
    assert os.environ["PATH"] == str(tmp_path / "system-bin")


def test_shell_fails_before_launch_with_missing_cwd(tmp_path):
    tools = _registry(tmp_path)
    result = tools.execute("run_shell", {
        "cmd": [sys.executable, "-c", "from pathlib import Path; Path('marker').touch()"],
        "cwd": "missing",
    })
    assert result.startswith("⚠️ SHELL_CWD_ERROR:")
    assert not (tmp_path / "marker").exists()


def test_long_output_exposes_exit_tail_and_saved_pages(tmp_path, monkeypatch):
    tools = _registry(tmp_path)
    secret = "example-private-runtime-value"
    monkeypatch.setenv("VELES_TEST_PASSWORD", secret)
    result = tools.execute("run_shell", {
        "cmd": [sys.executable, "-c", "import os, sys; print('header'); "
                "print('x' * 2300); print('MIDDLE'); print('x' * 2300); "
                "print(os.environ['VELES_TEST_PASSWORD']); print('failure summary', file=sys.stderr); sys.exit(3)"],
    })
    assert result.startswith("exit_code=3\n")
    assert "failure summary" in result and secret not in result
    assert result == _truncate_tool_result(result, "run_shell", "codex")
    path = re.search(r'drive_read\(path="([^"]+)"', result).group(1)
    page = tools.execute("drive_read", {"path": path, "offset": 0})
    revision = page.splitlines()[0].split("=", 1)[1]
    next_page = tools.execute("drive_read", {"path": path, "offset": 2000, "revision": revision})
    assert "MIDDLE" in next_page
    saved = (tmp_path / path).read_text(encoding="utf-8")
    assert "failure summary" in saved and "***REDACTED***" in saved and secret not in saved
    calls = [
        {"role": "assistant", "tool_calls": [
            {"id": "shell", "function": {"name": "run_shell", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "shell", "content": result},
        {"role": "assistant", "tool_calls": [
            {"id": "next", "function": {"name": "repo_read", "arguments": "{}"}},
        ]},
    ]
    compacted = compact_tool_history(calls, keep_recent=1)[1]["content"]
    assert path in compacted and len(compacted) < len(result)


def test_shell_loop_timeout_is_above_every_allowed_process_timeout(tmp_path):
    tools = _registry(tmp_path)
    schema = tools.get_schema_by_name("run_shell")["function"]
    timeout = schema["parameters"]["properties"]["timeout_sec"]
    assert tools.get_timeout("run_shell") > timeout["maximum"] >= timeout["default"] > 60


@pytest.mark.skipif(os.name != "posix" or not shutil.which("bash"), reason="POSIX Bash runtime")
def test_explicit_bash_script_preserves_pipeline_failure(tmp_path):
    tools = _registry(tmp_path)
    result = tools.execute("run_shell", {"script": "false | cat\nprintf '%s\\n' \"$?\"\nfalse | cat"})
    assert result.startswith("exit_code=1\n")
    assert result.rstrip().endswith("\n1")


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_shell_timeout_kills_children_and_keeps_partial_output(tmp_path):
    tools = _registry(tmp_path)
    marker = tmp_path / "should-not-exist"
    child = "import sys,time,pathlib; time.sleep(1.4); pathlib.Path(sys.argv[1]).touch()"
    parent = (
        "import sys,subprocess,time; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
        "print('started', flush=True); time.sleep(30)"
    )
    result = tools.execute("run_shell", {
        "cmd": [sys.executable, "-c", parent, child, str(marker)], "timeout_sec": 1,
    })
    assert result.startswith("⚠️ SHELL_TIMEOUT:") and "started" in result
    time.sleep(0.8)
    assert not marker.exists()
