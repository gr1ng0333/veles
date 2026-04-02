from __future__ import annotations

import json
import os
import pathlib
import shlex
import subprocess
import time
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry


_REGISTRY_REL = "state/ssh_targets.json"
_SESSION_CACHE: Dict[str, Dict[str, Any]] = {}
_CONTROL_DIR_ENV = "VELES_SSH_CONTROL_DIR"
_DEFAULT_CONNECT_TIMEOUT = 10


class SshTargetError(ValueError):
    pass


class SshConnectionError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


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
    )


def _registry_path(ctx: ToolContext) -> pathlib.Path:
    return ctx.drive_path(_REGISTRY_REL)


def _load_registry(ctx: ToolContext) -> Dict[str, Any]:
    path = _registry_path(ctx)
    if not path.exists():
        return {"targets": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"targets": {}}
    if not isinstance(data, dict):
        return {"targets": {}}
    targets = data.get("targets")
    if not isinstance(targets, dict):
        data["targets"] = {}
    return data


def _save_registry(ctx: ToolContext, payload: Dict[str, Any]) -> None:
    path = _registry_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _normalize_alias(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in (value or "").strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    if not cleaned:
        raise SshTargetError("ssh target alias must not be empty")
    return cleaned


def _normalize_port(value: Any) -> int:
    try:
        port = int(value)
    except Exception as exc:
        raise SshTargetError("ssh port must be an integer") from exc
    if port < 1 or port > 65535:
        raise SshTargetError("ssh port must be between 1 and 65535")
    return port


def _normalize_auth_mode(value: str) -> str:
    mode = (value or "").strip().lower()
    if mode not in {"password", "key"}:
        raise SshTargetError("ssh auth_mode must be either 'password' or 'key'")
    return mode


def _normalize_path_list(values: Optional[List[str]]) -> List[str]:
    result: List[str] = []
    for item in values or []:
        text = (item or "").strip()
        if text:
            result.append(text)
    return result


def _normalize_string_list(values: Optional[List[str]]) -> List[str]:
    result: List[str] = []
    for item in values or []:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _normalize_port_list(values: Optional[List[Any]]) -> List[int]:
    result: List[int] = []
    for item in values or []:
        port = _normalize_port(item)
        if port not in result:
            result.append(port)
    return result


def _normalize_target_record(
    *,
    alias: str,
    host: str,
    port: Any = 22,
    user: str,
    auth_mode: str,
    label: str = "",
    default_remote_root: str = "",
    known_projects_paths: Optional[List[str]] = None,
    known_services: Optional[List[str]] = None,
    known_ports: Optional[List[Any]] = None,
    known_tls_domains: Optional[List[str]] = None,
    ssh_key_path: str = "",
    password: str = "",
) -> Dict[str, Any]:
    alias_norm = _normalize_alias(alias)
    host_norm = (host or "").strip()
    user_norm = (user or "").strip()
    if not host_norm:
        raise SshTargetError("ssh host must not be empty")
    if not user_norm:
        raise SshTargetError("ssh user must not be empty")
    mode = _normalize_auth_mode(auth_mode)
    key_path = os.path.expanduser((ssh_key_path or "").strip())
    secret_password = password or ""
    if mode == "key" and not key_path:
        raise SshTargetError("ssh_key_path is required when auth_mode='key'")
    if mode == "password" and not secret_password:
        raise SshTargetError("password is required when auth_mode='password'")
    return {
        "alias": alias_norm,
        "host": host_norm,
        "port": _normalize_port(port),
        "user": user_norm,
        "auth_mode": mode,
        "label": (label or alias_norm).strip() or alias_norm,
        "default_remote_root": (default_remote_root or "").strip(),
        "known_projects_paths": _normalize_path_list(known_projects_paths),
        "known_services": _normalize_string_list(known_services),
        "known_ports": _normalize_port_list(known_ports),
        "known_tls_domains": _normalize_string_list(known_tls_domains),
        "ssh_key_path": key_path,
        "password": secret_password,
    }


def _public_target_view(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "alias": record["alias"],
        "host": record["host"],
        "port": record["port"],
        "user": record["user"],
        "auth_mode": record["auth_mode"],
        "label": record.get("label", record["alias"]),
        "default_remote_root": record.get("default_remote_root", ""),
        "known_projects_paths": list(record.get("known_projects_paths") or []),
        "known_services": list(record.get("known_services") or []),
        "known_ports": list(record.get("known_ports") or []),
        "known_tls_domains": list(record.get("known_tls_domains") or []),
        "ssh_key_path": record.get("ssh_key_path", "") if record.get("auth_mode") == "key" else "",
        "has_password": bool(record.get("password")) if record.get("auth_mode") == "password" else False,
    }


def _get_target_record(ctx: ToolContext, alias: str) -> Dict[str, Any]:
    alias_norm = _normalize_alias(alias)
    registry = _load_registry(ctx)
    record = registry.get("targets", {}).get(alias_norm)
    if not isinstance(record, dict):
        raise SshTargetError(f"ssh target alias not found: {alias_norm}")
    return record


def _control_dir(ctx: ToolContext) -> pathlib.Path:
    root = os.environ.get(_CONTROL_DIR_ENV)
    if root:
        path = pathlib.Path(root)
    else:
        path = ctx.drive_path("state/ssh-control")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _control_path(ctx: ToolContext, alias: str) -> pathlib.Path:
    return _control_dir(ctx) / f"{_normalize_alias(alias)}.sock"


def _base_ssh_command(ctx: ToolContext, record: Dict[str, Any], *, include_control: bool = True) -> List[str]:
    command = [
        "ssh",
        "-p",
        str(record["port"]),
        "-o",
        f"ConnectTimeout={_DEFAULT_CONNECT_TIMEOUT}",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
    ]
    if include_control:
        command += [
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPersist=600",
            "-o",
            f"ControlPath={_control_path(ctx, record['alias'])}",
        ]
    if record.get("auth_mode") == "key":
        command += ["-i", record.get("ssh_key_path", "")]
    command.append(f"{record['user']}@{record['host']}")
    return command


def _run_ssh_probe(ctx: ToolContext, record: Dict[str, Any], *, command: str = "true", timeout: int = 15) -> subprocess.CompletedProcess[str]:
    ssh_cmd = _base_ssh_command(ctx, record)
    ssh_cmd.append(command)
    env = os.environ.copy()
    password = record.get("password", "") if record.get("auth_mode") == "password" else ""
    if password:
        askpass_script = "#!/bin/sh\nprintf '%s' \"$VELES_SSH_PASSWORD\"\n"
        askpass_path = ctx.drive_path("state/ssh_askpass.sh")
        askpass_path.parent.mkdir(parents=True, exist_ok=True)
        askpass_path.write_text(askpass_script, encoding="utf-8")
        askpass_path.chmod(0o700)
        env["SSH_ASKPASS"] = str(askpass_path)
        env["VELES_SSH_PASSWORD"] = password
        env["DISPLAY"] = env.get("DISPLAY") or "veles-ssh"
        ssh_cmd = ["setsid", "-w", *ssh_cmd]
        ssh_cmd.insert(1, "env")
        ssh_cmd.insert(2, "SSH_ASKPASS_REQUIRE=force")
    return subprocess.run(ssh_cmd, cwd=ctx.repo_dir, capture_output=True, text=True, timeout=timeout, env=env)


def _normalize_probe_error(stderr: str, returncode: int) -> SshConnectionError:
    text = (stderr or "").strip()
    lowered = text.lower()
    if "permission denied" in lowered or "authentication failed" in lowered:
        return SshConnectionError("auth_failed", text or "ssh authentication failed")
    if "no route to host" in lowered or "name or service not known" in lowered or "could not resolve hostname" in lowered:
        return SshConnectionError("host_unreachable", text or "ssh host unreachable")
    if "connection timed out" in lowered or "operation timed out" in lowered:
        return SshConnectionError("timeout", text or "ssh connection timed out")
    if "connection refused" in lowered:
        return SshConnectionError("connection_refused", text or "ssh connection refused")
    return SshConnectionError("connection_failed", text or f"ssh command failed with exit code {returncode}")


def _bootstrap_session(ctx: ToolContext, alias: str, *, probe_command: str = "true") -> Dict[str, Any]:
    record = _get_target_record(ctx, alias)
    alias_norm = record["alias"]
    cached = _SESSION_CACHE.get(alias_norm)
    control_path = _control_path(ctx, alias_norm)
    if cached:
        cached["last_reused_at"] = time.time()
        return {
            "status": "ok",
            "bootstrap": "reused",
            "session": {
                "alias": alias_norm,
                "control_path": str(control_path),
                "connected_at": cached["connected_at"],
                "last_reused_at": cached["last_reused_at"],
            },
            "target": _public_target_view(record),
        }
    probe = _run_ssh_probe(ctx, record, command=probe_command)
    if probe.returncode != 0:
        raise _normalize_probe_error(probe.stderr, probe.returncode)
    now = time.time()
    _SESSION_CACHE[alias_norm] = {
        "alias": alias_norm,
        "connected_at": now,
        "last_reused_at": now,
        "control_path": str(control_path),
    }
    return {
        "status": "ok",
        "bootstrap": "fresh",
        "session": {
            "alias": alias_norm,
            "control_path": str(control_path),
            "connected_at": now,
            "last_reused_at": now,
        },
        "target": _public_target_view(record),
    }


def _ssh_target_register(
    ctx: ToolContext,
    alias: str,
    host: str,
    user: str,
    auth_mode: str,
    port: int = 22,
    label: str = "",
    default_remote_root: str = "",
    known_projects_paths: Optional[List[str]] = None,
    known_services: Optional[List[str]] = None,
    known_ports: Optional[List[Any]] = None,
    known_tls_domains: Optional[List[str]] = None,
    ssh_key_path: str = "",
    password: str = "",
) -> str:
    record = _normalize_target_record(
        alias=alias,
        host=host,
        port=port,
        user=user,
        auth_mode=auth_mode,
        label=label,
        default_remote_root=default_remote_root,
        known_projects_paths=known_projects_paths,
        known_services=known_services,
        known_ports=known_ports,
        known_tls_domains=known_tls_domains,
        ssh_key_path=ssh_key_path,
        password=password,
    )
    registry = _load_registry(ctx)
    targets = registry.setdefault("targets", {})
    targets[record["alias"]] = record
    _save_registry(ctx, registry)
    return json.dumps(
        {
            "status": "ok",
            "target": _public_target_view(record),
            "registry": {
                "path": str(_registry_path(ctx)),
                "count": len(targets),
                "aliases": sorted(targets.keys()),
            },
        },
        ensure_ascii=False,
    )


def _ssh_target_list(ctx: ToolContext) -> str:
    registry = _load_registry(ctx)
    targets = registry.get("targets", {})
    items = []
    for alias in sorted(targets.keys()):
        record = targets[alias]
        public = _public_target_view(record)
        cached = _SESSION_CACHE.get(alias)
        public["session_cached"] = bool(cached)
        public["session_control_path"] = cached.get("control_path") if cached else ""
        items.append(public)
    return json.dumps(
        {
            "status": "ok",
            "targets": items,
            "registry": {
                "path": str(_registry_path(ctx)),
                "count": len(items),
                "aliases": [item["alias"] for item in items],
            },
        },
        ensure_ascii=False,
    )


def _ssh_target_get(ctx: ToolContext, alias: str) -> str:
    record = _get_target_record(ctx, alias)
    payload = _public_target_view(record)
    cached = _SESSION_CACHE.get(record["alias"])
    payload["session_cached"] = bool(cached)
    payload["session_control_path"] = cached.get("control_path") if cached else ""
    return json.dumps({"status": "ok", "target": payload}, ensure_ascii=False)


def _ssh_session_bootstrap(ctx: ToolContext, alias: str, probe_command: str = "true") -> str:
    result = _bootstrap_session(ctx, alias, probe_command=probe_command)
    return json.dumps(result, ensure_ascii=False)


def _ssh_target_ping(ctx: ToolContext, alias: str, command: str = "pwd") -> str:
    bootstrap = _bootstrap_session(ctx, alias)
    record = _get_target_record(ctx, alias)
    probe = _run_ssh_probe(ctx, record, command=command)
    if probe.returncode != 0:
        raise _normalize_probe_error(probe.stderr, probe.returncode)
    return json.dumps(
        {
            "status": "ok",
            "target": _public_target_view(record),
            "bootstrap": bootstrap.get("bootstrap", "fresh"),
            "command": command,
            "stdout": probe.stdout,
            "stderr": probe.stderr,
            "returncode": probe.returncode,
        },
        ensure_ascii=False,
    )


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            "ssh_target_register",
            "Register a first-class SSH target by stable alias for later remote work.",
            {
                "alias": {"type": "string", "description": "Stable alias for the remote host"},
                "host": {"type": "string", "description": "SSH hostname or IP"},
                "port": {"type": "integer", "description": "SSH port (default 22)"},
                "user": {"type": "string", "description": "SSH username"},
                "auth_mode": {"type": "string", "enum": ["password", "key"], "description": "Authentication mode"},
                "label": {"type": "string", "description": "Optional human-friendly label"},
                "default_remote_root": {"type": "string", "description": "Default remote working root"},
                "known_projects_paths": {"type": "array", "items": {"type": "string"}, "description": "Known project directories on the remote host"},
                "known_services": {"type": "array", "items": {"type": "string"}, "description": "systemd units that should stay healthy on the host"},
                "known_ports": {"type": "array", "items": {"type": "integer"}, "description": "TCP ports that should be listening on the host"},
                "known_tls_domains": {"type": "array", "items": {"type": "string"}, "description": "TLS domains to check from the controller side"},
                "ssh_key_path": {"type": "string", "description": "Required for auth_mode=key"},
                "password": {"type": "string", "description": "Required for auth_mode=password"},
            },
            ["alias", "host", "user", "auth_mode"],
            _ssh_target_register,
        ),
        _tool_entry(
            "ssh_target_list",
            "List registered SSH targets and cached session metadata.",
            {},
            [],
            _ssh_target_list,
        ),
        _tool_entry(
            "ssh_target_get",
            "Get one registered SSH target by alias.",
            {
                "alias": {"type": "string", "description": "SSH target alias"},
            },
            ["alias"],
            _ssh_target_get,
        ),
        _tool_entry(
            "ssh_session_bootstrap",
            "Open or reuse a cached SSH session by target alias.",
            {
                "alias": {"type": "string", "description": "SSH target alias"},
                "probe_command": {"type": "string", "description": "Optional remote command to validate the session"},
            },
            ["alias"],
            _ssh_session_bootstrap,
        ),
        _tool_entry(
            "ssh_target_ping",
            "Run a lightweight probe command against a registered SSH target and return normalized connection info.",
            {
                "alias": {"type": "string", "description": "SSH target alias"},
                "command": {"type": "string", "description": "Remote command to execute for probe"},
            },
            ["alias"],
            _ssh_target_ping,
        ),
    ]
