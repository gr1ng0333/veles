from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List

from ouroboros.tools.project_github_dev import _project_github_slug, _run_project_gh_json


def _decode_payload(raw: str) -> Dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError('tool returned invalid JSON payload') from e
    if not isinstance(payload, dict):
        raise RuntimeError('tool payload must be a JSON object')
    return payload


def _dedupe_items(items: List[str]) -> List[str]:
    deduped: List[str] = []
    for item in items:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _meaningful_working_tree_entries(status_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    working_tree = status_payload.get('working_tree') or {}
    entries = working_tree.get('entries') or []
    meaningful_entries: List[Dict[str, Any]] = []
    for item in entries:
        path_value = str((item or {}).get('path') or '').strip()
        if not path_value or path_value == '.veles' or path_value.startswith('.veles/'):
            continue
        meaningful_entries.append(item)
    return meaningful_entries


def _working_tree_signal(status_payload: Dict[str, Any]) -> Dict[str, Any]:
    meaningful_entries = _meaningful_working_tree_entries(status_payload)
    return {
        'clean': len(meaningful_entries) == 0,
        'changed_count': len(meaningful_entries),
        'entries': meaningful_entries,
    }


def _github_operational_summary(repo_dir: pathlib.Path, issue_limit: int, pr_limit: int) -> Dict[str, Any]:
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


def _github_summary(repo_dir: pathlib.Path, issue_limit: int, pr_limit: int) -> Dict[str, Any]:
    summary = _github_operational_summary(repo_dir, issue_limit, pr_limit)
    if not summary.get('configured'):
        return {
            'configured': False,
            'available': False,
            'repo': '',
            'reason': summary.get('reason') or '',
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

    payload: Dict[str, Any] = {
        'configured': True,
        'available': bool(summary.get('available')),
        'repo': summary.get('repo') or '',
    }

    if not summary.get('available'):
        payload.update({
            'reason': summary.get('reason') or '',
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
        return payload

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
        payload.update({
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
        return payload

    payload['issues'] = {
        'returned_count': len(issues),
        'limit': issue_limit,
        'items': issues,
    }
    payload['pull_requests'] = {
        'returned_count': len(prs),
        'limit': pr_limit,
        'items': prs,
    }
    return payload


def _build_operational_readiness(
    status_payload: Dict[str, Any],
    github: Dict[str, Any],
    runtime: Dict[str, Any],
    last_outcome: Dict[str, Any] | None,
) -> Dict[str, Any]:
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


def _build_operational_risk_flags(
    status_payload: Dict[str, Any],
    github: Dict[str, Any],
    runtime: Dict[str, Any],
    last_outcome: Dict[str, Any] | None,
) -> List[str]:
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


def _build_operational_next_actions(
    status_payload: Dict[str, Any],
    github: Dict[str, Any],
    runtime: Dict[str, Any],
    last_outcome: Dict[str, Any] | None,
) -> List[str]:
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


def _build_bootstrap_publish_verdict(
    init_payload: Dict[str, Any],
    github_payload: Dict[str, Any],
    overview_payload: Dict[str, Any],
) -> Dict[str, Any]:
    summary = overview_payload.get('summary') or {}
    github = overview_payload.get('github') or {}
    actions = overview_payload.get('next_actions') or []
    return {
        'ready': bool(
            init_payload.get('status') == 'ok'
            and github_payload.get('status') == 'ok'
            and bool(summary.get('github_configured'))
        ),
        'github_configured': bool(summary.get('github_configured')),
        'working_tree_clean': summary.get('working_tree_clean'),
        'registered_server_count': summary.get('registered_server_count'),
        'meaningful_working_tree_change_count': summary.get('meaningful_working_tree_change_count'),
        'github_repo': github.get('repo') or (github_payload.get('github') or {}).get('slug') or '',
        'next_actions': actions,
    }


def _build_deploy_verify_verdict(
    deploy_payload: Dict[str, Any],
    snapshot_payload: Dict[str, Any],
    dry_run: bool,
) -> Dict[str, Any]:
    deploy_status = str(deploy_payload.get('status') or '')
    execution = deploy_payload.get('execution') or {}
    readiness = snapshot_payload.get('readiness') or {}
    risk_flags = snapshot_payload.get('risk_flags') or []
    next_actions = snapshot_payload.get('next_actions') or []
    runtime = snapshot_payload.get('runtime') or {}
    diagnostics = runtime.get('diagnostics') or {}

    if dry_run:
        healthy = bool(readiness.get('rollout_ready'))
    else:
        healthy = (
            deploy_status == 'ok'
            and bool(readiness.get('rollout_ready'))
            and str(diagnostics.get('severity') or 'healthy') not in {'warning', 'critical'}
        )

    return {
        'healthy': bool(healthy),
        'deploy_status': deploy_status,
        'failed_step': str(deploy_payload.get('failed_step') or execution.get('failed_step') or ''),
        'rollout_ready': bool(readiness.get('rollout_ready')),
        'service_running': readiness.get('service_running'),
        'blocked_reasons': readiness.get('blocked_reasons') or [],
        'risk_flags': risk_flags,
        'next_actions': next_actions,
    }
