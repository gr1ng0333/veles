from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List

from ouroboros.tools.project_bootstrap import _git, _git_remote_url, _project_status, _repo_info, _require_local_project, _tool_entry, _utc_now_iso
from ouroboros.tools.project_branch_info import _ahead_behind, _current_branch, _default_branch, _remote_branch_name
from ouroboros.tools.registry import ToolContext, ToolEntry


def _remote_ref_exists(repo_dir: pathlib.Path, ref: str) -> bool:
    res = _git(['show-ref', '--verify', '--quiet', f'refs/remotes/{ref}'], repo_dir, timeout=30)
    return res.returncode == 0


def _commit_subject(repo_dir: pathlib.Path, rev: str) -> str:
    res = _git(['log', '-1', '--format=%s', rev], repo_dir, timeout=30)
    if res.returncode != 0:
        return ''
    return (res.stdout or '').strip()


def _commit_list(repo_dir: pathlib.Path, revspec: str, limit: int = 20) -> List[Dict[str, str]]:
    res = _git(['log', f'--max-count={limit}', '--format=%H	%s', revspec], repo_dir, timeout=60)
    if res.returncode != 0:
        return []
    items: List[Dict[str, str]] = []
    for line in (res.stdout or '').splitlines():
        line = line.strip()
        if not line:
            continue
        sha, _, subject = line.partition('	')
        items.append({'sha': sha, 'subject': subject})
    return items


def _remote_summary(repo_dir: pathlib.Path, branch: str) -> Dict[str, Any]:
    remote_branch = _remote_branch_name(repo_dir, branch)
    ahead_behind = _ahead_behind(repo_dir, branch, remote_branch)
    summary: Dict[str, Any] = {
        'branch': branch,
        'default_branch': _default_branch(repo_dir),
        'remote_ref': remote_branch,
        'origin': _git_remote_url(repo_dir) or '',
        'tracking_available': bool(remote_branch),
        'ahead_behind': ahead_behind,
    }
    if not remote_branch:
        summary['compare'] = {
            'available': False,
            'reason': 'remote tracking branch not found',
        }
        return summary

    merge_base_res = _git(['merge-base', branch, remote_branch], repo_dir, timeout=30)
    if merge_base_res.returncode != 0:
        summary['compare'] = {
            'available': False,
            'reason': merge_base_res.stderr.strip() or merge_base_res.stdout.strip() or 'merge-base failed',
        }
        return summary
    merge_base = (merge_base_res.stdout or '').strip()

    local_sha_res = _git(['rev-parse', branch], repo_dir, timeout=30)
    remote_sha_res = _git(['rev-parse', remote_branch], repo_dir, timeout=30)
    local_sha = (local_sha_res.stdout or '').strip() if local_sha_res.returncode == 0 else ''
    remote_sha = (remote_sha_res.stdout or '').strip() if remote_sha_res.returncode == 0 else ''

    summary['compare'] = {
        'available': True,
        'merge_base': merge_base,
        'local': {
            'ref': branch,
            'sha': local_sha,
            'subject': _commit_subject(repo_dir, branch) if local_sha else '',
            'unique_commits': _commit_list(repo_dir, f'{remote_branch}..{branch}'),
        },
        'remote': {
            'ref': remote_branch,
            'sha': remote_sha,
            'subject': _commit_subject(repo_dir, remote_branch) if remote_sha else '',
            'unique_commits': _commit_list(repo_dir, f'{branch}..{remote_branch}'),
        },
    }
    return summary


def remote_awareness_snapshot(repo_dir: pathlib.Path) -> Dict[str, Any]:
    origin = _git_remote_url(repo_dir, 'origin')
    if not origin:
        return {
            'available': False,
            'reason': 'origin remote not configured',
        }
    branch = _current_branch(repo_dir)
    remote_branch = _remote_branch_name(repo_dir, branch)
    ahead_behind = _ahead_behind(repo_dir, branch, remote_branch)
    return {
        'available': True,
        'origin': origin,
        'branch': branch,
        'default_branch': _default_branch(repo_dir),
        'remote_ref': remote_branch,
        'ahead_behind': ahead_behind,
    }


def _project_git_fetch(ctx: ToolContext, name: str, remote: str = 'origin', prune: bool = False) -> str:
    del ctx
    repo_dir = _require_local_project(name)
    project_name = str(name or '').strip()
    remote_name = str(remote or '').strip() or 'origin'
    remote_url = _git_remote_url(repo_dir, remote_name)
    if not remote_url:
        raise ValueError(f'remote not found: {remote_name}')

    before_branch = _current_branch(repo_dir)
    before_summary = _remote_summary(repo_dir, before_branch)

    args = ['fetch']
    if bool(prune):
        args.append('--prune')
    args.append(remote_name)
    fetch_res = _git(args, repo_dir, timeout=120)
    if fetch_res.returncode != 0:
        raise RuntimeError(fetch_res.stderr.strip() or fetch_res.stdout.strip() or 'git fetch failed')

    after_branch = _current_branch(repo_dir)
    after_summary = _remote_summary(repo_dir, after_branch)
    payload = {
        'status': 'ok',
        'fetched_at': _utc_now_iso(),
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'fetch': {
            'remote': remote_name,
            'remote_url': remote_url,
            'prune': bool(prune),
            'stdout': (fetch_res.stdout or '').strip(),
            'stderr': (fetch_res.stderr or '').strip(),
        },
        'remote': {
            'before': before_summary,
            'after': after_summary,
        },
        'repo': _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _project_branch_compare(ctx: ToolContext, name: str, branch: str = '', remote: str = 'origin') -> str:
    del ctx
    repo_dir = _require_local_project(name)
    project_name = str(name or '').strip()
    target_branch = str(branch or '').strip() or _current_branch(repo_dir)
    remote_name = str(remote or '').strip() or 'origin'

    local_exists_res = _git(['show-ref', '--verify', '--quiet', f'refs/heads/{target_branch}'], repo_dir, timeout=30)
    if local_exists_res.returncode != 0:
        raise ValueError(f'local branch not found: {target_branch}')

    remote_url = _git_remote_url(repo_dir, remote_name)
    if not remote_url:
        raise ValueError(f'remote not found: {remote_name}')

    remote_ref = f'{remote_name}/{target_branch}'
    if not _remote_ref_exists(repo_dir, remote_ref):
        raise ValueError(f'remote tracking branch not found: {remote_ref}; run project_git_fetch first')

    summary = _remote_summary(repo_dir, target_branch)
    payload = {
        'status': 'ok',
        'compared_at': _utc_now_iso(),
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'github': {
            'origin': remote_url,
        },
        'branch': summary,
        'repo': _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            'project_git_fetch',
            'Fetch remote refs for a bootstrapped local project repo and return before/after remote-awareness summary.',
            {
                'name': {'type': 'string', 'description': 'Local project name or slug.'},
                'remote': {'type': 'string', 'description': 'Remote name to fetch.', 'default': 'origin'},
                'prune': {'type': 'boolean', 'description': 'Whether to pass --prune to git fetch.', 'default': False},
            },
            ['name'],
            _project_git_fetch,
        ),
        _tool_entry(
            'project_branch_compare',
            'Compare a local branch against its remote tracking branch for a bootstrapped project repo.',
            {
                'name': {'type': 'string', 'description': 'Local project name or slug.'},
                'branch': {'type': 'string', 'description': 'Local branch name; defaults to the current branch.'},
                'remote': {'type': 'string', 'description': 'Remote name; defaults to origin.', 'default': 'origin'},
            },
            ['name'],
            _project_branch_compare,
        ),
    ]
