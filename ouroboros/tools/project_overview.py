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
from ouroboros.tools import project_read_side as _project_read_side
from ouroboros.tools.project_read_side import (
    _decode_payload,
    _dedupe_items,
    _meaningful_working_tree_entries,
    _run_project_gh_json,
)
from ouroboros.tools.project_server_observability import _project_deploy_status
from ouroboros.tools.registry import ToolContext, ToolEntry


# compatibility alias for tests that monkeypatch module-local gh entrypoints
_IMPORTED_RUN_PROJECT_GH_JSON = _run_project_gh_json
_run_project_gh_json = _run_project_gh_json


def _github_summary(repo_dir, issue_limit: int, pr_limit: int) -> Dict[str, Any]:
    original = _project_read_side._run_project_gh_json
    hook = _run_project_gh_json if _run_project_gh_json is not _IMPORTED_RUN_PROJECT_GH_JSON else original
    try:
        _project_read_side._run_project_gh_json = hook
        return _project_read_side._github_summary(repo_dir, issue_limit, pr_limit)
    finally:
        _project_read_side._run_project_gh_json = original


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


def _meaningful_working_tree_changes(status_payload: Dict[str, Any]) -> int:
    return len(_meaningful_working_tree_entries(status_payload))


def _next_actions(status_payload: Dict[str, Any], github: Dict[str, Any], servers: Dict[str, Any], deploy: Dict[str, Any]) -> List[str]:
    actions: List[str] = []

    working_tree = status_payload.get('working_tree') or {}
    meaningful_change_count = _meaningful_working_tree_changes(status_payload)
    if meaningful_change_count > 0:
        actions.append('commit or discard local changes before the next GitHub/deploy cycle')

    github_configured = bool(github.get('configured'))
    if not github_configured:
        actions.append('create and attach a GitHub origin with project_github_create')

    if int(servers.get('count') or 0) == 0:
        actions.append('register at least one deploy target with project_server_register')

    recipe_preview = deploy.get('recipe_preview') or {}
    runtime_snapshot = deploy.get('runtime_snapshot') or {}
    last_outcome = deploy.get('last_outcome') or {}

    if int(servers.get('count') or 0) > 0 and not bool(recipe_preview.get('available')):
        actions.append('generate a deploy plan by providing alias + service_name to project_overview or project_deploy_recipe')

    last_status = str(last_outcome.get('status') or '').strip()
    if last_status and last_status != 'ok':
        failed_step = str(last_outcome.get('failed_step') or '').strip()
        if failed_step:
            actions.append(f'resolve the last failed deploy step: {failed_step}')
        else:
            actions.append('resolve the last failed deploy outcome before the next rollout')
    elif not last_outcome and int(servers.get('count') or 0) > 0:
        runtime_service = runtime_snapshot.get('service') or {}
        diagnostics = runtime_snapshot.get('diagnostics') or {}
        if not (bool(runtime_snapshot.get('included')) and bool(runtime_service.get('running')) and str(diagnostics.get('severity') or '') == 'healthy'):
            actions.append('run project_deploy_apply once the deploy recipe looks correct')

    if bool(runtime_snapshot.get('included')):
        diagnostics = runtime_snapshot.get('diagnostics') or {}
        severity = str(diagnostics.get('severity') or '').strip()
        for item in diagnostics.get('recommended_checks') or []:
            hint = str(item or '').strip()
            if hint and hint not in actions:
                actions.append(hint)
        if severity in {'critical', 'warning'} and not diagnostics.get('recommended_checks'):
            actions.append('inspect project_deploy_status and project_service_logs for live diagnostics')

    return _dedupe_items(actions)


def _operational_summary(status_payload: Dict[str, Any], github: Dict[str, Any], servers: Dict[str, Any], deploy: Dict[str, Any]) -> Dict[str, Any]:
    repo_head = status_payload.get('repo') or {}
    working_tree = status_payload.get('working_tree') or {}
    runtime_snapshot = deploy.get('runtime_snapshot') or {}
    last_outcome = deploy.get('last_outcome') or {}
    diagnostics = runtime_snapshot.get('diagnostics') or {}
    service = runtime_snapshot.get('service') or {}

    return {
        'branch': repo_head.get('branch') or '',
        'head': repo_head.get('head') or '',
        'working_tree_clean': bool(working_tree.get('clean')),
        'working_tree_change_count': int(working_tree.get('changed_count') or 0),
        'meaningful_working_tree_change_count': _meaningful_working_tree_changes(status_payload),
        'github_configured': bool(github.get('configured')),
        'github_available': bool(github.get('available')),
        'open_issue_count': int((github.get('issues') or {}).get('returned_count') or 0),
        'open_pull_request_count': int((github.get('pull_requests') or {}).get('returned_count') or 0),
        'registered_server_count': int(servers.get('count') or 0),
        'selected_server_alias': ((servers.get('selected') or {}).get('alias') or ''),
        'has_deploy_state': bool(last_outcome),
        'last_deploy_status': str(last_outcome.get('status') or ''),
        'runtime_included': bool(runtime_snapshot.get('included')),
        'runtime_status': str(runtime_snapshot.get('status') or ''),
        'service_running': bool(service.get('running')),
        'diagnostic_severity': str(diagnostics.get('severity') or ''),
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

    github = _github_summary(repo_dir, issue_limit_value, pr_limit_value)
    servers_payload = {
        'count': len(servers),
        'aliases': [item.get('alias') for item in servers],
        'items': [_public_server_view(item) for item in servers],
        'selected': selected_server,
    }
    deploy_payload = {
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
    }

    payload = {
        'status': 'ok',
        'generated_at': status_payload.get('checked_at') or '',
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'summary': _operational_summary(status_payload, github, servers_payload, deploy_payload),
        'next_actions': _next_actions(status_payload, github, servers_payload, deploy_payload),
        'repo': {
            'snapshot': status_payload,
            'current': _repo_info(repo_dir),
        },
        'github': github,
        'servers': servers_payload,
        'deploy': deploy_payload,
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
