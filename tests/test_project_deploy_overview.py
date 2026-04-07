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
def test_project_overview_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_overview' in names

def test_project_overview_returns_unified_local_snapshot_without_runtime(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
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
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    remote_res = _git(['remote', 'add', 'origin', 'git@github.com:example/demo-api.git'], repo_dir, timeout=30)
    assert remote_res.returncode == 0
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')

    def fake_run_project_gh_json(repo_dir, args, timeout=120):
        if args[:2] == ['issue', 'list']:
            return [{'number': 1, 'title': 'Bug', 'state': 'OPEN', 'url': 'https://github.com/example/demo-api/issues/1', 'author': {'login': 'alice'}, 'labels': []}]
        if args[:2] == ['pr', 'list']:
            return [{'number': 2, 'title': 'Fix', 'state': 'OPEN', 'headRefName': 'fix', 'baseRefName': 'main', 'url': 'https://github.com/example/demo-api/pull/2', 'isDraft': False, 'author': {'login': 'bob'}}]
        raise AssertionError(args)

    def fake_project_deploy_recipe(ctx, name, alias, service_name, **kwargs):
        return json.dumps({'generated_at': '2026-03-13T14:30:00Z', 'runtime': {'requested': 'auto', 'resolved': 'python'}, 'server': {'alias': alias, 'deploy_path': '/srv/demo-api'}, 'recipe': {'kind': 'project_deploy_recipe', 'steps': [{'key': 'sync'}, {'key': 'setup'}, {'key': 'install_service'}]}})

    def fake_project_deploy_status(ctx, name, alias, service_name, **kwargs):
        return json.dumps({'status': 'ok', 'deploy': {'exists': True, 'writable': True, 'path': '/srv/demo-api'}, 'service': {'unit_name': 'demo-api.service', 'active_state': 'active', 'running': True}, 'diagnostics': {'severity': 'healthy', 'summary': 'service is running'}, 'last_deploy': {'exists': False, 'outcome': None}})
    monkeypatch.setattr('ouroboros.tools.project_overview._run_project_gh_json', fake_run_project_gh_json)
    monkeypatch.setattr('ouroboros.tools.project_overview._project_deploy_recipe', fake_project_deploy_recipe)
    monkeypatch.setattr('ouroboros.tools.project_overview._project_deploy_status', fake_project_deploy_status)
    payload = json.loads(_project_overview(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', include_runtime=True, issue_limit=5, pr_limit=5))
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
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')
    state_path = _project_deploy_state_path(repo_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({'status': 'error', 'failed_step': 'setup', 'deploy': {'execution': {'last_step_key': 'setup'}}}), encoding='utf-8')

    def fake_project_deploy_status(ctx, name, alias, service_name, **kwargs):
        return json.dumps({'status': 'error', 'deploy': {'exists': True, 'writable': True, 'path': '/srv/demo-api'}, 'service': {'unit_name': 'demo-api.service', 'active_state': 'failed', 'running': False, 'exists': True}, 'diagnostics': {'severity': 'critical', 'summary': 'service is failed', 'recommended_checks': ['read project_service_logs for the most recent journal output']}, 'last_deploy': {'status': 'error', 'failed_step': 'setup'}})
    monkeypatch.setattr('ouroboros.tools.project_overview._project_deploy_status', fake_project_deploy_status)
    payload = json.loads(_project_overview(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api', include_runtime=True))
    assert payload['summary']['last_deploy_status'] == 'error'
    assert payload['summary']['diagnostic_severity'] == 'critical'
    assert 'resolve the last failed deploy step: setup' in payload['next_actions']
    assert 'read project_service_logs for the most recent journal output' in payload['next_actions']

def test_project_operational_snapshot_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_operational_snapshot' in names

def test_project_operational_snapshot_focuses_rollout_readiness(tmp_path, monkeypatch):
    from ouroboros.tools.project_operational_snapshot import _project_operational_snapshot
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(_ctx(tmp_path), name='demo-api', alias='prod', host='example.com', user='deploy', ssh_key_path='~/id_test', deploy_path='/srv/demo-api')

    def fake_run_project_gh_json(repo_dir, args, timeout=120):
        if args[:2] == ['issue', 'list']:
            return [{'number': 1}, {'number': 2}]
        if args[:2] == ['pr', 'list']:
            return [{'number': 7}]
        raise AssertionError(args)

    def fake_project_deploy_status(ctx, name, alias, service_name, **kwargs):
        return json.dumps({'status': 'error', 'deploy': {'exists': True, 'writable': False, 'path': '/srv/demo-api'}, 'service': {'unit_name': 'demo-api.service', 'active_state': 'failed', 'running': False, 'exists': True}, 'diagnostics': {'severity': 'critical', 'summary': 'service is failed', 'recommended_checks': ['read project_service_logs for the most recent journal output']}})
    monkeypatch.setattr('ouroboros.tools.project_operational_snapshot._project_github_slug', lambda repo_dir: 'example/demo-api')
    monkeypatch.setattr('ouroboros.tools.project_operational_snapshot._run_project_gh_json', fake_run_project_gh_json)
    monkeypatch.setattr('ouroboros.tools.project_operational_snapshot._project_deploy_status', fake_project_deploy_status)
    payload = json.loads(_project_operational_snapshot(_ctx(tmp_path), name='demo-api', alias='prod', service_name='demo-api'))
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

def test_project_operational_snapshot_without_runtime_stays_local(tmp_path, monkeypatch):
    from ouroboros.tools.project_operational_snapshot import _project_operational_snapshot
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    payload = json.loads(_project_operational_snapshot(_ctx(tmp_path), name='demo-api'))
    assert payload['selection']['runtime_included'] is False
    assert payload['readiness']['deploy_target_ready'] is None
    assert payload['readiness']['service_running'] is None
    assert payload['readiness']['rollout_ready'] is True
    assert payload['github']['configured'] is False
    assert payload['risk_flags'] == []
    assert 'attach a GitHub origin with project_github_create' in payload['next_actions']
