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


def test_fleet_health_filters_by_tags_and_aggregates_extended_modules(monkeypatch, tmp_path):
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
        known_ports=[443, 8443],
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
        return json.dumps({'status': 'ok', 'verdict': 'ok', 'summary': f'health:{alias}'})

    def fake_xray(_ctx, alias):
        calls.append(('xray', alias))
        return json.dumps({'status': 'ok', 'managed_by': 'x-ui.service', 'state': 'running'})

    def fake_panel(_ctx, alias):
        calls.append(('panel', alias))
        return json.dumps({'status': 'ok', 'verdict': 'ok', 'inbounds_count': 4, 'enabled_inbounds_count': 3})

    def fake_extended(_ctx, alias, target):
        calls.append(('extended', alias, tuple(target.get('known_ports') or [])))
        return {
            'status': 'ok',
            'overall_verdict': 'warn',
            'module_status_counts': {'ok': 13, 'skip': 0, 'warn': 2, 'critical': 0},
            'issues': [
                {'module': 'conntrack_status', 'severity': 'warn', 'code': 'conntrack_high', 'message': 'usage 75%'},
                {'module': 'network_tuning_check', 'severity': 'warn', 'code': 'bbr_disabled', 'message': 'reno'},
            ],
            'modules': {
                'conntrack_status': {'status': 'warn', 'summary': 'usage 75%'},
                'network_tuning_check': {'status': 'warn', 'summary': 'bbr disabled'},
                'backup_freshness': {'status': 'ok', 'summary': 'fresh'},
            },
        }

    monkeypatch.setattr(fleet_health_module, '_remote_server_health', fake_health)
    monkeypatch.setattr(fleet_health_module, '_remote_xray_status', fake_xray)
    monkeypatch.setattr(fleet_health_module, '_xui_panel_status', fake_panel)
    monkeypatch.setattr(fleet_health_module, '_run_extended_checks', fake_extended)

    payload = json.loads(fleet_health_module.fleet_health(ctx, tags=['edge'], max_workers=2))

    assert payload['status'] == 'ok'
    assert payload['summary']['matched_targets'] == 1
    assert payload['summary']['overall_verdict'] == 'warn'
    assert payload['summary']['module_status_totals']['conntrack_status']['warn'] == 1
    assert payload['summary']['issue_count'] == 2
    assert payload['targets'][0]['alias'] == 'edge-1'
    assert payload['targets'][0]['checks']['extended']['overall_verdict'] == 'warn'
    assert payload['targets'][0]['checks']['extended']['modules']['network_tuning_check']['status'] == 'warn'
    assert ('health', 'edge-1') in calls
    assert ('panel', 'edge-1') in calls
    assert ('xray', 'edge-1') in calls
    assert ('extended', 'edge-1', (443, 8443)) in calls
    assert all(entry[1] != 'lab-1' for entry in calls if len(entry) > 1)


def test_fleet_health_warns_when_panel_metadata_missing(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _ssh_target_register(
        ctx,
        alias='edge-2',
        host='203.0.113.20',
        user='root',
        auth_mode='password',
        password='secret',
        panel_type='3x-ui',
        tags=['vpn'],
    )

    monkeypatch.setattr(fleet_health_module, '_remote_server_health', lambda *_args, **_kwargs: json.dumps({'status': 'ok', 'verdict': 'ok'}))
    monkeypatch.setattr(fleet_health_module, '_remote_xray_status', lambda *_args, **_kwargs: json.dumps({'status': 'ok', 'state': 'running'}))
    monkeypatch.setattr(
        fleet_health_module,
        '_run_extended_checks',
        lambda *_args, **_kwargs: {
            'status': 'ok',
            'overall_verdict': 'ok',
            'module_status_counts': {'ok': 15, 'skip': 0, 'warn': 0, 'critical': 0},
            'issues': [],
            'modules': {'backup_freshness': {'status': 'ok', 'summary': 'fresh'}},
        },
    )

    payload = json.loads(fleet_health_module.fleet_health(ctx, tags=['vpn']))
    target = payload['targets'][0]
    assert payload['summary']['overall_verdict'] == 'warn'
    assert target['checks']['panel']['status'] == 'missing_config'
    assert any(issue['code'] == 'panel_url_missing' for issue in target['issues'])


def test_fleet_health_handles_extended_probe_failure(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _ssh_target_register(
        ctx,
        alias='edge-3',
        host='203.0.113.30',
        user='root',
        auth_mode='password',
        password='secret',
        tags=['vpn'],
    )

    monkeypatch.setattr(fleet_health_module, '_remote_server_health', lambda *_args, **_kwargs: json.dumps({'status': 'ok', 'verdict': 'ok'}))
    monkeypatch.setattr(fleet_health_module, '_remote_xray_status', lambda *_args, **_kwargs: json.dumps({'status': 'ok', 'state': 'running'}))
    monkeypatch.setattr(fleet_health_module, '_run_extended_checks', lambda *_args, **_kwargs: {'status': 'error', 'error': 'python3 missing'})

    payload = json.loads(fleet_health_module.fleet_health(ctx, tags=['vpn'], include_panel=False))
    target = payload['targets'][0]
    assert payload['summary']['overall_verdict'] == 'warn'
    assert target['checks']['extended']['status'] == 'error'
    assert any(issue['code'] == 'extended_checks_failed' for issue in target['issues'])


def test_fleet_health_returns_empty_match_payload(tmp_path):
    ctx = _ctx(tmp_path)
    payload = json.loads(fleet_health_module.fleet_health(ctx, tags=['missing']))
    assert payload['status'] == 'ok'
    assert payload['summary']['matched_targets'] == 0
    assert payload['summary']['overall_verdict'] == 'ok'
    assert payload['summary']['module_status_totals']['backup_freshness']['warn'] == 0
