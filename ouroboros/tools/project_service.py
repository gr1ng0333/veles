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


_ALLOWED_PROJECT_SERVICE_RUNTIMES = {'auto', 'python', 'node', 'static'}


def _detect_project_runtime(repo_dir) -> str:
    if (repo_dir / 'package.json').exists():
        return 'node'
    if (repo_dir / 'requirements.txt').exists() or (repo_dir / 'src').exists():
        return 'python'
    if (repo_dir / 'index.html').exists():
        return 'static'
    raise ValueError('could not detect project runtime; pass runtime explicitly')


def _normalize_project_service_runtime(runtime: str) -> str:
    value = str(runtime or 'auto').strip().lower()
    if value not in _ALLOWED_PROJECT_SERVICE_RUNTIMES:
        raise ValueError(f"runtime must be one of: {', '.join(sorted(_ALLOWED_PROJECT_SERVICE_RUNTIMES))}")
    return value


def _default_exec_start_for_runtime(repo_dir, deploy_path: str, runtime: str) -> str:
    if runtime == 'python':
        package_dirs = sorted(
            p.name for p in (repo_dir / 'src').iterdir()
            if p.is_dir() and (p / '__init__.py').exists() and (p / 'main.py').exists()
        ) if (repo_dir / 'src').exists() else []
        if package_dirs:
            package = package_dirs[0]
            return f'/usr/bin/python3 -m src.{package}.main'
        return f"/usr/bin/python3 {deploy_path.rstrip('/')}/main.py"
    if runtime == 'node':
        return '/usr/bin/node src/index.js'
    return '/usr/bin/python3 -m http.server ${PORT:-8000} --directory .'


def _render_project_service_unit(
    repo_dir,
    service_name: str,
    deploy_path: str,
    runtime: str = 'auto',
    description: str = '',
    working_directory: str = '',
    exec_start: str = '',
    environment_file: str = '',
    environment: list[str] | None = None,
    user: str = '',
    restart: str = 'always',
    restart_sec: int = 3,
    wanted_by: str = 'multi-user.target',
) -> dict:
    runtime_value = _normalize_project_service_runtime(runtime)
    resolved_runtime = _detect_project_runtime(repo_dir) if runtime_value == 'auto' else runtime_value
    service_base, service_unit = _normalize_service_unit_name(service_name)
    deploy_root = str(deploy_path or '').strip()
    if not deploy_root.startswith('/'):
        raise ValueError('deploy_path must be absolute')
    workdir = str(working_directory or deploy_root).strip()
    if not workdir.startswith('/'):
        raise ValueError('working_directory must be absolute')
    if environment_file and not str(environment_file).strip().startswith('/'):
        raise ValueError('environment_file must be absolute when provided')
    restart_policy = str(restart or '').strip()
    if not restart_policy:
        raise ValueError('restart must be non-empty')
    wanted = str(wanted_by or '').strip()
    if not wanted:
        raise ValueError('wanted_by must be non-empty')
    try:
        restart_delay = int(restart_sec)
    except (TypeError, ValueError) as e:
        raise ValueError('restart_sec must be an integer') from e
    if restart_delay < 0:
        raise ValueError('restart_sec must be >= 0')

    env_lines = []
    for item in environment or []:
        raw = str(item or '').strip()
        if not raw:
            continue
        if '=' not in raw:
            raise ValueError('environment entries must use KEY=VALUE format')
        env_lines.append(raw)

    exec_command = str(exec_start or '').strip() or _default_exec_start_for_runtime(repo_dir, deploy_root, resolved_runtime)
    description_text = str(description or '').strip() or f'{service_base} ({resolved_runtime})'
    run_user = str(user or '').strip()

    lines = [
        '[Unit]',
        f'Description={description_text}',
        'After=network.target',
        '',
        '[Service]',
        'Type=simple',
        f'WorkingDirectory={workdir}',
    ]
    if run_user:
        lines.append(f'User={run_user}')
    if environment_file:
        lines.append(f'EnvironmentFile={str(environment_file).strip()}')
    for item in env_lines:
        lines.append(f'Environment={item}')
    lines.extend([
        f'ExecStart={exec_command}',
        f'Restart={restart_policy}',
        f'RestartSec={restart_delay}',
        '',
        '[Install]',
        f'WantedBy={wanted}',
        '',
    ])
    content = '\n'.join(lines)
    return {
        'service_name': service_base,
        'unit_name': service_unit,
        'runtime': resolved_runtime,
        'deploy_path': deploy_root,
        'working_directory': workdir,
        'exec_start': exec_command,
        'environment_file': str(environment_file or '').strip(),
        'environment': env_lines,
        'user': run_user,
        'restart': restart_policy,
        'restart_sec': restart_delay,
        'wanted_by': wanted,
        'content': content,
    }


