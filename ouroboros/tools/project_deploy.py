from __future__ import annotations

import io
import json
import os
import pathlib
import shlex
import subprocess
import tarfile
from typing import Any, Dict, List, Tuple

from ouroboros.tools.project_bootstrap import (
    _DEFAULT_SERVER_RUN_TIMEOUT,
    _find_project_server,
    _normalize_project_name,
    _project_server_registry_path,
    _repo_info,
    _require_local_project,
    _tool_entry,
    _utc_now_iso,
)
from ouroboros.tools.registry import ToolContext, ToolEntry

_EXCLUDED_ROOT_NAMES = {'.git', '.veles'}
_MAX_SYNC_FILE_COUNT = 10_000


_ALLOWED_DEPLOY_RECIPE_RUNTIMES = {'auto', 'python', 'node', 'static'}
_ALLOWED_DEPLOY_APPLY_MODES = {'install', 'update', 'start'}



def _normalize_recipe_runtime(runtime: str) -> str:
    value = str(runtime or 'auto').strip().lower()
    if value not in _ALLOWED_DEPLOY_RECIPE_RUNTIMES:
        raise ValueError(f"runtime must be one of: {', '.join(sorted(_ALLOWED_DEPLOY_RECIPE_RUNTIMES))}")
    return value


def _normalize_deploy_apply_mode(mode: str) -> str:
    value = str(mode or 'install').strip().lower()
    if value not in _ALLOWED_DEPLOY_APPLY_MODES:
        raise ValueError(f"mode must be one of: {', '.join(sorted(_ALLOWED_DEPLOY_APPLY_MODES))}")
    return value


def _decode_tool_payload(raw: str) -> Dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError('internal deploy tool returned invalid JSON') from e
    if not isinstance(payload, dict):
        raise RuntimeError('internal deploy tool returned non-object payload')
    return payload


def _step_result(key: str, tool: str, args: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'key': key,
        'tool': tool,
        'args': args,
        'status': payload.get('status') or 'unknown',
        'payload': payload,
    }


def _setup_result(key: str, commands: List[str], payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'key': key,
        'tool': 'project_server_run',
        'args': {
            'commands': commands,
            'combined_command': payload.get('command', {}).get('raw') or ' && '.join(commands),
        },
        'status': payload.get('status') or 'unknown',
        'payload': payload,
    }



def _project_setup_run(
    ctx: ToolContext,
    name: str,
    alias: str,
    commands: List[str],
    timeout: int,
) -> Dict[str, Any]:
    normalized_commands = [str(item or '').strip() for item in commands if str(item or '').strip()]
    if not normalized_commands:
        return {
            'status': 'ok',
            'executed_at': _utc_now_iso(),
            'project': {
                'name': _normalize_project_name(name),
                'path': str(_require_local_project(name)),
            },
            'server': _find_project_server(_require_local_project(name), alias),
            'command': {
                'raw': '',
                'transport': 'ssh',
                'timeout_seconds': int(timeout),
            },
            'setup': {
                'commands': [],
                'count': 0,
                'skipped': True,
            },
            'result': {
                'ok': True,
                'exit_code': 0,
                'stdout': '',
                'stderr': '',
                'output': '',
                'truncated': False,
                'max_output_chars': None,
            },
            'repo': _repo_info(_require_local_project(name)),
        }

    from ouroboros.tools.project_bootstrap import _project_server_run

    raw = _project_server_run(
        ctx,
        name=name,
        alias=alias,
        command=' && '.join(normalized_commands),
        timeout=timeout,
    )
    payload = _decode_tool_payload(raw)
    payload['setup'] = {
        'commands': normalized_commands,
        'count': len(normalized_commands),
        'skipped': False,
    }
    return payload


