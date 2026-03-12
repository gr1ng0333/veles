from __future__ import annotations

import json
import shlex
from typing import Any, Dict, List

from ouroboros.tools.project_bootstrap import (
    _MAX_SERVER_RUN_OUTPUT_CHARS,
    _clip_server_run_output,
    _find_project_server,
    _normalize_project_name,
    _project_server_registry_path,
    _public_server_view,
    _repo_info,
    _require_local_project,
    _tool_entry,
    _utc_now_iso,
)
from ouroboros.tools.project_deploy_state import _project_deploy_state_path, _read_project_deploy_state
from ouroboros.tools.project_service import (
    _DEFAULT_SERVER_RUN_TIMEOUT,
    _default_unit_path,
    _normalize_service_unit_name,
    _run_ssh_text,
)
from ouroboros.tools.registry import ToolContext, ToolEntry


def _normalize_positive_int(value: Any, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f'{field} must be an integer') from e
    if parsed <= 0:
        raise ValueError(f'{field} must be > 0')
    return parsed


def _ssh_prefix(server: Dict[str, Any]) -> List[str]:
    return [
        '-i', server['ssh_key_path'],
        '-p', str(server['port']),
        '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', 'IdentitiesOnly=yes',
        f"{server['user']}@{server['host']}",
        '--',
    ]


def _run_remote_text(server: Dict[str, Any], command: str, timeout: int):
    return _run_ssh_text(_ssh_prefix(server) + [command], timeout=timeout)


def _parse_key_value_lines(stdout: str) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for line in (stdout or '').splitlines():
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        key_name = key.strip()
        if not key_name:
            continue
        parsed[key_name] = value.strip()
    return parsed


def _as_bool_flag(value: str) -> bool:
    return str(value or '').strip() in {'1', 'true', 'yes', 'on'}


def _base_payload(project_name: str, repo_dir, server: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'checked_at': _utc_now_iso(),
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'server': {
            **_public_server_view(server),
            'registry_path': str(_project_server_registry_path(repo_dir)),
        },
        'repo': _repo_info(repo_dir),
    }


def _service_status_snapshot(
    server: Dict[str, Any],
    service_unit: str,
    *,
    timeout: int,
    sudo: bool,
    max_output_chars: int,
) -> Dict[str, Any]:
    prefix = 'sudo systemctl' if sudo else 'systemctl'
    quoted = shlex.quote(service_unit)
    command = (
        f"{prefix} show {quoted} --no-page "
        '--property=LoadState,ActiveState,SubState,UnitFileState,FragmentPath,ExecMainPID,ExecMainStatus,Result '
        f"&& {prefix} is-enabled {quoted} 2>/dev/null || true"
    )
    res = _run_remote_text(server, command, timeout)
    stdout = res.stdout or ''
    stderr = res.stderr or ''
    combined = stdout + stderr
    parsed = _parse_key_value_lines(stdout)
    enabled_line = ''
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    for line in reversed(lines):
        if '=' not in line:
            enabled_line = line
            break
    enabled_state = enabled_line or parsed.get('UnitFileState') or 'unknown'
    clipped_stdout = _clip_server_run_output(stdout, max_output_chars)
    clipped_stderr = _clip_server_run_output(stderr, max_output_chars)
    clipped_output = _clip_server_run_output(combined, max_output_chars)
    return {
        'status': 'ok' if res.returncode == 0 else 'error',
        'command': {
            'raw': command,
            'transport': 'ssh',
            'timeout_seconds': timeout,
        },
        'service': {
            'name': service_unit[:-len('.service')] if service_unit.endswith('.service') else service_unit,
            'unit_name': service_unit,
            'unit_path': _default_unit_path(service_unit),
            'sudo': bool(sudo),
            'load_state': parsed.get('LoadState') or '',
            'active_state': parsed.get('ActiveState') or '',
            'sub_state': parsed.get('SubState') or '',
            'unit_file_state': parsed.get('UnitFileState') or '',
            'enabled_state': enabled_state,
            'fragment_path': parsed.get('FragmentPath') or '',
            'exec_main_pid': parsed.get('ExecMainPID') or '',
            'exec_main_status': parsed.get('ExecMainStatus') or '',
            'result_state': parsed.get('Result') or '',
            'exists': (parsed.get('LoadState') or '') not in {'', 'not-found'},
            'running': (parsed.get('ActiveState') or '') == 'active',
        },
        'result': {
            'ok': res.returncode == 0,
            'exit_code': res.returncode,
            'stdout': clipped_stdout,
            'stderr': clipped_stderr,
            'output': clipped_output,
            'truncated': clipped_output != combined,
            'max_output_chars': max_output_chars,
        },
    }


