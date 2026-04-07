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
def test_project_issue_list_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_issue_list' in names

def test_project_issue_get_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_issue_get' in names

def test_project_issue_create_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_issue_create' in names

def test_project_issue_comment_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_issue_comment' in names

def test_project_issue_list_reads_github_issues(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'git@github.com:acme/demo-api.git'], cwd=repo_dir, check=True)

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        assert cwd == repo_dir
        assert args == ['issue', 'list', '--state', 'open', '--limit', '5', '--json', 'number,title,state,url,author,labels']
        payload = [{'number': 12, 'title': 'Broken login', 'state': 'OPEN', 'url': 'https://github.com/acme/demo-api/issues/12', 'author': {'login': 'alice'}, 'labels': [{'name': 'bug'}]}]
        return subprocess.CompletedProcess(['gh', *args], 0, stdout=json.dumps(payload), stderr='')
    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)
    payload = json.loads(_project_issue_list(_ctx(tmp_path), name='demo-api', limit=5))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['state'] == 'open'
    assert payload['github']['limit'] == 5
    assert payload['github']['issues'][0]['number'] == 12
    assert payload['github']['issues'][0]['title'] == 'Broken login'

def test_project_issue_get_reads_one_github_issue(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/acme/demo-api.git'], cwd=repo_dir, check=True)

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        assert cwd == repo_dir
        assert args == ['issue', 'view', '7', '--json', 'number,title,body,state,url,author,labels,comments']
        payload = {'number': 7, 'title': 'Need healthcheck', 'body': 'Please add /health', 'state': 'OPEN', 'url': 'https://github.com/acme/demo-api/issues/7', 'author': {'login': 'bob'}, 'labels': [{'name': 'enhancement'}], 'comments': [{'author': {'login': 'alice'}, 'body': 'working on it'}]}
        return subprocess.CompletedProcess(['gh', *args], 0, stdout=json.dumps(payload), stderr='')
    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)
    payload = json.loads(_project_issue_get(_ctx(tmp_path), name='demo-api', number=7))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue']['number'] == 7
    assert payload['github']['issue']['body'] == 'Please add /health'
    assert payload['github']['issue']['comments'][0]['body'] == 'working on it'

def test_project_issue_create_returns_url_and_uses_stdin_for_body(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout='https://github.com/acme/demo-api/issues/9\n', stderr='')
    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)
    payload = json.loads(_project_issue_create(_ctx(tmp_path), name='demo-api', title='Need /health', body='Please add a health endpoint'))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue']['title'] == 'Need /health'
    assert payload['github']['issue']['body_provided'] is True
    assert payload['github']['issue']['url'] == 'https://github.com/acme/demo-api/issues/9'
    assert calls[0]['args'][:2] == ['issue', 'create']
    assert '--title=Need /health' in calls[0]['args']
    assert '--body-file=-' in calls[0]['args']
    assert calls[0]['input'] == 'Please add a health endpoint'

def test_project_issue_comment_passes_body_via_stdin(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'git@github.com:acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout='comment added\n', stderr='')
    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)
    payload = json.loads(_project_issue_comment(_ctx(tmp_path), name='demo-api', number=9, body='I am taking this'))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue_comment']['number'] == 9
    assert payload['github']['issue_comment']['body'] == 'I am taking this'
    assert payload['github']['issue_comment']['result'] == 'comment added'
    assert calls[0]['args'] == ['issue', 'comment', '9', '--body-file', '-']
    assert calls[0]['input'] == 'I am taking this'

def test_project_issue_label_add_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_issue_label_add' in names

def test_project_issue_label_remove_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_issue_label_remove' in names

def test_project_issue_assign_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_issue_assign' in names

def test_project_issue_unassign_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_issue_unassign' in names

def test_project_issue_update_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_issue_update' in names

def test_project_issue_close_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_issue_close' in names

def test_project_issue_reopen_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_issue_reopen' in names

def test_project_issue_update_passes_title_and_body_via_stdin(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout='issue updated\n', stderr='')
    monkeypatch.setattr('ouroboros.tools.project_issue_update._run_gh', fake_run_gh)
    payload = json.loads(_project_issue_update(_ctx(tmp_path), name='demo-api', number=9, title='Need /ready', body='Please rename /health to /ready'))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue_update']['number'] == 9
    assert payload['github']['issue_update']['title'] == 'Need /ready'
    assert payload['github']['issue_update']['body_provided'] is True
    assert payload['github']['issue_update']['result'] == 'issue updated'
    assert calls[0]['args'] == ['issue', 'edit', '9', '--title=Need /ready', '--body-file', '-']
    assert calls[0]['input'] == 'Please rename /health to /ready'

