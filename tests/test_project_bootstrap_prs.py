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
def test_project_pr_list_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_pr_list' in names

def test_project_pr_get_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_pr_get' in names

def test_project_pr_create_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_pr_create' in names

def test_project_pr_comment_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_pr_comment' in names

def test_project_pr_merge_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_pr_merge' in names

def test_project_pr_close_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_pr_close' in names

def test_project_pr_reopen_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_pr_reopen' in names

def test_project_pr_changed_files_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_pr_changed_files' in names

def test_project_pr_diff_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_pr_diff' in names

def test_project_pr_review_list_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_pr_review_list' in names

def test_project_pr_review_submit_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_pr_review_submit' in names

def test_project_pr_create_uses_current_branch_and_reports_url(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'git@github.com:acme/demo-api.git'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'checkout', '-b', 'feature/auth'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout='https://github.com/acme/demo-api/pull/7\n', stderr='')
    from ouroboros.tools.project_github_dev import _git as real_git

    def fake_git(args, cwd, timeout=20):
        if args[:3] == ['ls-remote', '--heads', 'origin']:
            return subprocess.CompletedProcess(['git', *args], 0, stdout='abc\trefs/heads/feature/auth\n', stderr='')
        return real_git(args, cwd, timeout)
    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)
    monkeypatch.setattr('ouroboros.tools.project_github_dev._git', fake_git)
    payload = json.loads(_project_pr_create(_ctx(tmp_path), name='demo-api', title='Add auth'))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['pull_request']['base'] == 'main'
    assert payload['github']['pull_request']['head'] == 'feature/auth'
    assert payload['github']['pull_request']['url'] == 'https://github.com/acme/demo-api/pull/7'
    assert calls[0]['args'][:2] == ['pr', 'create']
    assert '--base=main' in calls[0]['args']
    assert '--head=feature/auth' in calls[0]['args']
    assert calls[0]['input'] is None

def test_project_pr_create_requires_pushed_head_branch(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'git@github.com:acme/demo-api.git'], cwd=repo_dir, check=True)
    with pytest.raises(ValueError, match='head branch is not pushed to origin'):
        _project_pr_create(_ctx(tmp_path), name='demo-api', title='Add auth')

def test_project_pr_list_reads_remote_prs(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'git@github.com:acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout=json.dumps([{'number': 7, 'title': 'Add auth', 'state': 'OPEN', 'headRefName': 'feature/auth', 'baseRefName': 'main', 'url': 'https://github.com/acme/demo-api/pull/7', 'isDraft': False, 'author': {'login': 'veles'}}]), stderr='')
    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)
    payload = json.loads(_project_pr_list(_ctx(tmp_path), name='demo-api', state='open', limit=5))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['state'] == 'open'
    assert payload['github']['limit'] == 5
    assert payload['github']['pull_requests'][0]['number'] == 7
    assert calls[0]['args'][:2] == ['pr', 'list']
    assert '--state' in calls[0]['args']
    assert '--limit' in calls[0]['args']
    assert '--json' in calls[0]['args']

def test_project_pr_get_reads_one_remote_pr(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout=json.dumps({'number': 8, 'title': 'Add auth', 'body': 'Detailed body', 'state': 'OPEN', 'headRefName': 'feature/auth', 'baseRefName': 'main', 'url': 'https://github.com/acme/demo-api/pull/8', 'isDraft': False, 'author': {'login': 'veles'}, 'commits': [{'oid': 'abc'}], 'comments': [{'id': 'note-1'}]}), stderr='')
    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)
    payload = json.loads(_project_pr_get(_ctx(tmp_path), name='demo-api', number=8))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['pull_request']['number'] == 8
    assert payload['github']['pull_request']['body'] == 'Detailed body'
    assert payload['github']['pull_request']['commits'][0]['oid'] == 'abc'
    assert calls[0]['args'][:3] == ['pr', 'view', '8']
    assert '--json' in calls[0]['args']

def test_project_pr_create_passes_body_via_stdin(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/acme/demo-api.git'], cwd=repo_dir, check=True)
    subprocess.run(['git', 'checkout', '-b', 'feature/body'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout='https://github.com/acme/demo-api/pull/8\n', stderr='')
    from ouroboros.tools.project_github_dev import _git as real_git

    def fake_git(args, cwd, timeout=20):
        if args[:3] == ['ls-remote', '--heads', 'origin']:
            return subprocess.CompletedProcess(['git', *args], 0, stdout='abc\trefs/heads/feature/body\n', stderr='')
        return real_git(args, cwd, timeout)
    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)
    monkeypatch.setattr('ouroboros.tools.project_github_dev._git', fake_git)
    payload = json.loads(_project_pr_create(_ctx(tmp_path), name='demo-api', title='Add auth', body='Detailed PR body', base='main', head='feature/body'))
    assert payload['status'] == 'ok'
    assert payload['github']['pull_request']['body_provided'] is True
    assert payload['github']['pull_request']['url'] == 'https://github.com/acme/demo-api/pull/8'
    assert '--body-file=-' in calls[0]['args']
    assert calls[0]['input'] == 'Detailed PR body'

def test_project_pr_comment_passes_body_via_stdin(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout='https://github.com/acme/demo-api/pull/8#issuecomment-1\n', stderr='')
    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)
    payload = json.loads(_project_pr_comment(_ctx(tmp_path), name='demo-api', number=8, body='Looks good'))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['pull_request_comment']['number'] == 8
    assert payload['github']['pull_request_comment']['body'] == 'Looks good'
    assert payload['github']['pull_request_comment']['result'] == 'https://github.com/acme/demo-api/pull/8#issuecomment-1'
    assert calls[0]['args'] == ['pr', 'comment', '8', '--body-file', '-']
    assert calls[0]['input'] == 'Looks good'

