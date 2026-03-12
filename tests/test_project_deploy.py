import json
import pathlib
import subprocess

import pytest

from ouroboros.tools.project_bootstrap import _project_init, _project_server_register
from ouroboros.tools.project_deploy import _build_sync_archive, _project_deploy_recipe, _project_server_sync
from ouroboros.tools.project_service import _project_service_control, _project_service_render_unit
from ouroboros.tools.registry import ToolContext, ToolRegistry


def _ctx(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path)


@pytest.fixture(autouse=True)
def _projects_root_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VELES_PROJECTS_ROOT", str(tmp_path / "projects"))


def test_project_server_sync_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_server_sync" in names


def test_build_sync_archive_excludes_git_and_veles_metadata(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    (repo_dir / ".veles").mkdir(exist_ok=True)
    (repo_dir / ".veles" / "servers.json").write_text('[]\n', encoding='utf-8')
    (repo_dir / "deploy" / "app.env").parent.mkdir(parents=True, exist_ok=True)
    (repo_dir / "deploy" / "app.env").write_text('PORT=8080\n', encoding='utf-8')

    archive_bytes, files, archive_size = _build_sync_archive(repo_dir)

    assert archive_size > 0
    assert 'README.md' in files
    assert 'deploy/app.env' in files
    assert '.veles/servers.json' not in files
    assert all(not path.startswith('.git/') for path in files)
    assert isinstance(archive_bytes, bytes)


def test_project_server_sync_streams_archive_to_registered_server(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='prod',
        host='example.com',
        user='deploy',
        ssh_key_path='~/id_test',
        deploy_path='/srv/demo-api',
    )

    captured = {}

    def fake_run_ssh_stream(args, stdin_bytes, timeout):
        captured['args'] = args
        captured['stdin_bytes'] = stdin_bytes
        captured['timeout'] = timeout
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout=b'synced\n', stderr=b'')

    monkeypatch.setattr('ouroboros.tools.project_deploy._run_ssh_stream', fake_run_ssh_stream)

    payload = json.loads(_project_server_sync(_ctx(tmp_path), name='demo-api', alias='prod', delete=True, timeout=90))

    assert payload['status'] == 'ok'
    assert payload['server']['alias'] == 'prod'
    assert payload['sync']['transport'] == 'ssh+tar'
    assert payload['sync']['delete'] is True
    assert payload['sync']['file_count'] >= 1
    assert 'README.md' in payload['sync']['files']
    assert payload['result']['stdout'] == 'synced\n'
    assert payload['result']['exit_code'] == 0
    assert captured['timeout'] == 90
    assert captured['stdin_bytes']
    assert 'deploy@example.com' in captured['args']
    assert any('/srv/demo-api' in part for part in captured['args'])
    assert any('find ' in part for part in captured['args'])


def test_project_server_sync_rejects_bad_timeout(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='prod',
        host='example.com',
        user='deploy',
        ssh_key_path='~/id_test',
        deploy_path='/srv/demo-api',
    )

    with pytest.raises(ValueError):
        _project_server_sync(_ctx(tmp_path), name='demo-api', alias='prod', timeout=0)


def test_project_service_render_unit_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_service_render_unit" in names


