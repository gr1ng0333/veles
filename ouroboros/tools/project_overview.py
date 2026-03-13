from __future__ import annotations

import json
from typing import Any, Dict, List

from ouroboros.tools.external_repos import _tool_entry
from ouroboros.tools.project_bootstrap import (
    _find_project_server,
    _load_project_server_registry,
    _normalize_project_name,
    _project_status,
    _public_server_view,
    _repo_info,
    _require_local_project,
)
from ouroboros.tools.project_deploy import _project_deploy_recipe
from ouroboros.tools.project_deploy_state import _project_deploy_state_path, _read_project_deploy_state
from ouroboros.tools.project_github_dev import _project_github_slug, _run_project_gh_json
from ouroboros.tools.project_server_observability import _project_deploy_status
from ouroboros.tools.registry import ToolContext, ToolEntry


def _decode_payload(raw: str) -> Dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError('tool returned invalid JSON payload') from e
    if not isinstance(payload, dict):
        raise RuntimeError('tool payload must be a JSON object')
    return payload


def _github_summary(repo_dir, issue_limit: int, pr_limit: int) -> Dict[str, Any]:
    try:
        repo_slug = _project_github_slug(repo_dir)
    except Exception as e:
        return {
            'configured': False,
            'available': False,
            'repo': '',
            'reason': str(e),
            'issues': {
                'returned_count': 0,
                'limit': issue_limit,
                'items': [],
            },
            'pull_requests': {
                'returned_count': 0,
                'limit': pr_limit,
                'items': [],
            },
        }

    summary: Dict[str, Any] = {
        'configured': True,
        'available': True,
        'repo': repo_slug,
    }
    try:
        issues = _run_project_gh_json(
            repo_dir,
            [
                'issue', 'list',
                '--state', 'open',
                '--limit', str(issue_limit),
                '--json', 'number,title,state,url,author,labels',
            ],
            timeout=60,
        ) or []
        prs = _run_project_gh_json(
            repo_dir,
            [
                'pr', 'list',
                '--state', 'open',
                '--limit', str(pr_limit),
                '--json', 'number,title,state,headRefName,baseRefName,url,isDraft,author',
            ],
            timeout=60,
        ) or []
    except Exception as e:
        summary.update({
            'available': False,
            'reason': str(e),
            'issues': {
                'returned_count': 0,
                'limit': issue_limit,
                'items': [],
            },
            'pull_requests': {
                'returned_count': 0,
                'limit': pr_limit,
                'items': [],
            },
        })
        return summary

    summary['issues'] = {
        'returned_count': len(issues),
        'limit': issue_limit,
        'items': issues,
    }
    summary['pull_requests'] = {
        'returned_count': len(prs),
        'limit': pr_limit,
        'items': prs,
    }
    return summary


def _recipe_preview(ctx: ToolContext, project_name: str, alias: str, service_name: str) -> Dict[str, Any]:
    if not alias or not service_name:
        return {
            'available': False,
            'reason': 'recipe preview requires both alias and service_name',
        }
    payload = _decode_payload(
        _project_deploy_recipe(
            ctx,
            name=project_name,
            alias=alias,
            service_name=service_name,
        )
    )
    return {
        'available': True,
        'generated_at': payload.get('generated_at') or '',
        'runtime': payload.get('runtime') or {},
        'server': payload.get('server') or {},
        'recipe': payload.get('recipe') or {},
    }


def _runtime_snapshot(ctx: ToolContext, project_name: str, alias: str, service_name: str, include_runtime: bool) -> Dict[str, Any]:
    if not include_runtime:
        return {
            'included': False,
            'reason': 'set include_runtime=true to collect remote deploy/service snapshot',
        }
    if not alias or not service_name:
        return {
            'included': False,
            'reason': 'runtime snapshot requires both alias and service_name',
        }
    payload = _decode_payload(
        _project_deploy_status(
            ctx,
            name=project_name,
            alias=alias,
            service_name=service_name,
        )
    )
    return {
        'included': True,
        'status': payload.get('status') or 'unknown',
        'deploy': payload.get('deploy') or {},
        'service': payload.get('service') or {},
        'diagnostics': payload.get('diagnostics') or {},
        'last_deploy': payload.get('last_deploy') or {},
    }


def _project_overview(
    ctx: ToolContext,
    name: str,
    issue_limit: int = 10,
    pr_limit: int = 10,
    alias: str = '',
    service_name: str = '',
    include_runtime: bool = False,
) -> str:
    repo_dir = _require_local_project(name)
    project_name = _normalize_project_name(name)

    try:
        issue_limit_value = max(1, min(int(issue_limit), 50))
    except (TypeError, ValueError) as e:
        raise ValueError('issue_limit must be an integer') from e
    try:
        pr_limit_value = max(1, min(int(pr_limit), 50))
    except (TypeError, ValueError) as e:
        raise ValueError('pr_limit must be an integer') from e

    status_payload = _decode_payload(_project_status(ctx, project_name))
    servers = _load_project_server_registry(repo_dir)
    selected_server = None
    if str(alias or '').strip():
        selected_server = _public_server_view(_find_project_server(repo_dir, alias))

    deploy_state_path = _project_deploy_state_path(repo_dir)
    last_deploy = _read_project_deploy_state(repo_dir)

    payload = {
        'status': 'ok',
        'generated_at': status_payload.get('checked_at') or '',
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'repo': {
            'snapshot': status_payload,
            'current': _repo_info(repo_dir),
        },
        'github': _github_summary(repo_dir, issue_limit_value, pr_limit_value),
        'servers': {
            'count': len(servers),
            'aliases': [item.get('alias') for item in servers],
            'items': [_public_server_view(item) for item in servers],
            'selected': selected_server,
        },
        'deploy': {
            'state_file': {
                'path': str(deploy_state_path),
                'exists': deploy_state_path.exists(),
            },
            'last_outcome': last_deploy,
            'recipe_preview': _recipe_preview(ctx, project_name, str(alias or '').strip(), str(service_name or '').strip()),
            'runtime_snapshot': _runtime_snapshot(
                ctx,
                project_name,
                str(alias or '').strip(),
                str(service_name or '').strip(),
                bool(include_runtime),
            ),
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            'project_overview',
            'Build a unified read-side overview for a bootstrapped local project: local repo snapshot, GitHub open issue/PR summary, registered servers, last deploy outcome, optional deploy recipe preview, and optional live remote deploy/service snapshot.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'issue_limit': {'type': 'integer', 'description': 'Maximum number of open GitHub issues to include in the summary', 'default': 10},
                'pr_limit': {'type': 'integer', 'description': 'Maximum number of open GitHub pull requests to include in the summary', 'default': 10},
                'alias': {'type': 'string', 'description': 'Optional registered server alias for recipe preview and runtime snapshot'},
                'service_name': {'type': 'string', 'description': 'Optional systemd service name for recipe preview and runtime snapshot'},
                'include_runtime': {'type': 'boolean', 'description': 'Whether to probe live remote deploy/service status over SSH; requires alias and service_name', 'default': False},
            },
            ['name'],
            _project_overview,
            is_code_tool=True,
        ),
    ]
