from __future__ import annotations

import json
import shlex
import socket
import ssl
import subprocess
from datetime import datetime, timezone
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
_DEFAULT_TLS_TIMEOUT_SEC = 5
_TLS_WARNING_DAYS = 21
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


def _system_health_command() -> str:
    script = " ; ".join(
        [
            "printf '%s\n' __UPTIME__",
            r"cat /proc/uptime 2>/dev/null || true",
            "printf '%s\n' __LOADAVG__",
            r"cat /proc/loadavg 2>/dev/null || true",
            "printf '%s\n' __DF__",
            r"df -P -k / 2>/dev/null || true",
            "printf '%s\n' __FREE__",
            r"free -b 2>/dev/null || true",
            "printf '%s\n' __PORTS__",
            r"(ss -ltnH 2>/dev/null || netstat -ltn 2>/dev/null || true)",
        ]
    )
    return f"sh -lc {shlex.quote(script)}"


def _parse_health_sections(stdout: str) -> Dict[str, str]:
    sections: Dict[str, List[str]] = {}
    current = None
    for raw_line in (stdout or "").splitlines():
        line = raw_line.rstrip("\n")
        if line in {"__UPTIME__", "__LOADAVG__", "__DF__", "__FREE__", "__PORTS__"}:
            current = line.strip("_").lower()
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def _parse_uptime_seconds(section: str) -> float | None:
    first = (section or "").split()
    if not first:
        return None
    try:
        return float(first[0])
    except Exception:
        return None


def _parse_load_average(section: str) -> List[float]:
    parts = (section or "").split()[:3]
    values: List[float] = []
    for item in parts:
        try:
            values.append(float(item))
        except Exception:
            break
    return values