def test_project_service_render_unit_auto_detects_python_defaults(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")

    payload = json.loads(
        _project_service_render_unit(
            _ctx(tmp_path),
            name='demo-api',
            service_name='demo-api',
            deploy_path='/srv/demo-api',
            environment=['PORT=8080'],
            environment_file='/etc/demo-api.env',
            user='deploy',
        )
    )

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
    _project_init(_ctx(tmp_path), name="Demo API", language="python")

    with pytest.raises(ValueError):
        _project_service_render_unit(
            _ctx(tmp_path),
            name='demo-api',
            service_name='demo-api',
            deploy_path='/srv/demo-api',
            environment=['BROKEN_ENV'],
        )


def test_project_service_control_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_service_control" in names


def test_project_service_control_install_streams_unit_and_reloads_systemd(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='prod',
        host='example.com',
        user='deploy',
        ssh_key_path='~/id_test',
        deploy_path='/srv/demo-api',
    )

    captured = {}

    def fake_run_ssh_stream(args, stdin_bytes, timeout):
        captured['args'] = args
        captured['stdin_bytes'] = stdin_bytes
        captured['timeout'] = timeout
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout=b'installed\n', stderr=b'')

    monkeypatch.setattr('ouroboros.tools.project_service._run_ssh_stream', fake_run_ssh_stream)

    unit = "[Unit]\nDescription=Demo API\n[Service]\nExecStart=/usr/bin/python3 /srv/demo-api/app.py\n"
    payload = json.loads(
        _project_service_control(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            action='install',
            unit_content=unit,
            start_on_install=True,
            timeout=90,
        )
    )

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
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='prod',
        host='example.com',
        user='deploy',
        ssh_key_path='~/id_test',
        deploy_path='/srv/demo-api',
    )

    captured = {}

    def fake_run_ssh_stream(args, stdin_bytes, timeout):
        captured['args'] = args
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout=b'installed\n', stderr=b'')

    monkeypatch.setattr('ouroboros.tools.project_service._run_ssh_stream', fake_run_ssh_stream)

    payload = json.loads(
        _project_service_control(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api.service',
            action='install',
            unit_content='[Service]\nExecStart=/bin/true\n',
        )
    )

    command = captured['args'][-1]
    assert '/etc/systemd/system/demo-api.service.service' not in command
    assert '/etc/systemd/system/demo-api.service' in command
    assert 'sudo systemctl enable demo-api.service' in command
    assert payload['service']['name'] == 'demo-api'
    assert payload['service']['unit_name'] == 'demo-api.service'
    assert payload['service']['unit_path'] == '/etc/systemd/system/demo-api.service'


def test_project_service_control_status_runs_systemctl_over_ssh(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='prod',
        host='example.com',
        user='deploy',
        ssh_key_path='~/id_test',
        deploy_path='/srv/demo-api',
    )

    captured = {}

    def fake_run_ssh_text(args, timeout):
        captured['args'] = args
        captured['timeout'] = timeout
        return subprocess.CompletedProcess(['ssh', *args], 3, stdout='inactive\n', stderr='failed\n')

    monkeypatch.setattr('ouroboros.tools.project_service._run_ssh_text', fake_run_ssh_text)

    payload = json.loads(
        _project_service_control(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api.service',
            action='status',
            max_output_chars=10,
            sudo=False,
        )
    )

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
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='prod',
        host='example.com',
        user='deploy',
        ssh_key_path='~/id_test',
        deploy_path='/srv/demo-api',
    )

    with pytest.raises(ValueError):
        _project_service_control(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            action='install',
        )


def test_project_deploy_recipe_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_deploy_recipe" in names


def test_project_deploy_recipe_builds_runtime_aware_python_recipe(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='prod',
        host='example.com',
        user='deploy',
        ssh_key_path='/tmp/id_demo',
        deploy_path='/srv/demo-api',
    )

    payload = json.loads(
        _project_deploy_recipe(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            environment=['PORT=9000'],
            delete=True,
            sync_timeout=90,
            service_timeout=120,
        )
    )

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
    assert steps[0]['recommended_args'] == {
        'name': 'demo-api',
        'alias': 'prod',
        'timeout': 90,
        'delete': True,
    }
    assert steps[2]['tool'] == 'project_service_control'
    assert steps[2]['recommended_args']['action'] == 'install'
    assert steps[2]['recommended_args']['timeout'] == 120
    assert steps[2]['recommended_args']['unit_content'] == payload['service']['unit_content']
    assert any('pip install -r requirements.txt' in command for command in steps[1]['commands'])
    assert any('systemctl enable --now demo-api.service' in command for command in steps[3]['commands'])


def test_project_deploy_recipe_rejects_bad_timeouts(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='prod',
        host='example.com',
        user='deploy',
        ssh_key_path='/tmp/id_demo',
        deploy_path='/srv/demo-api',
    )

    with pytest.raises(ValueError, match='sync_timeout must be > 0'):
        _project_deploy_recipe(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            sync_timeout=0,
        )

    with pytest.raises(ValueError, match='service_timeout must be > 0'):
        _project_deploy_recipe(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            service_timeout=0,
        )
