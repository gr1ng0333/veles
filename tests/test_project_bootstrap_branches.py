# Split from tests/test_project_bootstrap.py to keep test modules readable.
import json
import pathlib
import subprocess
import pytest
from ouroboros.tools.project_bootstrap import _normalize_project_name, _project_commit, _project_file_read, _project_file_write, _project_github_create, _project_init, _project_push, _project_server_register, _project_server_list, _project_server_run, _project_status
from ouroboros.tools.project_server_info import _project_server_get, _project_server_remove
from ouroboros.tools.project_server_management import _project_server_update
from ouroboros.tools.project_branch_info import _project_branch_delete, _project_branch_get, _project_branch_list, _project_branch_rename
from ouroboros.tools.project_remote_awareness import _project_branch_compare, _project_git_fetch
from ouroboros.tools.project_issue_update import _project_issue_assign, _project_issue_close, _project_issue_label_add, _project_issue_label_remove, _project_issue_reopen, _project_issue_unassign, _project_issue_update
from ouroboros.tools.project_pr_update import _project_pr_changed_files, _project_pr_close, _project_pr_diff, _project_pr_reopen, _project_pr_review_list, _project_pr_review_submit
from ouroboros.tools.project_github_dev import _project_branch_checkout, _project_issue_comment, _project_issue_create, _project_issue_get, _project_issue_list, _project_pr_comment, _project_pr_create, _project_pr_get, _project_pr_list, _project_pr_merge
from ouroboros.tools.registry import ToolContext, ToolRegistry
def _ctx(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path)

@pytest.fixture(autouse=True)
def _projects_root_env(monkeypatch, tmp_path):
    monkeypatch.setenv('VELES_PROJECTS_ROOT', str(tmp_path / 'projects'))
def test_project_branch_checkout_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_branch_checkout' in names

def test_project_branch_checkout_creates_and_switches_new_branch(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    payload = json.loads(_project_branch_checkout(_ctx(tmp_path), name='demo-api', branch='feature/auth'))
    assert payload['status'] == 'ok'
    assert payload['branch']['action'] == 'created'
    assert payload['branch']['created'] is True
    assert payload['branch']['switched'] is True
    assert payload['branch']['previous'] == 'main'
    assert payload['branch']['current'] == 'feature/auth'
    assert payload['branch']['base'] == 'main'
    assert payload['repo']['branch'] == 'feature/auth'

def test_project_branch_checkout_switches_existing_branch_when_clean(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'checkout', '-b', 'feature/auth'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'checkout', 'main'], cwd=repo_dir, check=True)
    payload = json.loads(_project_branch_checkout(_ctx(tmp_path), name='demo-api', branch='feature/auth', create=False))
    assert payload['status'] == 'ok'
    assert payload['branch']['action'] == 'switched'
    assert payload['branch']['created'] is False
    assert payload['branch']['switched'] is True
    assert payload['branch']['previous'] == 'main'
    assert payload['branch']['current'] == 'feature/auth'

def test_project_branch_checkout_refuses_switch_with_dirty_working_tree(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'checkout', '-b', 'feature/auth'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'checkout', 'main'], cwd=repo_dir, check=True)
    (repo_dir / 'README.md').write_text('dirty\n', encoding='utf-8')
    with pytest.raises(ValueError, match='working tree must be clean'):
        _project_branch_checkout(_ctx(tmp_path), name='demo-api', branch='feature/auth', create=False)

def test_project_branch_list_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_branch_list' in names

def test_project_branch_get_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_branch_get' in names

def test_project_branch_list_reads_local_branches_with_origin_context(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'checkout', '-b', 'feature/auth'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'checkout', 'main'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'remote', 'add', 'origin', 'git@github.com:acme/demo-api.git'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'update-ref', 'refs/remotes/origin/main', 'HEAD'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main'], cwd=repo_dir, check=True)
    payload = json.loads(_project_branch_list(_ctx(tmp_path), name='demo-api'))
    assert payload['status'] == 'ok'
    assert payload['branches']['current'] == 'main'
    assert payload['branches']['default'] == 'main'
    assert payload['branches']['count'] == 2
    names = {item['name']: item for item in payload['branches']['items']}
    assert set(names.keys()) == {'main', 'feature/auth'}
    assert names['main']['current'] is True
    assert names['main']['default'] is True
    assert names['main']['remote_ref'] == 'origin/main'
    assert names['main']['ahead_behind']['available'] is True
    assert names['main']['ahead_behind']['ahead'] == 0
    assert names['main']['ahead_behind']['behind'] == 0
    assert names['feature/auth']['current'] is False
    assert names['feature/auth']['remote_ref'] == ''
    assert names['feature/auth']['ahead_behind']['available'] is False

def test_project_branch_get_defaults_to_current_branch(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/acme/demo-api.git'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'update-ref', 'refs/remotes/origin/main', 'HEAD'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main'], cwd=repo_dir, check=True)
    payload = json.loads(_project_branch_get(_ctx(tmp_path), name='demo-api'))
    assert payload['status'] == 'ok'
    assert payload['branch']['name'] == 'main'
    assert payload['branch']['current'] is True
    assert payload['branch']['default'] is True
    assert payload['branch']['remote_ref'] == 'origin/main'
    assert payload['branch']['ahead_behind']['available'] is True
    assert payload['github']['origin'] == 'https://github.com/acme/demo-api.git'

