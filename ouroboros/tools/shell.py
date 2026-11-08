"""Shell tools: run_shell."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import shlex
import signal
import subprocess
import sys
import time
import uuid
from typing import List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso, append_jsonl, sanitize_tool_result_for_log
from ouroboros.tools.git import _acquire_copilot_write_lock

log = logging.getLogger(__name__)
_DEFAULT_TIMEOUT_SEC = 120
_MAX_TIMEOUT_SEC = 300
_INLINE_CHARS = 1900
_STREAM_BYTES = 512 * 1024
_SAVED_RESULTS = 128


def _try_parse_python_list_string(s: str) -> Optional[List[str]]:
    """Try to parse a Python-style list string like `[grep, -n, pattern, file.py]`.

    LLMs sometimes produce this malformed format — elements without quotes.
    json.loads and ast.literal_eval both fail on it; this handles the gap.
    Returns list if parseable, None otherwise.
    """
    stripped = s.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        return None
    inner = stripped[1:-1].strip()
    if not inner:
        return None

    # Try JSON first (handles properly quoted elements like ['git', 'add'])
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except (json.JSONDecodeError, ValueError):
        pass

    # Heuristic split: split by comma, strip each element of whitespace and quotes.
    # Handles: [grep, -n, pattern, file.py] → ['grep', '-n', 'pattern', 'file.py']
    # Also handles: ['git', 'add', '-A'] via JSON above; this handles unquoted only.
    parts = [p.strip().strip("'\"").strip() for p in inner.split(",")]
    parts = [p for p in parts if p]
    if not parts:
        return None

    # Sanity: if any part still starts/ends with [ or ] it's malformed — fall back
    if any(p.startswith("[") or p.endswith("]") for p in parts):
        return None

    return parts


def _recover_argv(ctx: ToolContext, cmd: str) -> list[str]:
    recovered = _try_parse_python_list_string(cmd)
    if recovered is None:
        try:
            parsed = json.loads(cmd)
        except ValueError:
            parsed = cmd
        try:
            recovered = shlex.split(parsed if isinstance(parsed, str) else cmd)
        except ValueError:
            recovered = cmd.split()
    try:
        append_jsonl(ctx.drive_path("logs/events.jsonl"), {
            "ts": utc_now_iso(), "type": "tool_warning", "tool": "run_shell",
            "task_id": ctx.task_id, "warning": "run_shell_cmd_string_recovered",
        })
    except OSError:
        log.debug("Could not log recovered shell arguments", exc_info=True)
    return recovered


def _command_environment() -> dict[str, str]:
    env = os.environ.copy()
    if sys.prefix != sys.base_prefix:
        env["PATH"] = os.pathsep.join((str(pathlib.Path(sys.executable).parent), env.get("PATH", "")))
        env["VIRTUAL_ENV"] = sys.prefix
    return env


def _read_stream(data: bytes | None) -> str:
    data = data or b""
    size = len(data)
    if size <= _STREAM_BYTES:
        return data.decode("utf-8", errors="replace")
    half = _STREAM_BYTES // 2
    marker = f"\n[… {size - _STREAM_BYTES} bytes omitted from this stream …]\n".encode()
    return (data[:half] + marker + data[-half:]).decode("utf-8", errors="replace")


def _save_output(ctx: ToolContext, text: str) -> str:
    relative = f"logs/shell-output/output-{time.time_ns()}-{uuid.uuid4().hex}.txt"
    path = ctx.drive_path(relative)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
        file.write(text)
    for old in sorted(path.parent.glob("output-*.txt"), reverse=True)[_SAVED_RESULTS:]:
        try:
            old.unlink(missing_ok=True)
        except OSError:
            log.debug("Could not expire saved shell output", exc_info=True)
    return relative


def _render_output(ctx: ToolContext, header: str, stdout: str, stderr: str) -> str:
    body = stdout + ("\n--- STDERR ---\n" + stderr if stderr else "")
    header = sanitize_tool_result_for_log(header)
    body = sanitize_tool_result_for_log(body)
    secrets = {
        value for name, value in os.environ.items()
        if len(value) >= 8 and name.upper().endswith(("_API_KEY", "_TOKEN", "_PASSWORD", "_SECRET", "_PASSWD"))
    }
    for secret in sorted(secrets, key=len, reverse=True):
        header = header.replace(secret, "***REDACTED***")
        body = body.replace(secret, "***REDACTED***")
    result = header + body
    if len(result) <= _INLINE_CHARS:
        return result
    try:
        relative = _save_output(ctx, result)
        notice = f'Saved output: drive_read(path="{relative}", offset=0)\n'
    except OSError:
        log.warning("Could not retain long shell output", exc_info=True)
        notice = "Long output could not be saved. Showing its beginning and end.\n"
    if len(header) > 400:
        header = header[:399] + "…\n"
    gap = "\n[… see saved output for the middle …]\n" if notice.startswith("Saved output") else "\n[…]\n"
    room = max(0, _INLINE_CHARS - len(header) - len(notice) - len(gap))
    head = room // 3
    tail = room - head
    return header + notice + body[:head] + gap + (body[-tail:] if tail else "")


def _stop_process(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass
    proc.wait(timeout=5)


def _run_shell(
    ctx: ToolContext, cmd=None, cwd: str = "", *, script: str | None = None,
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
) -> str:
    if type(timeout_sec) is not int or not 1 <= timeout_sec <= _MAX_TIMEOUT_SEC:
        return f"⚠️ SHELL_ARG_ERROR: timeout_sec must be between 1 and {_MAX_TIMEOUT_SEC}."
    if script is not None:
        if cmd is not None or not isinstance(script, str) or not script.strip():
            return "⚠️ SHELL_ARG_ERROR: provide either cmd or a nonempty Bash script."
        cmd = ["bash", "-o", "pipefail", "-c", script]
    elif isinstance(cmd, str):
        cmd = _recover_argv(ctx, cmd)
    if not isinstance(cmd, list) or not cmd or not all(isinstance(arg, str) for arg in cmd):
        return "⚠️ SHELL_ARG_ERROR: cmd must be a nonempty list of strings."

    work_dir = (ctx.repo_dir / (cwd or ".")).resolve()
    if not work_dir.is_dir():
        return f"⚠️ SHELL_CWD_ERROR: directory does not exist: {work_dir}"

    shell_text = " " + " ".join(cmd).lower() + " "
    mutating_markers = (
        " git add ", " git commit ", " git push ", " git tag ", " git reset ", " git checkout ",
        " git cherry-pick ", " apply_patch ", " patch ", " sed -i", " perl -pi", " mv ", " cp ",
        " rm ", " mkdir ", " touch ", " python - <<", " python3 - <<",
    )
    if any(marker in shell_text for marker in mutating_markers):
        msg = _acquire_copilot_write_lock(ctx)
        if msg:
            return msg
        ctx.write_attempted = True

    try:
        with subprocess.Popen(
            cmd, cwd=work_dir, env=_command_environment(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name == "posix",
        ) as proc:
            try:
                stdout, stderr = proc.communicate(timeout=timeout_sec)
                header = f"exit_code={proc.returncode}\ncwd={work_dir}\n"
            except subprocess.TimeoutExpired as expired:
                _stop_process(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    stdout, stderr = expired.output, expired.stderr
                header = f"⚠️ SHELL_TIMEOUT: command stopped after {timeout_sec}s.\ncwd={work_dir}\n"
            return _render_output(ctx, header, _read_stream(stdout), _read_stream(stderr))
    except FileNotFoundError:
        return f"⚠️ SHELL_EXEC_NOT_FOUND: {cmd[0]} (cwd={work_dir})."
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"⚠️ SHELL_ERROR: {exc}"


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("run_shell", {
            "name": "run_shell",
            "description": (
                "Run argv or an explicit Bash script. Set cwd directly. Commands inherit the Veles Python environment. "
                "Returns the exit code and resolved cwd. Long output can be read with drive_read without rerunning."
            ),
            "parameters": {"type": "object", "properties": {
                "cmd": {"type": "array", "items": {"type": "string"}, "description": "Command and arguments."},
                "script": {"type": "string", "description": "Bash source for pipes or multiple commands. Use instead of cmd."},
                "cwd": {"type": "string", "default": "", "description": "Existing path. Defaults to the repository."},
                "timeout_sec": {"type": "integer", "minimum": 1, "maximum": _MAX_TIMEOUT_SEC,
                                "default": _DEFAULT_TIMEOUT_SEC},
            }, "required": []},
        }, _run_shell, is_code_tool=True, timeout_sec=_MAX_TIMEOUT_SEC + 15),
    ]
