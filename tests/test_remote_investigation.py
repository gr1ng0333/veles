import json
import pathlib

from ouroboros.tools.registry import ToolContext, ToolRegistry
from ouroboros.tools.remote_investigation import _remote_investigate_project



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


def test_remote_investigation_tool_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = _schema_names(registry)
    assert 'remote_investigate_project' in names


def test_remote_investigate_project_selects_source_tree_and_builds_summary(tmp_path, monkeypatch):
    discover_payload = {
        'status': 'ok',
        'target': {'alias': 'prod'},
        'items': [
            {
                'path': '/srv/www/releases/20260315',
                'type': 'dir',
                'project_markers': ['package.json'],
                'looks_like_project': True,
                'looks_like_deploy_artifact': True,
                'looks_like_source_tree': False,
                'hints': {
                    'looks_like_project': True,
                    'looks_like_deploy_artifact': True,
                    'looks_like_source_tree': False,
                },
            },
            {
                'path': '/srv/www/app',
                'type': 'dir',
                'project_markers': ['package.json', '.git'],
                'looks_like_project': True,
                'looks_like_deploy_artifact': False,
                'looks_like_source_tree': True,
                'hints': {
                    'looks_like_project': True,
                    'looks_like_deploy_artifact': False,
                    'looks_like_source_tree': True,
                },
            },
        ],
    }
    inspect_calls = []
    fetch_calls = []

    monkeypatch.setattr(
        'ouroboros.tools.remote_investigation._remote_project_discover',
        lambda ctx, alias, roots=None, max_depth=6, max_results=30: json.dumps(discover_payload),
    )

    def fake_exec(ctx, alias, command, cwd='', execution_mode='read_only', timeout_sec=30, max_output_chars=6000):
        inspect_calls.append({'alias': alias, 'cwd': cwd, 'command': command, 'mode': execution_mode})
        return json.dumps({'status': 'ok', 'stdout': 'top_entries=.git,package.json,src\ninteresting=.git,package.json,src\n'})

    def fake_fetch(ctx, alias, remote_path, mode='source_only', exclude_heavy_dirs=False, snapshot_kind='auto', destination_label='', max_files=20000):
        fetch_calls.append({'alias': alias, 'remote_path': remote_path, 'mode': mode, 'exclude_heavy_dirs': exclude_heavy_dirs})
        return json.dumps({
            'status': 'ok',
            'target': {'alias': alias},
            'remote_root': remote_path,
            'snapshot_kind': 'source_snapshot',
            'mode': mode,
            'local_snapshot_dir': '/tmp/snapshots/prod-app',
            'local_files_dir': '/tmp/snapshots/prod-app/files',
            'manifest_path': '/tmp/snapshots/prod-app/manifest.json',
            'selection': {'files_count': 3},
            'integrity': {'suspected_incomplete_copy': False, 'key_hashes': {'package.json': 'abc'}},
            'files_preview': ['package.json', 'src/index.js', '.git/HEAD'],
        })

    monkeypatch.setattr('ouroboros.tools.remote_investigation.remote_command_exec', fake_exec)
    monkeypatch.setattr('ouroboros.tools.remote_investigation._remote_project_fetch', fake_fetch)

    payload = json.loads(
        _remote_investigate_project(
            _ctx(tmp_path),
            alias='prod',
            roots=['/srv/www'],
            fetch_mode='source_only',
            exclude_heavy_dirs=True,
            destination_label='ghost-check',
        )
    )

    assert payload['status'] == 'ok'
    assert payload['selection']['selected_remote_path'] == '/srv/www/app'
    assert payload['tree']['nature'] == 'source_tree'
    assert payload['project_summary']['stack'] == ['node']
    assert payload['manifest']['path'].endswith('manifest.json')
    assert payload['verdict']['healthy'] is True
    assert inspect_calls[0]['cwd'] == '/srv/www/app'
    assert inspect_calls[0]['mode'] == 'read_only'
    assert fetch_calls[0]['remote_path'] == '/srv/www/app'
    assert fetch_calls[0]['exclude_heavy_dirs'] is True


def test_remote_investigate_project_prefers_requested_path_and_flags_deploy_artifact(tmp_path, monkeypatch):
    discover_payload = {
        'status': 'ok',
        'items': [
            {
                'path': '/var/www/current',
                'type': 'dir',
                'project_markers': ['package.json'],
                'looks_like_project': True,
                'looks_like_deploy_artifact': True,
                'looks_like_source_tree': False,
                'hints': {
                    'looks_like_project': True,
                    'looks_like_deploy_artifact': True,
                    'looks_like_source_tree': False,
                },
            },
            {
                'path': '/srv/source/ghost',
                'type': 'dir',
                'project_markers': ['package.json', '.git'],
                'looks_like_project': True,
                'looks_like_deploy_artifact': False,
                'looks_like_source_tree': True,
                'hints': {
                    'looks_like_project': True,
                    'looks_like_deploy_artifact': False,
                    'looks_like_source_tree': True,
                },
            },
        ],
    }

    monkeypatch.setattr(
        'ouroboros.tools.remote_investigation._remote_project_discover',
        lambda ctx, alias, roots=None, max_depth=6, max_results=30: json.dumps(discover_payload),
    )
    monkeypatch.setattr(
        'ouroboros.tools.remote_investigation.remote_command_exec',
        lambda *args, **kwargs: json.dumps({'status': 'ok', 'stdout': 'top_entries=current,releases,shared\ninteresting=package.json\n'}),
    )
    monkeypatch.setattr(
        'ouroboros.tools.remote_investigation._remote_project_fetch',
        lambda *args, **kwargs: json.dumps({
            'status': 'ok',
            'target': {'alias': 'prod'},
            'remote_root': '/var/www/current',
            'snapshot_kind': 'deployment_artifact_snapshot',
            'mode': 'full',
            'local_snapshot_dir': '/tmp/snapshots/current',
            'local_files_dir': '/tmp/snapshots/current/files',
            'manifest_path': '/tmp/snapshots/current/manifest.json',
            'selection': {'files_count': 12},
            'integrity': {'suspected_incomplete_copy': False},
            'files_preview': ['package.json', 'current/index.js', 'shared/config.production.json'],
        }),
    )

    payload = json.loads(
        _remote_investigate_project(
            _ctx(tmp_path),
            alias='prod',
            preferred_path='/var/www/current',
            fetch_mode='full',
            exclude_heavy_dirs=False,
        )
    )

    assert payload['selection']['selected_remote_path'] == '/var/www/current'
    assert payload['tree']['nature'] == 'deployment_artifact'
    assert payload['verdict']['healthy'] is True
    assert any('deploy artifact' in reason for reason in payload['verdict']['blocked_reasons'])
    assert payload['project_summary']['deploy_signals']
