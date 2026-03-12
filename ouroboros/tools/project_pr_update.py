from __future__ import annotations

import json
from typing import List

from ouroboros.tools.project_bootstrap import _repo_info, _require_local_project, _run_gh, _tool_entry, _utc_now_iso
from ouroboros.tools.project_github_dev import _project_github_slug
from ouroboros.tools.registry import ToolContext, ToolEntry


def _pr_number(number: int) -> int:
    try:
        pr_number = int(number)
    except (TypeError, ValueError) as e:
        raise ValueError('number must be an integer') from e
    if pr_number <= 0:
        raise ValueError('number must be positive')
    return pr_number


def _review_event(event: str) -> str:
    value = str(event or '').strip().lower()
    allowed = {
        'comment': 'COMMENT',
        'approve': 'APPROVE',
        'request_changes': 'REQUEST_CHANGES',
    }
    if value not in allowed:
        raise ValueError('event must be one of: comment, approve, request_changes')
    return allowed[value]


def _project_pr_close(ctx: ToolContext, name: str, number: int) -> str:
    del ctx
    repo_dir = _require_local_project(name)
    project_name = str(name or '').strip()
    pr_number = _pr_number(number)
    repo_slug = _project_github_slug(repo_dir)

    res = _run_gh(['pr', 'close', str(pr_number)], cwd=repo_dir, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or 'gh pr close failed')

    result_text = (res.stdout or '').strip() or 'pull request closed'
    return json.dumps(
        {
            'status': 'ok',
            'closed_at': _utc_now_iso(),
            'project': {
                'name': project_name,
                'path': str(repo_dir),
            },
            'github': {
                'repo': repo_slug,
                'pull_request_close': {
                    'number': pr_number,
                    'result': result_text,
                },
            },
            'repo': _repo_info(repo_dir),
        },
        ensure_ascii=False,
        indent=2,
    )


def _project_pr_reopen(ctx: ToolContext, name: str, number: int) -> str:
    del ctx
    repo_dir = _require_local_project(name)
    project_name = str(name or '').strip()
    pr_number = _pr_number(number)
    repo_slug = _project_github_slug(repo_dir)

    res = _run_gh(['pr', 'reopen', str(pr_number)], cwd=repo_dir, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or 'gh pr reopen failed')

    result_text = (res.stdout or '').strip() or 'pull request reopened'
    return json.dumps(
        {
            'status': 'ok',
            'reopened_at': _utc_now_iso(),
            'project': {
                'name': project_name,
                'path': str(repo_dir),
            },
            'github': {
                'repo': repo_slug,
                'pull_request_reopen': {
                    'number': pr_number,
                    'result': result_text,
                },
            },
            'repo': _repo_info(repo_dir),
        },
        ensure_ascii=False,
        indent=2,
    )


def _project_pr_review_list(ctx: ToolContext, name: str, number: int) -> str:
    del ctx
    repo_dir = _require_local_project(name)
    project_name = str(name or '').strip()
    pr_number = _pr_number(number)
    repo_slug = _project_github_slug(repo_dir)

    res = _run_gh(
        ['pr', 'view', str(pr_number), '--json', 'reviews'],
        cwd=repo_dir,
        timeout=60,
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or 'gh pr view --json reviews failed')

    out = (res.stdout or '').strip()
    try:
        payload = json.loads(out) if out else {}
    except json.JSONDecodeError as e:
        raise RuntimeError(f'failed to parse gh JSON output: {out[:300]}') from e
    reviews = payload.get('reviews') or []

    return json.dumps(
        {
            'status': 'ok',
            'read_at': _utc_now_iso(),
            'project': {
                'name': project_name,
                'path': str(repo_dir),
            },
            'github': {
                'repo': repo_slug,
                'pull_request_reviews': {
                    'number': pr_number,
                    'count': len(reviews),
                    'items': reviews,
                },
            },
            'repo': _repo_info(repo_dir),
        },
        ensure_ascii=False,
        indent=2,
    )


def _project_pr_review_submit(ctx: ToolContext, name: str, number: int, event: str, body: str = '') -> str:
    del ctx
    repo_dir = _require_local_project(name)
    project_name = str(name or '').strip()
    pr_number = _pr_number(number)
    event_value = _review_event(event)
    body_value = str(body or '').strip()
    repo_slug = _project_github_slug(repo_dir)

    args = ['pr', 'review', str(pr_number), f'--{event_value.lower()}']
    if body_value:
        args.extend(['--body-file', '-'])
        res = _run_gh(args, cwd=repo_dir, timeout=180, input_data=body_value)
    else:
        res = _run_gh(args, cwd=repo_dir, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or 'gh pr review failed')

    result_text = (res.stdout or '').strip() or 'pull request review submitted'
    return json.dumps(
        {
            'status': 'ok',
            'reviewed_at': _utc_now_iso(),
            'project': {
                'name': project_name,
                'path': str(repo_dir),
            },
            'github': {
                'repo': repo_slug,
                'pull_request_review_submit': {
                    'number': pr_number,
                    'event': event_value.lower(),
                    'body': body_value or None,
                    'result': result_text,
                },
            },
            'repo': _repo_info(repo_dir),
        },
        ensure_ascii=False,
        indent=2,
    )


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            'project_pr_close',
            'Close a GitHub pull request in an existing bootstrapped local project repository, using its configured origin remote.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'number': {'type': 'integer', 'description': 'Pull request number to close'},
            },
            ['name', 'number'],
            _project_pr_close,
            is_code_tool=True,
        ),
        _tool_entry(
            'project_pr_reopen',
            'Reopen a GitHub pull request in an existing bootstrapped local project repository, using its configured origin remote.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'number': {'type': 'integer', 'description': 'Pull request number to reopen'},
            },
            ['name', 'number'],
            _project_pr_reopen,
            is_code_tool=True,
        ),
        _tool_entry(
            'project_pr_review_list',
            'List submitted reviews for a GitHub pull request in an existing bootstrapped local project repository via its configured origin remote.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'number': {'type': 'integer', 'description': 'Pull request number to inspect'},
            },
            ['name', 'number'],
            _project_pr_review_list,
        ),
        _tool_entry(
            'project_pr_review_submit',
            'Submit a GitHub pull request review in an existing bootstrapped local project repository, supporting comment, approve, or request_changes review events.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'number': {'type': 'integer', 'description': 'Pull request number to review'},
                'event': {'type': 'string', 'description': 'Review event: comment, approve, or request_changes'},
                'body': {'type': 'string', 'description': 'Optional review body/comment'},
            },
            ['name', 'number', 'event'],
            _project_pr_review_submit,
            is_code_tool=True,
        ),
    ]