def test_project_branch_get_rejects_missing_local_branch(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    with pytest.raises(ValueError, match='local branch not found'):
        _project_branch_get(_ctx(tmp_path), name='demo-api', branch='missing')

def test_project_branch_rename_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_branch_rename' in names

def test_project_branch_rename_renames_local_branch_and_updates_current_when_active(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'checkout', '-b', 'feature/auth'], cwd=repo_dir, check=True)
    payload = json.loads(_project_branch_rename(_ctx(tmp_path), name='demo-api', branch='feature/auth', new_branch='feature/login'))
    assert payload['status'] == 'ok'
    assert payload['branch']['old_name'] == 'feature/auth'
    assert payload['branch']['name'] == 'feature/login'
    assert payload['branch']['renamed'] is True
    assert payload['branch']['current_before'] == 'feature/auth'
    assert payload['branch']['current_after'] == 'feature/login'
    refs_old = subprocess.run(['git', 'branch', '--list', 'feature/auth'], cwd=repo_dir, check=True, capture_output=True, text=True)
    refs_new = subprocess.run(['git', 'branch', '--list', 'feature/login'], cwd=repo_dir, check=True, capture_output=True, text=True)
    current = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=repo_dir, check=True, capture_output=True, text=True)
    assert refs_old.stdout.strip() == ''
    assert 'feature/login' in refs_new.stdout
    assert current.stdout.strip() == 'feature/login'

