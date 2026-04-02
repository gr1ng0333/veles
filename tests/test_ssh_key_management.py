import json
from pathlib import Path
import pathlib

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

from ouroboros.tools.ssh_key_management import ssh_key_deploy, ssh_key_generate, ssh_key_list
from ouroboros.tools.ssh_targets import SshConnectionError, _get_target_record, _ssh_target_register


def _ctx(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path, pending_events=[])


def test_ssh_key_tools_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = _schema_names(registry)
    assert 'ssh_key_generate' in names
    assert 'ssh_key_list' in names
    assert 'ssh_key_deploy' in names


def test_ssh_key_generate_creates_paths_and_reports_fingerprint(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)

    def fake_run(cmd, cwd, capture_output, text, timeout):
        if cmd[:2] == ['ssh-keygen', '-t']:
            private_path = pathlib.Path(cmd[4])
            private_path.parent.mkdir(parents=True, exist_ok=True)
            private_path.write_text('PRIVATE', encoding='utf-8')
            private_path.with_name(private_path.name + '.pub').write_text('ssh-ed25519 AAAATEST generated@test', encoding='utf-8')
            return type('CP', (), {'returncode': 0, 'stdout': 'ok', 'stderr': ''})()
        return type('CP', (), {'returncode': 0, 'stdout': '256 SHA256:abc generated@test (ED25519)\n', 'stderr': ''})()

    monkeypatch.setattr('ouroboros.tools.ssh_key_management.subprocess.run', fake_run)
    payload = json.loads(ssh_key_generate(ctx, key_name='prod-box'))

    assert payload['status'] == 'ok'
    assert payload['key']['name'] == 'prod-box'
    assert payload['key']['private_key_path'].endswith('/state/ssh_keys/prod-box')
    assert payload['key']['fingerprint'].startswith('256 SHA256:abc')


def test_ssh_key_list_returns_existing_keys(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    key_dir = tmp_path / 'state' / 'ssh_keys'
    key_dir.mkdir(parents=True, exist_ok=True)
    (key_dir / 'demo').write_text('PRIVATE', encoding='utf-8')
    (key_dir / 'demo.pub').write_text('ssh-ed25519 AAAATEST demo@test', encoding='utf-8')
    monkeypatch.setattr('ouroboros.tools.ssh_key_management._fingerprint', lambda _path: '256 SHA256:list demo@test (ED25519)')

    payload = json.loads(ssh_key_list(ctx))

    assert payload['status'] == 'ok'
    assert payload['count'] == 1
    assert payload['keys'][0]['name'] == 'demo'
    assert payload['keys'][0]['fingerprint'].startswith('256 SHA256:list')


def test_ssh_key_deploy_switches_target_to_key_after_success(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _ssh_target_register(ctx, alias='prod-box', host='203.0.113.10', user='root', auth_mode='password', password='secret')
    key_dir = tmp_path / 'state' / 'ssh_keys'
    key_dir.mkdir(parents=True, exist_ok=True)
    (key_dir / 'prod-box').write_text('PRIVATE', encoding='utf-8')
    (key_dir / 'prod-box.pub').write_text('ssh-ed25519 AAAATEST prod-box@test', encoding='utf-8')

    monkeypatch.setattr('ouroboros.tools.ssh_key_management._run_ssh_probe', lambda _ctx, _record, command, timeout: type('CP', (), {'returncode': 0, 'stdout': 'ok', 'stderr': ''})())
    monkeypatch.setattr('ouroboros.tools.ssh_key_management._bootstrap_session', lambda _ctx, alias: {'status': 'ok', 'alias': alias})
    monkeypatch.setattr('ouroboros.tools.ssh_key_management._fingerprint', lambda _path: '256 SHA256:deploy prod-box@test (ED25519)')

    payload = json.loads(ssh_key_deploy(ctx, alias='prod-box', key_name='prod-box'))
    record = _get_target_record(ctx, 'prod-box')

    assert payload['status'] == 'ok'
    assert record['auth_mode'] == 'key'
    assert record['ssh_key_path'].endswith('/state/ssh_keys/prod-box')
    assert payload['verification']['status'] == 'ok'




def test_ssh_key_deploy_uses_run_ssh_probe_for_password_targets(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _ssh_target_register(ctx, alias='prod-box', host='203.0.113.10', user='root', auth_mode='password', password='secret')
    key_dir = tmp_path / 'state' / 'ssh_keys'
    key_dir.mkdir(parents=True, exist_ok=True)
    (key_dir / 'prod-box').write_text('PRIVATE', encoding='utf-8')
    (key_dir / 'prod-box.pub').write_text('ssh-ed25519 AAAATEST prod-box@test', encoding='utf-8')

    calls = []

    def fake_probe(_ctx, record, command, timeout):
        calls.append({'alias': record['alias'], 'command': command, 'timeout': timeout, 'auth_mode': record['auth_mode']})
        return type('CP', (), {'returncode': 0, 'stdout': 'ok', 'stderr': ''})()

    monkeypatch.setattr('ouroboros.tools.ssh_key_management._run_ssh_probe', fake_probe)
    monkeypatch.setattr('ouroboros.tools.ssh_key_management._bootstrap_session', lambda _ctx, alias: {'status': 'ok', 'alias': alias})
    monkeypatch.setattr('ouroboros.tools.ssh_key_management._fingerprint', lambda _path: '256 SHA256:deploy prod-box@test (ED25519)')

    payload = json.loads(ssh_key_deploy(ctx, alias='prod-box', key_name='prod-box', timeout_sec=33))

    assert payload['status'] == 'ok'
    assert calls and calls[0]['alias'] == 'prod-box'
    assert calls[0]['timeout'] == 33
    assert 'authorized_keys' in calls[0]['command']
def test_ssh_key_deploy_reverts_registry_when_verification_fails(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _ssh_target_register(ctx, alias='prod-box', host='203.0.113.10', user='root', auth_mode='password', password='secret')
    key_dir = tmp_path / 'state' / 'ssh_keys'
    key_dir.mkdir(parents=True, exist_ok=True)
    (key_dir / 'prod-box').write_text('PRIVATE', encoding='utf-8')
    (key_dir / 'prod-box.pub').write_text('ssh-ed25519 AAAATEST prod-box@test', encoding='utf-8')

    monkeypatch.setattr('ouroboros.tools.ssh_key_management._run_ssh_probe', lambda _ctx, _record, command, timeout: type('CP', (), {'returncode': 0, 'stdout': 'ok', 'stderr': ''})())
    monkeypatch.setattr('ouroboros.tools.ssh_key_management._bootstrap_session', lambda _ctx, alias: (_ for _ in ()).throw(SshConnectionError('auth_failed', 'still password only')))

    payload = json.loads(ssh_key_deploy(ctx, alias='prod-box', key_name='prod-box'))
    record = _get_target_record(ctx, 'prod-box')

    assert payload['status'] == 'error'
    assert payload['kind'] == 'verification_failed'
    assert record['auth_mode'] == 'password'


