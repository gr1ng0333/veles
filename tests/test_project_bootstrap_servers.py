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
def test_project_server_register_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_server_register' in names

def test_project_server_get_rejects_unknown_alias(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    with pytest.raises(ValueError, match='project server alias not found'):
        _project_server_get(_ctx(tmp_path), name='demo-api', alias='prod')

def test_project_server_remove_rejects_unknown_alias(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    with pytest.raises(ValueError, match='project server alias not found'):
        _project_server_remove(_ctx(tmp_path), name='demo-api', alias='prod')

def test_project_server_run_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_server_run' in names

def test_project_server_list_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_server_list' in names

def test_project_server_get_reads_registered_server(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api', label='Production')
    payload = json.loads(_project_server_get(_ctx(tmp_path), name='demo-api', alias='prod'))
    assert payload['status'] == 'ok'
    assert payload['server']['alias'] == 'prod'
    assert payload['server']['label'] == 'Production'
    assert payload['server']['host'] == 'example.com'
    assert payload['server']['deploy_path'] == '/srv/demo-api'
    assert payload['registry']['count'] == 1
    assert payload['registry']['aliases'] == ['prod']

def test_project_server_get_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_server_get' in names

def test_project_server_remove_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_server_remove' in names

def test_project_server_remove_deletes_registered_server(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api', label='Production')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='staging', host='staging.example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api-staging', label='Staging')
    payload = json.loads(_project_server_remove(_ctx(tmp_path), name='demo-api', alias='prod'))
    assert payload['status'] == 'ok'
    assert payload['removed_server']['alias'] == 'prod'
    assert payload['removed_server']['label'] == 'Production'
    assert payload['registry']['count'] == 1
    assert payload['registry']['aliases'] == ['staging']

def test_project_server_update_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_server_update' in names

def test_project_server_update_updates_registered_server_metadata(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api', label='Production')
    payload = json.loads(_project_server_update(_ctx(tmp_path), name='demo-api', alias='prod', new_alias='primary', host='api.example.com', user='ubuntu', port=2222, deploy_path='/srv/demo-api-v2', label='Primary'))
    assert payload['status'] == 'ok'
    assert payload['server']['alias'] == 'primary'
    assert payload['server']['host'] == 'api.example.com'
    assert payload['server']['user'] == 'ubuntu'
    assert payload['server']['port'] == 2222
    assert payload['server']['deploy_path'] == '/srv/demo-api-v2'
    assert payload['server']['label'] == 'Primary'
    assert payload['registry']['aliases'] == ['primary']

def test_project_server_update_rejects_duplicate_new_alias(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='staging', host='staging.example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api-staging')
    with pytest.raises(ValueError):
        _project_server_update(_ctx(tmp_path), name='demo-api', alias='prod', new_alias='staging')

def test_project_server_remove_leaves_empty_registry_file_when_last_server_removed(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')
    payload = json.loads(_project_server_remove(_ctx(tmp_path), name='demo-api', alias='prod'))
    assert payload['status'] == 'ok'
    assert payload['registry']['count'] == 0
    assert payload['registry']['aliases'] == []
    assert payload['registry']['exists'] is True

def test_project_server_register_persists_validated_server_metadata(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    payload = json.loads(_project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='203.0.113.10', user='deploy', ssh_key_path='~/.ssh/demo_prod', deploy_path='/srv/demo-api', port=2222, label='Production'))
    repo_dir = pathlib.Path(payload['project']['path'])
    registry_path = repo_dir / '.veles' / 'servers.json'
    saved = json.loads(registry_path.read_text(encoding='utf-8'))
    assert payload['status'] == 'ok'
    assert payload['server']['alias'] == 'prod'
    assert payload['server']['host'] == '203.0.113.10'
    assert payload['server']['user'] == 'deploy'
    assert payload['server']['port'] == 2222
    assert payload['server']['deploy_path'] == '/srv/demo-api'
    assert payload['server']['ssh_key_path'].endswith('/.ssh/demo_prod')
    assert payload['registry']['count'] == 1
    assert payload['registry']['aliases'] == ['prod']
    assert saved[0]['alias'] == 'prod'
    assert saved[0]['label'] == 'Production'

def test_project_server_register_updates_existing_alias_instead_of_duplicating(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='203.0.113.10', user='deploy', ssh_key_path='/home/veles/.ssh/demo_prod', deploy_path='/srv/demo-api')
    payload = json.loads(_project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='demo.example.com', user='root', ssh_key_path='/home/veles/.ssh/demo_root', deploy_path='/opt/demo', label='Primary'))
    repo_dir = pathlib.Path(payload['project']['path'])
    saved = json.loads((repo_dir / '.veles' / 'servers.json').read_text(encoding='utf-8'))
    assert payload['registry']['count'] == 1
    assert payload['server']['host'] == 'demo.example.com'
    assert payload['server']['user'] == 'root'
    assert payload['server']['deploy_path'] == '/opt/demo'
    assert saved == [saved[0]]
    assert saved[0]['alias'] == 'prod'
    assert saved[0]['host'] == 'demo.example.com'
    assert saved[0]['label'] == 'Primary'

def test_project_server_register_rejects_relative_deploy_path(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    with pytest.raises(ValueError):
        _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='demo.example.com', user='deploy', ssh_key_path='/home/veles/.ssh/demo', deploy_path='srv/demo-api')

def test_project_server_list_reports_empty_registry_when_missing(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    payload = json.loads(_project_server_list(_ctx(tmp_path), name='demo-api'))
    assert payload['status'] == 'ok'
    assert payload['registry']['count'] == 0
    assert payload['registry']['aliases'] == []
    assert payload['registry']['exists'] is False
    assert payload['servers'] == []

def test_project_server_list_returns_public_sorted_server_views(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='prod.example.com', user='deploy', ssh_key_path='/home/veles/.ssh/prod', deploy_path='/srv/demo-api', label='Production')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='staging', host='staging.example.com', user='ubuntu', ssh_key_path='/home/veles/.ssh/staging', deploy_path='/srv/demo-api-staging', port=2222)
    payload = json.loads(_project_server_list(_ctx(tmp_path), name='demo-api'))
    assert payload['status'] == 'ok'
    assert payload['registry']['count'] == 2
    assert payload['registry']['aliases'] == ['prod', 'staging']
    assert payload['registry']['exists'] is True
    assert [item['alias'] for item in payload['servers']] == ['prod', 'staging']
    assert payload['servers'][0] == {'alias': 'prod', 'label': 'Production', 'host': 'prod.example.com', 'port': 22, 'user': 'deploy', 'auth': 'ssh_key_path', 'ssh_key_path': '/home/veles/.ssh/prod', 'deploy_path': '/srv/demo-api', 'created_at': payload['servers'][0]['created_at'], 'updated_at': payload['servers'][0]['updated_at']}
    assert payload['servers'][1]['alias'] == 'staging'
    assert payload['servers'][1]['port'] == 2222
    assert payload['servers'][1]['label'] == ''

def test_project_server_run_executes_command_via_registered_alias(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='demo.example.com', user='deploy', ssh_key_path='/home/veles/.ssh/demo', deploy_path='/srv/demo-api', port=2222)
    seen = {}

    def fake_run_ssh(args, timeout):
        seen['args'] = args
        seen['timeout'] = timeout
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout='ok\n', stderr='')
    monkeypatch.setattr('ouroboros.tools.project_bootstrap._run_ssh', fake_run_ssh)
    payload = json.loads(_project_server_run(_ctx(tmp_path), name='demo-api', alias='prod', command='uname -a', timeout=45))
    assert payload['status'] == 'ok'
    assert payload['server']['alias'] == 'prod'
    assert payload['command']['raw'] == 'uname -a'
    assert payload['command']['timeout_seconds'] == 45
    assert payload['result']['ok'] is True
    assert payload['result']['exit_code'] == 0
    assert payload['result']['stdout'] == 'ok\n'
    assert payload['result']['stderr'] == ''
    assert payload['result']['output'] == 'ok\n'
    assert payload['result']['truncated'] is False
    assert seen['timeout'] == 45
    assert seen['args'][:10] == ['-i', '/home/veles/.ssh/demo', '-p', '2222', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'IdentitiesOnly=yes']
    assert seen['args'][10:] == ['deploy@demo.example.com', '--', 'uname -a']

def test_project_server_run_reports_nonzero_exit_and_clips_output(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='demo.example.com', user='deploy', ssh_key_path='/home/veles/.ssh/demo', deploy_path='/srv/demo-api')

    def fake_run_ssh(args, timeout):
        return subprocess.CompletedProcess(['ssh', *args], 17, stdout='abcdef', stderr='ghijkl')
    monkeypatch.setattr('ouroboros.tools.project_bootstrap._run_ssh', fake_run_ssh)
    payload = json.loads(_project_server_run(_ctx(tmp_path), name='demo-api', alias='prod', command='failing-command', max_output_chars=8))
    assert payload['status'] == 'error'
    assert payload['result']['ok'] is False
    assert payload['result']['exit_code'] == 17
    assert payload['result']['stdout'] == 'abcdef'
    assert payload['result']['stderr'] == 'ghijkl'
    assert payload['result']['output'] == 'abcdefgh'
    assert payload['result']['truncated'] is True
    assert payload['result']['max_output_chars'] == 8
    assert len(payload['result']['output']) <= len('abcdefghijkl')

def test_project_server_run_rejects_unknown_alias(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    with pytest.raises(ValueError):
        _project_server_run(_ctx(tmp_path), name='demo-api', alias='missing', command='pwd')
