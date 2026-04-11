from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any, Dict, List, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.ssh_targets import (
    SshConnectionError,
    _base_ssh_command,
    _bootstrap_session,
    _get_target_record,
)
from ouroboros.utils import utc_now_iso


_DEFAULT_TIMEOUT_SEC = 20
_MAX_TIMEOUT_SEC = 300
_DEFAULT_OUTPUT_LIMIT = 12000
_MAX_OUTPUT_LIMIT = 100000
_INTERACTIVE_FLAGS = {"-i", "--interactive", "-it", "-ti"}
_READ_ONLY_ALLOWLIST = {
    "pwd",
    "ls",
    "find",
    "stat",
    "cat",
    "head",
    "tail",
    "grep",
    "rg",
    "sed",
    "awk",
    "cut",
    "sort",
    "uniq",
    "wc",
    "du",
    "df",
    "file",
    "readlink",
    "realpath",
    "git",
    "python",
    "python3",
    "node",
    "which",
    "env",
    "printenv",
    "ps",
    "ss",
    "netstat",
    "systemctl",
    "journalctl",
}
_GIT_READONLY_SUBCOMMANDS = {
    "status",
    "log",
    "show",
    "rev-parse",
    "branch",
    "remote",
    "diff",
    "ls-files",
    "describe",
    "tag",
    "grep",
}
_PYTHON_READONLY_PREFIXES = (
    "-c import os",
    "-c import pathlib",
    "-c import json",
    "-c import sys",
    "-c print(",
    "- <<'PY'",
)
_DENY_PATTERNS = [
    "rm -rf /",
    "mkfs",
    "shutdown",
    "reboot",
    "poweroff",
    ":(){:|:&};:",
    "dd if=",
    "chmod -R 777 /",
]


class RemoteExecutionError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class RemoteExecutionPolicyError(RemoteExecutionError):
    pass


