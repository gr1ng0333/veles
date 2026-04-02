import json
import pathlib

from ouroboros.tools.registry import ToolContext, ToolRegistry
from ouroboros.tools.remote_service import (
    _remote_server_health,
    _remote_service_action,
    _remote_service_list,
    _remote_service_logs,
    _remote_service_status,
)
from ouroboros.tools.ssh_targets import _ssh_target_register


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


def test_remote_service_tools_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = _schema_names(registry)
    expected = {
        'remote_service_status',
        'remote_service_action',
        'remote_service_logs',
        'remote_service_list',
        'remote_server_health',
    }
    assert expected.issubset(names)


def test_remote_service_status_action_logs_and_list(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _ssh_target_register(
        ctx,
        alias='prod-box',
        host='203.0.113.10',
        user='root',
        auth_mode='password',
        password='secret',
    )

    monkeypatch.setattr('ouroboros.tools.remote_service._bootstrap_session', lambda *args, **kwargs: {'status': 'ok'})

    def fake_run_remote_command(_ctx, alias, command, *, timeout):
        assert alias == 'prod-box'
        if 'systemctl show' in command:
            return {
                'returncode': 0,
                'stdout': (
                    'Id=nginx.service\n'
                    'Description=A high performance web server\n'
                    'LoadState=loaded\n'
                    'ActiveState=active\n'
                    'SubState=running\n'
                    'UnitFileState=enabled\n'
                    'FragmentPath=/usr/lib/systemd/system/nginx.service\n'
                    'MainPID=123\n'
                    'ExecMainStatus=0\n'
                    'ExecMainCode=0\n'
                    'ActiveEnterTimestamp=Wed 2026-04-02 08:00:00 UTC\n'
                ),
                'stderr': '',
            }
        if 'journalctl -u' in command:
            return {
                'returncode': 0,
                'stdout': (
                    '2026-04-02T08:00:00Z host nginx[123]: started\n'
                    '2026-04-02T08:01:00Z host nginx[123]: reloaded\n'
                ),
                'stderr': '',
            }
        if 'systemctl list-units' in command:
            return {
                'returncode': 0,
                'stdout': (
                    'nginx.service loaded active running A high performance web server\n'
                    'ssh.service loaded active running OpenBSD Secure Shell server\n'
                ),
                'stderr': '',
            }
        if 'systemctl restart' in command:
            return {
                'returncode': 0,
                'stdout': '',
                'stderr': '',
            }
        raise AssertionError(command)

    monkeypatch.setattr('ouroboros.tools.remote_service._run_remote_command', fake_run_remote_command)

    status_payload = json.loads(_remote_service_status(ctx, 'prod-box', 'nginx.service'))
    assert status_payload['status'] == 'ok'
    assert status_payload['details']['active_state'] == 'active'
    assert status_payload['details']['unit'] == 'nginx.service'

    action_payload = json.loads(_remote_service_action(ctx, 'prod-box', 'nginx.service', action='restart'))
    assert action_payload['status'] == 'ok'
    assert action_payload['action'] == 'restart'

    logs_payload = json.loads(_remote_service_logs(ctx, 'prod-box', 'nginx.service', lines=2))
    assert logs_payload['status'] == 'ok'
    assert 'started' in logs_payload['logs']

    list_payload = json.loads(_remote_service_list(ctx, 'prod-box'))
    assert list_payload['status'] == 'ok'
    assert list_payload['count'] == 2
    assert list_payload['services'][0]['unit'] == 'nginx.service'
    assert list_payload['services'][1]['description'] == 'OpenBSD Secure Shell server'

    event_types = [event['type'] for event in ctx.pending_events]
    assert event_types == [
        'remote_service_status',
        'remote_service_action',
        'remote_service_logs',
        'remote_service_list',
    ]


def test_remote_service_action_validation_and_error_audit(tmp_path):
    ctx = _ctx(tmp_path)
    payload = json.loads(_remote_service_action(ctx, 'prod-box', 'nginx.service', action='bounce'))
    assert payload['status'] == 'error'
    assert payload['kind'] == 'invalid_action'
    assert ctx.pending_events[-1]['type'] == 'remote_service_action'
    assert ctx.pending_events[-1]['status'] == 'error'


def test_remote_server_health_collects_metrics_ports_services_and_tls(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _ssh_target_register(
        ctx,
        alias='prod-box',
        host='203.0.113.10',
        user='root',
        auth_mode='password',
        password='secret',
        known_services=['nginx.service'],
        known_ports=[22, 443],
        known_tls_domains=['example.com'],
    )

    monkeypatch.setattr('ouroboros.tools.remote_service._bootstrap_session', lambda *args, **kwargs: {'status': 'ok'})

    def fake_run_remote_command(_ctx, alias, command, *, timeout):
        assert alias == 'prod-box'
        if '__UPTIME__' in command:
            return {
                'returncode': 0,
                'stdout': (
                    '__UPTIME__\n'
                    '12345.67 8910.11\n'
                    '__LOADAVG__\n'
                    '0.15 0.20 0.25 1/100 1234\n'
                    '__DF__\n'
                    'Filesystem 1024-blocks Used Available Capacity Mounted on\n'
                    '/dev/sda1 100000 40000 60000 40% /\n'
                    '__FREE__\n'
                    '              total        used        free      shared  buff/cache   available\n'
                    'Mem:      1000000000   400000000   100000000    10000000   500000000   600000000\n'
                    '__PORTS__\n'
                    'LISTEN 0 511 0.0.0.0:22 0.0.0.0:*\n'
                    'LISTEN 0 511 0.0.0.0:443 0.0.0.0:*\n'
                ),
                'stderr': '',
            }
        if 'systemctl show' in command:
            return {
                'returncode': 0,
                'stdout': (
                    'Id=nginx.service\n'
                    'Description=A high performance web server\n'
                    'LoadState=loaded\n'
                    'ActiveState=active\n'
                    'SubState=running\n'
                    'UnitFileState=enabled\n'
                    'FragmentPath=/usr/lib/systemd/system/nginx.service\n'
                    'MainPID=123\n'
                    'ExecMainStatus=0\n'
                    'ExecMainCode=0\n'
                    'ActiveEnterTimestamp=Wed 2026-04-02 08:00:00 UTC\n'
                ),
                'stderr': '',
            }
        raise AssertionError(command)

    monkeypatch.setattr('ouroboros.tools.remote_service._run_remote_command', fake_run_remote_command)
    monkeypatch.setattr(
        'ouroboros.tools.remote_service._check_tls_domain',
        lambda domain, timeout_sec=5: {
            'domain': domain,
            'port': 443,
            'status': 'ok',
            'ok': True,
            'days_remaining': 50,
            'expires_at': '2026-05-22T00:00:00+00:00',
        },
    )

    payload = json.loads(_remote_server_health(ctx, 'prod-box'))

    assert payload['status'] == 'ok'
    assert payload['summary']['overall_status'] == 'ok'
    assert payload['summary']['red_flags'] == 0
    assert payload['system']['root_disk']['used_pct'] == 40
    assert payload['system']['memory']['available_pct'] == 60.0
    assert payload['system']['listening_ports'] == [22, 443]
    assert payload['ports'][0]['ok'] is True
    assert payload['services'][0]['ok'] is True
    assert payload['tls'][0]['ok'] is True
    assert any(event.get('type') == 'remote_server_health' for event in ctx.pending_events)
