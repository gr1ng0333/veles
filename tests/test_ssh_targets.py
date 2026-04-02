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
    _ssh_session_bootstrap,
    _ssh_target_get,
    _ssh_target_list,
    _ssh_target_register,
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
