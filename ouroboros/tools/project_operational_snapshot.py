from __future__ import annotations

import json
from typing import Any, Dict, List

from ouroboros.tools.external_repos import _tool_entry
from ouroboros.tools.project_bootstrap import _normalize_project_name, _project_status, _require_local_project
from ouroboros.tools.project_deploy_state import _read_project_deploy_state
from ouroboros.tools import project_read_side as _project_read_side
from ouroboros.tools.project_read_side import (
    _build_operational_next_actions,
    _build_operational_readiness,
    _build_operational_risk_flags,
    _decode_payload,
    _project_github_slug,
    _run_project_gh_json,
    _working_tree_signal,
)
from ouroboros.tools.project_server_observability import _project_deploy_status
from ouroboros.tools.registry import ToolContext, ToolEntry


# compatibility aliases for tests that monkeypatch previous module-local GitHub hooks
_IMPORTED_PROJECT_GITHUB_SLUG = _project_github_slug
_IMPORTED_RUN_PROJECT_GH_JSON = _run_project_gh_json
_project_github_slug = _project_github_slug
_run_project_gh_json = _run_project_gh_json


def _github_operational_summary(repo_dir, issue_limit: int, pr_limit: int) -> Dict[str, Any]:
    original_slug = _project_read_side._project_github_slug
    original_run = _project_read_side._run_project_gh_json
    slug_hook = _project_github_slug if _project_github_slug is not _IMPORTED_PROJECT_GITHUB_SLUG else original_slug
    run_hook = _run_project_gh_json if _run_project_gh_json is not _IMPORTED_RUN_PROJECT_GH_JSON else original_run
    try:
        _project_read_side._project_github_slug = slug_hook
        _project_read_side._run_project_gh_json = run_hook
        return _project_read_side._github_operational_summary(repo_dir, issue_limit, pr_limit)
    finally:
        _project_read_side._project_github_slug = original_slug
        _project_read_side._run_project_gh_json = original_run


def _readiness(status_payload: Dict[str, Any], github: Dict[str, Any], runtime: Dict[str, Any], last_outcome: Dict[str, Any] | None) -> Dict[str, Any]:
    return _build_operational_readiness(status_payload, github, runtime, last_outcome)


def _risk_flags(status_payload: Dict[str, Any], github: Dict[str, Any], runtime: Dict[str, Any], last_outcome: Dict[str, Any] | None) -> List[str]:
    return _build_operational_risk_flags(status_payload, github, runtime, last_outcome)


def _project_operational_snapshot(
    ctx: ToolContext,
    name: str,
    alias: str = '',
    service_name: str = '',
    issue_limit: int = 20,
    pr_limit: int = 20,
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
    github = _github_operational_summary(repo_dir, issue_limit_value, pr_limit_value)
    last_outcome = _read_project_deploy_state(repo_dir)

    runtime: Dict[str, Any] = {}
    if str(alias or '').strip() and str(service_name or '').strip():
        runtime = _decode_payload(
            _project_deploy_status(
                ctx,
                name=project_name,
                alias=str(alias).strip(),
                service_name=str(service_name).strip(),
            )
        )

    payload = {
        'status': 'ok',
        'generated_at': status_payload.get('checked_at') or '',
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'selection': {
            'alias': str(alias or '').strip(),
            'service_name': str(service_name or '').strip(),
            'runtime_included': bool(runtime),
        },
        'readiness': _readiness(status_payload, github, runtime, last_outcome),
        'risk_flags': _risk_flags(status_payload, github, runtime, last_outcome),
        'next_actions': _build_operational_next_actions(status_payload, github, runtime, last_outcome),
        'repo': {
            'branch': (status_payload.get('repo') or {}).get('branch') or '',
            'head': (status_payload.get('repo') or {}).get('head') or '',
            'working_tree': _working_tree_signal(status_payload),
        },
        'github': github,
        'runtime': {
            'deploy': runtime.get('deploy') or {},
            'service': runtime.get('service') or {},
            'diagnostics': runtime.get('diagnostics') or {},
        } if runtime else {},
        'last_deploy': last_outcome or {},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            'project_operational_snapshot',
            'Build a compact operator-facing snapshot for a bootstrapped project: rollout readiness, risk flags, focused repo/GitHub signal, and optional deploy/runtime state for a selected server/service.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'alias': {'type': 'string', 'description': 'Optional registered server alias for runtime/deploy checks'},
                'service_name': {'type': 'string', 'description': 'Optional systemd service name for runtime/deploy checks'},
                'issue_limit': {'type': 'integer', 'description': 'Maximum number of open GitHub issues to count', 'default': 20},
                'pr_limit': {'type': 'integer', 'description': 'Maximum number of open GitHub pull requests to count', 'default': 20},
            },
            ['name'],
            _project_operational_snapshot,
            is_code_tool=True,
        ),
    ]
