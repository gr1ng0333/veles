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


def _normalize_labels(labels: List[str] | str | None, *, field_name: str = 'labels') -> List[str]:
    if labels is None:
        return []
    raw_items = labels if isinstance(labels, list) else [labels]
    result: List[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        value = str(raw or '').strip()
        if not value:
            continue
        if value not in seen:
            result.append(value)
            seen.add(value)
    if not result:
        raise ValueError(f'{field_name} must contain at least one non-empty value')
    return result


def _normalize_assignees(assignees: List[str] | str | None, *, field_name: str = 'assignees') -> List[str]:
    if assignees is None:
        return []
    raw_items = assignees if isinstance(assignees, list) else [assignees]
    result: List[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        value = str(raw or '').strip()
        if not value:
            continue
        if any(ch.isspace() for ch in value):
            raise ValueError(f'{field_name} entries must not contain whitespace')
        if value not in seen:
            result.append(value)
            seen.add(value)
    if not result:
        raise ValueError(f'{field_name} must contain at least one non-empty value')
    return result


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


def _project_issue_label_add(ctx: ToolContext, name: str, number: int, labels: List[str] | str) -> str:
    del ctx
    repo_dir = _require_local_project(name)
    project_name = str(name or '').strip()
    issue_number = _issue_number(number)
    label_values = _normalize_labels(labels, field_name='labels')
    repo_slug = _project_github_slug(repo_dir)

    args = ['issue', 'edit', str(issue_number)]
    for label in label_values:
        args.extend(['--add-label', label])
    res = _run_gh(args, cwd=repo_dir, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or 'gh issue edit --add-label failed')

    result_text = (res.stdout or '').strip() or 'labels added'
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
                'issue_label_add': {
                    'number': issue_number,
                    'labels': label_values,
                    'result': result_text,
                },
            },
            'repo': _repo_info(repo_dir),
        },
        ensure_ascii=False,
        indent=2,
    )


def _project_issue_label_remove(ctx: ToolContext, name: str, number: int, labels: List[str] | str) -> str:
    del ctx
    repo_dir = _require_local_project(name)
    project_name = str(name or '').strip()
    issue_number = _issue_number(number)
    label_values = _normalize_labels(labels, field_name='labels')
    repo_slug = _project_github_slug(repo_dir)

    args = ['issue', 'edit', str(issue_number)]
    for label in label_values:
        args.extend(['--remove-label', label])
    res = _run_gh(args, cwd=repo_dir, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or 'gh issue edit --remove-label failed')

    result_text = (res.stdout or '').strip() or 'labels removed'
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
                'issue_label_remove': {
                    'number': issue_number,
                    'labels': label_values,
                    'result': result_text,
                },
            },
            'repo': _repo_info(repo_dir),
        },
        ensure_ascii=False,
        indent=2,
    )


def _project_issue_assign(ctx: ToolContext, name: str, number: int, assignees: List[str] | str) -> str:
    del ctx
    repo_dir = _require_local_project(name)
    project_name = str(name or '').strip()
    issue_number = _issue_number(number)
    assignee_values = _normalize_assignees(assignees, field_name='assignees')
    repo_slug = _project_github_slug(repo_dir)

    args = ['issue', 'edit', str(issue_number)]
    for assignee in assignee_values:
        args.extend(['--add-assignee', assignee])
    res = _run_gh(args, cwd=repo_dir, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or 'gh issue edit --add-assignee failed')

    result_text = (res.stdout or '').strip() or 'assignees added'
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
                'issue_assign': {
                    'number': issue_number,
                    'assignees': assignee_values,
                    'result': result_text,
                },
            },
            'repo': _repo_info(repo_dir),
        },
        ensure_ascii=False,
        indent=2,
    )


def _project_issue_unassign(ctx: ToolContext, name: str, number: int, assignees: List[str] | str) -> str:
    del ctx
    repo_dir = _require_local_project(name)
    project_name = str(name or '').strip()
    issue_number = _issue_number(number)
    assignee_values = _normalize_assignees(assignees, field_name='assignees')
    repo_slug = _project_github_slug(repo_dir)

    args = ['issue', 'edit', str(issue_number)]
    for assignee in assignee_values:
        args.extend(['--remove-assignee', assignee])
    res = _run_gh(args, cwd=repo_dir, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or 'gh issue edit --remove-assignee failed')

    result_text = (res.stdout or '').strip() or 'assignees removed'
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
                'issue_unassign': {
                    'number': issue_number,
                    'assignees': assignee_values,
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
        _tool_entry(
            'project_issue_label_add',
            'Add one or more labels to a GitHub issue in an existing bootstrapped local project repository, using its configured origin remote.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'number': {'type': 'integer', 'description': 'Issue number to update'},
                'labels': {
                    'oneOf': [
                        {'type': 'string', 'description': 'Single label to add'},
                        {'type': 'array', 'items': {'type': 'string'}, 'description': 'Labels to add'},
                    ],
                    'description': 'One or more label names to add to the issue',
                },
            },
            ['name', 'number', 'labels'],
            _project_issue_label_add,
            is_code_tool=True,
        ),
        _tool_entry(
            'project_issue_label_remove',
            'Remove one or more labels from a GitHub issue in an existing bootstrapped local project repository, using its configured origin remote.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'number': {'type': 'integer', 'description': 'Issue number to update'},
                'labels': {
                    'oneOf': [
                        {'type': 'string', 'description': 'Single label to remove'},
                        {'type': 'array', 'items': {'type': 'string'}, 'description': 'Labels to remove'},
                    ],
                    'description': 'One or more label names to remove from the issue',
                },
            },
            ['name', 'number', 'labels'],
            _project_issue_label_remove,
            is_code_tool=True,
        ),
        _tool_entry(
            'project_issue_assign',
            'Assign one or more GitHub users to a GitHub issue in an existing bootstrapped local project repository, using its configured origin remote.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'number': {'type': 'integer', 'description': 'Issue number to update'},
                'assignees': {
                    'oneOf': [
                        {'type': 'string', 'description': 'Single GitHub username to assign'},
                        {'type': 'array', 'items': {'type': 'string'}, 'description': 'GitHub usernames to assign'},
                    ],
                    'description': 'One or more GitHub usernames to assign to the issue',
                },
            },
            ['name', 'number', 'assignees'],
            _project_issue_assign,
            is_code_tool=True,
        ),
        _tool_entry(
            'project_issue_unassign',
            'Unassign one or more GitHub users from a GitHub issue in an existing bootstrapped local project repository, using its configured origin remote.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'number': {'type': 'integer', 'description': 'Issue number to update'},
                'assignees': {
                    'oneOf': [
                        {'type': 'string', 'description': 'Single GitHub username to unassign'},
                        {'type': 'array', 'items': {'type': 'string'}, 'description': 'GitHub usernames to unassign'},
                    ],
                    'description': 'One or more GitHub usernames to remove from the issue assignees',
                },
            },
            ['name', 'number', 'assignees'],
            _project_issue_unassign,
            is_code_tool=True,
        ),
    ]
