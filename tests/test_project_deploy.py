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




def test_project_overview_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_overview" in names


def test_project_overview_returns_unified_local_snapshot_without_runtime(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")

    payload = json.loads(_project_overview(_ctx(tmp_path), name='demo-api'))

    assert payload['status'] == 'ok'
    assert payload['project']['name'] == 'demo-api'
    assert payload['repo']['snapshot']['working_tree']['clean'] is True
    assert payload['github']['configured'] is False
    assert payload['servers']['count'] == 0
    assert payload['deploy']['state_file']['exists'] is False
    assert payload['deploy']['recipe_preview']['available'] is False
    assert payload['deploy']['runtime_snapshot']['included'] is False
    assert payload['summary']['working_tree_clean'] is True
    assert payload['summary']['meaningful_working_tree_change_count'] == 0
    assert payload['summary']['github_configured'] is False
    assert payload['summary']['registered_server_count'] == 0
    assert 'create and attach a GitHub origin with project_github_create' in payload['next_actions']
    assert 'register at least one deploy target with project_server_register' in payload['next_actions']


def test_project_overview_aggregates_github_recipe_and_runtime_snapshot(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    remote_res = _git(['remote', 'add', 'origin', 'git@github.com:example/demo-api.git'], repo_dir, timeout=30)
    assert remote_res.returncode == 0
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='prod',
        host='example.com',
        user='deploy',
        ssh_key_path='~/id_test',
        deploy_path='/srv/demo-api',
    )

    def fake_run_project_gh_json(repo_dir, args, timeout=120):
        if args[:2] == ['issue', 'list']:
            return [{'number': 1, 'title': 'Bug', 'state': 'OPEN', 'url': 'https://github.com/example/demo-api/issues/1', 'author': {'login': 'alice'}, 'labels': []}]
        if args[:2] == ['pr', 'list']:
            return [{'number': 2, 'title': 'Fix', 'state': 'OPEN', 'headRefName': 'fix', 'baseRefName': 'main', 'url': 'https://github.com/example/demo-api/pull/2', 'isDraft': False, 'author': {'login': 'bob'}}]
        raise AssertionError(args)

    def fake_project_deploy_recipe(ctx, name, alias, service_name, **kwargs):
        return json.dumps({
            'generated_at': '2026-03-13T14:30:00Z',
            'runtime': {'requested': 'auto', 'resolved': 'python'},
            'server': {'alias': alias, 'deploy_path': '/srv/demo-api'},
            'recipe': {'kind': 'project_deploy_recipe', 'steps': [{'key': 'sync'}, {'key': 'setup'}, {'key': 'install_service'}]},
        })

    def fake_project_deploy_status(ctx, name, alias, service_name, **kwargs):
        return json.dumps({
            'status': 'ok',
            'deploy': {'exists': True, 'writable': True, 'path': '/srv/demo-api'},
            'service': {'unit_name': 'demo-api.service', 'active_state': 'active', 'running': True},
            'diagnostics': {'severity': 'healthy', 'summary': 'service is running'},
            'last_deploy': {'exists': False, 'outcome': None},
        })

    monkeypatch.setattr('ouroboros.tools.project_overview._run_project_gh_json', fake_run_project_gh_json)
    monkeypatch.setattr('ouroboros.tools.project_overview._project_deploy_recipe', fake_project_deploy_recipe)
    monkeypatch.setattr('ouroboros.tools.project_overview._project_deploy_status', fake_project_deploy_status)

    payload = json.loads(
        _project_overview(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            include_runtime=True,
            issue_limit=5,
            pr_limit=5,
        )
    )

    assert payload['github']['configured'] is True
    assert payload['github']['available'] is True
    assert payload['github']['repo'] == 'example/demo-api'
    assert payload['github']['issues']['returned_count'] == 1
    assert payload['github']['pull_requests']['returned_count'] == 1
    assert payload['servers']['count'] == 1
    assert payload['servers']['selected']['alias'] == 'prod'
    assert payload['deploy']['recipe_preview']['available'] is True
    assert payload['deploy']['recipe_preview']['runtime']['resolved'] == 'python'
    assert payload['deploy']['runtime_snapshot']['included'] is True
    assert payload['deploy']['runtime_snapshot']['deploy']['exists'] is True
    assert payload['deploy']['runtime_snapshot']['service']['running'] is True
    assert payload['summary']['github_configured'] is True
    assert payload['summary']['open_issue_count'] == 1
    assert payload['summary']['open_pull_request_count'] == 1
    assert payload['summary']['selected_server_alias'] == 'prod'
    assert payload['summary']['service_running'] is True
    assert payload['summary']['meaningful_working_tree_change_count'] == 0
    assert payload['summary']['diagnostic_severity'] == 'healthy'
    assert payload['next_actions'] == []


def test_project_overview_surfaces_failed_deploy_next_action(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='prod',
        host='example.com',
        user='deploy',
        ssh_key_path='~/id_test',
        deploy_path='/srv/demo-api',
    )
    state_path = _project_deploy_state_path(repo_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        'status': 'error',
        'failed_step': 'setup',
        'deploy': {'execution': {'last_step_key': 'setup'}},
    }), encoding='utf-8')

    def fake_project_deploy_status(ctx, name, alias, service_name, **kwargs):
        return json.dumps({
            'status': 'error',
            'deploy': {'exists': True, 'writable': True, 'path': '/srv/demo-api'},
            'service': {'unit_name': 'demo-api.service', 'active_state': 'failed', 'running': False, 'exists': True},
            'diagnostics': {'severity': 'critical', 'summary': 'service is failed', 'recommended_checks': ['read project_service_logs for the most recent journal output']},
            'last_deploy': {'status': 'error', 'failed_step': 'setup'},
        })

    monkeypatch.setattr('ouroboros.tools.project_overview._project_deploy_status', fake_project_deploy_status)

    payload = json.loads(
        _project_overview(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            include_runtime=True,
        )
    )

    assert payload['summary']['last_deploy_status'] == 'error'
    assert payload['summary']['diagnostic_severity'] == 'critical'
    assert 'resolve the last failed deploy step: setup' in payload['next_actions']
    assert 'read project_service_logs for the most recent journal output' in payload['next_actions']


