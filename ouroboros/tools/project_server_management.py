from __future__ import annotations

import json
import shlex
from typing import Any, Dict, List

from ouroboros.tools.project_bootstrap import (
    _find_project_server,
    _load_project_server_registry,
    _normalize_project_name,
    _normalize_server_alias,
    _normalize_server_auth,
    _normalize_server_deploy_path,
    _normalize_server_host,
    _normalize_server_label,
    _normalize_server_port,
    _normalize_server_ssh_key_path,
    _normalize_server_user,
    _project_server_registry_path,
    _public_server_view,
    _repo_info,
    _require_local_project,
    _save_project_server_registry,
    _tool_entry,
    _utc_now_iso,
)
from ouroboros.tools.project_server_observability import (
    _as_bool_flag,
    _base_payload,
    _parse_key_value_lines,
    _run_remote_text,
    _ssh_prefix,
)
from ouroboros.tools.project_service import _DEFAULT_SERVER_RUN_TIMEOUT, _normalize_service_unit_name
from ouroboros.tools.registry import ToolContext, ToolEntry


def _normalize_optional_positive_int(value: Any, field: str) -> int | None:
    if value is None or str(value).strip() == '':
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f'{field} must be an integer') from e
    if parsed <= 0:
        raise ValueError(f'{field} must be > 0')
    return parsed


def _project_server_update(
    ctx: ToolContext,
    name: str,
    alias: str,
    new_alias: str = '',
    host: str = '',
    user: str = '',
    ssh_key_path: str = '',
    deploy_path: str = '',
    port: int | None = None,
    label: str = '',
    auth: str = '',
) -> str:
    repo_dir = _require_local_project(name)
    project_name = _normalize_project_name(name)
    alias_name = _normalize_server_alias(alias)
    servers = _load_project_server_registry(repo_dir)

    target_index = None
    for idx, item in enumerate(servers):
        if item.get('alias') == alias_name:
            target_index = idx
            break
    if target_index is None:
        raise ValueError(f'project server alias not found: {alias_name}')

    server = dict(servers[target_index])
    updated_alias = _normalize_server_alias(new_alias) if str(new_alias or '').strip() else alias_name
    if updated_alias != alias_name and any(item.get('alias') == updated_alias for item in servers):
        raise ValueError(f'project server alias already exists: {updated_alias}')

    if str(host or '').strip():
        server['host'] = _normalize_server_host(host)
    if str(user or '').strip():
        server['user'] = _normalize_server_user(user)
    normalized_port = _normalize_optional_positive_int(port, 'port')
    if normalized_port is not None:
        server['port'] = _normalize_server_port(normalized_port)
    if str(ssh_key_path or '').strip():
        server['ssh_key_path'] = _normalize_server_ssh_key_path(ssh_key_path)
    if str(deploy_path or '').strip():
        server['deploy_path'] = _normalize_server_deploy_path(deploy_path)
    if str(auth or '').strip():
        server['auth'] = _normalize_server_auth(auth)
    if label is not None and str(label).strip() != '':
        server['label'] = _normalize_server_label(label)

    server['alias'] = updated_alias
    server['updated_at'] = _utc_now_iso()
    servers[target_index] = server
    servers.sort(key=lambda item: item.get('alias', ''))
    _save_project_server_registry(repo_dir, servers)

    payload = {
        'status': 'ok',
        'updated_at': server['updated_at'],
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'server': _public_server_view(server),
        'registry': {
            'path': str(_project_server_registry_path(repo_dir)),
            'count': len(servers),
            'aliases': [item.get('alias') for item in servers],
            'exists': _project_server_registry_path(repo_dir).exists(),
        },
        'repo': _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)



