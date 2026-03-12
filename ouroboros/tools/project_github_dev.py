from __future__ import annotations

import json
import pathlib
import re
from typing import List

from ouroboros.tools.external_repos import _tool_entry
from ouroboros.tools.project_bootstrap import _git, _git_remote_url, _repo_info, _require_local_project, _run_gh, _utc_now_iso
from ouroboros.tools.registry import ToolContext, ToolEntry


_BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]+")


def _project_github_slug(repo_dir: pathlib.Path) -> str:
    remote_url = str(_git_remote_url(repo_dir) or '').strip()
    if not remote_url:
        raise ValueError('project has no origin remote configured')
    patterns = [
        r"git@github\.com:(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$",
        r"https://github\.com/(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$",
        r"ssh://git@github\.com/(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$",
    ]
    for pattern in patterns:
        m = re.match(pattern, remote_url)
        if m:
            return m.group('slug')
    raise ValueError(f'project origin is not a supported GitHub remote: {remote_url}')


def _remote_branch_exists(repo_dir: pathlib.Path, branch: str) -> bool:
    res = _git(['ls-remote', '--heads', 'origin', branch], repo_dir, timeout=60)
    return res.returncode == 0 and bool((res.stdout or '').strip())


def _normalize_branch_name(branch: str) -> str:
    raw = str(branch or '').strip()
    if not raw:
        raise ValueError('branch must be non-empty')
    if any(ch.isspace() for ch in raw):
        raise ValueError('branch must not contain whitespace')
    if raw in {'HEAD', '.', '..', '/', '-'}:
        raise ValueError('branch name is invalid')
    if raw.startswith('-') or raw.endswith('/') or raw.endswith('.') or raw.endswith('.lock'):
        raise ValueError('branch name is invalid')
    if raw.startswith('/') or raw.startswith('.'):
        raise ValueError('branch name is invalid')
    if '..' in raw or '@{' in raw or '\\' in raw or '//' in raw or raw.endswith('.lock'):
        raise ValueError('branch name is invalid')
    if not _BRANCH_RE.fullmatch(raw):
        raise ValueError('branch contains unsupported characters')
    return raw


def _current_branch(repo_dir: pathlib.Path) -> str:
    branch_res = _git(['rev-parse', '--abbrev-ref', 'HEAD'], repo_dir, timeout=30)
    if branch_res.returncode != 0:
        raise RuntimeError(branch_res.stderr.strip() or branch_res.stdout.strip() or 'git rev-parse failed')
    branch = (branch_res.stdout or '').strip()
    if not branch:
        raise RuntimeError('could not determine current branch')
    return branch


def _local_branch_exists(repo_dir: pathlib.Path, branch: str) -> bool:
    res = _git(['show-ref', '--verify', '--quiet', f'refs/heads/{branch}'], repo_dir, timeout=30)
    return res.returncode == 0


def _working_tree_is_clean(repo_dir: pathlib.Path) -> bool:
    res = _git(['status', '--porcelain', '--untracked-files=all'], repo_dir, timeout=30)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or 'git status failed')
    return not bool((res.stdout or '').strip())


