# Split from tests/test_project_deploy.py to keep test modules readable.
import json
import pathlib
import subprocess
import pytest
from ouroboros.tools.project_bootstrap import _git, _project_init, _project_server_register
from ouroboros.tools.project_deploy_state import _project_deploy_state_path, _read_project_deploy_state
from ouroboros.tools.project_deploy import _build_sync_archive, _project_deploy_apply, _project_deploy_recipe, _project_server_sync
from ouroboros.tools.project_service import _project_service_control, _project_service_render_unit
from ouroboros.tools.project_overview import _project_overview
from ouroboros.tools.registry import ToolContext, ToolRegistry
def _ctx(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path)

@pytest.fixture(autouse=True)
def _projects_root_env(monkeypatch, tmp_path):
    monkeypatch.setenv('VELES_PROJECTS_ROOT', str(tmp_path / 'projects'))
def test_project_server_observability_tools_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_server_health' in names
    assert 'project_service_status' in names
    assert 'project_service_logs' in names
    assert 'project_deploy_status' in names

def test_project_server_health_reads_remote_health_snapshot(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_observability import _project_server_health
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')
    captured = {}

    def fake_run_ssh_text(args, timeout):
        captured['args'] = args
        captured['timeout'] = timeout
        stdout = 'HOSTNAME=api-1\nKERNEL=Linux 6.8\nWHOAMI=deploy\nPWD=/home/deploy\nSYSTEMCTL=present\nDEPLOY_EXISTS=1\nDEPLOY_WRITABLE=0\n'
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout=stdout, stderr='')
    monkeypatch.setattr('ouroboros.tools.project_server_observability._run_ssh_text', fake_run_ssh_text)
    payload = json.loads(_project_server_health(_ctx(tmp_path), name='demo-api', alias='prod', timeout=45))
    assert payload['status'] == 'ok'
    assert payload['health']['reachable'] is True
    assert payload['health']['hostname'] == 'api-1'
    assert payload['health']['systemctl_available'] is True
    assert payload['health']['deploy_path_exists'] is True
    assert payload['health']['deploy_path_writable'] is False
    assert payload['command']['timeout_seconds'] == 45
    assert captured['timeout'] == 45
    assert 'deploy@example.com' in captured['args']

