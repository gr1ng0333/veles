from __future__ import annotations

import json
from typing import List

from ouroboros.tools.project_bootstrap import _repo_info, _require_local_project, _run_gh, _tool_entry, _utc_now_iso
from ouroboros.tools.project_github_dev import _project_github_slug
from ouroboros.tools.registry import ToolContext, ToolEntry


def _issue_number(number: int) -> int:
    try:
        issue_number = int(number)
    except (TypeError, ValueError) as e:
        raise ValueError('number must be an integer') from e
    if issue_number <= 0:
        raise ValueError('number must be positive')
    return issue_number


def _project_issue_update(
    ctx: ToolContext,
    name: str,
    number: int,
    title: str = '',
    body: str = '',
) -> str:
    del ctx
    repo_dir = _require_local_project(name)
    project_name = str(name or '').strip()
    issue_number = _issue_number(number)
    title_value = str(title or '').strip()
    body_value = str(body or '')

    if not title_value and body_value == '':
        raise ValueError('at least one of title or body must be provided')

    repo_slug = _project_github_slug(repo_dir)
    args = ['issue', 'edit', str(issue_number)]
    if title_value:
        args.append(f'--title={title_value}')

    if body_value != '':
        args.extend(['--body-file', '-'])
        res = _run_gh(args, cwd=repo_dir, timeout=180, input_data=body_value)
    else:
        res = _run_gh(args, cwd=repo_dir, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or 'gh issue edit failed')

    result_text = (res.stdout or '').strip() or 'issue updated'
    return json.dumps(
        {
            'status': 'ok',
            'updated_at': _utc_now_iso(),
            'project': {
                'name': project_name,
                'path': str(repo_dir),
            },
            'github': {
                'repo': repo_slug,
                'issue_update': {
                    'number': issue_number,
                    'title': title_value or None,
                    'body_provided': body_value != '',
                    'result': result_text,
                },
            },
            'repo': _repo_info(repo_dir),
        },
        ensure_ascii=False,
        indent=2,
    )


def _project_issue_close(ctx: ToolContext, name: str, number: int) -> str:
    del ctx
    repo_dir = _require_local_project(name)
    project_name = str(name or '').strip()
    issue_number = _issue_number(number)
    repo_slug = _project_github_slug(repo_dir)

    res = _run_gh(['issue', 'close', str(issue_number)], cwd=repo_dir, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or 'gh issue close failed')

    result_text = (res.stdout or '').strip() or 'issue closed'
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
                'issue_close': {
                    'number': issue_number,
                    'result': result_text,
                },
            },
            'repo': _repo_info(repo_dir),
        },
        ensure_ascii=False,
        indent=2,
    )


def _project_issue_reopen(ctx: ToolContext, name: str, number: int) -> str:
    del ctx
    repo_dir = _require_local_project(name)
    project_name = str(name or '').strip()
    issue_number = _issue_number(number)
    repo_slug = _project_github_slug(repo_dir)

    res = _run_gh(['issue', 'reopen', str(issue_number)], cwd=repo_dir, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or 'gh issue reopen failed')

    result_text = (res.stdout or '').strip() or 'issue reopened'
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
                'issue_reopen': {
                    'number': issue_number,
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
            'project_issue_update',
            'Update a GitHub issue in an existing bootstrapped local project repository, changing title and/or body via the configured origin remote.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'number': {'type': 'integer', 'description': 'Issue number to update'},
                'title': {'type': 'string', 'description': 'Optional new issue title'},
                'body': {'type': 'string', 'description': 'Optional new issue body'},
            },
            ['name', 'number'],
            _project_issue_update,
            is_code_tool=True,
        ),
        _tool_entry(
            'project_issue_close',
            'Close a GitHub issue in an existing bootstrapped local project repository, using its configured origin remote.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'number': {'type': 'integer', 'description': 'Issue number to close'},
            },
            ['name', 'number'],
            _project_issue_close,
            is_code_tool=True,
        ),
        _tool_entry(
            'project_issue_reopen',
            'Reopen a GitHub issue in an existing bootstrapped local project repository, using its configured origin remote.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'number': {'type': 'integer', 'description': 'Issue number to reopen'},
            },
            ['name', 'number'],
            _project_issue_reopen,
            is_code_tool=True,
        ),
    ]
