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
def test_project_service_render_unit_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_service_render_unit' in names

def test_project_service_render_unit_auto_detects_python_defaults(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    payload = json.loads(_project_service_render_unit(_ctx(tmp_path), name='demo-api', service_name='demo-api', deploy_path='/srv/demo-api', environment=['PORT=8080'], environment_file='/etc/demo-api.env', user='deploy'))
    assert payload['status'] == 'ok'
    assert payload['service']['name'] == 'demo-api'
    assert payload['service']['unit_name'] == 'demo-api.service'
    assert payload['service']['runtime'] == 'python'
    assert payload['service']['working_directory'] == '/srv/demo-api'
    assert payload['service']['exec_start'] == '/usr/bin/python3 -m src.demo_api.main'
    assert payload['service']['unit_path'] == '/etc/systemd/system/demo-api.service'
    unit = payload['service']['unit_content']
    assert 'WorkingDirectory=/srv/demo-api' in unit
    assert 'EnvironmentFile=/etc/demo-api.env' in unit
    assert 'Environment=PORT=8080' in unit
    assert 'User=deploy' in unit
    assert 'ExecStart=/usr/bin/python3 -m src.demo_api.main' in unit

def test_project_service_render_unit_rejects_bad_environment_entry(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    with pytest.raises(ValueError):
        _project_service_render_unit(_ctx(tmp_path), name='demo-api', service_name='demo-api', deploy_path='/srv/demo-api', environment=['BROKEN_ENV'])

def test_project_service_status_reads_structured_systemd_snapshot(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_observability import _project_service_status
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')
    captured = {}

    def fake_run_ssh_text(args, timeout):
        captured['args'] = args
        captured['timeout'] = timeout
        stdout = 'LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=enabled\nFragmentPath=/etc/systemd/system/demo-api.service\nExecMainPID=1234\nExecMainStatus=0\nResult=success\nenabled\n'
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout=stdout, stderr='')
    monkeypatch.setattr('ouroboros.tools.project_server_observability._run_ssh_text', fake_run_ssh_text)
    payload = json.loads(_project_service_status(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', timeout=30, sudo=False))
    assert payload['status'] == 'ok'
    assert payload['service']['unit_name'] == 'demo-api.service'
    assert payload['service']['running'] is True
    assert payload['service']['exists'] is True
    assert payload['service']['enabled_state'] == 'enabled'
    assert payload['service']['exec_main_pid'] == '1234'
    assert captured['timeout'] == 30
    assert captured['args'][-1].startswith('systemctl show demo-api.service --no-page')
    assert payload['diagnostics']['severity'] == 'healthy'
    assert payload['diagnostics']['summary'] == 'service is running'

def test_project_service_logs_reads_journalctl_output(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_observability import _project_service_logs
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')
    captured = {}

    def fake_run_ssh_text(args, timeout):
        captured['args'] = args
        captured['timeout'] = timeout
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout='L1\nL2\nL3\n', stderr='')
    monkeypatch.setattr('ouroboros.tools.project_server_observability._run_ssh_text', fake_run_ssh_text)
    payload = json.loads(_project_service_logs(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api.service', lines=50, timeout=20, max_output_chars=5, sudo=False))
    assert payload['status'] == 'ok'
    assert payload['logs']['lines_requested'] == 50
    assert payload['logs']['content'] == 'L1\nL2'
    assert payload['result']['truncated'] is True
    assert captured['timeout'] == 20
    assert captured['args'][-1] == 'journalctl -u demo-api.service -n 50 --no-pager --output=short-iso'

def test_project_service_status_reports_missing_unit_diagnostics(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_observability import _project_service_status
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')

    def fake_run_ssh_text(args, timeout):
        stdout = 'LoadState=not-found\nActiveState=inactive\nSubState=dead\nUnitFileState=\nFragmentPath=\nExecMainPID=0\nExecMainStatus=0\nResult=success\n'
        return subprocess.CompletedProcess(['ssh', *args], 1, stdout=stdout, stderr='not found\n')
    monkeypatch.setattr('ouroboros.tools.project_server_observability._run_ssh_text', fake_run_ssh_text)
    payload = json.loads(_project_service_status(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', timeout=30, sudo=False))
    assert payload['status'] == 'error'
    assert payload['service']['exists'] is False
    assert payload['diagnostics']['severity'] == 'critical'
    assert payload['diagnostics']['summary'] == 'systemd unit not found'
    assert any(('project_service_control(action=install)' in item for item in payload['diagnostics']['recommended_checks']))

def test_project_service_status_reports_transitional_diagnostics(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_observability import _project_service_status
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')

    def fake_run_ssh_text(args, timeout):
        stdout = 'LoadState=loaded\nActiveState=activating\nSubState=start-pre\nUnitFileState=enabled\nFragmentPath=/etc/systemd/system/demo-api.service\nExecMainPID=0\nExecMainStatus=0\nResult=success\nenabled\n'
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout=stdout, stderr='')
    monkeypatch.setattr('ouroboros.tools.project_server_observability._run_ssh_text', fake_run_ssh_text)
    payload = json.loads(_project_service_status(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', timeout=30, sudo=False))
    assert payload['status'] == 'ok'
    assert payload['service']['exists'] is True
    assert payload['service']['running'] is False
    assert payload['diagnostics']['severity'] == 'warning'
    assert payload['diagnostics']['summary'] == 'service is in transitional state: activating/start-pre'
    assert any(('re-check project_service_status' in item for item in payload['diagnostics']['recommended_checks']))

def test_project_service_control_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_service_control' in names

def test_project_service_control_install_streams_unit_and_reloads_systemd(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')
    captured = {}

    def fake_run_ssh_stream(args, stdin_bytes, timeout):
        captured['args'] = args
        captured['stdin_bytes'] = stdin_bytes
        captured['timeout'] = timeout
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout=b'installed\n', stderr=b'')
    monkeypatch.setattr('ouroboros.tools.project_service._run_ssh_stream', fake_run_ssh_stream)
    unit = '[Unit]\nDescription=Demo API\n[Service]\nExecStart=/usr/bin/python3 /srv/demo-api/app.py\n'
    payload = json.loads(_project_service_control(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', action='install', unit_content=unit, start_on_install=True, timeout=90))
    assert payload['status'] == 'ok'
    assert payload['service']['action'] == 'install'
    assert payload['service']['install']['enable_on_install'] is True
    assert payload['service']['install']['start_on_install'] is True
    assert payload['result']['stdout'] == 'installed\n'
    assert captured['timeout'] == 90
    assert captured['stdin_bytes'] == unit.encode('utf-8')
    command = captured['args'][-1]
    assert 'sudo mkdir -p /etc/systemd/system' in command
    assert 'sudo tee /etc/systemd/system/demo-api.service >/dev/null' in command
    assert 'sudo systemctl daemon-reload' in command
    assert 'sudo systemctl enable demo-api.service' in command
    assert 'sudo systemctl start demo-api.service' in command
    assert payload['service']['name'] == 'demo-api'
    assert payload['service']['unit_name'] == 'demo-api.service'

def test_project_service_control_install_does_not_double_suffix_service_unit(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')
    captured = {}

    def fake_run_ssh_stream(args, stdin_bytes, timeout):
        captured['args'] = args
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout=b'installed\n', stderr=b'')
    monkeypatch.setattr('ouroboros.tools.project_service._run_ssh_stream', fake_run_ssh_stream)
    payload = json.loads(_project_service_control(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api.service', action='install', unit_content='[Service]\nExecStart=/bin/true\n'))
    command = captured['args'][-1]
    assert '/etc/systemd/system/demo-api.service.service' not in command
    assert '/etc/systemd/system/demo-api.service' in command
    assert 'sudo systemctl enable demo-api.service' in command
    assert payload['service']['name'] == 'demo-api'
    assert payload['service']['unit_name'] == 'demo-api.service'
    assert payload['service']['unit_path'] == '/etc/systemd/system/demo-api.service'

def test_project_service_control_status_runs_systemctl_over_ssh(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')
    captured = {}

    def fake_run_ssh_text(args, timeout):
        captured['args'] = args
        captured['timeout'] = timeout
        return subprocess.CompletedProcess(['ssh', *args], 3, stdout='inactive\n', stderr='failed\n')
    monkeypatch.setattr('ouroboros.tools.project_service._run_ssh_text', fake_run_ssh_text)
    payload = json.loads(_project_service_control(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api.service', action='status', max_output_chars=10, sudo=False))
    assert payload['status'] == 'error'
    assert payload['service']['action'] == 'status'
    assert payload['service']['sudo'] is False
    assert payload['result']['exit_code'] == 3
    assert payload['result']['output'] == 'inactive\nf'
    assert payload['result']['truncated'] is True
    assert captured['timeout'] == 60
    assert captured['args'][-1] == 'systemctl status demo-api.service'

def test_project_service_control_install_requires_unit_content(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')
    with pytest.raises(ValueError):
        _project_service_control(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', action='install')