def test_project_issue_close_calls_gh_close(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'git@github.com:acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout='issue closed\n', stderr='')
    monkeypatch.setattr('ouroboros.tools.project_issue_update._run_gh', fake_run_gh)
    payload = json.loads(_project_issue_close(_ctx(tmp_path), name='demo-api', number=11))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue_close']['number'] == 11
    assert payload['github']['issue_close']['result'] == 'issue closed'
    assert calls[0]['args'] == ['issue', 'close', '11']
    assert calls[0]['input'] is None

def test_project_issue_reopen_calls_gh_reopen(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'git@github.com:acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout='issue reopened\n', stderr='')
    monkeypatch.setattr('ouroboros.tools.project_issue_update._run_gh', fake_run_gh)
    payload = json.loads(_project_issue_reopen(_ctx(tmp_path), name='demo-api', number=11))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue_reopen']['number'] == 11
    assert payload['github']['issue_reopen']['result'] == 'issue reopened'
    assert calls[0]['args'] == ['issue', 'reopen', '11']
    assert calls[0]['input'] is None

def test_project_issue_label_add_calls_gh_edit_with_add_label(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'git@github.com:acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout='labels added\n', stderr='')
    monkeypatch.setattr('ouroboros.tools.project_issue_update._run_gh', fake_run_gh)
    payload = json.loads(_project_issue_label_add(_ctx(tmp_path), name='demo-api', number=7, labels=['bug', 'backend']))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue_label_add']['number'] == 7
    assert payload['github']['issue_label_add']['labels'] == ['bug', 'backend']
    assert payload['github']['issue_label_add']['result'] == 'labels added'
    assert calls[0]['args'] == ['issue', 'edit', '7', '--add-label', 'bug', '--add-label', 'backend']
    assert calls[0]['input'] is None

def test_project_issue_label_remove_calls_gh_edit_with_remove_label(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'git@github.com:acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout='labels removed\n', stderr='')
    monkeypatch.setattr('ouroboros.tools.project_issue_update._run_gh', fake_run_gh)
    payload = json.loads(_project_issue_label_remove(_ctx(tmp_path), name='demo-api', number=7, labels='bug'))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue_label_remove']['number'] == 7
    assert payload['github']['issue_label_remove']['labels'] == ['bug']
    assert payload['github']['issue_label_remove']['result'] == 'labels removed'
    assert calls[0]['args'] == ['issue', 'edit', '7', '--remove-label', 'bug']
    assert calls[0]['input'] is None

def test_project_issue_assign_calls_gh_edit_with_add_assignee(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'git@github.com:acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout='assignees added\n', stderr='')
    monkeypatch.setattr('ouroboros.tools.project_issue_update._run_gh', fake_run_gh)
    payload = json.loads(_project_issue_assign(_ctx(tmp_path), name='demo-api', number=5, assignees=['alice', 'bob']))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue_assign']['number'] == 5
    assert payload['github']['issue_assign']['assignees'] == ['alice', 'bob']
    assert payload['github']['issue_assign']['result'] == 'assignees added'
    assert calls[0]['args'] == ['issue', 'edit', '5', '--add-assignee', 'alice', '--add-assignee', 'bob']
    assert calls[0]['input'] is None

def test_project_issue_unassign_calls_gh_edit_with_remove_assignee(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'git@github.com:acme/demo-api.git'], cwd=repo_dir, check=True)
    calls = []

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        calls.append({'args': args, 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        return subprocess.CompletedProcess(['gh', *args], 0, stdout='assignees removed\n', stderr='')
    monkeypatch.setattr('ouroboros.tools.project_issue_update._run_gh', fake_run_gh)
    payload = json.loads(_project_issue_unassign(_ctx(tmp_path), name='demo-api', number=5, assignees='alice'))
    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue_unassign']['number'] == 5
    assert payload['github']['issue_unassign']['assignees'] == ['alice']
    assert payload['github']['issue_unassign']['result'] == 'assignees removed'
    assert calls[0]['args'] == ['issue', 'edit', '5', '--remove-assignee', 'alice']
    assert calls[0]['input'] is None

def test_project_issue_label_add_rejects_empty_labels(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'git@github.com:acme/demo-api.git'], cwd=repo_dir, check=True)
    with pytest.raises(ValueError, match='labels must contain at least one non-empty value'):
        _project_issue_label_add(_ctx(tmp_path), name='demo-api', number=1, labels=['', '   '])

def test_project_issue_assign_rejects_whitespace_in_assignee(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    subprocess.run(['git', 'remote', 'add', 'origin', 'git@github.com:acme/demo-api.git'], cwd=repo_dir, check=True)
    with pytest.raises(ValueError, match='assignees entries must not contain whitespace'):
        _project_issue_assign(_ctx(tmp_path), name='demo-api', number=1, assignees=['alice smith'])