def _project_deploy_apply(
    ctx: ToolContext,
    name: str,
    alias: str,
    service_name: str,
    mode: str = 'install',
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
    sync_timeout: int = _DEFAULT_SERVER_RUN_TIMEOUT,
    service_timeout: int = _DEFAULT_SERVER_RUN_TIMEOUT,
    status_timeout: int = _DEFAULT_SERVER_RUN_TIMEOUT,
    delete: bool = False,
    enable_on_install: bool = True,
    start_on_install: bool = False,
    sudo: bool = True,
) -> str:
    project_name = _normalize_project_name(name)
    mode_value = _normalize_deploy_apply_mode(mode)

    try:
        status_timeout_value = int(status_timeout)
    except (TypeError, ValueError) as e:
        raise ValueError('status_timeout must be an integer') from e
    if status_timeout_value <= 0:
        raise ValueError('status_timeout must be > 0')

    recipe_payload = _decode_tool_payload(
        _project_deploy_recipe(
            ctx,
            name=project_name,
            alias=alias,
            service_name=service_name,
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
            sync_timeout=sync_timeout,
            service_timeout=service_timeout,
            delete=delete,
        )
    )

    from ouroboros.tools.project_service import _project_service_control

    sync_args = dict(recipe_payload['recipe']['steps'][0]['recommended_args'])
    setup_commands = list(recipe_payload['recipe']['steps'][1].get('commands') or [])
    install_args = dict(recipe_payload['recipe']['steps'][2]['recommended_args'])
    install_args.update({
        'enable_on_install': bool(enable_on_install),
        'start_on_install': bool(start_on_install),
        'sudo': bool(sudo),
    })

    steps: List[Dict[str, Any]] = []
    sync_payload = _decode_tool_payload(_project_server_sync(ctx, **sync_args))
    steps.append(_step_result('sync', 'project_server_sync', sync_args, sync_payload))
    if sync_payload.get('status') != 'ok':
        return json.dumps({
            'status': 'error',
            'applied_at': _utc_now_iso(),
            'project': recipe_payload['project'],
            'server': recipe_payload['server'],
            'mode': mode_value,
            'failed_step': 'sync',
            'steps': steps,
            'recipe': recipe_payload,
        }, ensure_ascii=False, indent=2)

    setup_payload = _project_setup_run(
        ctx,
        name=project_name,
        alias=alias,
        commands=setup_commands,
        timeout=sync_timeout,
    )
    steps.append(_setup_result('setup', setup_commands, setup_payload))
    if setup_payload.get('status') != 'ok':
        return json.dumps({
            'status': 'error',
            'applied_at': _utc_now_iso(),
            'project': recipe_payload['project'],
            'server': recipe_payload['server'],
            'mode': mode_value,
            'failed_step': 'setup',
            'steps': steps,
            'recipe': recipe_payload,
        }, ensure_ascii=False, indent=2)

    if mode_value in {'install', 'update'}:
        install_payload = _decode_tool_payload(_project_service_control(ctx, **install_args))
        steps.append(_step_result('install_service', 'project_service_control', install_args, install_payload))
        if install_payload.get('status') != 'ok':
            return json.dumps({
                'status': 'error',
                'applied_at': _utc_now_iso(),
                'project': recipe_payload['project'],
                'server': recipe_payload['server'],
                'mode': mode_value,
                'failed_step': 'install_service',
                'steps': steps,
                'recipe': recipe_payload,
            }, ensure_ascii=False, indent=2)

    lifecycle_action = 'start' if mode_value == 'install' else 'restart' if mode_value == 'update' else mode_value
    lifecycle_args = {
        'name': project_name,
        'alias': alias,
        'service_name': install_args['service_name'],
        'action': lifecycle_action,
        'timeout': service_timeout,
        'sudo': bool(sudo),
    }
    lifecycle_payload = _decode_tool_payload(_project_service_control(ctx, **lifecycle_args))
    steps.append(_step_result(lifecycle_action, 'project_service_control', lifecycle_args, lifecycle_payload))
    if lifecycle_payload.get('status') != 'ok':
        return json.dumps({
            'status': 'error',
            'applied_at': _utc_now_iso(),
            'project': recipe_payload['project'],
            'server': recipe_payload['server'],
            'mode': mode_value,
            'failed_step': lifecycle_action,
            'steps': steps,
            'recipe': recipe_payload,
        }, ensure_ascii=False, indent=2)

    status_args = {
        'name': project_name,
        'alias': alias,
        'service_name': install_args['service_name'],
        'action': 'status',
        'timeout': status_timeout_value,
        'sudo': bool(sudo),
    }
    status_payload = _decode_tool_payload(_project_service_control(ctx, **status_args))
    steps.append(_step_result('status', 'project_service_control', status_args, status_payload))

    payload = {
        'status': 'ok' if status_payload.get('status') == 'ok' else 'error',
        'applied_at': _utc_now_iso(),
        'project': recipe_payload['project'],
        'server': recipe_payload['server'],
        'mode': mode_value,
        'recipe': recipe_payload,
        'steps': steps,
        'summary': {
            'service_name': install_args['service_name'],
            'unit_name': recipe_payload['service']['unit_name'],
            'runtime': recipe_payload['runtime']['resolved'],
            'sync_file_count': recipe_payload['sync_preview']['file_count'],
            'lifecycle_action': lifecycle_action,
            'status_ok': status_payload.get('status') == 'ok',
        },
        'repo': recipe_payload.get('repo') or _repo_info(_require_local_project(project_name)),
    }
    if status_payload.get('status') != 'ok':
        payload['failed_step'] = 'status'
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _detect_recipe_runtime(repo_dir: pathlib.Path) -> str:
    if (repo_dir / 'package.json').exists():
        return 'node'
    if (repo_dir / 'requirements.txt').exists() or (repo_dir / 'src').exists():
        return 'python'
    if (repo_dir / 'index.html').exists():
        return 'static'
    raise ValueError('could not detect project runtime; pass runtime explicitly')