def _tool_entry(name: str, description: str, properties: Dict[str, Any], required: List[str], handler, is_code_tool: bool = False) -> ToolEntry:
    return ToolEntry(
        name=name,
        schema={
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
        handler=handler,
        is_code_tool=is_code_tool,
        timeout_sec=_MAX_TIMEOUT_SEC + 10,
    )


def _normalize_timeout(value: Any) -> int:
    try:
        timeout = int(value if value is not None else _DEFAULT_TIMEOUT_SEC)
    except Exception as exc:
        raise RemoteExecutionPolicyError("invalid_timeout", "timeout_sec must be an integer") from exc
    if timeout < 1 or timeout > _MAX_TIMEOUT_SEC:
        raise RemoteExecutionPolicyError("invalid_timeout", f"timeout_sec must be between 1 and {_MAX_TIMEOUT_SEC}")
    return timeout


def _normalize_output_limit(value: Any) -> int:
    try:
        limit = int(value if value is not None else _DEFAULT_OUTPUT_LIMIT)
    except Exception as exc:
        raise RemoteExecutionPolicyError("invalid_output_limit", "max_output_chars must be an integer") from exc
    if limit < 256 or limit > _MAX_OUTPUT_LIMIT:
        raise RemoteExecutionPolicyError("invalid_output_limit", f"max_output_chars must be between 256 and {_MAX_OUTPUT_LIMIT}")
    return limit


def _truncate_text(text: str, limit: int) -> Tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _summarize_text(text: str, limit: int = 300) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "…"


def _shell_join(parts: List[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def _looks_interactive(tokens: List[str], command: str) -> bool:
    if any(flag in _INTERACTIVE_FLAGS for flag in tokens[1:]):
        return True
    lowered = command.lower()
    return any(word in lowered for word in ["top", "less", "more", "vim", "nano", "htop", "watch", "tail -f"])


def _classify_mutation_risk(command: str, execution_mode: str) -> str:
    if execution_mode == "mutating":
        return "mutating"
    lowered = command.lower()
    if any(op in lowered for op in [">", " >>", "mv ", "cp ", "chmod ", "chown ", "mkdir ", "touch ", "tee ", "sed -i", "git checkout", "git switch", "git restore", "git reset", "npm install", "pip install"]):
        return "high"
    return "read_only"


def _parse_command(command: str) -> List[str]:
    try:
        tokens = shlex.split(command)
    except Exception as exc:
        raise RemoteExecutionPolicyError("invalid_command", f"command is not valid shell syntax: {exc}") from exc
    if not tokens:
        raise RemoteExecutionPolicyError("invalid_command", "command must not be empty")
    return tokens


def _deny_if_dangerous(command: str) -> None:
    lowered = command.lower()
    for pattern in _DENY_PATTERNS:
        if pattern in lowered:
            raise RemoteExecutionPolicyError("policy_deny", f"command denied by dangerous pattern: {pattern}")


def _is_readonly_git(tokens: List[str]) -> bool:
    return len(tokens) >= 2 and tokens[1] in _GIT_READONLY_SUBCOMMANDS


def _is_readonly_systemctl(tokens: List[str]) -> bool:
    if len(tokens) < 2:
        return False
    return tokens[1] in {"status", "show", "list-units", "list-unit-files", "is-active", "is-enabled", "cat"}


def _is_readonly_journalctl(tokens: List[str]) -> bool:
    denied = {"--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate", "--flush", "--sync", "--relinquish-var"}
    return not any(token in denied for token in tokens[1:])


def _is_readonly_python(command: str) -> bool:
    stripped = command.strip()
    return any(stripped.startswith(f"python {prefix}") or stripped.startswith(f"python3 {prefix}") for prefix in _PYTHON_READONLY_PREFIXES)


def _enforce_policy(command: str, execution_mode: str) -> Tuple[List[str], str]:
    if execution_mode not in {"read_only", "mutating"}:
        raise RemoteExecutionPolicyError("invalid_mode", "execution_mode must be either 'read_only' or 'mutating'")
    _deny_if_dangerous(command)
    tokens = _parse_command(command)
    if _looks_interactive(tokens, command):
        raise RemoteExecutionPolicyError("interactive_command", "interactive remote commands are not allowed")
    binary = tokens[0]
    risk = _classify_mutation_risk(command, execution_mode)
    if execution_mode == "read_only":
        if binary not in _READ_ONLY_ALLOWLIST:
            raise RemoteExecutionPolicyError("policy_deny", f"command '{binary}' is not allowed in read_only mode")
        if binary == "git" and not _is_readonly_git(tokens):
            raise RemoteExecutionPolicyError("policy_deny", "git subcommand is not allowed in read_only mode")
        if binary == "systemctl" and not _is_readonly_systemctl(tokens):
            raise RemoteExecutionPolicyError("policy_deny", "systemctl mutating subcommand is not allowed in read_only mode")
        if binary == "journalctl" and not _is_readonly_journalctl(tokens):
            raise RemoteExecutionPolicyError("policy_deny", "journalctl mutating flag is not allowed in read_only mode")
        if binary in {"python", "python3"} and not _is_readonly_python(command):
            raise RemoteExecutionPolicyError("policy_deny", "python is only allowed for simple read-only inspection snippets")
        if any(op in command for op in [">", ">>", "| sh", "| bash"]):
            raise RemoteExecutionPolicyError("policy_deny", "shell redirection/piping to shell is not allowed in read_only mode")
        risk = "read_only"
    return tokens, risk


def _normalize_remote_exec_error(stderr: str, returncode: int) -> RemoteExecutionError:
    text = (stderr or "").strip()
    lowered = text.lower()
    if "permission denied" in lowered or "authentication failed" in lowered:
        return RemoteExecutionError("auth_failed", text or "ssh authentication failed")
    if "could not resolve hostname" in lowered or "name or service not known" in lowered or "no route to host" in lowered:
        return RemoteExecutionError("host_unreachable", text or "ssh host unreachable")
    if "connection timed out" in lowered or "operation timed out" in lowered:
        return RemoteExecutionError("timeout", text or "ssh connection timed out")
    if "no such file or directory" in lowered and "cd:" in lowered:
        return RemoteExecutionError("cwd_missing", text or "remote working directory does not exist")
    if "command not found" in lowered or "not found" in lowered:
        return RemoteExecutionError("remote_missing_binary", text or "remote binary missing")
    if returncode == 124:
        return RemoteExecutionError("timeout", text or "remote command timed out")
    return RemoteExecutionError("remote_command_failed", text or f"remote command failed with exit code {returncode}")


def _build_remote_command(command: str, cwd: str) -> str:
    normalized_cwd = (cwd or "").strip() or "."
    return f"cd {shlex.quote(normalized_cwd)} && {command}"


def _append_audit_event(ctx: ToolContext, *, alias: str, cwd: str, command: str, execution_mode: str, mutation_risk_level: str, result: subprocess.CompletedProcess[str] | None = None, error_kind: str = "", error_message: str = "") -> None:
    stdout = result.stdout if result is not None else ""
    stderr = result.stderr if result is not None else ""
    ctx.pending_events.append({
        "type": "remote_execution",
        "ts": utc_now_iso(),
        "target": alias,
        "cwd": cwd,
        "command": command,
        "execution_mode": execution_mode,
        "mutation_risk_level": mutation_risk_level,
        "exit_code": result.returncode if result is not None else None,
        "stdout_summary": _summarize_text(stdout),
        "stderr_summary": _summarize_text(stderr),
        "error_kind": error_kind,
        "error_message": error_message,
    })


def remote_command_exec(
    ctx: ToolContext,
    alias: str,
    command: str,
    cwd: str = "",
    execution_mode: str = "read_only",
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
    max_output_chars: int = _DEFAULT_OUTPUT_LIMIT,
) -> str:
    timeout = _normalize_timeout(timeout_sec)
    output_limit = _normalize_output_limit(max_output_chars)
    record = _get_target_record(ctx, alias)
    alias_norm = record["alias"]
    try:
        tokens, risk = _enforce_policy(command, execution_mode)
        _bootstrap_session(ctx, alias_norm)
        ssh_cmd = _base_ssh_command(ctx, record)
        ssh_cmd.append(_build_remote_command(_shell_join(tokens), cwd))
        result = subprocess.run(
            ssh_cmd,
            cwd=ctx.repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout, stdout_truncated = _truncate_text(result.stdout or "", output_limit)
        stderr, stderr_truncated = _truncate_text(result.stderr or "", output_limit)
        if result.returncode != 0:
            error = _normalize_remote_exec_error(result.stderr or "", result.returncode)
            _append_audit_event(
                ctx,
                alias=alias_norm,
                cwd=(cwd or "."),
                command=command,
                execution_mode=execution_mode,
                mutation_risk_level=risk,
                result=result,
                error_kind=error.kind,
                error_message=error.message,
            )
            return json.dumps({
                "status": "error",
                "kind": error.kind,
                "error": error.message,
                "target": alias_norm,
                "cwd": (cwd or "."),
                "command": command,
                "exit_code": result.returncode,
                "mutation_risk_level": risk,
                "stdout": stdout,
                "stderr": stderr,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
            }, ensure_ascii=False, indent=2)
        _append_audit_event(
            ctx,
            alias=alias_norm,
            cwd=(cwd or "."),
            command=command,
            execution_mode=execution_mode,
            mutation_risk_level=risk,
            result=result,
        )
        return json.dumps({
            "status": "ok",
            "target": alias_norm,
            "cwd": (cwd or "."),
            "command": command,
            "execution_mode": execution_mode,
            "mutation_risk_level": risk,
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }, ensure_ascii=False, indent=2)
    except subprocess.TimeoutExpired:
        error = RemoteExecutionError("timeout", f"remote command timed out after {timeout}s")
        _append_audit_event(
            ctx,
            alias=alias_norm,
            cwd=(cwd or "."),
            command=command,
            execution_mode=execution_mode,
            mutation_risk_level="mutating" if execution_mode == "mutating" else "read_only",
            error_kind=error.kind,
            error_message=error.message,
        )
        return json.dumps({
            "status": "error",
            "kind": error.kind,
            "error": error.message,
            "target": alias_norm,
            "cwd": (cwd or "."),
            "command": command,
            "mutation_risk_level": "mutating" if execution_mode == "mutating" else "read_only",
        }, ensure_ascii=False, indent=2)
    except SshConnectionError as error:
        _append_audit_event(
            ctx,
            alias=alias_norm,
            cwd=(cwd or "."),
            command=command,
            execution_mode=execution_mode,
            mutation_risk_level="mutating" if execution_mode == "mutating" else "read_only",
            error_kind=error.kind,
            error_message=error.message,
        )
        return json.dumps({
            "status": "error",
            "kind": error.kind,
            "error": error.message,
            "target": alias_norm,
            "cwd": (cwd or "."),
            "command": command,
        }, ensure_ascii=False, indent=2)
    except RemoteExecutionPolicyError as error:
        _append_audit_event(
            ctx,
            alias=alias_norm,
            cwd=(cwd or "."),
            command=command,
            execution_mode=execution_mode,
            mutation_risk_level="mutating" if execution_mode == "mutating" else "read_only",
            error_kind=error.kind,
            error_message=error.message,
        )
        return json.dumps({
            "status": "error",
            "kind": error.kind,
            "error": error.message,
            "target": alias_norm,
            "cwd": (cwd or "."),
            "command": command,
        }, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            "remote_command_exec",
            "Execute a remote shell command through a policy-guarded SSH path with read-only default mode and audit trail.",
            {
                "alias": {"type": "string", "description": "Registered SSH target alias."},
                "command": {"type": "string", "description": "Remote command to execute."},
                "cwd": {"type": "string", "description": "Remote working directory (default: .)."},
                "execution_mode": {"type": "string", "enum": ["read_only", "mutating"], "description": "Policy mode. read_only is default and restricts command set."},
                "timeout_sec": {"type": "integer", "description": "Execution timeout in seconds (1..300)."},
                "max_output_chars": {"type": "integer", "description": "Maximum stdout/stderr chars to return per stream."},
            },
            ["alias", "command"],
            remote_command_exec,
        )
    ]
