import pathlib
import json
import subprocess

from ouroboros.tools.registry import ToolRegistry
from ouroboros.tools.remote_filesystem import _discovery_roots, _remote_python_command



def test_remote_filesystem_tools_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t['function']['name'] for t in registry.schemas()}
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
