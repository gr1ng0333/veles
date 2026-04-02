from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.ssh_targets import (
    SshConnectionError,
    _base_ssh_command,
    _bootstrap_session,
    _get_target_record,
)
from ouroboros.utils import utc_now_iso

_DEFAULT_TIMEOUT_SEC = 20
_MAX_TIMEOUT_SEC = 120
_DEFAULT_LOG_LINES = 50
_MAX_LOG_LINES = 500
_MAX_OUTPUT_CHARS = 20000
_ALLOWED_ACTIONS = {"restart", "start", "stop", "reload"}
_SYSTEMCTL_LIST_FLAGS = "systemctl list-units --type=service --state=running --all --no-pager --no-legend --plain"


class RemoteServiceError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class RemoteServicePolicyError(RemoteServiceError):
    pass


def _tool_entry(
    name: str,
    description: str,
    properties: Dict[str, Any],
    required: List[str],
    handler,
    is_code_tool: bool = False,
) -> ToolEntry:
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


def _normalize_alias(value: Any) -> str:
    alias = str(value or "").strip()
    if not alias:
        raise RemoteServicePolicyError("invalid_alias", "alias must not be empty")
    return alias


def _normalize_service_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise RemoteServicePolicyError("invalid_service_name", "service_name must not be empty")
    if any(ch in name for ch in ("\n", "\r", "\x00")):
        raise RemoteServicePolicyError(
            "invalid_service_name",
            "service_name contains unsupported control characters",
        )
    return name


def _normalize_timeout(value: Any) -> int:
    try:
        timeout = int(value if value is not None else _DEFAULT_TIMEOUT_SEC)
    except Exception as exc:
        raise RemoteServicePolicyError("invalid_timeout", "timeout_sec must be an integer") from exc
    if timeout < 1 or timeout > _MAX_TIMEOUT_SEC:
        raise RemoteServicePolicyError(
            "invalid_timeout",
            f"timeout_sec must be between 1 and {_MAX_TIMEOUT_SEC}",
        )
    return timeout


def _normalize_log_lines(value: Any) -> int:
    try:
        lines = int(value if value is not None else _DEFAULT_LOG_LINES)
    except Exception as exc:
        raise RemoteServicePolicyError("invalid_lines", "lines must be an integer") from exc
    if lines < 1 or lines > _MAX_LOG_LINES:
        raise RemoteServicePolicyError(
            "invalid_lines",
            f"lines must be between 1 and {_MAX_LOG_LINES}",
        )
    return lines


def _normalize_action(value: Any) -> str:
    action = str(value or "restart").strip().lower()
    if action not in _ALLOWED_ACTIONS:
        allowed = ", ".join(sorted(_ALLOWED_ACTIONS))
        raise RemoteServicePolicyError("invalid_action", f"action must be one of: {allowed}")
    return action


def _trim_output(value: str, *, max_chars: int = _MAX_OUTPUT_CHARS) -> str:
    text = value or ""
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return text[:max_chars] + f"\n...[truncated {omitted} chars]"