def _project_server_health(ctx: ToolContext, name: str, alias: str, timeout: int = _DEFAULT_SERVER_RUN_TIMEOUT) -> str:
    repo_dir = _require_local_project(name)
    project_name = _normalize_project_name(name)
    server = _find_project_server(repo_dir, alias)
    timeout_value = _normalize_positive_int(timeout, 'timeout')
    deploy_path = shlex.quote(server['deploy_path'])
    command = (
        'printf "HOSTNAME=%s\\n" "$(hostname 2>/dev/null || true)"; '
        'printf "KERNEL=%s\\n" "$(uname -sr 2>/dev/null || true)"; '
        'printf "WHOAMI=%s\\n" "$(whoami 2>/dev/null || true)"; '
        'printf "PWD=%s\\n" "$(pwd 2>/dev/null || true)"; '
        'if command -v systemctl >/dev/null 2>&1; then printf "SYSTEMCTL=present\\n"; else printf "SYSTEMCTL=missing\\n"; fi; '
        f'if [ -d {deploy_path} ]; then printf "DEPLOY_EXISTS=1\\n"; else printf "DEPLOY_EXISTS=0\\n"; fi; '
        f'if [ -w {deploy_path} ]; then printf "DEPLOY_WRITABLE=1\\n"; else printf "DEPLOY_WRITABLE=0\\n"; fi'
    )
    res = _run_remote_text(server, command, timeout_value)
    stdout = res.stdout or ''
    stderr = res.stderr or ''
    combined = stdout + stderr
    parsed = _parse_key_value_lines(stdout)
    payload = {
        'status': 'ok' if res.returncode == 0 else 'error',
        **_base_payload(project_name, repo_dir, server),
        'health': {
            'reachable': res.returncode == 0,
            'hostname': parsed.get('HOSTNAME') or '',
            'kernel': parsed.get('KERNEL') or '',
            'remote_user': parsed.get('WHOAMI') or '',
            'remote_pwd': parsed.get('PWD') or '',
            'systemctl_available': (parsed.get('SYSTEMCTL') or '') == 'present',
            'deploy_path_exists': _as_bool_flag(parsed.get('DEPLOY_EXISTS', '0')),
            'deploy_path_writable': _as_bool_flag(parsed.get('DEPLOY_WRITABLE', '0')),
        },
        'command': {
            'raw': command,
            'transport': 'ssh',
            'timeout_seconds': timeout_value,
        },
        'result': {
            'ok': res.returncode == 0,
            'exit_code': res.returncode,
            'stdout': stdout,
            'stderr': stderr,
            'output': combined,
            'truncated': False,
            'max_output_chars': None,
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _project_service_status(
    ctx: ToolContext,
    name: str,
    alias: str,
    service_name: str,
    timeout: int = _DEFAULT_SERVER_RUN_TIMEOUT,
    max_output_chars: int = _MAX_SERVER_RUN_OUTPUT_CHARS,
    sudo: bool = True,
) -> str:
    repo_dir = _require_local_project(name)
    project_name = _normalize_project_name(name)
    server = _find_project_server(repo_dir, alias)
    timeout_value = _normalize_positive_int(timeout, 'timeout')
    max_chars = _normalize_positive_int(max_output_chars, 'max_output_chars')
    _, service_unit = _normalize_service_unit_name(service_name)
    snapshot = _service_status_snapshot(
        server,
        service_unit,
        timeout=timeout_value,
        sudo=bool(sudo),
        max_output_chars=max_chars,
    )
    payload = {
        'status': snapshot['status'],
        **_base_payload(project_name, repo_dir, server),
        'service': snapshot['service'],
        'command': snapshot['command'],
        'result': snapshot['result'],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _project_service_logs(
    ctx: ToolContext,
    name: str,
    alias: str,
    service_name: str,
    lines: int = 100,
    timeout: int = _DEFAULT_SERVER_RUN_TIMEOUT,
    max_output_chars: int = _MAX_SERVER_RUN_OUTPUT_CHARS,
    sudo: bool = True,
) -> str:
    repo_dir = _require_local_project(name)
    project_name = _normalize_project_name(name)
    server = _find_project_server(repo_dir, alias)
    line_count = _normalize_positive_int(lines, 'lines')
    timeout_value = _normalize_positive_int(timeout, 'timeout')
    max_chars = _normalize_positive_int(max_output_chars, 'max_output_chars')
    _, service_unit = _normalize_service_unit_name(service_name)
    prefix = 'sudo journalctl' if sudo else 'journalctl'
    command = f"{prefix} -u {shlex.quote(service_unit)} -n {line_count} --no-pager --output=short-iso"
    res = _run_remote_text(server, command, timeout_value)
    stdout = res.stdout or ''
    stderr = res.stderr or ''
    combined = stdout + stderr
    payload = {
        'status': 'ok' if res.returncode == 0 else 'error',
        **_base_payload(project_name, repo_dir, server),
        'service': {
            'name': service_unit[:-len('.service')] if service_unit.endswith('.service') else service_unit,
            'unit_name': service_unit,
            'sudo': bool(sudo),
        },
        'logs': {
            'lines_requested': line_count,
            'content': _clip_server_run_output(stdout, max_chars),
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
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _project_deploy_status(
    ctx: ToolContext,
    name: str,
    alias: str,
    service_name: str = '',
    timeout: int = _DEFAULT_SERVER_RUN_TIMEOUT,
    max_output_chars: int = _MAX_SERVER_RUN_OUTPUT_CHARS,
    sudo: bool = True,
) -> str:
    repo_dir = _require_local_project(name)
    project_name = _normalize_project_name(name)
    server = _find_project_server(repo_dir, alias)
    timeout_value = _normalize_positive_int(timeout, 'timeout')
    max_chars = _normalize_positive_int(max_output_chars, 'max_output_chars')

    deploy_path = shlex.quote(server['deploy_path'])
    command = (
        f'if [ -d {deploy_path} ]; then '
        'printf "DEPLOY_EXISTS=1\\n"; '
        f'printf "DEPLOY_REALPATH=%s\\n" "$(cd {deploy_path} 2>/dev/null && pwd || true)"; '
        f'printf "DEPLOY_TOP_LEVEL_COUNT=%s\\n" "$(find {deploy_path} -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d \"[:space:]\")"; '
        f'if [ -w {deploy_path} ]; then printf "DEPLOY_WRITABLE=1\\n"; else printf "DEPLOY_WRITABLE=0\\n"; fi; '
        f'if [ -f {deploy_path}/.git/HEAD ]; then printf "DEPLOY_GIT=1\\n"; else printf "DEPLOY_GIT=0\\n"; fi; '
        'else printf "DEPLOY_EXISTS=0\\nDEPLOY_REALPATH=\\nDEPLOY_TOP_LEVEL_COUNT=0\\nDEPLOY_WRITABLE=0\\nDEPLOY_GIT=0\\n"; fi'
    )
    probe = _run_remote_text(server, command, timeout_value)
    stdout = probe.stdout or ''
    stderr = probe.stderr or ''
    combined = stdout + stderr
    parsed = _parse_key_value_lines(stdout)

    service_payload = None
    if str(service_name or '').strip():
        _, service_unit = _normalize_service_unit_name(service_name)
        service_payload = _service_status_snapshot(
            server,
            service_unit,
            timeout=timeout_value,
            sudo=bool(sudo),
            max_output_chars=max_chars,
        )

    deploy_state_path = _project_deploy_state_path(repo_dir)
    last_deploy = _read_project_deploy_state(repo_dir)
    payload = {
        'status': 'ok' if probe.returncode == 0 and (service_payload is None or service_payload['status'] == 'ok') else 'error',
        **_base_payload(project_name, repo_dir, server),
        'deploy': {
            'path': server['deploy_path'],
            'exists': _as_bool_flag(parsed.get('DEPLOY_EXISTS', '0')),
            'writable': _as_bool_flag(parsed.get('DEPLOY_WRITABLE', '0')),
            'realpath': parsed.get('DEPLOY_REALPATH') or '',
            'top_level_entry_count': int(parsed.get('DEPLOY_TOP_LEVEL_COUNT') or 0),
            'looks_like_git_checkout': _as_bool_flag(parsed.get('DEPLOY_GIT', '0')),
        },
        'last_deploy': {
            'path': str(deploy_state_path),
            'exists': deploy_state_path.exists(),
            'outcome': last_deploy,
        },
        'command': {
            'raw': command,
            'transport': 'ssh',
            'timeout_seconds': timeout_value,
        },
        'result': {
            'ok': probe.returncode == 0,
            'exit_code': probe.returncode,
            'stdout': _clip_server_run_output(stdout, max_chars),
            'stderr': _clip_server_run_output(stderr, max_chars),
            'output': _clip_server_run_output(combined, max_chars),
            'truncated': _clip_server_run_output(combined, max_chars) != combined,
            'max_output_chars': max_chars,
        },
    }
    if service_payload is not None:
        payload['service'] = service_payload['service']
        payload['service_result'] = service_payload['result']
    return json.dumps(payload, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            'project_server_health',
            'Read a compact remote server health snapshot for a registered project deploy target over SSH: connectivity, host identity, systemctl availability, and deploy path presence/writability.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'alias': {'type': 'string', 'description': 'Registered server alias from the project-local .veles server registry'},
                'timeout': {'type': 'integer', 'description': 'SSH timeout in seconds', 'default': _DEFAULT_SERVER_RUN_TIMEOUT},
            },
            ['name', 'alias'],
            _project_server_health,
            is_code_tool=True,
        ),
        _tool_entry(
            'project_service_status',
            'Read structured systemd status for a service on a registered project deploy target: load/active/sub states, unit file state, PID, result state, and raw command output.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'alias': {'type': 'string', 'description': 'Registered server alias from the project-local .veles server registry'},
                'service_name': {'type': 'string', 'description': 'Systemd service name, with or without .service suffix'},
                'timeout': {'type': 'integer', 'description': 'SSH timeout in seconds', 'default': _DEFAULT_SERVER_RUN_TIMEOUT},
                'max_output_chars': {'type': 'integer', 'description': 'Maximum returned output characters', 'default': _MAX_SERVER_RUN_OUTPUT_CHARS},
                'sudo': {'type': 'boolean', 'description': 'Whether to call systemctl through sudo', 'default': True},
            },
            ['name', 'alias', 'service_name'],
            _project_service_status,
            is_code_tool=True,
        ),
        _tool_entry(
            'project_service_logs',
            'Read the latest journalctl logs for a systemd service on a registered project deploy target, with bounded line count and output clipping.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'alias': {'type': 'string', 'description': 'Registered server alias from the project-local .veles server registry'},
                'service_name': {'type': 'string', 'description': 'Systemd service name, with or without .service suffix'},
                'lines': {'type': 'integer', 'description': 'How many latest log lines to request', 'default': 100},
                'timeout': {'type': 'integer', 'description': 'SSH timeout in seconds', 'default': _DEFAULT_SERVER_RUN_TIMEOUT},
                'max_output_chars': {'type': 'integer', 'description': 'Maximum returned output characters', 'default': _MAX_SERVER_RUN_OUTPUT_CHARS},
                'sudo': {'type': 'boolean', 'description': 'Whether to call journalctl through sudo', 'default': True},
            },
            ['name', 'alias', 'service_name'],
            _project_service_logs,
            is_code_tool=True,
        ),
        _tool_entry(
            'project_deploy_status',
            'Read a combined deploy-target status snapshot for a registered project server alias: deploy path presence/writability, top-level file count, git-checkout hint, and optional systemd service status.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'alias': {'type': 'string', 'description': 'Registered server alias from the project-local .veles server registry'},
                'service_name': {'type': 'string', 'description': 'Optional systemd service name to include in the deploy status snapshot'},
                'timeout': {'type': 'integer', 'description': 'SSH timeout in seconds', 'default': _DEFAULT_SERVER_RUN_TIMEOUT},
                'max_output_chars': {'type': 'integer', 'description': 'Maximum returned output characters', 'default': _MAX_SERVER_RUN_OUTPUT_CHARS},
                'sudo': {'type': 'boolean', 'description': 'Whether to call systemctl through sudo when service_name is provided', 'default': True},
            },
            ['name', 'alias'],
            _project_deploy_status,
            is_code_tool=True,
        ),
    ]