def test_project_operational_snapshot_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_operational_snapshot" in names


def test_project_operational_snapshot_focuses_rollout_readiness(tmp_path, monkeypatch):
    from ouroboros.tools.project_operational_snapshot import _project_operational_snapshot

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

    def fake_run_project_gh_json(repo_dir, args, timeout=120):
        if args[:2] == ['issue', 'list']:
            return [{'number': 1}, {'number': 2}]
        if args[:2] == ['pr', 'list']:
            return [{'number': 7}]
        raise AssertionError(args)

    def fake_project_deploy_status(ctx, name, alias, service_name, **kwargs):
        return json.dumps({
            'status': 'error',
            'deploy': {'exists': True, 'writable': False, 'path': '/srv/demo-api'},
            'service': {'unit_name': 'demo-api.service', 'active_state': 'failed', 'running': False, 'exists': True},
            'diagnostics': {
                'severity': 'critical',
                'summary': 'service is failed',
                'recommended_checks': ['read project_service_logs for the most recent journal output'],
            },
        })

    monkeypatch.setattr('ouroboros.tools.project_operational_snapshot._project_github_slug', lambda repo_dir: 'example/demo-api')
    monkeypatch.setattr('ouroboros.tools.project_operational_snapshot._run_project_gh_json', fake_run_project_gh_json)
    monkeypatch.setattr('ouroboros.tools.project_operational_snapshot._project_deploy_status', fake_project_deploy_status)

    payload = json.loads(
        _project_operational_snapshot(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
        )
    )

    assert payload['github']['configured'] is True
    assert payload['github']['open_issue_count'] == 2
    assert payload['github']['open_pull_request_count'] == 1
    assert payload['selection']['runtime_included'] is True
    assert payload['readiness']['deploy_target_ready'] is False
    assert payload['readiness']['service_running'] is False
    assert payload['readiness']['rollout_ready'] is False
    assert 'deploy_path_not_writable' in payload['risk_flags']
    assert 'runtime_critical' in payload['risk_flags']
    assert 'validate or fix the deploy target before the next apply' in payload['next_actions']
    assert 'read project_service_logs for the most recent journal output' in payload['next_actions']


