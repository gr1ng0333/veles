from __future__ import annotations

import json
from typing import List

from ouroboros.tools.project_bootstrap import (
    _find_project_server,
    _load_project_server_registry,
    _normalize_project_name,
    _normalize_server_alias,
    _project_server_registry_path,
    _public_server_view,
    _repo_info,
    _require_local_project,
    _save_project_server_registry,
    _tool_entry,
    _utc_now_iso,
)
from ouroboros.tools.registry import ToolContext, ToolEntry


def _project_server_get(ctx: ToolContext, name: str, alias: str) -> str:
    repo_dir = _require_local_project(name)
    project_name = _normalize_project_name(name)
    server = _find_project_server(repo_dir, alias)
    servers = _load_project_server_registry(repo_dir)

    payload = {
        'status': 'ok',
        'read_at': _utc_now_iso(),
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


def _project_server_remove(ctx: ToolContext, name: str, alias: str) -> str:
    repo_dir = _require_local_project(name)
    project_name = _normalize_project_name(name)
    alias_name = _normalize_server_alias(alias)
    server = _find_project_server(repo_dir, alias_name)
    servers = _load_project_server_registry(repo_dir)
    remaining = [item for item in servers if item.get('alias') != alias_name]
    if len(remaining) == len(servers):
        raise ValueError(f"project server alias not found: {alias_name}")
    _save_project_server_registry(repo_dir, remaining)

    payload = {
        'status': 'ok',
        'removed_at': _utc_now_iso(),
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'removed_server': _public_server_view(server),
        'registry': {
            'path': str(_project_server_registry_path(repo_dir)),
            'count': len(remaining),
            'aliases': [item.get('alias') for item in remaining],
            'exists': _project_server_registry_path(repo_dir).exists(),
        },
        'repo': _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            'project_server_get',
            'Read one registered deploy server target by alias for an existing bootstrapped local project repository from the project-local .veles server registry.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'alias': {'type': 'string', 'description': 'Registered server alias to read'},
            },
            ['name', 'alias'],
            _project_server_get,
            is_code_tool=True,
        ),
        _tool_entry(
            'project_server_remove',
            'Remove one registered deploy server target by alias from the project-local .veles server registry of an existing bootstrapped local project repository.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'alias': {'type': 'string', 'description': 'Registered server alias to remove'},
            },
            ['name', 'alias'],
            _project_server_remove,
            is_code_tool=True,
        ),
    ]
