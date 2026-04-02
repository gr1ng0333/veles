import json
import pathlib
import subprocess

import pytest

from ouroboros.tools.registry import ToolContext, ToolRegistry
from ouroboros.tools.remote_materialization import (
    _build_manifest,
    _remote_materialize_command,
    _remote_project_fetch,
)



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


def test_remote_materialization_tool_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = _schema_names(registry)
    assert 'remote_project_fetch' in names


def test_remote_materialize_plan_source_only_and_exclusions(tmp_path):
    root = tmp_path / 'srv' / 'ghost'
    (root / 'src').mkdir(parents=True)
    (root / 'dist').mkdir()
    (root / 'node_modules').mkdir()
    (root / 'src' / 'main.py').write_text('print("hi")\n', encoding='utf-8')
    (root / 'README.md').write_text('# demo\n', encoding='utf-8')
    (root / 'dist' / 'bundle.js').write_text('compiled\n', encoding='utf-8')
    (root / 'node_modules' / 'lib.js').write_text('skip\n', encoding='utf-8')

    command = _remote_materialize_command(
        'plan',
        {
            'remote_path': str(root),
            'mode': 'source_only',
            'exclude_heavy_dirs': True,
            'exclude_patterns': ['dist/*'],
        },
    )
    run = subprocess.run(['bash', '-lc', command], capture_output=True, text=True, check=True)
    payload = json.loads(run.stdout)
    assert payload['status'] == 'ok'
    assert payload['snapshot_kind'] == 'source_snapshot'
    rels = {item['relative_path'] for item in payload['files']}
    assert 'src/main.py' in rels
    assert 'README.md' in rels
    assert 'dist/bundle.js' not in rels
    assert 'node_modules/lib.js' not in rels
    assert payload['selection']['excluded_heavy_dir_count'] >= 1


def test_remote_materialize_tar_stream_extracts_files(tmp_path):
    root = tmp_path / 'opt' / 'app'
    (root / 'src').mkdir(parents=True)
    (root / 'src' / 'main.py').write_text('print("ghost")\n', encoding='utf-8')
    (root / 'pyproject.toml').write_text('[project]\nname="demo"\n', encoding='utf-8')

    command = _remote_materialize_command(
        'tar',
        {
            'remote_path': str(root),
            'mode': 'source_only',
            'exclude_heavy_dirs': True,
        },
    )
    out = tmp_path / 'out'
    out.mkdir()
    pipeline = f"{command} | tar -xf - -C {out}"
    subprocess.run(['bash', '-lc', pipeline], check=True)
    assert (out / 'src' / 'main.py').read_text(encoding='utf-8') == 'print("ghost")\n'
    assert (out / 'pyproject.toml').exists()


def test_build_manifest_flags_missing_and_hash_mismatch(tmp_path):
    snapshot_dir = tmp_path / 'snap'
    files_dir = snapshot_dir / 'files'
    files_dir.mkdir(parents=True)
    good = files_dir / 'pyproject.toml'
    good.write_text('[project]\n', encoding='utf-8')
    plan = {
        'target': {'alias': 'box'},
        'remote_root': {'absolute_path': '/srv/app'},
        'snapshot_kind': 'source_snapshot',
        'selection': {'selected_bytes': 20},
        'files': [
            {'relative_path': 'pyproject.toml', 'size': 10, 'mtime': '2020-01-01T00:00:00Z', 'sha256': 'deadbeef', 'is_key_file': True},
            {'relative_path': 'missing.py', 'size': 10, 'mtime': '2020-01-01T00:00:00Z', 'sha256': '', 'is_key_file': False},
        ],
    }
    manifest = _build_manifest(
        alias='box',
        payload={'mode': 'source_only', 'exclude_heavy_dirs': True, 'exclude_patterns': []},
        plan=plan,
        snapshot_dir=snapshot_dir,
        files_dir=files_dir,
    )
    signals = set(manifest['integrity']['incomplete_copy_signals'])
    assert 'missing_local_files_after_transfer' in signals
    assert 'key_file_hash_mismatch' in signals
    assert (snapshot_dir / 'manifest.json').exists()


def test_remote_project_fetch_writes_manifest_with_monkeypatched_transport(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(
        'ouroboros.tools.remote_materialization._run_remote_plan',
        lambda ctx, alias, payload: {
            'status': 'ok',
            'target': {'alias': alias},
            'remote_root': {'absolute_path': payload['remote_path'], 'is_source_tree': True, 'markers': ['pyproject.toml']},
            'snapshot_kind': 'source_snapshot',
            'selection': {'selected_bytes': 12, 'selected_file_count': 1, 'excluded_by_policy_count': 0, 'excluded_heavy_dir_count': 0, 'excluded_source_filter_count': 0, 'non_regular_skipped_count': 0, 'plan_truncated': False},
            'files': [{'relative_path': 'pyproject.toml', 'size': 12, 'mtime': '2020-01-01T00:00:00Z', 'sha256': '', 'is_key_file': True}],
        },
    )
    monkeypatch.setattr(
        'ouroboros.tools.remote_materialization._get_target_record',
        lambda ctx, alias: {'alias': alias, 'auth_mode': 'key', 'password': ''},
    )

    def fake_stream(ctx, record, payload, destination_dir, timeout=600):
        destination_dir.mkdir(parents=True, exist_ok=True)
        (destination_dir / 'pyproject.toml').write_text('[project]\n', encoding='utf-8')

    monkeypatch.setattr('ouroboros.tools.remote_materialization._stream_remote_tar_to_local', fake_stream)
    result = json.loads(_remote_project_fetch(ctx, 'box', '/srv/app', mode='source_only', exclude_heavy_dirs=True))
    assert result['status'] == 'ok'
    assert result['snapshot_kind'] == 'source_snapshot'
    assert pathlib.Path(result['manifest_path']).exists()
    manifest = json.loads(pathlib.Path(result['manifest_path']).read_text(encoding='utf-8'))
    assert manifest['integrity']['planned_file_count'] == 1
    assert manifest['integrity']['extracted_file_count'] == 1
