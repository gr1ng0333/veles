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

def test_stage3_readme_mentions_project_deploy_and_verify():
    readme = pathlib.Path(__file__).resolve().parent.parent / 'README.md'
    text = readme.read_text(encoding='utf-8')
    assert '`project_deploy_and_verify`' in text

def test_project_bootstrap_and_publish_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
    assert 'project_bootstrap_and_publish' in names

def test_project_bootstrap_and_publish_returns_bootstrap_publish_and_overview_layers(tmp_path, monkeypatch):
    from ouroboros.tools.project_composite_flows import _project_bootstrap_and_publish

    def fake_project_init(ctx, **kwargs):
        return json.dumps({'status': 'ok', 'project': {'name': 'demo-api', 'language': 'python'}, 'repo': {'branch': 'main'}, 'commit_message': 'Bootstrap demo-api'})

    def fake_project_github_create(ctx, **kwargs):
        return json.dumps({'status': 'ok', 'project': {'name': 'demo-api', 'path': str(tmp_path / 'projects' / 'demo-api')}, 'github': {'slug': 'acme/demo-api', 'remote': 'git@github.com:acme/demo-api.git'}})

    def fake_project_overview(ctx, **kwargs):
        return json.dumps({'status': 'ok', 'project': {'name': 'demo-api', 'path': str(tmp_path / 'projects' / 'demo-api')}, 'github': {'configured': True, 'repo': 'acme/demo-api'}, 'summary': {'github_configured': True, 'working_tree_clean': True, 'registered_server_count': 0, 'meaningful_working_tree_change_count': 0}, 'next_actions': ['register at least one deploy target with project_server_register']})
    monkeypatch.setattr('ouroboros.tools.project_composite_flows._project_init', fake_project_init)
    monkeypatch.setattr('ouroboros.tools.project_composite_flows._project_github_create', fake_project_github_create)
    monkeypatch.setattr('ouroboros.tools.project_composite_flows._project_overview', fake_project_overview)
    payload = json.loads(_project_bootstrap_and_publish(_ctx(tmp_path), name='Demo API', language='python', owner='acme', private=True, description='Demo API'))
    assert payload['status'] == 'ok'
    assert payload['selection'] == {'name': 'demo-api', 'language': 'python', 'github_name': '', 'owner': 'acme', 'private': True}
    assert [step['key'] for step in payload['steps']] == ['project_init', 'github_create', 'project_overview']
    assert payload['verdict']['ready'] is True
    assert payload['verdict']['github_repo'] == 'acme/demo-api'
    assert payload['verdict']['next_actions'] == ['register at least one deploy target with project_server_register']

def test_stage3_readme_mentions_project_bootstrap_and_publish():
    readme = pathlib.Path(__file__).resolve().parent.parent / 'README.md'
    text = readme.read_text(encoding='utf-8')
    assert '`project_bootstrap_and_publish`' in text