def _project_service_render_unit(
    ctx: ToolContext,
    name: str,
    service_name: str,
    runtime: str = 'auto',
    deploy_path: str = '',
    description: str = '',
    working_directory: str = '',
    exec_start: str = '',
    environment_file: str = '',
    environment: list[str] | None = None,
    user: str = '',
    restart: str = 'always',
    restart_sec: int = 3,
    wanted_by: str = 'multi-user.target',
) -> str:
    repo_dir = _require_local_project(name)
    project_name = _normalize_project_name(name)
    deploy_root = str(deploy_path or '').strip()
    if not deploy_root.startswith('/'):
        raise ValueError('deploy_path must be absolute')

    rendered = _render_project_service_unit(
        repo_dir=repo_dir,
        service_name=service_name,
        deploy_path=deploy_root,
        runtime=runtime,
        description=description,
        working_directory=working_directory,
        exec_start=exec_start,
        environment_file=environment_file,
        environment=environment,
        user=user,
        restart=restart,
        restart_sec=restart_sec,
        wanted_by=wanted_by,
    )

    payload = {
        'status': 'ok',
        'rendered_at': _utc_now_iso(),
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'service': {
            'name': rendered['service_name'],
            'unit_name': rendered['unit_name'],
            'runtime': rendered['runtime'],
            'deploy_path': rendered['deploy_path'],
            'working_directory': rendered['working_directory'],
            'exec_start': rendered['exec_start'],
            'environment_file': rendered['environment_file'],
            'environment': rendered['environment'],
            'user': rendered['user'],
            'restart': rendered['restart'],
            'restart_sec': rendered['restart_sec'],
            'wanted_by': rendered['wanted_by'],
            'unit_content': rendered['content'],
            'unit_path': _default_unit_path(rendered['unit_name']),
        },
        'repo': _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _normalize_service_unit_name(service_name: str) -> tuple[str, str]:
    raw = str(service_name or '').strip()
    if not raw:
        raise ValueError('service_name must be non-empty')
    if '/' in raw or raw in {'.', '..'}:
        raise ValueError('service_name must be a plain systemd unit name')
    unit_name = raw if raw.endswith('.service') else f'{raw}.service'
    base_name = unit_name[:-len('.service')]
    if not base_name:
        raise ValueError('service_name must contain characters before .service')
    return base_name, unit_name


def _default_unit_path(unit_name: str) -> str:
    return f'/etc/systemd/system/{unit_name}'


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

    service_base, service_unit = _normalize_service_unit_name(service_name)
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

    unit_target = str(unit_path or _default_unit_path(service_unit)).strip()
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
        mkdir_prefix = 'sudo mkdir -p' if sudo else 'mkdir -p'
        command_parts = [
            f"{mkdir_prefix} {shlex.quote(unit_target.rsplit('/', 1)[0] or '/')}",
            f"{tee_prefix} {quoted_target} >/dev/null",
        ]
        systemctl = _systemctl_prefix(bool(sudo))
        command_parts.append(f'{systemctl} daemon-reload')
        if enable_on_install:
            command_parts.append(f'{systemctl} enable {shlex.quote(service_unit)}')
        if start_on_install:
            command_parts.append(f'{systemctl} start {shlex.quote(service_unit)}')
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
        command = f'{systemctl} {action_name} {shlex.quote(service_unit)}'
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
            'name': service_base,
            'unit_name': service_unit,
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
            'project_service_render_unit',
            'Render a systemd unit file for a bootstrapped project as a structured artifact, with runtime-aware defaults for python/node/static deployments.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'service_name': {'type': 'string', 'description': 'Systemd service name, with or without .service suffix'},
                'runtime': {'type': 'string', 'description': 'Runtime kind to render for; auto detects from project structure', 'enum': sorted(_ALLOWED_PROJECT_SERVICE_RUNTIMES), 'default': 'auto'},
                'deploy_path': {'type': 'string', 'description': 'Absolute remote deploy path that the unit will run from'},
                'description': {'type': 'string', 'description': 'Optional systemd Description value'},
                'working_directory': {'type': 'string', 'description': 'Absolute WorkingDirectory override; defaults to deploy_path'},
                'exec_start': {'type': 'string', 'description': 'Optional explicit ExecStart override'},
                'environment_file': {'type': 'string', 'description': 'Optional absolute EnvironmentFile path'},
                'environment': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Optional Environment= entries in KEY=VALUE form'},
                'user': {'type': 'string', 'description': 'Optional remote user to run the service as'},
                'restart': {'type': 'string', 'description': 'systemd Restart policy', 'default': 'always'},
                'restart_sec': {'type': 'integer', 'description': 'systemd RestartSec value in seconds', 'default': 3},
                'wanted_by': {'type': 'string', 'description': 'Install target for the [Install] section', 'default': 'multi-user.target'},
            },
            ['name', 'service_name', 'deploy_path'],
            _project_service_render_unit,
            is_code_tool=True,
        ),
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
