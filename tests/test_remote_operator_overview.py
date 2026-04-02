import json
import pathlib

from ouroboros.tools.registry import ToolContext, ToolRegistry


def _schema_names(registry: ToolRegistry) -> set[str]:
    names = set()
    for item in registry.schemas():
        fn = item.get('function') if isinstance(item, dict) else None
        if isinstance(fn, dict):
            if 'name' in fn:
                names.add(fn['name'])
            elif isinstance(fn.get('function'), dict) and 'name' in fn['function']:
                names.add(fn['function']['name'])
        elif isinstance(item, dict) and 'name' in item:
            names.add(item['name'])
    return names

from ouroboros.tools.remote_operator_overview import remote_capabilities_overview
from ouroboros.tools.ssh_targets import _ssh_target_register


def _ctx(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path)


def test_remote_capabilities_overview_tool_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = _schema_names(registry)
    assert 'remote_capabilities_overview' in names


def test_remote_capabilities_overview_summarizes_targets_and_workflows(tmp_path):
    ctx = _ctx(tmp_path)
    _ssh_target_register(
        ctx,
        alias='prod-box',
        host='203.0.113.10',
        user='root',
        auth_mode='password',
        password='secret',
        default_remote_root='/srv',
        known_projects_paths=['/srv/ghost'],
        label='Production box',
    )

    payload = json.loads(remote_capabilities_overview(ctx))

    assert payload['status'] == 'ok'
    assert payload['summary']['registered_target_count'] == 1
    assert payload['summary']['default_mode'] == 'read_only_first'
    assert payload['targets'][0]['alias'] == 'prod-box'
    assert payload['targets'][0]['has_recommended_root'] is True
    assert 'remote_project_fetch' in payload['policy']['mutating_tools']
    assert 'remote_service_action' in payload['policy']['mutating_tools']
    assert 'remote_command_exec' in payload['policy']['read_only_tools']
    assert 'remote_service_status' in payload['policy']['read_only_tools']
    assert payload['recommended_workflows'][0]['key'] == 'target_bootstrap'
    assert 'ssh_key_generate' in payload['recommended_workflows'][0]['steps']
    assert 'ssh_key_deploy' in payload['recommended_workflows'][0]['steps']
    assert any(step == 'remote_investigate_project' for step in payload['recommended_workflows'][-1]['steps'])