def test_stage3_readme_keeps_minimal_lifecycle_map():
    readme = pathlib.Path(__file__).resolve().parent.parent / 'README.md'
    text = readme.read_text(encoding='utf-8')

    assert '## Unified Project Lifecycle' in text
    assert '### Minimal Stage 3 scenarios' in text
    assert '**1. Новый проект -> GitHub publish**' in text
    assert '**2. Изменение -> collaboration loop**' in text
    assert '**3. Deploy / operate loop**' in text
    assert '`project_init`' in text
    assert '`project_github_create`' in text
    assert '`project_deploy_apply`' in text
    assert '`project_operational_snapshot`' in text


def test_project_operational_snapshot_without_runtime_stays_local(tmp_path, monkeypatch):
    from ouroboros.tools.project_operational_snapshot import _project_operational_snapshot

    _project_init(_ctx(tmp_path), name="Demo API", language="python")

    payload = json.loads(_project_operational_snapshot(_ctx(tmp_path), name='demo-api'))

    assert payload['selection']['runtime_included'] is False
    assert payload['readiness']['deploy_target_ready'] is None
    assert payload['readiness']['service_running'] is None
    assert payload['readiness']['rollout_ready'] is True
    assert payload['github']['configured'] is False
    assert payload['risk_flags'] == []
    assert 'attach a GitHub origin with project_github_create' in payload['next_actions']

def test_project_server_observability_tools_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_server_health" in names
    assert "project_service_status" in names
    assert "project_service_logs" in names
    assert "project_deploy_status" in names


def test_project_server_health_reads_remote_health_snapshot(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_observability import _project_server_health

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
        stdout = (
            'HOSTNAME=api-1\n'
            'KERNEL=Linux 6.8\n'
            'WHOAMI=deploy\n'
            'PWD=/home/deploy\n'
            'SYSTEMCTL=present\n'
            'DEPLOY_EXISTS=1\n'
            'DEPLOY_WRITABLE=0\n'
        )
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


def test_project_service_status_reads_structured_systemd_snapshot(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_observability import _project_service_status

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
        stdout = (
            'LoadState=loaded\n'
            'ActiveState=active\n'
            'SubState=running\n'
            'UnitFileState=enabled\n'
            'FragmentPath=/etc/systemd/system/demo-api.service\n'
            'ExecMainPID=1234\n'
            'ExecMainStatus=0\n'
            'Result=success\n'
            'enabled\n'
        )
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout=stdout, stderr='')

    monkeypatch.setattr('ouroboros.tools.project_server_observability._run_ssh_text', fake_run_ssh_text)

    payload = json.loads(
        _project_service_status(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            timeout=30,
            sudo=False,
        )
    )

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
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout='L1\nL2\nL3\n', stderr='')

    monkeypatch.setattr('ouroboros.tools.project_server_observability._run_ssh_text', fake_run_ssh_text)

    payload = json.loads(
        _project_service_logs(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api.service',
            lines=50,
            timeout=20,
            max_output_chars=5,
            sudo=False,
        )
    )

    assert payload['status'] == 'ok'
    assert payload['logs']['lines_requested'] == 50
    assert payload['logs']['content'] == 'L1\nL2'
    assert payload['result']['truncated'] is True
    assert captured['timeout'] == 20
    assert captured['args'][-1] == 'journalctl -u demo-api.service -n 50 --no-pager --output=short-iso'


def test_project_deploy_status_combines_deploy_probe_and_service_snapshot(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_observability import _project_deploy_status

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

    calls = []

    def fake_run_ssh_text(args, timeout):
        calls.append(args[-1])
        if 'systemctl show' in args[-1]:
            stdout = (
                'LoadState=loaded\n'
                'ActiveState=failed\n'
                'SubState=failed\n'
                'UnitFileState=enabled\n'
                'FragmentPath=/etc/systemd/system/demo-api.service\n'
                'ExecMainPID=0\n'
                'ExecMainStatus=1\n'
                'Result=exit-code\n'
                'enabled\n'
            )
            return subprocess.CompletedProcess(['ssh', *args], 0, stdout=stdout, stderr='')
        stdout = (
            'DEPLOY_EXISTS=1\n'
            'DEPLOY_REALPATH=/srv/demo-api\n'
            'DEPLOY_TOP_LEVEL_COUNT=7\n'
            'DEPLOY_WRITABLE=1\n'
            'DEPLOY_GIT=0\n'
        )
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout=stdout, stderr='')

    monkeypatch.setattr('ouroboros.tools.project_server_observability._run_ssh_text', fake_run_ssh_text)

    payload = json.loads(
        _project_deploy_status(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            timeout=15,
            sudo=False,
        )
    )

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
    assert any('last recorded deploy' not in item for item in payload['diagnostics']['issues'])
    assert payload['diagnostics']['service']['severity'] == 'critical'
