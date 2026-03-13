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
