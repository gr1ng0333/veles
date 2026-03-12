import json
import pathlib
import subprocess

import pytest

from ouroboros.tools.project_bootstrap import _project_init, _project_server_register
from ouroboros.tools.project_deploy import _build_sync_archive, _project_server_sync
from ouroboros.tools.registry import ToolContext, ToolRegistry


def _ctx(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path)


@pytest.fixture(autouse=True)
def _projects_root_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VELES_PROJECTS_ROOT", str(tmp_path / "projects"))


def test_project_server_sync_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_server_sync" in names


def test_build_sync_archive_excludes_git_and_veles_metadata(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    (repo_dir / ".veles").mkdir(exist_ok=True)
    (repo_dir / ".veles" / "servers.json").write_text('[]\n', encoding='utf-8')
    (repo_dir / "deploy" / "app.env").parent.mkdir(parents=True, exist_ok=True)
    (repo_dir / "deploy" / "app.env").write_text('PORT=8080\n', encoding='utf-8')

    archive_bytes, files, archive_size = _build_sync_archive(repo_dir)

    assert archive_size > 0
    assert 'README.md' in files
    assert 'deploy/app.env' in files
    assert '.veles/servers.json' not in files
    assert all(not path.startswith('.git/') for path in files)
    assert isinstance(archive_bytes, bytes)


def test_project_server_sync_streams_archive_to_registered_server(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='prod',
        host='example.com',
        user='deploy',
        ssh_key_path='~/id_test',
        deploy_path='/srv/demo-api',
    )

    captured = {}

    def fake_run_ssh_stream(args, stdin_bytes, timeout):
        captured['args'] = args
        captured['stdin_bytes'] = stdin_bytes
        captured['timeout'] = timeout
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout=b'synced\n', stderr=b'')

    monkeypatch.setattr('ouroboros.tools.project_deploy._run_ssh_stream', fake_run_ssh_stream)

    payload = json.loads(_project_server_sync(_ctx(tmp_path), name='demo-api', alias='prod', delete=True, timeout=90))

    assert payload['status'] == 'ok'
    assert payload['server']['alias'] == 'prod'
    assert payload['sync']['transport'] == 'ssh+tar'
    assert payload['sync']['delete'] is True
    assert payload['sync']['file_count'] >= 1
    assert 'README.md' in payload['sync']['files']
    assert payload['result']['stdout'] == 'synced\n'
    assert payload['result']['exit_code'] == 0
    assert captured['timeout'] == 90
    assert captured['stdin_bytes']
    assert 'deploy@example.com' in captured['args']
    assert any('/srv/demo-api' in part for part in captured['args'])
    assert any('find ' in part for part in captured['args'])


def test_project_server_sync_rejects_bad_timeout(tmp_path):
    _project_init(_ctx(tmp_path), name='Demo API', language='python')
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='prod',
        host='example.com',
        user='deploy',
        ssh_key_path='~/id_test',
        deploy_path='/srv/demo-api',
    )

    with pytest.raises(ValueError):
        _project_server_sync(_ctx(tmp_path), name='demo-api', alias='prod', timeout=0)
