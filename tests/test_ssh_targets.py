import json
import pathlib
import subprocess

import pytest

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

from ouroboros.tools.ssh_targets import (
    _SESSION_CACHE,
    SshConnectionError,
    _normalize_probe_error,
    _normalize_target_record,
    _public_target_view,
    _run_ssh_probe,
    _ssh_session_bootstrap,
    _ssh_target_get,
    _ssh_target_list,
    _ssh_target_register,
    _ssh_target_update,
)


def _ctx(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path)


@pytest.fixture(autouse=True)
def _clear_cache():
    _SESSION_CACHE.clear()
    yield
    _SESSION_CACHE.clear()


def test_ssh_tools_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = _schema_names(registry)
    assert 'ssh_target_register' in names
    assert 'ssh_target_list' in names
    assert 'ssh_target_get' in names
    assert 'ssh_target_update' in names
    assert 'ssh_session_bootstrap' in names
    assert 'ssh_target_ping' in names


def test_normalize_target_record_for_key_auth_expands_key_path():
    record = _normalize_target_record(
        alias='Test Box',
        host='example.com',
        port=2222,
        user='root',
        auth_mode='key',
        ssh_key_path='~/.ssh/test_key',
        known_projects_paths=[' /srv/app ', '', '/opt/work '],
    )
    assert record['alias'] == 'test-box'
    assert record['port'] == 2222
    assert record['auth_mode'] == 'key'
    assert record['ssh_key_path'].endswith('/.ssh/test_key')
    assert record['known_projects_paths'] == ['/srv/app', '/opt/work']


def test_normalize_target_record_rejects_missing_password_for_password_auth():
    with pytest.raises(ValueError, match='password is required'):
        _normalize_target_record(
            alias='demo',
            host='example.com',
            user='root',
            auth_mode='password',
        )


def test_public_target_view_hides_password():
    record = _normalize_target_record(
        alias='demo',
        host='example.com',
        user='root',
        auth_mode='password',
        password='secret',
    )
    public = _public_target_view(record)
    assert public['has_password'] is True
    assert 'password' not in public


def test_normalize_target_record_accepts_registry_metadata():
    record = _normalize_target_record(
        alias='demo-node',
        host='example.com',
        user='root',
        auth_mode='key',
        ssh_key_path='~/.ssh/test_key',
        provider='spacecore',
        location='nl-ams',
        panel_type='3x-ui',
        panel_url='https://demo.example.com/panel/',
        tags=['vpn', 'eu-west', 'vpn'],
        status='warn',
        last_health_at='2026-04-11T13:00:00+00:00',
        legacy_aliases=['demo-old', 'demo-legacy'],
    )
    assert record['provider'] == 'spacecore'
    assert record['location'] == 'nl-ams'
    assert record['panel_type'] == '3x-ui'
    assert record['panel_url'] == 'https://demo.example.com/panel/'
    assert record['tags'] == ['vpn', 'eu-west']
    assert record['status'] == 'warn'
    assert record['legacy_aliases'] == ['demo-old', 'demo-legacy']


def test_ssh_target_register_and_list_roundtrip(tmp_path):
    ctx = _ctx(tmp_path)
    register_payload = json.loads(
        _ssh_target_register(
            ctx,
            alias='lab-box',
            host='192.0.2.10',
            user='root',
            auth_mode='password',
            password='secret',
            label='Lab server',
            default_remote_root='/srv',
            known_projects_paths=['/srv/ghost'],
        )
    )
    list_payload = json.loads(_ssh_target_list(ctx))
    get_payload = json.loads(_ssh_target_get(ctx, 'lab-box'))

    assert register_payload['status'] == 'ok'
    assert register_payload['registry']['count'] == 1
    assert list_payload['registry']['aliases'] == ['lab-box']
    assert get_payload['target']['label'] == 'Lab server'
    assert get_payload['target']['default_remote_root'] == '/srv'
    assert get_payload['target']['known_projects_paths'] == ['/srv/ghost']
    assert get_payload['target']['has_password'] is True


def test_ssh_target_get_rejects_unknown_alias(tmp_path):
    with pytest.raises(ValueError, match='ssh target alias not found'):
        _ssh_target_get(_ctx(tmp_path), 'missing')


