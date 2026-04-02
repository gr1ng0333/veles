import pathlib
import json
import subprocess

from ouroboros.tools.registry import ToolContext, ToolRegistry


def _schema_names(registry: ToolRegistry) -> set[str]:
    names: set[str] = set()
    for schema in registry.schemas():
        fn = schema.get('function') or {}
        name = fn.get('name') or schema.get('name')
        if name:
            names.add(name)
    return names

from ouroboros.tools.remote_filesystem import (
    _discovery_roots,
    _remote_mkdir,
    _remote_python_command,
    _remote_write_file,
)



def test_remote_filesystem_tools_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = _schema_names(registry)
    expected = {
        'remote_list_dir',
        'remote_read_file',
        'remote_stat',
        'remote_find',
        'remote_grep',
        'remote_project_discover',
    }
    assert expected.issubset(names)


def test_remote_python_command_lists_directory(tmp_path):
    root = tmp_path / 'srv'
    project = root / 'ghost'
    project.mkdir(parents=True)
    (project / 'package.json').write_text('{"name":"ghost"}\n', encoding='utf-8')
    (project / 'Dockerfile').write_text('FROM node:20\n', encoding='utf-8')
    command = _remote_python_command('list_dir', {'base_root': str(root), 'path': '.', 'max_entries': 20})
    run = subprocess.run(['bash', '-lc', command], capture_output=True, text=True, check=True)
    payload = json.loads(run.stdout)
    assert payload['status'] == 'ok'
    assert payload['count'] == 1
    entry = payload['entries'][0]
    assert entry['absolute_path'].endswith('/srv/ghost')
    assert entry['type'] == 'directory'
    assert entry['is_project_like'] is True
    assert entry['is_source_tree'] is True


def test_remote_python_command_finds_projects_and_grep_matches(tmp_path):
    root = tmp_path / 'opt'
    app = root / 'app'
    app.mkdir(parents=True)
    (app / '.git').mkdir()
    (app / 'pyproject.toml').write_text('[project]\nname="demo"\n', encoding='utf-8')
    (app / 'main.py').write_text('print("ghost")\n', encoding='utf-8')

    discover_cmd = _remote_python_command('project_discover', {'base_root': str(root), 'roots': ['.'], 'max_depth': 4, 'max_results': 10})
    discover_run = subprocess.run(['bash', '-lc', discover_cmd], capture_output=True, text=True, check=True)
    discover = json.loads(discover_run.stdout)
    assert discover['status'] == 'ok'
    assert discover['count'] == 1
    assert discover['projects'][0]['project_markers']

    grep_cmd = _remote_python_command('grep', {'base_root': str(root), 'root': '.', 'query': 'ghost', 'glob': '*.py', 'max_depth': 4, 'max_results': 10})
    grep_run = subprocess.run(['bash', '-lc', grep_cmd], capture_output=True, text=True, check=True)
    grep = json.loads(grep_run.stdout)
    assert grep['status'] == 'ok'
    assert grep['count'] == 1
    assert grep['matches'][0]['line_number'] == 1
    assert grep['matches'][0]['path']['is_source_tree'] is True


def test_discovery_roots_merge_target_defaults():
    roots = _discovery_roots(
        {
            'default_remote_root': '/srv',
            'known_projects_paths': ['/srv/ghost', '/opt/services'],
        },
        ['/data', '/srv'],
    )
    assert roots == ['/data', '/srv', '/srv/ghost', '/opt/services']



def _ctx(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path, pending_events=[])


def test_remote_python_command_write_file_and_append(tmp_path):
    root = tmp_path / 'srv'
    app = root / 'app'
    app.mkdir(parents=True)

    write_cmd = _remote_python_command('write_file', {
        'base_root': str(root),
        'path': 'app/config.txt',
        'content': 'alpha',
        'mode': 'overwrite',
    })
    write_run = subprocess.run(['bash', '-lc', write_cmd], capture_output=True, text=True, check=True)
    write_payload = json.loads(write_run.stdout)
    assert write_payload['status'] == 'ok'
    assert write_payload['mode'] == 'overwrite'
    assert write_payload['chars_written'] == 5
    assert write_payload['previously_existed'] is False

    append_cmd = _remote_python_command('write_file', {
        'base_root': str(root),
        'path': 'app/config.txt',
        'content': '\nbeta',
        'mode': 'append',
    })
    append_run = subprocess.run(['bash', '-lc', append_cmd], capture_output=True, text=True, check=True)
    append_payload = json.loads(append_run.stdout)
    assert append_payload['status'] == 'ok'
    assert append_payload['mode'] == 'append'
    assert append_payload['previously_existed'] is True
    assert (app / 'config.txt').read_text(encoding='utf-8') == 'alpha\nbeta'


def test_remote_python_command_denies_critical_write_path(tmp_path):
    root = tmp_path / 'srv'
    root.mkdir(parents=True)
    command = _remote_python_command('write_file', {
        'base_root': str(root),
        'path': '/etc/passwd',
        'content': 'nope',
    })
    run = subprocess.run(['bash', '-lc', command], capture_output=True, text=True, check=True)
    payload = json.loads(run.stdout)
    assert payload['status'] == 'error'
    assert payload['kind'] == 'PermissionError'
    assert 'critical path' in payload['error']


def test_remote_mkdir_and_write_file_append_audit_events(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)

    monkeypatch.setattr('ouroboros.tools.remote_filesystem._run_remote_fs', lambda *args, **kwargs: {
        'status': 'ok',
        'entry': {'absolute_path': '/srv/app/config', 'type': 'directory'},
    })
    mkdir_payload = json.loads(_remote_mkdir(ctx, 'prod', '/srv/app/config'))
    assert mkdir_payload['status'] == 'ok'
    assert ctx.pending_events[-1]['type'] == 'remote_filesystem'
    assert ctx.pending_events[-1]['operation'] == 'mkdir'
    assert ctx.pending_events[-1]['status'] == 'ok'

    def fake_write(*args, **kwargs):
        return {
            'status': 'ok',
            'entry': {'absolute_path': '/srv/app/config/app.env', 'type': 'file'},
            'mode': 'append',
            'chars_written': 11,
            'bytes_written': 11,
            'previously_existed': True,
        }

    monkeypatch.setattr('ouroboros.tools.remote_filesystem._run_remote_fs', fake_write)
    write_payload = json.loads(_remote_write_file(ctx, 'prod', '/srv/app/config/app.env', 'HELLO=WORLD', mode='append'))
    assert write_payload['status'] == 'ok'
    event = ctx.pending_events[-1]
    assert event['type'] == 'remote_filesystem'
    assert event['operation'] == 'write_file'
    assert event['status'] == 'ok'
    assert event['mode'] == 'append'
    assert event['chars_written'] == 11
    assert event['previously_existed'] is True
