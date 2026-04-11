import importlib
import json
import pathlib

from ouroboros.tools.registry import ToolContext, ToolRegistry
from ouroboros.tools.ssh_targets import _ssh_target_register

fleet_health_module = importlib.import_module('ouroboros.tools.fleet_health')


def _schema_names(registry: ToolRegistry) -> set[str]:
    names: set[str] = set()
    for schema in registry.schemas():
        fn = schema.get('function') or {}
        name = fn.get('name') or schema.get('name')
        if name:
            names.add(name)
    return names


def _ctx(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path)


def test_fleet_health_tool_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    assert 'fleet_health' in _schema_names(registry)


def test_fleet_health_filters_by_tags_and_aggregates(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _ssh_target_register(
        ctx,
        alias='edge-1',
        host='203.0.113.10',
        user='root',
        auth_mode='password',
        password='secret',
        panel_type='3x-ui',
        panel_url='https://panel.example.com/base/',
        panel_username='admin',
        panel_password='adminpass',
        tags=['edge', 'ru'],
    )
    _ssh_target_register(
        ctx,
        alias='lab-1',
        host='203.0.113.11',
        user='root',
        auth_mode='password',
        password='secret',
        tags=['lab'],
    )

    calls = []

    def fake_health(_ctx, alias):
        calls.append(('health', alias))
        return json.dumps({'status': 'ok', 'verdict': 'ok'})

    def fake_xray(_ctx, alias):
        calls.append(('xray', alias))
        return json.dumps({'status': 'ok', 'state': 'running', 'managed_by': 'x-ui.service'})

    def fake_panel(_ctx, alias):
        calls.append(('panel', alias))
        return json.dumps(
            {
                'status': 'ok',
                'inbounds': [
                    {'id': 1, 'enable': True},
                    {'id': 2, 'enable': False},
                ],
            }
        )

    monkeypatch.setattr(fleet_health_module, '_remote_server_health', fake_health)
    monkeypatch.setattr(fleet_health_module, '_remote_xray_status', fake_xray)
    monkeypatch.setattr(fleet_health_module, '_xui_panel_status', fake_panel)

    payload = json.loads(fleet_health_module.fleet_health(ctx, tags=['edge'], max_workers=2))

    assert payload['status'] == 'ok'
    assert payload['summary']['matched_targets'] == 1
    assert payload['summary']['by_verdict']['ok'] == 1
    assert payload['summary']['overall_verdict'] == 'ok'
    assert payload['targets'][0]['alias'] == 'edge-1'
    assert payload['targets'][0]['verdict'] == 'ok'
    assert payload['targets'][0]['checks']['panel']['enabled_inbounds'] == 1
    assert ('panel', 'edge-1') in calls
    assert all(alias != 'lab-1' for _, alias in calls)




def test_fleet_health_infers_running_xray_from_real_payload_shape(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _ssh_target_register(
        ctx,
        alias='spacecore-94',
        host='94.156.122.66',
        user='root',
        auth_mode='password',
        password='secret',
        tags=['vpn'],
    )

    monkeypatch.setattr(fleet_health_module, '_remote_server_health', lambda _ctx, alias: json.dumps({'status': 'ok', 'verdict': 'ok'}))
    monkeypatch.setattr(
        fleet_health_module,
        '_remote_xray_status',
        lambda _ctx, alias: json.dumps(
            {
                'status': 'ok',
                'managed_by': 'x-ui.service',
                'xray_process_present': True,
                'services': [
                    {
                        'service_name': 'x-ui.service',
                        'service': {'active_state': 'active', 'sub_state': 'running'},
                    },
                    {
                        'service_name': 'xray.service',
                        'service': {'active_state': 'inactive', 'sub_state': 'dead'},
                    },
                ],
            }
        ),
    )

    payload = json.loads(fleet_health_module.fleet_health(ctx, tags=['vpn'], include_panel=False))
    target = payload['targets'][0]
    assert target['verdict'] == 'ok'
    assert target['checks']['xray']['state'] == 'running'
    assert not any(issue['code'] == 'xray_state_unknown' for issue in target['issues'])

def test_fleet_health_marks_critical_and_warn(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _ssh_target_register(
        ctx,
        alias='down-box',
        host='203.0.113.20',
        user='root',
        auth_mode='password',
        password='secret',
        tags=['edge'],
    )
    _ssh_target_register(
        ctx,
        alias='panel-box',
        host='203.0.113.21',
        user='root',
        auth_mode='password',
        password='secret',
        panel_type='3x-ui',
        panel_url='https://panel.example.com/base/',
        tags=['edge'],
    )

    def fake_health(_ctx, alias):
        if alias == 'down-box':
            return json.dumps({'status': 'error', 'error': 'ssh timeout'})
        return json.dumps({'status': 'ok', 'verdict': 'ok'})

    def fake_xray(_ctx, alias):
        if alias == 'panel-box':
            return json.dumps({'status': 'ok', 'state': 'stopped'})
        return json.dumps({'status': 'ok', 'state': 'running'})

    monkeypatch.setattr(fleet_health_module, '_remote_server_health', fake_health)
    monkeypatch.setattr(fleet_health_module, '_remote_xray_status', fake_xray)
    monkeypatch.setattr(
        fleet_health_module,
        '_xui_panel_status',
        lambda _ctx, alias: json.dumps({'status': 'ok', 'inbounds': []}),
    )

    payload = json.loads(fleet_health_module.fleet_health(ctx, tags=['edge']))
    targets = {item['alias']: item for item in payload['targets']}

    assert payload['summary']['by_verdict']['critical'] == 1
    assert payload['summary']['by_verdict']['warn'] == 1
    assert payload['summary']['overall_verdict'] == 'critical'
    assert targets['down-box']['verdict'] == 'critical'
    assert any(issue['code'] == 'ssh_health_failed' for issue in targets['down-box']['issues'])
    assert targets['panel-box']['verdict'] == 'warn'
    assert any(issue['code'] == 'xray_not_running' for issue in targets['panel-box']['issues'])
    assert any(issue['code'] == 'panel_credentials_missing' for issue in targets['panel-box']['issues'])
