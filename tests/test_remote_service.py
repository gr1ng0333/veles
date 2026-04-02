import json
import pathlib

from ouroboros.tools.registry import ToolContext, ToolRegistry
from ouroboros.tools.remote_service import (
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