def test_project_pr_merge_uses_requested_method_and_delete_branch(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout='Merged pull request #8\n', stderr='')
    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)
    payload = json.loads(_project_pr_merge(_ctx(tmp_path), name='demo-api', number=8, method='squash', delete_branch=True))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['pull_request_merge']['number'] == 8
    assert payload['github']['pull_request_merge']['method'] == 'squash'
    assert payload['github']['pull_request_merge']['delete_branch'] is True
    assert payload['github']['pull_request_merge']['result'] == 'Merged pull request #8'
    assert calls[0]['args'] == ['pr', 'merge', '8', '--squash', '--delete-branch']
    assert calls[0]['input'] is None

def test_project_pr_close_calls_gh(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout='Closed pull request #8\n', stderr='')
    monkeypatch.setattr('ouroboros.tools.project_pr_update._run_gh', fake_run_gh)
    payload = json.loads(_project_pr_close(_ctx(tmp_path), name='demo-api', number=8))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['pull_request_close']['number'] == 8
    assert payload['github']['pull_request_close']['result'] == 'Closed pull request #8'
    assert calls[0]['args'] == ['pr', 'close', '8']

def test_project_pr_reopen_calls_gh(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout='Reopened pull request #8\n', stderr='')
    monkeypatch.setattr('ouroboros.tools.project_pr_update._run_gh', fake_run_gh)
    payload = json.loads(_project_pr_reopen(_ctx(tmp_path), name='demo-api', number=8))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['pull_request_reopen']['number'] == 8
    assert payload['github']['pull_request_reopen']['result'] == 'Reopened pull request #8'
    assert calls[0]['args'] == ['pr', 'reopen', '8']

def test_project_pr_changed_files_reads_files(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout=json.dumps({'files': [{'path': 'src/app.py', 'additions': 12, 'deletions': 3}, {'path': 'README.md', 'additions': 2, 'deletions': 0}]}), stderr='')
    monkeypatch.setattr('ouroboros.tools.project_pr_update._run_gh', fake_run_gh)
    payload = json.loads(_project_pr_changed_files(_ctx(tmp_path), name='demo-api', number=8))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['pull_request_files']['number'] == 8
    assert payload['github']['pull_request_files']['count'] == 2
    assert payload['github']['pull_request_files']['items'][0]['path'] == 'src/app.py'
    assert calls[0]['args'] == ['pr', 'view', '8', '--json', 'files']

def test_project_pr_diff_reads_patch_and_clips(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []
    diff_text = 'diff --git a/src/app.py b/src/app.py\n' + '+line\n' * 20

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout=diff_text, stderr='')
    monkeypatch.setattr('ouroboros.tools.project_pr_update._run_gh', fake_run_gh)
    payload = json.loads(_project_pr_diff(_ctx(tmp_path), name='demo-api', number=8, max_chars=40))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['pull_request_diff']['number'] == 8
    assert payload['github']['pull_request_diff']['max_chars'] == 40
    assert payload['github']['pull_request_diff']['truncated'] is True
    assert len(payload['github']['pull_request_diff']['content']) == 40
    assert calls[0]['args'] == ['pr', 'diff', '8', '--patch']

def test_project_pr_diff_rejects_non_positive_max_chars(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/acme/demo-api.git'], cwd=repo_dir, check=True)
    with pytest.raises(ValueError, match='max_chars must be > 0'):
        _project_pr_diff(_ctx(tmp_path), name='demo-api', number=8, max_chars=0)

def test_project_pr_review_list_reads_reviews(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout=json.dumps({'reviews': [{'id': 'r1', 'author': {'login': 'veles'}, 'state': 'APPROVED', 'body': 'ok'}, {'id': 'r2', 'author': {'login': 'andrey'}, 'state': 'COMMENTED', 'body': 'nit'}]}), stderr='')
    monkeypatch.setattr('ouroboros.tools.project_pr_update._run_gh', fake_run_gh)
    payload = json.loads(_project_pr_review_list(_ctx(tmp_path), name='demo-api', number=8))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['pull_request_reviews']['number'] == 8
    assert payload['github']['pull_request_reviews']['count'] == 2
    assert payload['github']['pull_request_reviews']['items'][0]['state'] == 'APPROVED'
    assert calls[0]['args'] == ['pr', 'view', '8', '--json', 'reviews']

def test_project_pr_review_submit_approve_calls_gh(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout='Review submitted\n', stderr='')
    monkeypatch.setattr('ouroboros.tools.project_pr_update._run_gh', fake_run_gh)
    payload = json.loads(_project_pr_review_submit(_ctx(tmp_path), name='demo-api', number=8, event='approve', body='Ship it'))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['pull_request_review_submit']['number'] == 8
    assert payload['github']['pull_request_review_submit']['event'] == 'approve'
    assert payload['github']['pull_request_review_submit']['body'] == 'Ship it'
    assert calls[0]['args'] == ['pr', 'review', '8', '--approve', '--body-file', '-']
    assert calls[0]['input'] == 'Ship it'

def test_project_pr_review_submit_rejects_unknown_event(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/acme/demo-api.git'], cwd=repo_dir, check=True)
    with pytest.raises(ValueError, match='event must be one of: comment, approve, request_changes'):
        _project_pr_review_submit(_ctx(tmp_path), name='demo-api', number=8, event='dismiss')

def test_project_pr_merge_rejects_unknown_method(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/acme/demo-api.git'], cwd=repo_dir, check=True)
    with pytest.raises(ValueError, match='method must be one of: merge, squash, rebase'):
        _project_pr_merge(_ctx(tmp_path), name='demo-api', number=8, method='fast-forward')
