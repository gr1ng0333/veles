from __future__ import annotations

import json
from typing import List

from ouroboros.tools.project_bootstrap import (
    _find_project_server,
    _load_project_server_registry,
    _normalize_project_name,
    _project_server_registry_path,
    _public_server_view,
    _repo_info,
    _require_local_project,
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
    ]