def _default_setup_steps(runtime: str, deploy_path: str) -> List[str]:
    if runtime == 'python':
        return [
            'Ensure python3 is installed on the server',
            f'Create a virtualenv if you do not want to use system Python: python3 -m venv {deploy_path.rstrip("/")}/.venv',
            f'Install dependencies if requirements.txt exists: cd {deploy_path.rstrip("/")} && python3 -m pip install -r requirements.txt',
        ]
    if runtime == 'node':
        return [
            'Ensure node and npm are installed on the server',
            f'Install dependencies after sync: cd {deploy_path.rstrip("/")} && npm install --production',
        ]
    return [
        'Ensure python3 is installed on the server (used for the built-in static file server)',
        'No package-install step is required for a plain static site unless you add your own build pipeline',
    ]


def _default_start_steps(runtime: str, unit_name: str) -> List[str]:
    return [
        f'Install the rendered unit as /etc/systemd/system/{unit_name}',
        'Run: sudo systemctl daemon-reload',
        f'Run: sudo systemctl enable --now {unit_name}',
        f'Check status: sudo systemctl status {unit_name} --no-pager',
    ]


def _project_deploy_recipe(
    ctx: ToolContext,
    name: str,
    alias: str,
    service_name: str,
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
    sync_timeout: int = _DEFAULT_SERVER_RUN_TIMEOUT,
    service_timeout: int = _DEFAULT_SERVER_RUN_TIMEOUT,
    delete: bool = False,
) -> str:
    repo_dir = _require_local_project(name)
    project_name = _normalize_project_name(name)
    server = _find_project_server(repo_dir, alias)

    runtime_value = _normalize_recipe_runtime(runtime)
    resolved_runtime = _detect_recipe_runtime(repo_dir) if runtime_value == 'auto' else runtime_value

    try:
        sync_timeout_value = int(sync_timeout)
    except (TypeError, ValueError) as e:
        raise ValueError('sync_timeout must be an integer') from e
    if sync_timeout_value <= 0:
        raise ValueError('sync_timeout must be > 0')

    try:
        service_timeout_value = int(service_timeout)
    except (TypeError, ValueError) as e:
        raise ValueError('service_timeout must be an integer') from e
    if service_timeout_value <= 0:
        raise ValueError('service_timeout must be > 0')

    deploy_path = server['deploy_path']
    from ouroboros.tools.project_service import _render_project_service_unit

    rendered = _render_project_service_unit(
        repo_dir=repo_dir,
        service_name=service_name,
        deploy_path=deploy_path,
        runtime=resolved_runtime,
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

    sync_preview = _build_sync_archive(repo_dir)
    archive_bytes, synced_files, archive_size = sync_preview
    del archive_bytes

    payload = {
        'status': 'ok',
        'generated_at': _utc_now_iso(),
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'server': {
            'alias': server['alias'],
            'host': server['host'],
            'port': server['port'],
            'user': server['user'],
            'deploy_path': deploy_path,
            'registry_path': str(_project_server_registry_path(repo_dir)),
        },
        'runtime': {
            'requested': runtime_value,
            'resolved': resolved_runtime,
        },
        'recipe': {
            'kind': 'project_deploy_recipe',
            'steps': [
                {
                    'key': 'sync',
                    'tool': 'project_server_sync',
                    'recommended_args': {
                        'name': project_name,
                        'alias': server['alias'],
                        'timeout': sync_timeout_value,
                        'delete': bool(delete),
                    },
                    'summary': 'Sync the current project working tree to the remote deploy path over ssh+tar.',
                },
                {
                    'key': 'setup',
                    'tool': None,
                    'summary': 'Run any runtime-specific package/install/bootstrap commands on the remote host before starting the service.',
                    'commands': _default_setup_steps(resolved_runtime, deploy_path),
                },
                {
                    'key': 'install_service',
                    'tool': 'project_service_control',
                    'recommended_args': {
                        'name': project_name,
                        'alias': server['alias'],
                        'action': 'install',
                        'service_name': rendered['service_name'],
                        'runtime': resolved_runtime,
                        'deploy_path': deploy_path,
                        'working_directory': rendered['working_directory'],
                        'exec_start': rendered['exec_start'],
                        'environment_file': rendered['environment_file'],
                        'environment': rendered['environment'],
                        'user': rendered['user'],
                        'restart': rendered['restart'],
                        'restart_sec': rendered['restart_sec'],
                        'wanted_by': rendered['wanted_by'],
                        'unit_content': rendered['content'],
                        'timeout': service_timeout_value,
                    },
                    'summary': 'Install the rendered systemd unit on the target server and reload systemd.',
                },
                {
                    'key': 'enable_start',
                    'tool': None,
                    'summary': 'Enable and start the service, then inspect its status.',
                    'commands': _default_start_steps(resolved_runtime, rendered['unit_name']),
                },
            ],
        },
        'sync_preview': {
            'timeout_seconds': sync_timeout_value,
            'delete': bool(delete),
            'archive_bytes': archive_size,
            'file_count': len(synced_files),
            'files': synced_files,
            'excluded_roots': sorted(_EXCLUDED_ROOT_NAMES),
        },
        'service': {
            'timeout_seconds': service_timeout_value,
            'name': rendered['service_name'],
            'unit_name': rendered['unit_name'],
            'deploy_path': rendered['deploy_path'],
            'working_directory': rendered['working_directory'],
            'exec_start': rendered['exec_start'],
            'environment_file': rendered['environment_file'],
            'environment': rendered['environment'],
            'user': rendered['user'],
            'restart': rendered['restart'],
            'restart_sec': rendered['restart_sec'],
            'wanted_by': rendered['wanted_by'],
            'unit_path': f"/etc/systemd/system/{rendered['unit_name']}",
            'unit_content': rendered['content'],
        },
        'repo': _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _iter_sync_files(repo_dir: pathlib.Path) -> List[pathlib.Path]:
    files: List[pathlib.Path] = []
    for root, dirnames, filenames in os.walk(repo_dir):
        root_path = pathlib.Path(root)
        rel_root = root_path.relative_to(repo_dir)
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in _EXCLUDED_ROOT_NAMES
        )
        if rel_root != pathlib.Path('.') and any(part in _EXCLUDED_ROOT_NAMES for part in rel_root.parts):
            continue
        for filename in sorted(filenames):
            rel_path = (rel_root / filename) if rel_root != pathlib.Path('.') else pathlib.Path(filename)
            if any(part in _EXCLUDED_ROOT_NAMES for part in rel_path.parts):
                continue
            files.append(rel_path)
            if len(files) > _MAX_SYNC_FILE_COUNT:
                raise ValueError(f'project sync file count exceeds limit ({_MAX_SYNC_FILE_COUNT})')
    return files



def _build_sync_archive(repo_dir: pathlib.Path) -> Tuple[bytes, List[str], int]:
    rel_paths = _iter_sync_files(repo_dir)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as tar:
        for rel_path in rel_paths:
            tar.add(repo_dir / rel_path, arcname=rel_path.as_posix(), recursive=False)
    payload = buffer.getvalue()
    return payload, [p.as_posix() for p in rel_paths], len(payload)



def _run_ssh_stream(args: List[str], stdin_bytes: bytes, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ['ssh', *args],
            input=stdin_bytes,
            capture_output=True,
            text=False,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise RuntimeError('ssh client not found on VPS') from e



def _decode_output(value: bytes | str | None) -> str:
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return value



def _project_server_sync(
    ctx: ToolContext,
    name: str,
    alias: str,
    timeout: int = _DEFAULT_SERVER_RUN_TIMEOUT,
    delete: bool = False,
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

    archive_bytes, synced_files, archive_size = _build_sync_archive(repo_dir)
    deploy_path = server['deploy_path']
    quoted_path = shlex.quote(deploy_path)
    remote_cmd_parts = [f"mkdir -p {quoted_path}"]
    if delete:
        remote_cmd_parts.append(
            f"find {quoted_path} -mindepth 1 -maxdepth 1 ! -name '.well-known' -exec rm -rf -- {{}} +"
        )
    remote_cmd_parts.append(f"tar -xmf - -C {quoted_path}")
    remote_command = ' && '.join(remote_cmd_parts)

    ssh_args = [
        '-i', server['ssh_key_path'],
        '-p', str(server['port']),
        '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', 'IdentitiesOnly=yes',
        f"{server['user']}@{server['host']}",
        '--',
        remote_command,
    ]
    res = _run_ssh_stream(ssh_args, stdin_bytes=archive_bytes, timeout=timeout_value)
    stdout = _decode_output(res.stdout)
    stderr = _decode_output(res.stderr)

    payload = {
        'status': 'ok' if res.returncode == 0 else 'error',
        'synced_at': _utc_now_iso(),
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'server': {
            'alias': server['alias'],
            'host': server['host'],
            'port': server['port'],
            'user': server['user'],
            'deploy_path': deploy_path,
            'registry_path': str(_project_server_registry_path(repo_dir)),
        },
        'sync': {
            'transport': 'ssh+tar',
            'timeout_seconds': timeout_value,
            'delete': bool(delete),
            'archive_bytes': archive_size,
            'file_count': len(synced_files),
            'files': synced_files,
            'excluded_roots': sorted(_EXCLUDED_ROOT_NAMES),
            'remote_command': remote_command,
        },
        'result': {
            'ok': res.returncode == 0,
            'exit_code': res.returncode,
            'stdout': stdout,
            'stderr': stderr,
        },
        'repo': _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)



def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            'project_server_sync',
            'Sync the current working tree of a bootstrapped local project repository to a registered remote server deploy path over SSH as a tar stream, excluding local-only metadata like .git and .veles.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'alias': {'type': 'string', 'description': 'Registered server alias from the project-local .veles server registry'},
                'timeout': {'type': 'integer', 'description': 'SSH sync timeout in seconds', 'default': _DEFAULT_SERVER_RUN_TIMEOUT},
                'delete': {'type': 'boolean', 'description': 'Whether to wipe the remote deploy directory contents before extracting the new archive', 'default': False},
            },
            ['name', 'alias'],
            _project_server_sync,
            is_code_tool=True,
        ),
        _tool_entry(
            'project_deploy_recipe',
            'Build a runtime-aware deploy recipe for a bootstrapped local project on a registered server alias, including sync preview, rendered systemd unit content, and recommended follow-up tool arguments for install/start.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'alias': {'type': 'string', 'description': 'Registered server alias from the project-local .veles server registry'},
                'service_name': {'type': 'string', 'description': 'Systemd service name to prepare for this deploy'},
                'runtime': {'type': 'string', 'description': 'Runtime to plan for: auto, python, node, or static', 'default': 'auto'},
                'description': {'type': 'string', 'description': 'Optional systemd unit Description override'},
                'working_directory': {'type': 'string', 'description': 'Optional absolute working directory override inside deploy_path'},
                'exec_start': {'type': 'string', 'description': 'Optional ExecStart override; default depends on runtime'},
                'environment_file': {'type': 'string', 'description': 'Optional absolute EnvironmentFile path'},
                'environment': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Optional KEY=VALUE entries for the systemd unit environment', 'default': []},
                'user': {'type': 'string', 'description': 'Optional system user for the service'},
                'restart': {'type': 'string', 'description': 'Systemd Restart policy', 'default': 'always'},
                'restart_sec': {'type': 'integer', 'description': 'Delay before restart in seconds', 'default': 3},
                'wanted_by': {'type': 'string', 'description': 'WantedBy target for the install section', 'default': 'multi-user.target'},
                'sync_timeout': {'type': 'integer', 'description': 'Recommended timeout for project_server_sync in seconds', 'default': _DEFAULT_SERVER_RUN_TIMEOUT},
                'service_timeout': {'type': 'integer', 'description': 'Recommended timeout for project_service_control install in seconds', 'default': _DEFAULT_SERVER_RUN_TIMEOUT},
                'delete': {'type': 'boolean', 'description': 'Whether the recommended sync step should wipe the remote deploy directory first', 'default': False},
            },
            ['name', 'alias', 'service_name'],
            _project_deploy_recipe,
            is_code_tool=True,
        ),
        _tool_entry(
            'project_deploy_apply',
            'Apply a transparent typed deploy flow for a bootstrapped project on a registered server alias by running sync plus the necessary service actions, and return the full per-step result trace.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'alias': {'type': 'string', 'description': 'Registered server alias from the project-local .veles server registry'},
                'service_name': {'type': 'string', 'description': 'Systemd service name to apply this deploy against'},
                'mode': {'type': 'string', 'description': 'Typed deploy flow to run', 'enum': sorted(_ALLOWED_DEPLOY_APPLY_MODES), 'default': 'install'},
                'runtime': {'type': 'string', 'description': 'Runtime to plan for: auto, python, node, or static', 'default': 'auto'},
                'description': {'type': 'string', 'description': 'Optional systemd unit Description override'},
                'working_directory': {'type': 'string', 'description': 'Optional absolute working directory override inside deploy_path'},
                'exec_start': {'type': 'string', 'description': 'Optional ExecStart override; default depends on runtime'},
                'environment_file': {'type': 'string', 'description': 'Optional absolute EnvironmentFile path'},
                'environment': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Optional KEY=VALUE entries for the systemd unit environment', 'default': []},
                'user': {'type': 'string', 'description': 'Optional system user for the service'},
                'restart': {'type': 'string', 'description': 'Systemd Restart policy', 'default': 'always'},
                'restart_sec': {'type': 'integer', 'description': 'Delay before restart in seconds', 'default': 3},
                'wanted_by': {'type': 'string', 'description': 'WantedBy target for the install section', 'default': 'multi-user.target'},
                'sync_timeout': {'type': 'integer', 'description': 'Timeout for project_server_sync in seconds', 'default': _DEFAULT_SERVER_RUN_TIMEOUT},
                'service_timeout': {'type': 'integer', 'description': 'Timeout for project_service_control lifecycle actions in seconds', 'default': _DEFAULT_SERVER_RUN_TIMEOUT},
                'status_timeout': {'type': 'integer', 'description': 'Timeout for the final project_service_control status check in seconds', 'default': _DEFAULT_SERVER_RUN_TIMEOUT},
                'delete': {'type': 'boolean', 'description': 'Whether to wipe the remote deploy directory contents before sync', 'default': False},
                'enable_on_install': {'type': 'boolean', 'description': 'Whether install mode should enable the unit during the install step', 'default': True},
                'start_on_install': {'type': 'boolean', 'description': 'Whether install mode should also start the unit inside the install step before the explicit lifecycle action', 'default': False},
                'sudo': {'type': 'boolean', 'description': 'Whether remote service actions should use sudo systemctl', 'default': True},
            },
            ['name', 'alias', 'service_name'],
            _project_deploy_apply,
            is_code_tool=True,
        ),
    ]
