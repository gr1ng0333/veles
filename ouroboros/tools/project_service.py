from __future__ import annotations

import json
import shlex
import subprocess
from typing import List

from ouroboros.tools.project_bootstrap import (
    _DEFAULT_SERVER_RUN_TIMEOUT,
    _MAX_SERVER_RUN_OUTPUT_CHARS,
    _clip_server_run_output,
    _find_project_server,
    _normalize_project_name,
    _project_server_registry_path,
    _repo_info,
    _require_local_project,
    _tool_entry,
    _utc_now_iso,
)
from ouroboros.tools.project_deploy import _decode_output, _run_ssh_stream
from ouroboros.tools.registry import ToolContext, ToolEntry

_ALLOWED_SERVICE_ACTIONS = {'install', 'start', 'stop', 'restart', 'status', 'enable', 'disable'}


def _run_ssh_text(args: List[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ['ssh', *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise RuntimeError('ssh client not found on VPS') from e



def _systemctl_prefix(sudo: bool) -> str:
    return 'sudo systemctl' if sudo else 'systemctl'



def _project_service_control(
    ctx: ToolContext,
    name: str,
    alias: str,
    service_name: str,
    action: str,
    timeout: int = _DEFAULT_SERVER_RUN_TIMEOUT,
    max_output_chars: int = _MAX_SERVER_RUN_OUTPUT_CHARS,
    unit_content: str = '',
    unit_path: str = '',
    sudo: bool = True,
    enable_on_install: bool = True,
    start_on_install: bool = False,
) -> str:
    repo_dir = _require_local_project(name)
    project_name = _normalize_project_name(name)
    server = _find_project_server(repo_dir, alias)

    service = str(service_name or '').strip()
    if not service:
        raise ValueError('service_name must be non-empty')
    action_name = str(action or '').strip().lower()
    if action_name not in _ALLOWED_SERVICE_ACTIONS:
        raise ValueError(f"action must be one of: {', '.join(sorted(_ALLOWED_SERVICE_ACTIONS))}")

    try:
        timeout_value = int(timeout)
    except (TypeError, ValueError) as e:
        raise ValueError('timeout must be an integer') from e
    if timeout_value <= 0:
        raise ValueError('timeout must be > 0')

    try:
        max_chars = int(max_output_chars)
    except (TypeError, ValueError) as e:
        raise ValueError('max_output_chars must be an integer') from e
    if max_chars <= 0:
        raise ValueError('max_output_chars must be > 0')

    unit_target = str(unit_path or f'/etc/systemd/system/{service}.service').strip()
    if not unit_target.startswith('/'):
        raise ValueError('unit_path must be absolute')

    ssh_prefix = [
        '-i', server['ssh_key_path'],
        '-p', str(server['port']),
        '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', 'IdentitiesOnly=yes',
        f"{server['user']}@{server['host']}",
        '--',
    ]

    command = ''
    install_meta = None
    if action_name == 'install':
        raw_unit = str(unit_content or '')
        if not raw_unit.strip():
            raise ValueError('unit_content must be non-empty for action=install')
        quoted_target = shlex.quote(unit_target)
        tee_prefix = 'sudo tee' if sudo else 'tee'
        command_parts = [
            f"mkdir -p {shlex.quote(unit_target.rsplit('/', 1)[0] or '/')}",
            f"{tee_prefix} {quoted_target} >/dev/null",
        ]
        systemctl = _systemctl_prefix(bool(sudo))
        command_parts.append(f'{systemctl} daemon-reload')
        if enable_on_install:
            command_parts.append(f'{systemctl} enable {shlex.quote(service)}')
        if start_on_install:
            command_parts.append(f'{systemctl} start {shlex.quote(service)}')
        command = ' && '.join(command_parts)
        res = _run_ssh_stream(ssh_prefix + [command], stdin_bytes=raw_unit.encode('utf-8'), timeout=timeout_value)
        stdout = _decode_output(res.stdout)
        stderr = _decode_output(res.stderr)
        combined = stdout + stderr
        install_meta = {
            'unit_path': unit_target,
            'unit_content_bytes': len(raw_unit.encode('utf-8')),
            'enable_on_install': bool(enable_on_install),
            'start_on_install': bool(start_on_install),
        }
    else:
        systemctl = _systemctl_prefix(bool(sudo))
        command = f'{systemctl} {action_name} {shlex.quote(service)}'
        res = _run_ssh_text(ssh_prefix + [command], timeout=timeout_value)
        stdout = res.stdout or ''
        stderr = res.stderr or ''
        combined = stdout + stderr

    payload = {
        'status': 'ok' if res.returncode == 0 else 'error',
        'executed_at': _utc_now_iso(),
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'server': {
            'alias': server['alias'],
            'host': server['host'],
            'port': server['port'],
            'user': server['user'],
            'deploy_path': server['deploy_path'],
            'registry_path': str(_project_server_registry_path(repo_dir)),
        },
        'service': {
            'name': service,
            'action': action_name,
            'sudo': bool(sudo),
            'unit_path': unit_target,
            'install': install_meta,
        },
        'command': {
            'raw': command,
            'transport': 'ssh',
            'timeout_seconds': timeout_value,
        },
        'result': {
            'ok': res.returncode == 0,
            'exit_code': res.returncode,
            'stdout': _clip_server_run_output(stdout, max_chars),
            'stderr': _clip_server_run_output(stderr, max_chars),
            'output': _clip_server_run_output(combined, max_chars),
            'truncated': _clip_server_run_output(combined, max_chars) != combined,
            'max_output_chars': max_chars,
        },
        'repo': _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)



def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            'project_service_control',
            'Manage a systemd service for a bootstrapped project on a registered remote server target: install/update a unit file or run start/stop/restart/status/enable/disable through SSH.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'alias': {'type': 'string', 'description': 'Registered server alias from the project-local .veles server registry'},
                'service_name': {'type': 'string', 'description': 'Systemd service name, with or without .service suffix'},
                'action': {'type': 'string', 'description': 'Lifecycle action to run', 'enum': sorted(_ALLOWED_SERVICE_ACTIONS)},
                'timeout': {'type': 'integer', 'description': 'SSH command timeout in seconds', 'default': _DEFAULT_SERVER_RUN_TIMEOUT},
                'max_output_chars': {'type': 'integer', 'description': 'Maximum combined stdout/stderr characters to return before clipping', 'default': _MAX_SERVER_RUN_OUTPUT_CHARS},
                'unit_content': {'type': 'string', 'description': 'Full systemd unit file content; required for action=install'},
                'unit_path': {'type': 'string', 'description': 'Absolute remote path for the unit file; defaults to /etc/systemd/system/<service>.service'},
                'sudo': {'type': 'boolean', 'description': 'Whether to prefix systemctl/tee with sudo on the remote host', 'default': True},
                'enable_on_install': {'type': 'boolean', 'description': 'Whether install should also run systemctl enable', 'default': True},
                'start_on_install': {'type': 'boolean', 'description': 'Whether install should also run systemctl start', 'default': False},
            },
            ['name', 'alias', 'service_name', 'action'],
            _project_service_control,
            is_code_tool=True,
        ),
    ]
