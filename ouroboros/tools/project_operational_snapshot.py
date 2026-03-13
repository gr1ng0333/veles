from __future__ import annotations

import json
from typing import Any, Dict, List

from ouroboros.tools.external_repos import _tool_entry
from ouroboros.tools.project_bootstrap import _normalize_project_name, _project_status, _require_local_project
from ouroboros.tools.project_deploy_state import _read_project_deploy_state
from ouroboros.tools.project_github_dev import _project_github_slug, _run_project_gh_json
from ouroboros.tools.project_read_side import _decode_payload, _dedupe_items, _working_tree_signal


def _github_operational_summary(repo_dir, issue_limit: int, pr_limit: int) -> Dict[str, Any]:
    try:
        repo_slug = _project_github_slug(repo_dir)
    except Exception as e:
        return {
            'configured': False,
            'available': False,
            'repo': '',
            'reason': str(e),
            'open_issue_count': 0,
            'open_pull_request_count': 0,
        }

    summary: Dict[str, Any] = {
        'configured': True,
        'available': True,
        'repo': repo_slug,
        'open_issue_count': 0,
        'open_pull_request_count': 0,
    }
    try:
        issues = _run_project_gh_json(
            repo_dir,
            ['issue', 'list', '--state', 'open', '--limit', str(issue_limit), '--json', 'number'],
            timeout=60,
        ) or []
        prs = _run_project_gh_json(
            repo_dir,
            ['pr', 'list', '--state', 'open', '--limit', str(pr_limit), '--json', 'number'],
            timeout=60,
        ) or []
    except Exception as e:
        summary['available'] = False
        summary['reason'] = str(e)
        return summary

    summary['open_issue_count'] = len(issues)
    summary['open_pull_request_count'] = len(prs)
    return summary

from ouroboros.tools.project_server_observability import _project_deploy_status
from ouroboros.tools.registry import ToolContext, ToolEntry


def _readiness(status_payload: Dict[str, Any], github: Dict[str, Any], runtime: Dict[str, Any], last_outcome: Dict[str, Any] | None) -> Dict[str, Any]:
    local_clean = _working_tree_signal(status_payload).get('clean', True)
    github_ready = bool(github.get('configured')) and bool(github.get('available'))
    deploy_ready = bool(runtime.get('deploy', {}).get('exists')) and bool(runtime.get('deploy', {}).get('writable'))
    service_running = bool(runtime.get('service', {}).get('running'))
    diagnostic_severity = str((runtime.get('diagnostics') or {}).get('severity') or '')
    last_status = str((last_outcome or {}).get('status') or '')
    blocked_reasons: List[str] = []
    if not local_clean:
        blocked_reasons.append('working tree has meaningful local changes')
    if github.get('configured') and not github.get('available'):
        blocked_reasons.append('GitHub is configured but not reachable via gh')
    if runtime:
        if not deploy_ready:
            blocked_reasons.append('deploy target is not writable/ready')
        if diagnostic_severity in {'critical', 'warning'}:
            blocked_reasons.append(f'runtime diagnostics severity={diagnostic_severity}')
        if last_status and last_status != 'ok':
            blocked_reasons.append(f'last deploy status={last_status}')
    rollout_ready = local_clean and (not runtime or (deploy_ready and diagnostic_severity not in {'critical'} and last_status in {'', 'ok'}))
    return {
        'local_clean': bool(local_clean),
        'github_ready': bool(github_ready),
        'deploy_target_ready': bool(deploy_ready) if runtime else None,
        'service_running': bool(service_running) if runtime else None,
        'rollout_ready': bool(rollout_ready),
        'blocked_reasons': blocked_reasons,
    }


def _risk_flags(status_payload: Dict[str, Any], github: Dict[str, Any], runtime: Dict[str, Any], last_outcome: Dict[str, Any] | None) -> List[str]:
    flags: List[str] = []
    working_tree = _working_tree_signal(status_payload)
    if not working_tree['clean']:
        flags.append('local_changes_pending')
    if github.get('configured') and not github.get('available'):
        flags.append('github_unavailable')
    if runtime:
        deploy = runtime.get('deploy') or {}
        diagnostics = runtime.get('diagnostics') or {}
        if not bool(deploy.get('exists')):
            flags.append('deploy_path_missing')
        if bool(deploy.get('exists')) and not bool(deploy.get('writable')):
            flags.append('deploy_path_not_writable')
        severity = str(diagnostics.get('severity') or '')
        if severity in {'warning', 'critical'}:
            flags.append(f'runtime_{severity}')
    status = str((last_outcome or {}).get('status') or '')
    if status and status != 'ok':
        flags.append(f'last_deploy_{status}')
    return _dedupe_items(flags)


def _next_actions(status_payload: Dict[str, Any], github: Dict[str, Any], runtime: Dict[str, Any], last_outcome: Dict[str, Any] | None) -> List[str]:
    actions: List[str] = []
    working_tree = _working_tree_signal(status_payload)
    if not working_tree['clean']:
        actions.append('commit or discard local changes before the next rollout')
    if not github.get('configured'):
        actions.append('attach a GitHub origin with project_github_create')
    elif not github.get('available'):
        actions.append('restore gh access before relying on GitHub collaboration state')
    if runtime:
        deploy = runtime.get('deploy') or {}
        diagnostics = runtime.get('diagnostics') or {}
        if not bool(deploy.get('exists')) or not bool(deploy.get('writable')):
            actions.append('validate or fix the deploy target before the next apply')
        for item in diagnostics.get('recommended_checks') or []:
            hint = str(item or '').strip()
            if hint and hint not in actions:
                actions.append(hint)
    if last_outcome and str(last_outcome.get('status') or '') not in {'', 'ok'}:
        failed_step = str(last_outcome.get('failed_step') or '').strip()
        if failed_step:
            actions.append(f'resolve the last failed deploy step: {failed_step}')
        else:
            actions.append('resolve the last failed deploy outcome before the next rollout')
    return _dedupe_items(actions)


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
        'next_actions': _next_actions(status_payload, github, runtime, last_outcome),
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
