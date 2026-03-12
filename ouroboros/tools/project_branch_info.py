from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List

from ouroboros.tools.project_bootstrap import _git, _git_remote_url, _repo_info, _require_local_project, _tool_entry, _utc_now_iso
from ouroboros.tools.registry import ToolContext, ToolEntry


def _current_branch(repo_dir: pathlib.Path) -> str:
    res = _git(['rev-parse', '--abbrev-ref', 'HEAD'], repo_dir, timeout=30)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or 'git rev-parse failed')
    branch = (res.stdout or '').strip()
    if not branch:
        raise RuntimeError('could not determine current branch')
    return branch


def _default_branch(repo_dir: pathlib.Path) -> str:
    res = _git(['symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD'], repo_dir, timeout=30)
    if res.returncode != 0:
        return ''
    value = (res.stdout or '').strip()
    if not value:
        return ''
    if value.startswith('origin/'):
        return value[len('origin/'):]
    return value


def _remote_branch_name(repo_dir: pathlib.Path, branch: str) -> str:
    res = _git(['show-ref', '--verify', '--quiet', f'refs/remotes/origin/{branch}'], repo_dir, timeout=30)
    if res.returncode == 0:
        return f'origin/{branch}'
    return ''


def _ahead_behind(repo_dir: pathlib.Path, branch: str, remote_branch: str) -> Dict[str, Any]:
    if not remote_branch:
        return {
            'available': False,
            'ahead': None,
            'behind': None,
            'remote_ref': '',
        }
    res = _git(['rev-list', '--left-right', '--count', f'{branch}...{remote_branch}'], repo_dir, timeout=30)
    if res.returncode != 0:
        return {
            'available': False,
            'ahead': None,
            'behind': None,
            'remote_ref': remote_branch,
        }
    parts = (res.stdout or '').strip().split()
    if len(parts) != 2:
        return {
            'available': False,
            'ahead': None,
            'behind': None,
            'remote_ref': remote_branch,
        }
    ahead, behind = int(parts[0]), int(parts[1])
    return {
        'available': True,
        'ahead': ahead,
        'behind': behind,
        'remote_ref': remote_branch,
    }


def _branch_snapshot(repo_dir: pathlib.Path, branch: str, current_branch: str, default_branch: str) -> Dict[str, Any]:
    remote_branch = _remote_branch_name(repo_dir, branch)
    ahead_behind = _ahead_behind(repo_dir, branch, remote_branch)
    sha_res = _git(['rev-parse', branch], repo_dir, timeout=30)
    if sha_res.returncode != 0:
        raise RuntimeError(sha_res.stderr.strip() or sha_res.stdout.strip() or 'git rev-parse branch failed')
    return {
        'name': branch,
        'current': branch == current_branch,
        'default': bool(default_branch) and branch == default_branch,
        'sha': (sha_res.stdout or '').strip(),
        'remote_ref': remote_branch,
        'ahead_behind': ahead_behind,
    }


def _project_branch_list(ctx: ToolContext, name: str) -> str:
    del ctx
    repo_dir = _require_local_project(name)
    project_name = str(name or '').strip()
    current_branch = _current_branch(repo_dir)
    default_branch = _default_branch(repo_dir)

    res = _git(['for-each-ref', '--format=%(refname:short)', 'refs/heads'], repo_dir, timeout=30)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or 'git for-each-ref failed')
    branches = [line.strip() for line in (res.stdout or '').splitlines() if line.strip()]

    payload = {
        'status': 'ok',
        'read_at': _utc_now_iso(),
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'branches': {
            'current': current_branch,
            'default': default_branch,
            'count': len(branches),
            'items': [
                _branch_snapshot(repo_dir, branch, current_branch, default_branch)
                for branch in branches
            ],
        },
        'github': {
            'origin': _git_remote_url(repo_dir) or '',
        },
        'repo': _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _project_branch_get(ctx: ToolContext, name: str, branch: str = '') -> str:
    del ctx
    repo_dir = _require_local_project(name)
    project_name = str(name or '').strip()
    current_branch = _current_branch(repo_dir)
    default_branch = _default_branch(repo_dir)
    target_branch = str(branch or '').strip() or current_branch

    exists_res = _git(['show-ref', '--verify', '--quiet', f'refs/heads/{target_branch}'], repo_dir, timeout=30)
    if exists_res.returncode != 0:
        raise ValueError(f'local branch not found: {target_branch}')

    payload = {
        'status': 'ok',
        'read_at': _utc_now_iso(),
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'branch': _branch_snapshot(repo_dir, target_branch, current_branch, default_branch),
        'branches': {
            'current': current_branch,
            'default': default_branch,
        },
        'github': {
            'origin': _git_remote_url(repo_dir) or '',
        },
        'repo': _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _project_branch_delete(ctx: ToolContext, name: str, branch: str, force: bool = False) -> str:
    del ctx
    repo_dir = _require_local_project(name)
    project_name = str(name or '').strip()
    target_branch = str(branch or '').strip()
    if not target_branch:
        raise ValueError('branch must be non-empty')

    current_branch = _current_branch(repo_dir)
    default_branch = _default_branch(repo_dir)
    exists_res = _git(['show-ref', '--verify', '--quiet', f'refs/heads/{target_branch}'], repo_dir, timeout=30)
    if exists_res.returncode != 0:
        raise ValueError(f'local branch not found: {target_branch}')
    if target_branch == current_branch:
        raise ValueError('cannot delete the active branch')
    if default_branch and target_branch == default_branch:
        raise ValueError('cannot delete the default branch')

    delete_flag = '-D' if bool(force) else '-d'
    res = _git(['branch', delete_flag, target_branch], repo_dir, timeout=60)
    if res.returncode != 0:
        message = res.stderr.strip() or res.stdout.strip() or 'git branch delete failed'
        if not force and ('not fully merged' in message or 'is not fully merged' in message):
            raise ValueError('branch is not fully merged; rerun with force=true to delete anyway')
        raise RuntimeError(message)

    payload = {
        'status': 'ok',
        'deleted_at': _utc_now_iso(),
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'branch': {
            'name': target_branch,
            'deleted': True,
            'force': bool(force),
            'current': current_branch,
            'default': default_branch,
        },
        'github': {
            'origin': _git_remote_url(repo_dir) or '',
        },
        'repo': _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            'project_branch_list',
            'List local branches for an existing bootstrapped project repository, including current/default branch info and ahead/behind against origin when available.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
            },
            ['name'],
            _project_branch_list,
            is_code_tool=True,
        ),
        _tool_entry(
            'project_branch_get',
            'Read one local branch for an existing bootstrapped project repository, defaulting to the current branch, including current/default branch info and ahead/behind against origin when available.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'branch': {'type': 'string', 'description': 'Optional local branch name to inspect; defaults to current branch'},
            },
            ['name'],
            _project_branch_get,
            is_code_tool=True,
        ),
        _tool_entry(
            'project_branch_delete',
            'Delete a local branch in an existing bootstrapped project repository with guardrails for active/default branches and optional force for unmerged branches.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'branch': {'type': 'string', 'description': 'Local branch name to delete'},
                'force': {'type': 'boolean', 'description': 'Force deletion even if the branch is not fully merged', 'default': False},
            },
            ['name', 'branch'],
            _project_branch_delete,
            is_code_tool=True,
        ),
    ]