def _run_remote_command(ctx: ToolContext, alias: str, command: str, *, timeout: int) -> Dict[str, Any]:
    record = _get_target_record(ctx, alias)
    _bootstrap_session(ctx, alias, probe_command="true")
    ssh_cmd = _base_ssh_command(ctx, record)
    proc = subprocess.run(
        ssh_cmd + [command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
    }


def _parse_service_line(line: str) -> Dict[str, Any] | None:
    text = (line or "").strip()
    if not text:
        return None
    parts = text.split(None, 4)
    if len(parts) < 4:
        return {
            "raw": text,
            "unit": text,
            "load": "",
            "active": "",
            "sub": "",
            "description": "",
        }
    unit, load, active, sub = parts[:4]
    description = parts[4] if len(parts) > 4 else ""
    return {
        "unit": unit,
        "load": load,
        "active": active,
        "sub": sub,
        "description": description,
        "raw": text,
    }


def _systemctl_status_command(service_name: str) -> str:
    quoted = shlex.quote(service_name)
    return (
        f"SYSTEMD_COLORS=0 SYSTEMD_PAGER=cat systemctl show {quoted} "
        "--no-pager "
        "--property Id,Description,LoadState,ActiveState,SubState,UnitFileState,"
        "FragmentPath,MainPID,ExecMainStatus,ExecMainCode,ActiveEnterTimestamp"
    )


def _journalctl_command(service_name: str, lines: int) -> str:
    quoted = shlex.quote(service_name)
    return (
        f"SYSTEMD_COLORS=0 SYSTEMD_PAGER=cat journalctl -u {quoted} "
        f"--no-pager -n {lines} -o short-iso"
    )


def _systemctl_action_command(service_name: str, action: str) -> str:
    quoted = shlex.quote(service_name)
    return f"systemctl {action} {quoted}"


def _list_services_command() -> str:
    return _SYSTEMCTL_LIST_FLAGS


def _parse_systemctl_show(stdout: str) -> Dict[str, Any]:
    data: Dict[str, str] = {}
    for line in (stdout or "").splitlines():
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value
    return {
        "unit": data.get("Id", ""),
        "description": data.get("Description", ""),
        "load_state": data.get("LoadState", ""),
        "active_state": data.get("ActiveState", ""),
        "sub_state": data.get("SubState", ""),
        "unit_file_state": data.get("UnitFileState", ""),
        "fragment_path": data.get("FragmentPath", ""),
        "main_pid": data.get("MainPID", ""),
        "exec_main_code": data.get("ExecMainCode", ""),
        "exec_main_status": data.get("ExecMainStatus", ""),
        "active_enter_timestamp": data.get("ActiveEnterTimestamp", ""),
    }


def _append_audit(ctx: ToolContext, payload: Dict[str, Any]) -> None:
    event = dict(payload)
    event.setdefault("ts", utc_now_iso())
    ctx.pending_events.append(event)


def _error_payload(kind: str, message: str, **extra: Any) -> str:
    payload = {"status": "error", "kind": kind, "error": message}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _remote_service_status(ctx: ToolContext, alias: str, service_name: str, *, timeout_sec: Any = None) -> str:
    normalized_alias = _normalize_alias(alias)
    normalized_service = _normalize_service_name(service_name)
    try:
        timeout = _normalize_timeout(timeout_sec)
        result = _run_remote_command(
            ctx,
            normalized_alias,
            _systemctl_status_command(normalized_service),
            timeout=timeout,
        )
        payload = {
            "status": "ok" if result["returncode"] == 0 else "error",
            "alias": normalized_alias,
            "service_name": normalized_service,
            "command": "systemctl show",
            "returncode": result["returncode"],
            "details": _parse_systemctl_show(result["stdout"]),
            "stdout": _trim_output(result["stdout"]),
            "stderr": _trim_output(result["stderr"]),
        }
        _append_audit(
            ctx,
            {
                "type": "remote_service_status",
                "alias": normalized_alias,
                "service_name": normalized_service,
                "returncode": result["returncode"],
            },
        )
        return json.dumps(payload, ensure_ascii=False)
    except (RemoteServiceError, SshConnectionError) as exc:
        _append_audit(
            ctx,
            {
                "type": "remote_service_status",
                "status": "error",
                "alias": normalized_alias,
                "service_name": normalized_service,
                "kind": exc.kind,
                "error": exc.message,
            },
        )
        return _error_payload(exc.kind, exc.message, alias=normalized_alias, service_name=normalized_service)


def _remote_service_action(
    ctx: ToolContext,
    alias: str,
    service_name: str,
    *,
    action: Any = None,
    timeout_sec: Any = None,
) -> str:
    normalized_alias = _normalize_alias(alias)
    normalized_service = _normalize_service_name(service_name)
    try:
        normalized_action = _normalize_action(action)
        timeout = _normalize_timeout(timeout_sec)
        result = _run_remote_command(
            ctx,
            normalized_alias,
            _systemctl_action_command(normalized_service, normalized_action),
            timeout=timeout,
        )
        payload = {
            "status": "ok" if result["returncode"] == 0 else "error",
            "alias": normalized_alias,
            "service_name": normalized_service,
            "action": normalized_action,
            "returncode": result["returncode"],
            "stdout": _trim_output(result["stdout"]),
            "stderr": _trim_output(result["stderr"]),
        }
        _append_audit(
            ctx,
            {
                "type": "remote_service_action",
                "alias": normalized_alias,
                "service_name": normalized_service,
                "action": normalized_action,
                "returncode": result["returncode"],
            },
        )
        return json.dumps(payload, ensure_ascii=False)
    except (RemoteServiceError, SshConnectionError) as exc:
        _append_audit(
            ctx,
            {
                "type": "remote_service_action",
                "status": "error",
                "alias": normalized_alias,
                "service_name": normalized_service,
                "action": str(action or "restart").strip().lower() or "restart",
                "kind": exc.kind,
                "error": exc.message,
            },
        )
        return _error_payload(
            exc.kind,
            exc.message,
            alias=normalized_alias,
            service_name=normalized_service,
            action=str(action or "restart").strip().lower() or "restart",
        )


def _remote_service_logs(
    ctx: ToolContext,
    alias: str,
    service_name: str,
    *,
    lines: Any = None,
    timeout_sec: Any = None,
) -> str:
    normalized_alias = _normalize_alias(alias)
    normalized_service = _normalize_service_name(service_name)
    try:
        normalized_lines = _normalize_log_lines(lines)
        timeout = _normalize_timeout(timeout_sec)
        result = _run_remote_command(
            ctx,
            normalized_alias,
            _journalctl_command(normalized_service, normalized_lines),
            timeout=timeout,
        )
        payload = {
            "status": "ok" if result["returncode"] == 0 else "error",
            "alias": normalized_alias,
            "service_name": normalized_service,
            "lines": normalized_lines,
            "returncode": result["returncode"],
            "logs": _trim_output(result["stdout"]),
            "stderr": _trim_output(result["stderr"]),
        }
        _append_audit(
            ctx,
            {
                "type": "remote_service_logs",
                "alias": normalized_alias,
                "service_name": normalized_service,
                "lines": normalized_lines,
                "returncode": result["returncode"],
            },
        )
        return json.dumps(payload, ensure_ascii=False)
    except (RemoteServiceError, SshConnectionError) as exc:
        fallback_lines = int(lines) if isinstance(lines, int) else _DEFAULT_LOG_LINES
        _append_audit(
            ctx,
            {
                "type": "remote_service_logs",
                "status": "error",
                "alias": normalized_alias,
                "service_name": normalized_service,
                "lines": fallback_lines,
                "kind": exc.kind,
                "error": exc.message,
            },
        )
        return _error_payload(
            exc.kind,
            exc.message,
            alias=normalized_alias,
            service_name=normalized_service,
            lines=fallback_lines,
        )


def _remote_service_list(ctx: ToolContext, alias: str, *, timeout_sec: Any = None) -> str:
    normalized_alias = _normalize_alias(alias)
    try:
        timeout = _normalize_timeout(timeout_sec)
        result = _run_remote_command(ctx, normalized_alias, _list_services_command(), timeout=timeout)
        services = [
            parsed
            for parsed in (_parse_service_line(line) for line in result["stdout"].splitlines())
            if parsed is not None
        ]
        payload = {
            "status": "ok" if result["returncode"] == 0 else "error",
            "alias": normalized_alias,
            "returncode": result["returncode"],
            "count": len(services),
            "services": services,
            "stderr": _trim_output(result["stderr"]),
        }
        _append_audit(
            ctx,
            {
                "type": "remote_service_list",
                "alias": normalized_alias,
                "returncode": result["returncode"],
                "count": len(services),
            },
        )
        return json.dumps(payload, ensure_ascii=False)
    except (RemoteServiceError, SshConnectionError) as exc:
        _append_audit(
            ctx,
            {
                "type": "remote_service_list",
                "status": "error",
                "alias": normalized_alias,
                "kind": exc.kind,
                "error": exc.message,
            },
        )
        return _error_payload(exc.kind, exc.message, alias=normalized_alias)


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            name="remote_service_status",
            description="Get structured systemd service status for a registered SSH target.",
            properties={
                "alias": {"type": "string", "description": "Registered SSH target alias."},
                "service_name": {"type": "string", "description": "systemd unit name, e.g. nginx.service."},
                "timeout_sec": {"type": "integer", "description": f"SSH timeout in seconds (1-{_MAX_TIMEOUT_SEC})."},
            },
            required=["alias", "service_name"],
            handler=lambda ctx, alias, service_name, timeout_sec=None: _remote_service_status(
                ctx,
                alias,
                service_name,
                timeout_sec=timeout_sec,
            ),
        ),
        _tool_entry(
            name="remote_service_action",
            description="Run a systemd action (start/stop/restart/reload) for a service on a registered SSH target.",
            properties={
                "alias": {"type": "string", "description": "Registered SSH target alias."},
                "service_name": {"type": "string", "description": "systemd unit name, e.g. x-ui.service."},
                "action": {"type": "string", "enum": sorted(_ALLOWED_ACTIONS), "description": "Service action to run."},
                "timeout_sec": {"type": "integer", "description": f"SSH timeout in seconds (1-{_MAX_TIMEOUT_SEC})."},
            },
            required=["alias", "service_name"],
            handler=lambda ctx, alias, service_name, action=None, timeout_sec=None: _remote_service_action(
                ctx,
                alias,
                service_name,
                action=action,
                timeout_sec=timeout_sec,
            ),
            is_code_tool=True,
        ),
        _tool_entry(
            name="remote_service_logs",
            description="Read recent journalctl logs for a service on a registered SSH target.",
            properties={
                "alias": {"type": "string", "description": "Registered SSH target alias."},
                "service_name": {"type": "string", "description": "systemd unit name."},
                "lines": {"type": "integer", "description": f"How many journal lines to return (1-{_MAX_LOG_LINES})."},
                "timeout_sec": {"type": "integer", "description": f"SSH timeout in seconds (1-{_MAX_TIMEOUT_SEC})."},
            },
            required=["alias", "service_name"],
            handler=lambda ctx, alias, service_name, lines=None, timeout_sec=None: _remote_service_logs(
                ctx,
                alias,
                service_name,
                lines=lines,
                timeout_sec=timeout_sec,
            ),
        ),
        _tool_entry(
            name="remote_service_list",
            description="List running systemd services on a registered SSH target.",
            properties={
                "alias": {"type": "string", "description": "Registered SSH target alias."},
                "timeout_sec": {"type": "integer", "description": f"SSH timeout in seconds (1-{_MAX_TIMEOUT_SEC})."},
            },
            required=["alias"],
            handler=lambda ctx, alias, timeout_sec=None: _remote_service_list(ctx, alias, timeout_sec=timeout_sec),
        ),
    ]