def test_bootstrap_reuses_cached_session_without_second_probe(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _ssh_target_register(
        ctx,
        alias='lab-box',
        host='192.0.2.10',
        user='root',
        auth_mode='password',
        password='secret',
    )
    calls = {'count': 0}

    def fake_run(*args, **kwargs):
        calls['count'] += 1
        return subprocess.CompletedProcess(args[0], 0, stdout='', stderr='')

    monkeypatch.setattr('ouroboros.tools.ssh_targets._run_ssh_probe', fake_run)
    first = json.loads(_ssh_session_bootstrap(ctx, 'lab-box'))
    second = json.loads(_ssh_session_bootstrap(ctx, 'lab-box'))

    assert first['bootstrap'] == 'fresh'
    assert second['bootstrap'] == 'reused'
    assert calls['count'] == 1


@pytest.mark.parametrize(
    ('stderr', 'expected_kind'),
    [
        ('Permission denied (publickey,password).', 'auth_failed'),
        ('ssh: connect to host 192.0.2.10 port 22: Connection refused', 'connection_refused'),
        ('ssh: Could not resolve hostname nowhere.local: Name or service not known', 'host_unreachable'),
        ('ssh: connect to host 192.0.2.10 port 22: Connection timed out', 'timeout'),
    ],
)
def test_normalize_probe_error(stderr, expected_kind):
    error = _normalize_probe_error(stderr, 255)
    assert isinstance(error, SshConnectionError)
    assert error.kind == expected_kind


def test_ssh_target_register_persists_health_metadata(tmp_path):
    ctx = _ctx(tmp_path)
    payload = json.loads(
        _ssh_target_register(
            ctx,
            alias='prod-box',
            host='203.0.113.10',
            user='root',
            auth_mode='password',
            password='secret',
            known_services=['nginx.service', 'xray.service'],
            known_ports=[22, 443, 2053],
            known_tls_domains=['example.com', 'vpn.example.com'],
        )
    )

    target = payload['target']
    assert target['known_services'] == ['nginx.service', 'xray.service']
    assert target['known_ports'] == [22, 443, 2053]
    assert target['known_tls_domains'] == ['example.com', 'vpn.example.com']

    listed = json.loads(_ssh_target_list(ctx))
    assert listed['targets'][0]['known_ports'] == [22, 443, 2053]

def test_run_ssh_probe_password_mode_places_env_before_setsid(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    record = _normalize_target_record(
        alias='lab-box',
        host='192.0.2.10',
        user='root',
        auth_mode='password',
        password='secret',
    )
    captured = {}

    def fake_subprocess_run(cmd, **kwargs):
        captured['cmd'] = cmd
        captured['env'] = kwargs.get('env', {})
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

    monkeypatch.setattr('ouroboros.tools.ssh_targets.subprocess.run', fake_subprocess_run)

    _run_ssh_probe(ctx, record, command='true', timeout=5)

    assert captured['cmd'][:4] == ['env', 'SSH_ASKPASS_REQUIRE=force', 'setsid', '-w']
    assert captured['env']['VELES_SSH_PASSWORD'] == 'secret'
    assert captured['env']['SSH_ASKPASS'].endswith('state/ssh_askpass.sh')



def test_ssh_target_update_preserves_existing_fields_and_allows_metadata_updates(tmp_path):
    ctx = _ctx(tmp_path)
    _ssh_target_register(
        ctx,
        alias='spacecore-94',
        host='94.156.122.66',
        user='root',
        auth_mode='key',
        ssh_key_path='~/.ssh/spacecore',
        provider='spacecore',
        panel_type='3x-ui',
        tags=['vpn'],
        legacy_aliases=['spacecore-vm'],
    )

    payload = json.loads(
        _ssh_target_update(
            ctx,
            alias='spacecore-vm',
            location='nl',
            panel_url='https://397841.vm.spacecore.network/panel/',
            status='ok',
            last_health_at='2026-04-11T13:15:00+00:00',
            tags=['vpn', 'fleet'],
        )
    )
    target = payload['target']
    assert target['alias'] == 'spacecore-94'
    assert target['location'] == 'nl'
    assert target['panel_url'] == 'https://397841.vm.spacecore.network/panel/'
    assert target['status'] == 'ok'
    assert target['tags'] == ['vpn', 'fleet']

    fetched = json.loads(_ssh_target_get(ctx, 'spacecore-vm'))
    assert fetched['target']['alias'] == 'spacecore-94'
    assert fetched['target']['legacy_aliases'] == ['spacecore-vm']