def test_project_service_status_reports_missing_unit_diagnostics(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_observability import _project_service_status

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

    def fake_run_ssh_text(args, timeout):
        stdout = (
            'LoadState=not-found\n'
            'ActiveState=inactive\n'
            'SubState=dead\n'
            'UnitFileState=\n'
            'FragmentPath=\n'
            'ExecMainPID=0\n'
            'ExecMainStatus=0\n'
            'Result=success\n'
        )
        return subprocess.CompletedProcess(['ssh', *args], 1, stdout=stdout, stderr='not found\n')

    monkeypatch.setattr('ouroboros.tools.project_server_observability._run_ssh_text', fake_run_ssh_text)

    payload = json.loads(
        _project_service_status(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            timeout=30,
            sudo=False,
        )
    )

    assert payload['status'] == 'error'
    assert payload['service']['exists'] is False
    assert payload['diagnostics']['severity'] == 'critical'
    assert payload['diagnostics']['summary'] == 'systemd unit not found'
    assert any('project_service_control(action=install)' in item for item in payload['diagnostics']['recommended_checks'])



def test_project_service_status_reports_transitional_diagnostics(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_observability import _project_service_status

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

    def fake_run_ssh_text(args, timeout):
        stdout = (
            'LoadState=loaded\n'
            'ActiveState=activating\n'
            'SubState=start-pre\n'
            'UnitFileState=enabled\n'
            'FragmentPath=/etc/systemd/system/demo-api.service\n'
            'ExecMainPID=0\n'
            'ExecMainStatus=0\n'
            'Result=success\n'
            'enabled\n'
        )
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout=stdout, stderr='')

    monkeypatch.setattr('ouroboros.tools.project_server_observability._run_ssh_text', fake_run_ssh_text)

    payload = json.loads(
        _project_service_status(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            timeout=30,
            sudo=False,
        )
    )

    assert payload['status'] == 'ok'
    assert payload['service']['exists'] is True
    assert payload['service']['running'] is False
    assert payload['diagnostics']['severity'] == 'warning'
    assert payload['diagnostics']['summary'] == 'service is in transitional state: activating/start-pre'
    assert any('re-check project_service_status' in item for item in payload['diagnostics']['recommended_checks'])


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


def test_project_deploy_apply_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_deploy_apply" in names


def test_project_deploy_apply_install_runs_sync_setup_install_start_status(tmp_path, monkeypatch):
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

    payload = json.loads(
        _project_deploy_apply(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            mode='install',
            delete=True,
            sync_timeout=90,
            service_timeout=120,
            status_timeout=30,
        )
    )

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

    calls = []

    monkeypatch.setattr(
        'ouroboros.tools.project_deploy._project_server_sync',
        lambda ctx, **kwargs: (calls.append(('sync', kwargs)) or json.dumps({'status': 'ok'}))
    )
    monkeypatch.setattr(
        'ouroboros.tools.project_bootstrap._project_server_run',
        lambda ctx, **kwargs: (calls.append(('setup', kwargs)) or json.dumps({'status': 'ok', 'command': {'raw': kwargs['command']}}))
    )

    def fake_service(ctx, **kwargs):
        calls.append((kwargs['action'], kwargs))
        return json.dumps({'status': 'ok', 'service': {'action': kwargs['action']}})

    monkeypatch.setattr('ouroboros.tools.project_service._project_service_control', fake_service)

    payload = json.loads(
        _project_deploy_apply(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            mode='update',
        )
    )

    assert payload['status'] == 'ok'
    assert [step['key'] for step in payload['steps']] == ['sync', 'setup', 'install_service', 'restart', 'status']
    assert [name for name, _ in calls] == ['sync', 'setup', 'install', 'restart', 'status']
    assert payload['summary']['lifecycle_action'] == 'restart'


def test_project_deploy_apply_dry_run_returns_planned_trace_without_execution(tmp_path, monkeypatch):
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

    def fail_sync(ctx, **kwargs):
        raise AssertionError('sync must not run during dry_run')

    def fail_run(ctx, **kwargs):
        raise AssertionError('setup must not run during dry_run')

    def fail_service(ctx, **kwargs):
        raise AssertionError('service control must not run during dry_run')

    monkeypatch.setattr('ouroboros.tools.project_deploy._project_server_sync', fail_sync)
    monkeypatch.setattr('ouroboros.tools.project_bootstrap._project_server_run', fail_run)
    monkeypatch.setattr('ouroboros.tools.project_service._project_service_control', fail_service)

    payload = json.loads(
        _project_deploy_apply(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            mode='install',
            delete=True,
            dry_run=True,
        )
    )

    assert payload['status'] == 'ok'
    assert payload['dry_run'] is True
    assert payload['execution']['dry_run'] is True
    assert payload['execution']['total_steps'] == 5
    assert payload['execution']['planned_steps'] == 5
    assert payload['execution']['executed_steps'] == 0
    assert payload['execution']['last_step_key'] == 'status'
    assert [step['key'] for step in payload['steps']] == ['sync', 'setup', 'install_service', 'start', 'status']
    assert all(step['status'] == 'planned' for step in payload['steps'])
    assert payload['steps'][0]['args']['delete'] is True
    assert payload['steps'][1]['payload']['setup']['count'] == 3
    assert payload['summary']['lifecycle_action'] == 'start'
    assert payload['summary']['status_ok'] is None
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    assert _project_deploy_state_path(repo_dir).exists() is False



def test_project_deploy_apply_stops_on_setup_failure(tmp_path, monkeypatch):
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

    monkeypatch.setattr(
        'ouroboros.tools.project_deploy._project_server_sync',
        lambda ctx, **kwargs: json.dumps({'status': 'ok', 'result': {'ok': True}})
    )
    monkeypatch.setattr(
        'ouroboros.tools.project_bootstrap._project_server_run',
        lambda ctx, **kwargs: json.dumps({'status': 'error', 'command': {'raw': kwargs['command']}, 'result': {'ok': False, 'exit_code': 1}})
    )

    def fail_if_called(ctx, **kwargs):
        raise AssertionError('service control must not run after setup failure')

    monkeypatch.setattr('ouroboros.tools.project_service._project_service_control', fail_if_called)

    payload = json.loads(
        _project_deploy_apply(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            mode='install',
        )
    )

    assert payload['status'] == 'error'
    assert payload['failed_step'] == 'setup'
    assert payload['execution']['failed_step'] == 'setup'
    assert payload['execution']['executed_steps'] == 2
    assert payload['execution']['error_steps'] == 1
    assert [step['key'] for step in payload['steps']] == ['sync', 'setup']
    assert payload['deploy_record']['outcome']['failed_step'] == 'setup'
    assert payload['deploy_record']['outcome']['deploy']['step_statuses']['setup'] == 'error'


def test_project_deploy_apply_stops_on_sync_failure(tmp_path, monkeypatch):
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

    monkeypatch.setattr(
        'ouroboros.tools.project_deploy._project_server_sync',
        lambda ctx, **kwargs: json.dumps({'status': 'error', 'result': {'ok': False, 'exit_code': 255}})
    )

    def fail_if_called(ctx, **kwargs):
        raise AssertionError('service control must not run after sync failure')

    monkeypatch.setattr('ouroboros.tools.project_service._project_service_control', fail_if_called)

    payload = json.loads(
        _project_deploy_apply(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            mode='install',
        )
    )

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
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    _project_deploy_state_path(repo_dir).parent.mkdir(parents=True, exist_ok=True)
    _project_deploy_state_path(repo_dir).write_text(json.dumps({
        'kind': 'project_deploy_outcome',
        'status': 'ok',
        'failed_step': '',
        'deploy': {'mode': 'update', 'lifecycle_action': 'restart'},
    }), encoding='utf-8')

    calls = []

    def fake_run_ssh_text(args, timeout):
        calls.append(args[-1])
        if 'systemctl show' in args[-1]:
            stdout = (
                'LoadState=loaded\n'
                'ActiveState=active\n'
                'SubState=running\n'
                'UnitFileState=enabled\n'
                'FragmentPath=/etc/systemd/system/demo-api.service\n'
                'ExecMainPID=1234\n'
                'ExecMainStatus=0\n'
                'Result=success\n'
                'enabled\n'
            )
            return subprocess.CompletedProcess(['ssh', *args], 0, stdout=stdout, stderr='')
        stdout = (
            'DEPLOY_EXISTS=1\n'
            'DEPLOY_REALPATH=/srv/demo-api\n'
            'DEPLOY_TOP_LEVEL_COUNT=7\n'
            'DEPLOY_WRITABLE=1\n'
            'DEPLOY_GIT=0\n'
        )
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout=stdout, stderr='')

    monkeypatch.setattr('ouroboros.tools.project_server_observability._run_ssh_text', fake_run_ssh_text)

    payload = json.loads(
        _project_deploy_status(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            timeout=15,
            sudo=False,
        )
    )

    assert payload['status'] == 'ok'
    assert payload['last_deploy']['exists'] is True
    assert payload['last_deploy']['outcome']['status'] == 'ok'
    assert payload['last_deploy']['outcome']['deploy']['mode'] == 'update'
    assert 'execution' in payload['last_deploy']['outcome']['deploy']
    assert len(calls) == 2