def test_project_deploy_status_combines_deploy_probe_and_service_snapshot(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_observability import _project_deploy_status
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')
    calls = []

    def fake_run_ssh_text(args, timeout):
        calls.append(args[-1])
        if 'systemctl show' in args[-1]:
            stdout = 'LoadState=loaded\nActiveState=failed\nSubState=failed\nUnitFileState=enabled\nFragmentPath=/etc/systemd/system/demo-api.service\nExecMainPID=0\nExecMainStatus=1\nResult=exit-code\nenabled\n'
            return subprocess.CompletedProcess(['ssh', *args], 0, stdout=stdout, stderr='')
        stdout = 'DEPLOY_EXISTS=1\nDEPLOY_REALPATH=/srv/demo-api\nDEPLOY_TOP_LEVEL_COUNT=7\nDEPLOY_WRITABLE=1\nDEPLOY_GIT=0\n'
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout=stdout, stderr='')
    monkeypatch.setattr('ouroboros.tools.project_server_observability._run_ssh_text', fake_run_ssh_text)
    payload = json.loads(_project_deploy_status(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', timeout=15, sudo=False))
    assert payload['status'] == 'ok'
    assert payload['deploy']['exists'] is True
    assert payload['deploy']['writable'] is True
    assert payload['deploy']['top_level_entry_count'] == 7
    assert payload['deploy']['looks_like_git_checkout'] is False
    assert payload['service']['active_state'] == 'failed'
    assert payload['service']['result_state'] == 'exit-code'
    assert len(calls) == 2
    assert payload['diagnostics']['severity'] == 'critical'
    assert 'service is failed' in payload['diagnostics']['summary']
    assert any(('last recorded deploy' not in item for item in payload['diagnostics']['issues']))
    assert payload['diagnostics']['service']['severity'] == 'critical'

def test_project_deploy_recipe_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_deploy_recipe' in names

def test_project_deploy_recipe_builds_runtime_aware_python_recipe(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='/tmp/id_demo', deploy_path='/srv/demo-api')
    payload = json.loads(_project_deploy_recipe(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', environment=['PORT=9000'], delete=True, sync_timeout=90, service_timeout=120))
    assert payload['status'] == 'ok'
    assert payload['runtime'] == {'requested': 'auto', 'resolved': 'python'}
    assert payload['server']['alias'] == 'prod'
    assert payload['sync_preview']['delete'] is True
    assert payload['sync_preview']['timeout_seconds'] == 90
    assert payload['service']['timeout_seconds'] == 120
    assert payload['service']['unit_name'] == 'demo-api.service'
    assert 'ExecStart=/usr/bin/python3' in payload['service']['unit_content']
    assert payload['service']['environment'] == ['PORT=9000']
    steps = payload['recipe']['steps']
    assert [step['key'] for step in steps] == ['sync', 'setup', 'install_service', 'enable_start']
    assert steps[0]['tool'] == 'project_server_sync'
    assert steps[0]['recommended_args'] == {'name': 'demo-api', 'alias': 'prod', 'timeout': 90, 'delete': True}
    assert steps[2]['tool'] == 'project_service_control'
    assert steps[2]['recommended_args']['action'] == 'install'
    assert steps[2]['recommended_args']['timeout'] == 120
    assert steps[2]['recommended_args']['unit_content'] == payload['service']['unit_content']
    assert any(('pip install -r requirements.txt' in command for command in steps[1]['commands']))
    assert any(('systemctl enable --now demo-api.service' in command for command in steps[3]['commands']))

def test_project_deploy_recipe_rejects_bad_timeouts(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='/tmp/id_demo', deploy_path='/srv/demo-api')
    with pytest.raises(ValueError, match='sync_timeout must be > 0'):
        _project_deploy_recipe(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', sync_timeout=0)
    with pytest.raises(ValueError, match='service_timeout must be > 0'):
        _project_deploy_recipe(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', service_timeout=0)

def test_project_deploy_apply_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_deploy_apply' in names

def test_project_deploy_apply_install_runs_sync_setup_install_start_status(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='/tmp/id_demo', deploy_path='/srv/demo-api')
    calls = []

    def fake_sync(ctx, **kwargs):
        calls.append(('sync', kwargs))
        return json.dumps({'status': 'ok', 'result': {'ok': True}})

    def fake_run(ctx, **kwargs):
        calls.append(('setup', kwargs))
        return json.dumps({'status': 'ok', 'command': {'raw': kwargs['command']}, 'result': {'ok': True}})

    def fake_service(ctx, **kwargs):
        calls.append((kwargs['action'], kwargs))
        return json.dumps({'status': 'ok', 'service': {'action': kwargs['action']}, 'result': {'ok': True}})
    monkeypatch.setattr('ouroboros.tools.project_deploy._project_server_sync', fake_sync)
    monkeypatch.setattr('ouroboros.tools.project_bootstrap._project_server_run', fake_run)
    monkeypatch.setattr('ouroboros.tools.project_service._project_service_control', fake_service)
    payload = json.loads(_project_deploy_apply(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', mode='install', delete=True, sync_timeout=90, service_timeout=120, status_timeout=30))
    assert payload['status'] == 'ok'
    assert payload['mode'] == 'install'
    assert [step['key'] for step in payload['steps']] == ['sync', 'setup', 'install_service', 'start', 'status']
    assert [name for name, _ in calls] == ['sync', 'setup', 'install', 'start', 'status']
    assert calls[0][1]['delete'] is True
    assert calls[0][1]['timeout'] == 90
    assert calls[1][1]['timeout'] == 90
    assert 'pip install -r requirements.txt' in calls[1][1]['command']
    assert 'python3 -m venv /srv/demo-api/.venv' in calls[1][1]['command']
    assert calls[2][1]['enable_on_install'] is True
    assert calls[2][1]['start_on_install'] is False
    assert calls[3][1]['action'] == 'start'
    assert calls[3][1]['timeout'] == 120
    assert calls[4][1]['action'] == 'status'
    assert calls[4][1]['timeout'] == 30
    assert payload['steps'][1]['payload']['setup']['count'] == 3
    assert payload['steps'][1]['payload']['setup']['skipped'] is False
    assert payload['summary']['lifecycle_action'] == 'start'
    assert payload['summary']['status_ok'] is True
    assert payload['execution']['dry_run'] is False
    assert payload['execution']['total_steps'] == 5
    assert payload['execution']['executed_steps'] == 5
    assert payload['execution']['ok_steps'] == 5
    assert payload['execution']['error_steps'] == 0
    assert payload['execution']['last_step_key'] == 'status'
    assert payload['deploy_record']['exists'] is True
    assert payload['deploy_record']['outcome']['status'] == 'ok'
    assert payload['deploy_record']['outcome']['deploy']['mode'] == 'install'
    assert payload['deploy_record']['outcome']['deploy']['lifecycle_action'] == 'start'
    state_payload = json.loads(_project_deploy_state_path(pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api').read_text(encoding='utf-8'))
    assert state_payload['status'] == 'ok'
    assert state_payload['deploy']['step_statuses']['status'] == 'ok'

def test_project_deploy_apply_update_restarts_after_setup_and_install(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='/tmp/id_demo', deploy_path='/srv/demo-api')
    calls = []
    monkeypatch.setattr('ouroboros.tools.project_deploy._project_server_sync', lambda ctx, **kwargs: calls.append(('sync', kwargs)) or json.dumps({'status': 'ok'}))
    monkeypatch.setattr('ouroboros.tools.project_bootstrap._project_server_run', lambda ctx, **kwargs: calls.append(('setup', kwargs)) or json.dumps({'status': 'ok', 'command': {'raw': kwargs['command']}}))

    def fake_service(ctx, **kwargs):
        calls.append((kwargs['action'], kwargs))
        return json.dumps({'status': 'ok', 'service': {'action': kwargs['action']}})
    monkeypatch.setattr('ouroboros.tools.project_service._project_service_control', fake_service)
    payload = json.loads(_project_deploy_apply(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', mode='update'))
    assert payload['status'] == 'ok'
    assert [step['key'] for step in payload['steps']] == ['sync', 'setup', 'install_service', 'restart', 'status']
    assert [name for name, _ in calls] == ['sync', 'setup', 'install', 'restart', 'status']
    assert payload['summary']['lifecycle_action'] == 'restart'

def test_project_deploy_apply_dry_run_returns_planned_trace_without_execution(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='/tmp/id_demo', deploy_path='/srv/demo-api')

    def fail_sync(ctx, **kwargs):
        raise AssertionError('sync must not run during dry_run')

    def fail_run(ctx, **kwargs):
        raise AssertionError('setup must not run during dry_run')

    def fail_service(ctx, **kwargs):
        raise AssertionError('service control must not run during dry_run')
    monkeypatch.setattr('ouroboros.tools.project_deploy._project_server_sync', fail_sync)
    monkeypatch.setattr('ouroboros.tools.project_bootstrap._project_server_run', fail_run)
    monkeypatch.setattr('ouroboros.tools.project_service._project_service_control', fail_service)
    payload = json.loads(_project_deploy_apply(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', mode='install', delete=True, dry_run=True))
    assert payload['status'] == 'ok'
    assert payload['dry_run'] is True
    assert payload['execution']['dry_run'] is True
    assert payload['execution']['total_steps'] == 5
    assert payload['execution']['planned_steps'] == 5
    assert payload['execution']['executed_steps'] == 0
    assert payload['execution']['last_step_key'] == 'status'
    assert [step['key'] for step in payload['steps']] == ['sync', 'setup', 'install_service', 'start', 'status']
    assert all((step['status'] == 'planned' for step in payload['steps']))
    assert payload['steps'][0]['args']['delete'] is True
    assert payload['steps'][1]['payload']['setup']['count'] == 3
    assert payload['summary']['lifecycle_action'] == 'start'
    assert payload['summary']['status_ok'] is None
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    assert _project_deploy_state_path(repo_dir).exists() is False

def test_project_deploy_apply_stops_on_setup_failure(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='/tmp/id_demo', deploy_path='/srv/demo-api')
    monkeypatch.setattr('ouroboros.tools.project_deploy._project_server_sync', lambda ctx, **kwargs: json.dumps({'status': 'ok', 'result': {'ok': True}}))
    monkeypatch.setattr('ouroboros.tools.project_bootstrap._project_server_run', lambda ctx, **kwargs: json.dumps({'status': 'error', 'command': {'raw': kwargs['command']}, 'result': {'ok': False, 'exit_code': 1}}))

    def fail_if_called(ctx, **kwargs):
        raise AssertionError('service control must not run after setup failure')
    monkeypatch.setattr('ouroboros.tools.project_service._project_service_control', fail_if_called)
    payload = json.loads(_project_deploy_apply(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', mode='install'))
    assert payload['status'] == 'error'
    assert payload['failed_step'] == 'setup'
    assert payload['execution']['failed_step'] == 'setup'
    assert payload['execution']['executed_steps'] == 2
    assert payload['execution']['error_steps'] == 1
    assert [step['key'] for step in payload['steps']] == ['sync', 'setup']
    assert payload['deploy_record']['outcome']['failed_step'] == 'setup'
    assert payload['deploy_record']['outcome']['deploy']['step_statuses']['setup'] == 'error'

def test_project_deploy_apply_stops_on_sync_failure(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='/tmp/id_demo', deploy_path='/srv/demo-api')
    monkeypatch.setattr('ouroboros.tools.project_deploy._project_server_sync', lambda ctx, **kwargs: json.dumps({'status': 'error', 'result': {'ok': False, 'exit_code': 255}}))

    def fail_if_called(ctx, **kwargs):
        raise AssertionError('service control must not run after sync failure')
    monkeypatch.setattr('ouroboros.tools.project_service._project_service_control', fail_if_called)
    payload = json.loads(_project_deploy_apply(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', mode='install'))
    assert payload['status'] == 'error'
    assert payload['failed_step'] == 'sync'
    assert payload['execution']['failed_step'] == 'sync'
    assert payload['execution']['executed_steps'] == 1
    assert payload['execution']['error_steps'] == 1
    assert [step['key'] for step in payload['steps']] == ['sync']
    assert payload['deploy_record']['outcome']['failed_step'] == 'sync'
    assert payload['deploy_record']['outcome']['deploy']['step_statuses']['sync'] == 'error'

def test_project_deploy_status_includes_last_recorded_deploy_outcome(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_observability import _project_deploy_status
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    _project_deploy_state_path(repo_dir).parent.mkdir(parents=True, exist_ok=True)
    _project_deploy_state_path(repo_dir).write_text(json.dumps({'kind': 'project_deploy_outcome', 'status': 'ok', 'failed_step': '', 'deploy': {'mode': 'update', 'lifecycle_action': 'restart'}}), encoding='utf-8')
    calls = []

    def fake_run_ssh_text(args, timeout):
        calls.append(args[-1])
        if 'systemctl show' in args[-1]:
            stdout = 'LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=enabled\nFragmentPath=/etc/systemd/system/demo-api.service\nExecMainPID=1234\nExecMainStatus=0\nResult=success\nenabled\n'
            return subprocess.CompletedProcess(['ssh', *args], 0, stdout=stdout, stderr='')
        stdout = 'DEPLOY_EXISTS=1\nDEPLOY_REALPATH=/srv/demo-api\nDEPLOY_TOP_LEVEL_COUNT=7\nDEPLOY_WRITABLE=1\nDEPLOY_GIT=0\n'
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout=stdout, stderr='')
    monkeypatch.setattr('ouroboros.tools.project_server_observability._run_ssh_text', fake_run_ssh_text)
    payload = json.loads(_project_deploy_status(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', timeout=15, sudo=False))
    assert payload['status'] == 'ok'
    assert payload['last_deploy']['exists'] is True
    assert payload['last_deploy']['outcome']['status'] == 'ok'
    assert payload['last_deploy']['outcome']['deploy']['mode'] == 'update'
    assert 'execution' in payload['last_deploy']['outcome']['deploy']
    assert len(calls) == 2

def test_read_project_deploy_state_backfills_missing_execution_fields(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    state_path = _project_deploy_state_path(repo_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({'kind': 'project_deploy_outcome', 'status': 'ok', 'deploy': {'mode': 'update', 'lifecycle_action': 'restart'}}), encoding='utf-8')
    payload = _read_project_deploy_state(repo_dir)
    assert payload is not None
    assert payload['deploy']['mode'] == 'update'
    assert payload['deploy']['execution']['dry_run'] is None
    assert payload['deploy']['execution']['total_steps'] is None
    assert payload['deploy']['execution']['last_step_key'] == ''

def test_project_server_management_tools_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_server_update' in names
    assert 'project_server_validate' in names

def test_project_server_validate_reports_ready_deploy_target(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_management import _project_server_validate
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')
    calls = []

    def fake_run_remote_text(server, command, timeout):
        calls.append(command)
        if 'systemctl show' in command:
            stdout = 'LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=enabled\nResult=success\nFragmentPath=/etc/systemd/system/demo-api.service\n'
            return subprocess.CompletedProcess(['ssh', command], 0, stdout=stdout, stderr='')
        stdout = 'SSH_OK=1\nWHOAMI=deploy\nSYSTEMCTL=present\nDEPLOY_EXISTS=1\nDEPLOY_WRITABLE=1\nPARENT_EXISTS=1\nPARENT_WRITABLE=1\n'
        return subprocess.CompletedProcess(['ssh', command], 0, stdout=stdout, stderr='')
    monkeypatch.setattr('ouroboros.tools.project_server_management._run_remote_text', fake_run_remote_text)
    payload = json.loads(_project_server_validate(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', timeout=25, sudo=False))
    assert payload['status'] == 'ok'
    assert payload['validation']['ok'] is True
    assert payload['validation']['checks']['ssh'] is True
    assert payload['validation']['checks']['deploy_path_ready'] is True
    assert payload['validation']['checks']['service_unit_exists'] is True
    assert payload['service']['unit_name'] == 'demo-api.service'
    assert len(calls) == 2

def test_project_server_validate_reports_not_ready_when_parent_is_not_writable(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_management import _project_server_validate
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')

    def fake_run_remote_text(server, command, timeout):
        stdout = 'SSH_OK=1\nWHOAMI=deploy\nSYSTEMCTL=present\nDEPLOY_EXISTS=0\nDEPLOY_WRITABLE=0\nPARENT_EXISTS=1\nPARENT_WRITABLE=0\n'
        return subprocess.CompletedProcess(['ssh', command], 0, stdout=stdout, stderr='')
    monkeypatch.setattr('ouroboros.tools.project_server_management._run_remote_text', fake_run_remote_text)
    payload = json.loads(_project_server_validate(_ctx(tmp_path), name='demo-api', alias='prod'))
    assert payload['status'] == 'error'
    assert payload['validation']['ok'] is False
    assert payload['validation']['checks']['deploy_path_exists'] is False
    assert payload['validation']['checks']['deploy_parent_writable'] is False
    assert payload['validation']['checks']['deploy_path_ready'] is False

def test_project_server_validate_accepts_missing_deploy_dir_when_parent_is_writable(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_management import _project_server_validate
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')

    def fake_run_remote_text(server, command, timeout):
        stdout = 'SSH_OK=1\nWHOAMI=deploy\nSYSTEMCTL=present\nDEPLOY_EXISTS=0\nDEPLOY_WRITABLE=0\nPARENT_EXISTS=1\nPARENT_WRITABLE=1\n'
        return subprocess.CompletedProcess(['ssh', command], 0, stdout=stdout, stderr='')
    monkeypatch.setattr('ouroboros.tools.project_server_management._run_remote_text', fake_run_remote_text)
    payload = json.loads(_project_server_validate(_ctx(tmp_path), name='demo-api', alias='prod'))
    assert payload['status'] == 'ok'
    assert payload['validation']['checks']['deploy_path_exists'] is False
    assert payload['validation']['checks']['deploy_parent_writable'] is True
    assert payload['validation']['checks']['deploy_path_ready'] is True

def test_project_server_validate_reports_not_ready_when_service_is_missing(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_management import _project_server_validate
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')
    calls = []

    def fake_run_remote_text(server, command, timeout):
        calls.append(command)
        if 'systemctl show' in command:
            stdout = 'LoadState=not-found\nActiveState=inactive\nSubState=dead\nUnitFileState=\nResult=success\nFragmentPath=\n'
            return subprocess.CompletedProcess(['ssh', command], 0, stdout=stdout, stderr='')
        stdout = 'SSH_OK=1\nWHOAMI=deploy\nSYSTEMCTL=present\nDEPLOY_EXISTS=1\nDEPLOY_WRITABLE=1\nPARENT_EXISTS=1\nPARENT_WRITABLE=1\n'
        return subprocess.CompletedProcess(['ssh', command], 0, stdout=stdout, stderr='')
    monkeypatch.setattr('ouroboros.tools.project_server_management._run_remote_text', fake_run_remote_text)
    payload = json.loads(_project_server_validate(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api'))
    assert payload['status'] == 'error'
    assert payload['validation']['ok'] is False
    assert payload['validation']['checks']['service_unit_exists'] is False
    assert len(calls) == 2

def test_read_project_deploy_state_rejects_invalid_json(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    state_path = _project_deploy_state_path(repo_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text('{invalid json\n', encoding='utf-8')
    with pytest.raises(RuntimeError, match='invalid JSON'):
        _read_project_deploy_state(repo_dir)

def test_project_deploy_status_surfaces_missing_deploy_record_and_path_problem(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_observability import _project_deploy_status
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')

    def fake_run_ssh_text(args, timeout):
        stdout = 'DEPLOY_EXISTS=0\nDEPLOY_REALPATH=\nDEPLOY_TOP_LEVEL_COUNT=0\nDEPLOY_WRITABLE=0\nDEPLOY_GIT=0\n'
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout=stdout, stderr='')
    monkeypatch.setattr('ouroboros.tools.project_server_observability._run_ssh_text', fake_run_ssh_text)
    payload = json.loads(_project_deploy_status(_ctx(tmp_path), name='demo-api', alias='prod', timeout=15, sudo=False))
    assert payload['status'] == 'ok'
    assert payload['last_deploy']['exists'] is False
    assert payload['diagnostics']['severity'] == 'warning'
    assert 'deploy path missing' in payload['diagnostics']['summary']
    assert 'no recorded deploy outcome' in payload['diagnostics']['summary']
    assert any(('project_server_validate' in item for item in payload['diagnostics']['recommended_checks']))
    assert any(('project_deploy_apply' in item for item in payload['diagnostics']['recommended_checks']))

def test_project_deploy_and_verify_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_deploy_and_verify' in names

def test_project_deploy_and_verify_returns_deploy_and_snapshot_layers(tmp_path, monkeypatch):
    from ouroboros.tools.project_composite_flows import _project_deploy_and_verify
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')

    def fake_project_deploy_apply(ctx, **kwargs):
        return json.dumps({'status': 'ok', 'project': {'name': 'demo-api', 'path': str(tmp_path / 'projects' / 'demo-api')}, 'server': {'alias': 'prod'}, 'execution': {'failed_step': '', 'last_step_key': 'status'}, 'summary': {'service_name': 'demo-api'}})

    def fake_project_operational_snapshot(ctx, **kwargs):
        return json.dumps({'status': 'ok', 'project': {'name': 'demo-api', 'path': str(tmp_path / 'projects' / 'demo-api')}, 'selection': {'alias': 'prod', 'service_name': 'demo-api', 'runtime_included': True}, 'readiness': {'local_clean': True, 'github_ready': True, 'deploy_target_ready': True, 'service_running': True, 'rollout_ready': True, 'blocked_reasons': []}, 'risk_flags': [], 'next_actions': [], 'runtime': {'diagnostics': {'severity': 'healthy'}}})
    monkeypatch.setattr('ouroboros.tools.project_composite_flows._project_deploy_apply', fake_project_deploy_apply)
    monkeypatch.setattr('ouroboros.tools.project_composite_flows._project_operational_snapshot', fake_project_operational_snapshot)
    payload = json.loads(_project_deploy_and_verify(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', mode='update'))
    assert payload['status'] == 'ok'
    assert payload['selection'] == {'alias': 'prod', 'service_name': 'demo-api', 'mode': 'update', 'dry_run': False}
    assert [step['key'] for step in payload['steps']] == ['deploy_apply', 'verify_snapshot']
    assert payload['verdict']['healthy'] is True
    assert payload['verdict']['rollout_ready'] is True
    assert payload['verdict']['service_running'] is True

def test_project_deploy_and_verify_surfaces_failed_step_and_actions(tmp_path, monkeypatch):
    from ouroboros.tools.project_composite_flows import _project_deploy_and_verify
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')

    def fake_project_deploy_apply(ctx, **kwargs):
        return json.dumps({'status': 'error', 'project': {'name': 'demo-api', 'path': str(tmp_path / 'projects' / 'demo-api')}, 'server': {'alias': 'prod'}, 'failed_step': 'setup', 'execution': {'failed_step': 'setup', 'last_step_key': 'setup'}})

    def fake_project_operational_snapshot(ctx, **kwargs):
        return json.dumps({'status': 'ok', 'project': {'name': 'demo-api', 'path': str(tmp_path / 'projects' / 'demo-api')}, 'selection': {'alias': 'prod', 'service_name': 'demo-api', 'runtime_included': True}, 'readiness': {'local_clean': True, 'github_ready': True, 'deploy_target_ready': False, 'service_running': False, 'rollout_ready': False, 'blocked_reasons': ['deploy target is not writable/ready']}, 'risk_flags': ['runtime_critical', 'last_deploy_error'], 'next_actions': ['resolve the last failed deploy step: setup'], 'runtime': {'diagnostics': {'severity': 'critical'}}})
    monkeypatch.setattr('ouroboros.tools.project_composite_flows._project_deploy_apply', fake_project_deploy_apply)
    monkeypatch.setattr('ouroboros.tools.project_composite_flows._project_operational_snapshot', fake_project_operational_snapshot)
    payload = json.loads(_project_deploy_and_verify(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api'))
    assert payload['status'] == 'error'
    assert payload['verdict']['healthy'] is False
    assert payload['verdict']['failed_step'] == 'setup'
    assert payload['verdict']['blocked_reasons'] == ['deploy target is not writable/ready']
    assert payload['verdict']['next_actions'] == ['resolve the last failed deploy step: setup']