def _project_branch_checkout(
    ctx: ToolContext,
    name: str,
    branch: str,
    base: str = '',
    create: bool = True,
) -> str:
    del ctx
    repo_dir = _require_local_project(name)
    project_name = str(name or '').strip()
    target_branch = _normalize_branch_name(branch)
    base_branch = str(base or '').strip()
    current_branch = _current_branch(repo_dir)
    branch_exists = _local_branch_exists(repo_dir, target_branch)

    if target_branch == current_branch:
        action = 'noop'
    elif branch_exists:
        if not _working_tree_is_clean(repo_dir):
            raise ValueError('working tree must be clean before switching to an existing branch')
        res = _git(['checkout', target_branch], repo_dir, timeout=60)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or res.stdout.strip() or 'git checkout failed')
        action = 'switched'
    else:
        if not bool(create):
            raise ValueError(f'branch does not exist locally: {target_branch}')
        if base_branch:
            normalized_base = _normalize_branch_name(base_branch)
            if not _local_branch_exists(repo_dir, normalized_base):
                raise ValueError(f'base branch does not exist locally: {normalized_base}')
            res = _git(['checkout', '-b', target_branch, normalized_base], repo_dir, timeout=60)
        else:
            res = _git(['checkout', '-b', target_branch], repo_dir, timeout=60)
            normalized_base = current_branch
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or res.stdout.strip() or 'git checkout -b failed')
        action = 'created'
        base_branch = normalized_base

    payload = {
        'status': 'ok',
        'created_at': _utc_now_iso(),
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'branch': {
            'name': target_branch,
            'current': _current_branch(repo_dir),
            'previous': current_branch,
            'action': action,
            'created': action == 'created',
            'switched': action in {'created', 'switched'},
            'exists_locally': _local_branch_exists(repo_dir, target_branch),
            'base': base_branch or current_branch,
        },
        'repo': _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _project_pr_create(
    ctx: ToolContext,
    name: str,
    title: str,
    body: str = '',
    base: str = 'main',
    head: str = '',
) -> str:
    del ctx
    repo_dir = _require_local_project(name)
    project_name = str(name or '').strip()
    if not str(title or '').strip():
        raise ValueError('title must be non-empty')

    repo_slug = _project_github_slug(repo_dir)
    head_branch = str(head or '').strip()
    if not head_branch:
        head_branch = _current_branch(repo_dir)
    if not head_branch:
        raise RuntimeError('could not determine PR head branch')

    base_branch = str(base or '').strip() or 'main'
    if any(ch.isspace() for ch in base_branch) or any(ch.isspace() for ch in head_branch):
        raise ValueError('base/head branch must not contain whitespace')
    if not _remote_branch_exists(repo_dir, head_branch):
        raise ValueError(f'head branch is not pushed to origin: {head_branch}')

    args = [
        'pr', 'create',
        f'--title={str(title).strip()}',
        f'--base={base_branch}',
        f'--head={head_branch}',
    ]
    raw_body = str(body or '')
    if raw_body:
        args.append('--body-file=-')
        res = _run_gh(args, cwd=repo_dir, timeout=180, input_data=raw_body)
    else:
        res = _run_gh(args, cwd=repo_dir, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or 'gh pr create failed')

    url = (res.stdout or '').strip()
    payload = {
        'status': 'ok',
        'created_at': _utc_now_iso(),
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'github': {
            'repo': repo_slug,
            'pull_request': {
                'title': str(title).strip(),
                'body_provided': bool(raw_body),
                'base': base_branch,
                'head': head_branch,
                'url': url,
            },
        },
        'repo': _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            'project_branch_checkout',
            'Create and/or switch the current branch inside an existing bootstrapped local project repository, to support honest GitHub development flow before push and PR creation.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'branch': {'type': 'string', 'description': 'Target local branch name to create or switch to'},
                'base': {'type': 'string', 'description': 'Optional local base branch to create the new branch from; defaults to current branch'},
                'create': {'type': 'boolean', 'description': 'Whether to create the branch if it does not exist locally', 'default': True},
            },
            ['name', 'branch'],
            _project_branch_checkout,
            is_code_tool=True,
        ),
        _tool_entry(
            'project_pr_create',
            'Create a GitHub pull request directly from an existing bootstrapped local project repository, using its configured origin remote and current or specified pushed branch.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'title': {'type': 'string', 'description': 'Pull request title'},
                'body': {'type': 'string', 'description': 'Optional pull request body/description'},
                'base': {'type': 'string', 'description': 'Base branch to merge into', 'default': 'main'},
                'head': {'type': 'string', 'description': 'Optional head branch to open the PR from; defaults to current HEAD branch'},
            },
            ['name', 'title'],
            _project_pr_create,
            is_code_tool=True,
        ),
    ]