def test_project_branch_rename_updates_default_branch_metadata_when_origin_head_matches(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'git@github.com:acme/demo-api.git'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'update-ref', 'refs/remotes/origin/main', 'HEAD'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main'], cwd=repo_dir, check=True)
    payload = json.loads(_project_branch_rename(_ctx(tmp_path), name='demo-api', branch='main', new_branch='stable'))
    assert payload['status'] == 'ok'
    assert payload['branch']['default_before'] == 'main'
    assert payload['branch']['default_after'] == 'stable'
    assert payload['branch']['current_after'] == 'stable'

def test_project_branch_rename_rejects_missing_source_branch(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    with pytest.raises(ValueError, match='local branch not found'):
        _project_branch_rename(_ctx(tmp_path), name='demo-api', branch='missing', new_branch='feature/login')

def test_project_branch_compare_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_branch_compare' in names

def test_project_branch_compare_reports_ahead_behind_and_unique_commits(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    remote_dir = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', str(remote_dir)], check=True)
    subprocess.run(['git', 'remote', 'add', 'origin', str(remote_dir)], cwd=repo_dir, check=True)
    subprocess.run(['git', 'push', '-u', 'origin', 'main'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'symbolic-ref', 'HEAD', 'refs/heads/main'], cwd=remote_dir, check=True)
    clone_dir = tmp_path / 'remote-work'
    subprocess.run(['git', 'clone', str(remote_dir), str(clone_dir)], check=True)
    subprocess.run(['git', 'config', 'user.name', 'Remote Bot'], cwd=clone_dir, check=True)
    subprocess.run(['git', 'config', 'user.email', 'remote@example.com'], cwd=clone_dir, check=True)
    (clone_dir / 'REMOTE.txt').write_text('remote change\n', encoding='utf-8')
    subprocess.run(['git', 'add', 'REMOTE.txt'], cwd=clone_dir, check=True)
    subprocess.run(['git', 'commit', '-m', 'Remote advance'], cwd=clone_dir, check=True)
    subprocess.run(['git', 'push', 'origin', 'main'], cwd=clone_dir, check=True)
    _project_git_fetch(_ctx(tmp_path), name='demo-api')
    _project_file_write(_ctx(tmp_path), name='demo-api', path='LOCAL.txt', content='local change\n')
    _project_commit(_ctx(tmp_path), name='demo-api', message='Local advance')
    payload = json.loads(_project_branch_compare(_ctx(tmp_path), name='demo-api'))
    assert payload['status'] == 'ok'
    assert payload['github']['origin'] == str(remote_dir)
    assert payload['branch']['branch'] == 'main'
    assert payload['branch']['remote_ref'] == 'origin/main'
    assert payload['branch']['ahead_behind']['available'] is True
    assert payload['branch']['ahead_behind']['ahead'] == 1
    assert payload['branch']['ahead_behind']['behind'] == 1
    assert payload['branch']['compare']['available'] is True
    assert payload['branch']['compare']['local']['subject'] == 'Local advance'
    assert payload['branch']['compare']['remote']['subject'] == 'Remote advance'
    assert payload['branch']['compare']['local']['unique_commits'][0]['subject'] == 'Local advance'
    assert payload['branch']['compare']['remote']['unique_commits'][0]['subject'] == 'Remote advance'

def test_project_branch_compare_requires_remote_tracking_branch(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/acme/demo-api.git'], cwd=repo_dir, check=True)
    with pytest.raises(ValueError, match='remote tracking branch not found: origin/main; run project_git_fetch first'):
        _project_branch_compare(_ctx(tmp_path), name='demo-api')

def test_project_branch_rename_rejects_existing_target_branch(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'checkout', '-b', 'feature/auth'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'checkout', 'main'], cwd=repo_dir, check=True)
    with pytest.raises(ValueError, match='local branch already exists'):
        _project_branch_rename(_ctx(tmp_path), name='demo-api', branch='feature/auth', new_branch='main')

def test_project_branch_rename_rejects_same_name(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    with pytest.raises(ValueError, match='new_branch must differ from branch'):
        _project_branch_rename(_ctx(tmp_path), name='demo-api', branch='main', new_branch='main')

def test_project_branch_rename_rejects_invalid_target_name(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    with pytest.raises(ValueError, match='must not contain whitespace'):
        _project_branch_rename(_ctx(tmp_path), name='demo-api', branch='main', new_branch='bad branch')

def test_project_branch_delete_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_branch_delete' in names

def test_project_branch_delete_removes_merged_branch(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'checkout', '-b', 'feature/auth'], cwd=repo_dir, check=True)
    (repo_dir / 'feature.txt').write_text('done\n', encoding='utf-8')
    subprocess.run(['git', 'add', 'feature.txt'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'commit', '-m', 'Add feature'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'checkout', 'main'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'merge', '--no-ff', 'feature/auth', '-m', 'Merge feature'], cwd=repo_dir, check=True)
    payload = json.loads(_project_branch_delete(_ctx(tmp_path), name='demo-api', branch='feature/auth'))
    assert payload['status'] == 'ok'
    assert payload['branch']['name'] == 'feature/auth'
    assert payload['branch']['deleted'] is True
    assert payload['branch']['force'] is False
    refs = subprocess.run(['git', 'branch', '--list', 'feature/auth'], cwd=repo_dir, check=True, capture_output=True, text=True)
    assert refs.stdout.strip() == ''

def test_project_branch_delete_refuses_active_branch(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    with pytest.raises(ValueError, match='cannot delete the active branch'):
        _project_branch_delete(_ctx(tmp_path), name='demo-api', branch='main')

def test_project_branch_delete_refuses_default_branch_even_if_not_active(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'checkout', '-b', 'feature/auth'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'remote', 'add', 'origin', 'git@github.com:acme/demo-api.git'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'update-ref', 'refs/remotes/origin/main', 'main'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main'], cwd=repo_dir, check=True)
    with pytest.raises(ValueError, match='cannot delete the default branch'):
        _project_branch_delete(_ctx(tmp_path), name='demo-api', branch='main')

def test_project_branch_delete_refuses_unmerged_branch_without_force(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'checkout', '-b', 'feature/auth'], cwd=repo_dir, check=True)
    (repo_dir / 'feature.txt').write_text('done\n', encoding='utf-8')
    subprocess.run(['git', 'add', 'feature.txt'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'commit', '-m', 'Add feature'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'checkout', 'main'], cwd=repo_dir, check=True)
    with pytest.raises(ValueError, match='branch is not fully merged'):
        _project_branch_delete(_ctx(tmp_path), name='demo-api', branch='feature/auth')

def test_project_branch_delete_force_removes_unmerged_branch(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'checkout', '-b', 'feature/auth'], cwd=repo_dir, check=True)
    (repo_dir / 'feature.txt').write_text('done\n', encoding='utf-8')
    subprocess.run(['git', 'add', 'feature.txt'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'commit', '-m', 'Add feature'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'checkout', 'main'], cwd=repo_dir, check=True)
    payload = json.loads(_project_branch_delete(_ctx(tmp_path), name='demo-api', branch='feature/auth', force=True))
    assert payload['status'] == 'ok'
    assert payload['branch']['force'] is True
    refs = subprocess.run(['git', 'branch', '--list', 'feature/auth'], cwd=repo_dir, check=True, capture_output=True, text=True)
    assert refs.stdout.strip() == ''