def _project_server_validate(
    ctx: ToolContext,
    name: str,
    alias: str,
    service_name: str = '',
    timeout: int = _DEFAULT_SERVER_RUN_TIMEOUT,
    sudo: bool = True,
) -> str:
    repo_dir = _require_local_project(name)
    project_name = _normalize_project_name(name)
    server = _find_project_server(repo_dir, alias)

    try:
        timeout_value = int(timeout)
    except (TypeError, ValueError) as e:
        raise ValueError('timeout must be an integer') from e
    if timeout_value <= 0:
        raise ValueError('timeout must be > 0')

    service_unit = ''
    if str(service_name or '').strip():
        _, service_unit = _normalize_service_unit_name(service_name)

    deploy_path = shlex.quote(server['deploy_path'])
    parent_path = shlex.quote(str((__import__('pathlib').Path(server['deploy_path']).parent)) or '/')
    command = (
        'printf "SSH_OK=1\n"; '
        'printf "WHOAMI=%s\n" "$(whoami 2>/dev/null || true)"; '
        'if command -v systemctl >/dev/null 2>&1; then printf "SYSTEMCTL=present\n"; else printf "SYSTEMCTL=missing\n"; fi; '
        f'if [ -d {deploy_path} ]; then printf "DEPLOY_EXISTS=1\n"; else printf "DEPLOY_EXISTS=0\n"; fi; '
        f'if [ -w {deploy_path} ]; then printf "DEPLOY_WRITABLE=1\n"; else printf "DEPLOY_WRITABLE=0\n"; fi; '
        f'if [ -d {parent_path} ]; then printf "PARENT_EXISTS=1\n"; else printf "PARENT_EXISTS=0\n"; fi; '
        f'if [ -w {parent_path} ]; then printf "PARENT_WRITABLE=1\n"; else printf "PARENT_WRITABLE=0\n"; fi'
    )
    probe = _run_remote_text(server, command, timeout_value)
    stdout = probe.stdout or ''
    stderr = probe.stderr or ''
    parsed = _parse_key_value_lines(stdout)

    service = None
    if service_unit:
        prefix = 'sudo systemctl' if sudo else 'systemctl'
        service_command = (
            f'{prefix} show {shlex.quote(service_unit)} --no-page '
            '--property=LoadState,ActiveState,SubState,UnitFileState,Result,FragmentPath'
        )
        service_probe = _run_remote_text(server, service_command, timeout_value)
        service_parsed = _parse_key_value_lines(service_probe.stdout or '')
        service = {
            'name': service_unit[:-len('.service')] if service_unit.endswith('.service') else service_unit,
            'unit_name': service_unit,
            'exists': (service_parsed.get('LoadState') or '') not in {'', 'not-found'},
            'load_state': service_parsed.get('LoadState') or '',
            'active_state': service_parsed.get('ActiveState') or '',
            'sub_state': service_parsed.get('SubState') or '',
            'unit_file_state': service_parsed.get('UnitFileState') or '',
            'result_state': service_parsed.get('Result') or '',
            'fragment_path': service_parsed.get('FragmentPath') or '',
            'status': 'ok' if service_probe.returncode == 0 else 'error',
        }
    systemctl_available = (parsed.get('SYSTEMCTL') or '') == 'present'
    deploy_exists = _as_bool_flag(parsed.get('DEPLOY_EXISTS', '0'))
    deploy_writable = _as_bool_flag(parsed.get('DEPLOY_WRITABLE', '0'))
    parent_exists = _as_bool_flag(parsed.get('PARENT_EXISTS', '0'))
    parent_writable = _as_bool_flag(parsed.get('PARENT_WRITABLE', '0'))

    checks = {
        'ssh': probe.returncode == 0,
        'systemctl_available': systemctl_available,
        'deploy_path_exists': deploy_exists,
        'deploy_path_writable': deploy_writable,
        'deploy_parent_exists': parent_exists,
        'deploy_parent_writable': parent_writable,
        'deploy_path_ready': deploy_writable or (not deploy_exists and parent_exists and parent_writable),
    }
    if service is not None:
        checks['service_unit_exists'] = bool(service['exists'])

    ok = checks['ssh'] and checks['deploy_path_ready']
    if service is not None:
        ok = ok and checks['service_unit_exists']

    payload = {
        'status': 'ok' if ok else 'error',
        **_base_payload(project_name, repo_dir, server),
        'validation': {
            'ok': ok,
            'checks': checks,
            'remote_user': parsed.get('WHOAMI') or '',
            'deploy_path': server['deploy_path'],
            'service_name': service_unit,
            'sudo': bool(sudo),
        },
        'command': {
            'raw': command,
            'transport': 'ssh',
            'timeout_seconds': timeout_value,
        },
        'result': {
            'ok': probe.returncode == 0,
            'exit_code': probe.returncode,
            'stdout': stdout,
            'stderr': stderr,
            'output': stdout + stderr,
            'truncated': False,
            'max_output_chars': None,
        },
    }
    if service is not None:
        payload['service'] = service
    return json.dumps(payload, ensure_ascii=False, indent=2)



def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            'project_server_update',
            'Update one registered deploy server target by alias in the project-local .veles server registry of an existing bootstrapped local project repository.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'alias': {'type': 'string', 'description': 'Registered server alias to update'},
                'new_alias': {'type': 'string', 'description': 'Optional new alias to rename the registered server target to'},
                'host': {'type': 'string', 'description': 'Optional new plain hostname or IP'},
                'user': {'type': 'string', 'description': 'Optional new SSH username'},
                'ssh_key_path': {'type': 'string', 'description': 'Optional new absolute SSH private key path or ~/...'},
                'deploy_path': {'type': 'string', 'description': 'Optional new absolute remote deploy path'},
                'port': {'type': 'integer', 'description': 'Optional new SSH port'},
                'label': {'type': 'string', 'description': 'Optional new human-readable label'},
                'auth': {'type': 'string', 'description': 'Optional auth kind; currently only ssh_key_path is supported'},
            },
            ['name', 'alias'],
            _project_server_update,
            is_code_tool=True,
        ),
        _tool_entry(
            'project_server_validate',
            'Validate a registered deploy server target over SSH: connectivity, deploy path readiness, systemctl availability, and optional systemd unit existence for a bootstrapped local project repository.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'alias': {'type': 'string', 'description': 'Registered server alias to validate'},
                'service_name': {'type': 'string', 'description': 'Optional systemd service name to validate if it exists on the target'},
                'timeout': {'type': 'integer', 'description': 'SSH timeout in seconds', 'default': _DEFAULT_SERVER_RUN_TIMEOUT},
                'sudo': {'type': 'boolean', 'description': 'Whether to use sudo for systemctl checks when validating service existence', 'default': True},
            },
            ['name', 'alias'],
            _project_server_validate,
            is_code_tool=True,
        ),
    ]
