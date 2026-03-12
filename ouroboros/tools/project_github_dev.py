from __future__ import annotations

import json
import pathlib
import re
from typing import List

from .project_bootstrap import (
    _git,
    _git_remote_url,
    _normalize_project_name,
    _repo_info,
    _require_local_project,
    _run_gh,
    _tool_entry,
    _utc_now_iso,
)
from .registry import ToolContext, ToolEntry


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
    res = _git(["ls-remote", "--heads", "origin", branch], repo_dir, timeout=60)
    return res.returncode == 0 and bool((res.stdout or '').strip())


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
    project_name = _normalize_project_name(name)
    if not str(title or '').strip():
        raise ValueError('title must be non-empty')

    repo_slug = _project_github_slug(repo_dir)
    head_branch = str(head or '').strip()
    if not head_branch:
        branch_res = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_dir, timeout=30)
        if branch_res.returncode != 0:
            raise RuntimeError(branch_res.stderr.strip() or branch_res.stdout.strip() or 'git rev-parse failed')
        head_branch = (branch_res.stdout or '').strip()
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
            "project_pr_create",
            "Create a GitHub pull request directly from an existing bootstrapped local project repository, using its configured origin remote and current or specified pushed branch.",
            {
                "name": {"type": "string", "description": "Existing local project name under the projects root"},
                "title": {"type": "string", "description": "Pull request title"},
                "body": {"type": "string", "description": "Optional pull request body/description"},
                "base": {"type": "string", "description": "Base branch to merge into", "default": "main"},
                "head": {"type": "string", "description": "Optional head branch to open the PR from; defaults to current HEAD branch"},
            },
            ["name", "title"],
            _project_pr_create,
            is_code_tool=True,
        ),
    ]