def _parse_root_disk_usage(section: str) -> Dict[str, Any]:
    lines = [line for line in (section or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return {}
    parts = lines[1].split()
    if len(parts) < 6:
        return {}
    used_pct_raw = parts[4].rstrip("%")
    try:
        used_pct = int(used_pct_raw)
    except Exception:
        used_pct = None
    return {
        "filesystem": parts[0],
        "total_kb": int(parts[1]) if parts[1].isdigit() else parts[1],
        "used_kb": int(parts[2]) if parts[2].isdigit() else parts[2],
        "available_kb": int(parts[3]) if parts[3].isdigit() else parts[3],
        "used_pct": used_pct,
        "mount": parts[5],
    }


def _parse_memory_usage(section: str) -> Dict[str, Any]:
    lines = [line for line in (section or "").splitlines() if line.strip()]
    mem_line = next((line for line in lines if line.startswith("Mem:")), "")
    if not mem_line:
        return {}
    parts = mem_line.split()
    if len(parts) < 7:
        return {}
    total = int(parts[1])
    used = int(parts[2])
    free = int(parts[3])
    shared = int(parts[4])
    buff_cache = int(parts[5])
    available = int(parts[6])
    available_pct = round((available / total) * 100, 2) if total else None
    return {
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "shared_bytes": shared,
        "buff_cache_bytes": buff_cache,
        "available_bytes": available,
        "available_pct": available_pct,
    }


def _extract_port(endpoint: str) -> int | None:
    text = (endpoint or "").strip()
    if not text:
        return None
    if text.startswith("[") and "]:" in text:
        text = text.rsplit("]:", 1)[-1]
    elif ":" in text:
        text = text.rsplit(":", 1)[-1]
    text = text.strip()
    if not text.isdigit():
        return None
    port = int(text)
    if 1 <= port <= 65535:
        return port
    return None


def _parse_listening_ports(section: str) -> List[int]:
    ports: List[int] = []
    for line in (section or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        local = ""
        if stripped.startswith("tcp") and len(parts) >= 4:
            local = parts[3]
        elif len(parts) >= 4:
            local = parts[3]
        port = _extract_port(local)
        if port is not None and port not in ports:
            ports.append(port)
    return sorted(ports)


def _check_tls_domain(domain: str, timeout_sec: int = _DEFAULT_TLS_TIMEOUT_SEC) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "domain": domain,
        "port": 443,
        "status": "error",
        "ok": False,
    }
    try:
        context = ssl.create_default_context()
        with socket.create_connection((domain, 443), timeout=timeout_sec) as sock:
            with context.wrap_socket(sock, server_hostname=domain) as tls_sock:
                cert = tls_sock.getpeercert()
        not_after = cert.get("notAfter", "")
        if not not_after:
            payload["error"] = "missing certificate expiry"
            return payload
        expires_at = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days_remaining = int((expires_at - datetime.now(timezone.utc)).total_seconds() // 86400)
        payload.update(
            {
                "status": "ok" if days_remaining >= _TLS_WARNING_DAYS else "warn",
                "ok": days_remaining >= _TLS_WARNING_DAYS,
                "expires_at": expires_at.isoformat(),
                "days_remaining": days_remaining,
                "subject": cert.get("subject", []),
                "issuer": cert.get("issuer", []),
            }
        )
        if days_remaining < _TLS_WARNING_DAYS:
            payload["error"] = f"certificate expires in {days_remaining} days"
        return payload
    except Exception as exc:
        payload["error"] = str(exc)
        return payload


def _system_health_flags(system: Dict[str, Any]) -> List[Dict[str, Any]]:
    flags: List[Dict[str, Any]] = []
    uptime_seconds = system.get("uptime_seconds")
    flags.append({
        "key": "uptime",
        "status": "ok" if uptime_seconds is not None else "warn",
        "summary": f"Uptime: {int(uptime_seconds)}s" if uptime_seconds is not None else "Uptime unavailable",
    })
    disk = system.get("root_disk") or {}
    disk_used_pct = disk.get("used_pct")
    flags.append({
        "key": "root_disk",
        "status": "ok" if isinstance(disk_used_pct, int) and disk_used_pct < 90 else "warn",
        "summary": f"Root disk used: {disk_used_pct}%" if disk_used_pct is not None else "Root disk usage unavailable",
    })
    memory = system.get("memory") or {}
    memory_available_pct = memory.get("available_pct")
    flags.append({
        "key": "memory",
        "status": "ok" if isinstance(memory_available_pct, (int, float)) and memory_available_pct >= 10 else "warn",
        "summary": f"Available memory: {memory_available_pct}%" if memory_available_pct is not None else "Memory availability unavailable",
    })
    load = system.get("load_average") or []
    flags.append({
        "key": "load_average",
        "status": "ok" if load else "warn",
        "summary": f"Load average: {load}" if load else "Load average unavailable",
    })
    return flags


def _remote_server_health(ctx: ToolContext, alias: str, *, timeout_sec: Any = None) -> str:
    normalized_alias = _normalize_alias(alias)
    try:
        timeout = _normalize_timeout(timeout_sec)
        record = _get_target_record(ctx, normalized_alias)
        result = _run_remote_command(ctx, normalized_alias, _system_health_command(), timeout=timeout)
        sections = _parse_health_sections(result["stdout"])
        system = {
            "uptime_seconds": _parse_uptime_seconds(sections.get("uptime", "")),
            "load_average": _parse_load_average(sections.get("loadavg", "")),
            "root_disk": _parse_root_disk_usage(sections.get("df", "")),
            "memory": _parse_memory_usage(sections.get("free", "")),
            "listening_ports": _parse_listening_ports(sections.get("ports", "")),
        }

        port_checks = []
        for port in list(record.get("known_ports") or []):
            listening = port in system["listening_ports"]
            port_checks.append({
                "port": port,
                "listening": listening,
                "status": "ok" if listening else "error",
                "ok": listening,
            })

        service_checks = []
        for service_name in list(record.get("known_services") or []):
            svc = _run_remote_command(ctx, normalized_alias, _systemctl_status_command(service_name), timeout=timeout)
            parsed = _parse_systemctl_show(svc["stdout"])
            service_ok = svc["returncode"] == 0 and parsed.get("active_state") == "active"
            service_checks.append({
                "service_name": service_name,
                "status": "ok" if service_ok else "error",
                "ok": service_ok,
                "returncode": svc["returncode"],
                "service": parsed,
                "stderr": _trim_output(svc["stderr"]),
            })

        tls_checks = [_check_tls_domain(domain) for domain in list(record.get("known_tls_domains") or [])]

        flags = _system_health_flags(system)
        flags.extend({
            "key": f"port:{item['port']}",
            "status": item["status"],
            "summary": f"Port {item['port']} listening" if item["ok"] else f"Port {item['port']} is not listening",
        } for item in port_checks)
        flags.extend({
            "key": f"service:{item['service_name']}",
            "status": item["status"],
            "summary": f"Service {item['service_name']} is active" if item["ok"] else f"Service {item['service_name']} is not active",
        } for item in service_checks)
        flags.extend({
            "key": f"tls:{item['domain']}",
            "status": item["status"],
            "summary": f"TLS {item['domain']} expires in {item.get('days_remaining')} days" if item.get("ok") else f"TLS {item['domain']} check failed: {item.get('error', 'unknown error')}",
        } for item in tls_checks)

        red_flags = sum(1 for item in flags if item["status"] == "error")
        warn_flags = sum(1 for item in flags if item["status"] == "warn")
        green_flags = sum(1 for item in flags if item["status"] == "ok")
        overall_status = "ok" if red_flags == 0 and warn_flags == 0 else "warn" if red_flags == 0 else "error"

        payload = {
            "status": "ok",
            "alias": normalized_alias,
            "checked_at": utc_now_iso(),
            "summary": {
                "overall_status": overall_status,
                "green_flags": green_flags,
                "warning_flags": warn_flags,
                "red_flags": red_flags,
                "checked_ports": len(port_checks),
                "checked_services": len(service_checks),
                "checked_tls_domains": len(tls_checks),
            },
            "system": system,
            "ports": port_checks,
            "services": service_checks,
            "tls": tls_checks,
            "flags": flags,
            "stderr": _trim_output(result["stderr"]),
        }
        _append_audit(
            ctx,
            {
                "type": "remote_server_health",
                "alias": normalized_alias,
                "overall_status": overall_status,
                "red_flags": red_flags,
                "warning_flags": warn_flags,
            },
        )
        return json.dumps(payload, ensure_ascii=False)
    except (RemoteServiceError, SshConnectionError) as exc:
        _append_audit(
            ctx,
            {
                "type": "remote_server_health",
                "status": "error",
                "alias": normalized_alias,
                "kind": exc.kind,
                "error": exc.message,
            },
        )
        return _error_payload(exc.kind, exc.message, alias=normalized_alias)


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
        _tool_entry(
            name="remote_server_health",
            description="Collect a structured health snapshot for a registered SSH target, including expected ports, services, and TLS domains.",
            properties={
                "alias": {"type": "string", "description": "Registered SSH target alias."},
                "timeout_sec": {"type": "integer", "description": f"SSH timeout in seconds (1-{_MAX_TIMEOUT_SEC})."},
            },
            required=["alias"],
            handler=lambda ctx, alias, timeout_sec=None: _remote_server_health(ctx, alias, timeout_sec=timeout_sec),
        ),
    ]