def test_read_project_deploy_state_backfills_missing_execution_fields(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    state_path = _project_deploy_state_path(repo_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        'kind': 'project_deploy_outcome',
        'status': 'ok',
        'deploy': {
            'mode': 'update',
            'lifecycle_action': 'restart',
        },
    }), encoding='utf-8')

    payload = _read_project_deploy_state(repo_dir)

    assert payload is not None
    assert payload['deploy']['mode'] == 'update'
    assert payload['deploy']['execution']['dry_run'] is None
    assert payload['deploy']['execution']['total_steps'] is None
    assert payload['deploy']['execution']['last_step_key'] == ''


def test_project_server_management_tools_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_server_update" in names
    assert "project_server_validate" in names


def test_project_server_validate_reports_ready_deploy_target(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_management import _project_server_validate

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

    calls = []

    def fake_run_remote_text(server, command, timeout):
        calls.append(command)
        if 'systemctl show' in command:
            stdout = (
                'LoadState=loaded\n'
                'ActiveState=active\n'
                'SubState=running\n'
                'UnitFileState=enabled\n'
                'Result=success\n'
                'FragmentPath=/etc/systemd/system/demo-api.service\n'
            )
            return subprocess.CompletedProcess(['ssh', command], 0, stdout=stdout, stderr='')
        stdout = (
            'SSH_OK=1\n'
            'WHOAMI=deploy\n'
            'SYSTEMCTL=present\n'
            'DEPLOY_EXISTS=1\n'
            'DEPLOY_WRITABLE=1\n'
            'PARENT_EXISTS=1\n'
            'PARENT_WRITABLE=1\n'
        )
        return subprocess.CompletedProcess(['ssh', command], 0, stdout=stdout, stderr='')

    monkeypatch.setattr('ouroboros.tools.project_server_management._run_remote_text', fake_run_remote_text)

    payload = json.loads(
        _project_server_validate(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            timeout=25,
            sudo=False,
        )
    )

    assert payload['status'] == 'ok'
    assert payload['validation']['ok'] is True
    assert payload['validation']['checks']['ssh'] is True
    assert payload['validation']['checks']['deploy_path_ready'] is True
    assert payload['validation']['checks']['service_unit_exists'] is True
    assert payload['service']['unit_name'] == 'demo-api.service'
    assert len(calls) == 2


def test_project_server_validate_reports_not_ready_when_parent_is_not_writable(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_management import _project_server_validate

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

    def fake_run_remote_text(server, command, timeout):
        stdout = (
            'SSH_OK=1\n'
            'WHOAMI=deploy\n'
            'SYSTEMCTL=present\n'
            'DEPLOY_EXISTS=0\n'
            'DEPLOY_WRITABLE=0\n'
            'PARENT_EXISTS=1\n'
            'PARENT_WRITABLE=0\n'
        )
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

    def fake_run_remote_text(server, command, timeout):
        stdout = (
            'SSH_OK=1\n'
            'WHOAMI=deploy\n'
            'SYSTEMCTL=present\n'
            'DEPLOY_EXISTS=0\n'
            'DEPLOY_WRITABLE=0\n'
            'PARENT_EXISTS=1\n'
            'PARENT_WRITABLE=1\n'
        )
        return subprocess.CompletedProcess(['ssh', command], 0, stdout=stdout, stderr='')

    monkeypatch.setattr('ouroboros.tools.project_server_management._run_remote_text', fake_run_remote_text)

    payload = json.loads(_project_server_validate(_ctx(tmp_path), name='demo-api', alias='prod'))

    assert payload['status'] == 'ok'
    assert payload['validation']['checks']['deploy_path_exists'] is False
    assert payload['validation']['checks']['deploy_parent_writable'] is True
    assert payload['validation']['checks']['deploy_path_ready'] is True


def test_project_server_validate_reports_not_ready_when_service_is_missing(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_management import _project_server_validate

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

    calls = []

    def fake_run_remote_text(server, command, timeout):
        calls.append(command)
        if 'systemctl show' in command:
            stdout = (
                'LoadState=not-found\n'
                'ActiveState=inactive\n'
                'SubState=dead\n'
                'UnitFileState=\n'
                'Result=success\n'
                'FragmentPath=\n'
            )
            return subprocess.CompletedProcess(['ssh', command], 0, stdout=stdout, stderr='')
        stdout = (
            'SSH_OK=1\n'
            'WHOAMI=deploy\n'
            'SYSTEMCTL=present\n'
            'DEPLOY_EXISTS=1\n'
            'DEPLOY_WRITABLE=1\n'
            'PARENT_EXISTS=1\n'
            'PARENT_WRITABLE=1\n'
        )
        return subprocess.CompletedProcess(['ssh', command], 0, stdout=stdout, stderr='')

    monkeypatch.setattr('ouroboros.tools.project_server_management._run_remote_text', fake_run_remote_text)

    payload = json.loads(
        _project_server_validate(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
        )
    )

    assert payload['status'] == 'error'
    assert payload['validation']['ok'] is False
    assert payload['validation']['checks']['service_unit_exists'] is False
    assert len(calls) == 2




def test_read_project_deploy_state_rejects_invalid_json(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    state_path = _project_deploy_state_path(repo_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text('{invalid json\n', encoding='utf-8')

    with pytest.raises(RuntimeError, match='invalid JSON'):
        _read_project_deploy_state(repo_dir)


def test_project_deploy_status_surfaces_missing_deploy_record_and_path_problem(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_observability import _project_deploy_status

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

    def fake_run_ssh_text(args, timeout):
        stdout = (
            'DEPLOY_EXISTS=0\n'
            'DEPLOY_REALPATH=\n'
            'DEPLOY_TOP_LEVEL_COUNT=0\n'
            'DEPLOY_WRITABLE=0\n'
            'DEPLOY_GIT=0\n'
        )
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout=stdout, stderr='')

    monkeypatch.setattr('ouroboros.tools.project_server_observability._run_ssh_text', fake_run_ssh_text)

    payload = json.loads(
        _project_deploy_status(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            timeout=15,
            sudo=False,
        )
    )

    assert payload['status'] == 'ok'
    assert payload['last_deploy']['exists'] is False
    assert payload['diagnostics']['severity'] == 'warning'
    assert 'deploy path missing' in payload['diagnostics']['summary']
    assert 'no recorded deploy outcome' in payload['diagnostics']['summary']
    assert any('project_server_validate' in item for item in payload['diagnostics']['recommended_checks'])
    assert any('project_deploy_apply' in item for item in payload['diagnostics']['recommended_checks'])
