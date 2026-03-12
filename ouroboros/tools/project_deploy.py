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
    ]
