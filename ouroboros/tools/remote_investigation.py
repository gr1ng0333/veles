from __future__ import annotations

import json
from typing import Any, Dict, List

from ouroboros.tools.remote_materialization import _tool_entry
from ouroboros.tools.project_read_side import _decode_payload
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.remote_execution import remote_command_exec
from ouroboros.tools.remote_filesystem import _remote_project_discover
from ouroboros.tools.remote_materialization import _remote_project_fetch


_PROJECT_SENTINELS = (
    '.git',
    'package.json',
    'pyproject.toml',
    'go.mod',
    'Cargo.toml',
    'docker-compose.yml',
    'docker-compose.yaml',
    'Dockerfile',
    'compose.yml',
    'compose.yaml',
)

_DEPLOY_SENTINELS = (
    'current',
    'releases',
    'shared',
    'public',
    'vendor',
    'node_modules',
    '.next',
    'dist',
    'build',
)

_SOURCE_SENTINELS = (
    'src',
    'app',
    'lib',
    'internal',
    'cmd',
    'services',
    'api',
)


def _coerce_discover_item(item: Dict[str, Any]) -> Dict[str, Any]:
    path = str(item.get('path') or item.get('absolute_path') or '').strip()
    project_markers = item.get('project_markers') or []
    hints = item.get('hints') or {}
    return {
        'path': path,
        'absolute_path': path,
        'type': item.get('type') or 'dir',
        'size': item.get('size'),
        'mtime': item.get('mtime'),
        'project_markers': project_markers,
        'hints': hints,
        'looks_like_project': bool(item.get('looks_like_project') or hints.get('looks_like_project') or project_markers),
        'looks_like_deploy_artifact': bool(item.get('looks_like_deploy_artifact') or hints.get('looks_like_deploy_artifact')),
        'looks_like_source_tree': bool(item.get('looks_like_source_tree') or hints.get('looks_like_source_tree')),
    }



def _score_candidate(item: Dict[str, Any]) -> int:
    score = 0
    markers = {str(x).strip() for x in (item.get('project_markers') or []) if str(x).strip()}
    path_lower = str(item.get('path') or '').lower()
    if item.get('looks_like_project'):
        score += 5
    if item.get('looks_like_source_tree'):
        score += 4
    if item.get('looks_like_deploy_artifact'):
        score -= 3
    score += min(len(markers), 4)
    for sentinel in _PROJECT_SENTINELS:
        if sentinel.lower() in path_lower:
            score += 1
    for sentinel in _SOURCE_SENTINELS:
        if f'/{sentinel.lower()}' in path_lower:
            score += 1
    for sentinel in _DEPLOY_SENTINELS:
        if f'/{sentinel.lower()}' in path_lower:
            score -= 1
    depth = len([part for part in path_lower.split('/') if part])
    score -= min(depth, 12) // 4
    return score



def _select_candidate(discover_payload: Dict[str, Any], prefer_path: str = '') -> Dict[str, Any]:
    candidates = [_coerce_discover_item(item) for item in (discover_payload.get('items') or discover_payload.get('results') or [])]
    if not candidates:
        raise RuntimeError('remote_project_discover returned no candidates')
    preferred = str(prefer_path or '').strip()
    if preferred:
        for item in candidates:
            if item['path'] == preferred:
                return item
    ranked = sorted(candidates, key=lambda item: (_score_candidate(item), len(item['path'])), reverse=True)
    return ranked[0]



def _classify_tree(selection: Dict[str, Any], manifest: Dict[str, Any]) -> Dict[str, Any]:
    selection_hints = selection.get('hints') or {}
    snapshot_kind = str(manifest.get('snapshot_kind') or '').strip()
    files = manifest.get('files_preview') or []
    manifest_integrity = manifest.get('integrity') or {}
    confidence = 'medium'
    nature = 'mixed_tree'
    reasons: List[str] = []

    if selection.get('looks_like_source_tree') or snapshot_kind == 'source_snapshot':
        nature = 'source_tree'
        confidence = 'high'
        reasons.append('selection/source snapshot hints indicate real source tree')
    if selection.get('looks_like_deploy_artifact') or snapshot_kind == 'deployment_artifact_snapshot':
        nature = 'deployment_artifact'
        confidence = 'high'
        reasons.append('selection/snapshot classification indicates deploy artifact')
    if selection_hints.get('looks_like_project'):
        reasons.append('remote discovery marked the tree as project-like')
    if manifest_integrity.get('suspected_incomplete_copy'):
        confidence = 'warning'
        reasons.append('fetch manifest marked the snapshot as potentially incomplete')
    if not reasons:
        reasons.append('classification inferred from combined discovery and fetch heuristics')

    return {
        'nature': nature,
        'confidence': confidence,
        'reasons': reasons,
        'snapshot_kind': snapshot_kind,
        'files_preview': files[:10],
    }



def _profile_project(selection: Dict[str, Any], manifest: Dict[str, Any], inspect_payload: Dict[str, Any]) -> Dict[str, Any]:
    files_preview = [str(x) for x in (manifest.get('files_preview') or [])]
    inspect_stdout = str(inspect_payload.get('stdout') or '')
    lines = [line.strip() for line in inspect_stdout.splitlines() if line.strip()]
    markers = selection.get('project_markers') or []

    stack: List[str] = []
    if any('package.json' in item for item in files_preview) or 'package.json' in markers:
        stack.append('node')
    if any('pyproject.toml' in item for item in files_preview) or 'pyproject.toml' in markers:
        stack.append('python')
    if any('go.mod' in item for item in files_preview) or 'go.mod' in markers:
        stack.append('go')
    if any('Cargo.toml' in item for item in files_preview) or 'Cargo.toml' in markers:
        stack.append('rust')
    if not stack:
        stack.append('unknown')

    deploy_signals = []
    if selection.get('looks_like_deploy_artifact'):
        deploy_signals.append('remote discovery flagged deploy artifact shape')
    if (manifest.get('snapshot_kind') or '') == 'deployment_artifact_snapshot':
        deploy_signals.append('materialization classified snapshot as deployment artifact')

    summary_parts = [f"stack={','.join(stack)}"]
    if markers:
        summary_parts.append(f"markers={','.join(markers[:5])}")
    if lines:
        summary_parts.append(f"inspect={'; '.join(lines[:3])}")

    return {
        'stack': stack,
        'project_markers': markers,
        'deploy_signals': deploy_signals,
        'key_files': files_preview[:12],
        'summary': ' | '.join(summary_parts),
    }



def _investigation_verdict(selection: Dict[str, Any], tree_kind: Dict[str, Any], manifest: Dict[str, Any]) -> Dict[str, Any]:
    integrity = manifest.get('integrity') or {}
    healthy = bool(selection.get('looks_like_project')) and not bool(integrity.get('suspected_incomplete_copy'))
    blocked_reasons: List[str] = []
    if not selection.get('looks_like_project'):
        blocked_reasons.append('selected path does not strongly look like a project root')
    if integrity.get('suspected_incomplete_copy'):
        blocked_reasons.append('fetch manifest suggests incomplete copy')
    if tree_kind.get('nature') == 'deployment_artifact':
        blocked_reasons.append('selected tree looks like a deploy artifact, inspect source origin before editing')
    return {
        'healthy': healthy,
        'selected_remote_path': selection.get('path') or '',
        'tree_nature': tree_kind.get('nature') or 'unknown',
        'blocked_reasons': blocked_reasons,
        'next_actions': [
            'Use remote_read_file / remote_grep for targeted inspection if the manifest already looks sufficient.',
            'If the selected tree is a deploy artifact, locate the real source repo before making code judgments.',
            'Use remote_command_exec in read_only mode for deeper remote diagnostics before any mutating action.',
        ],
    }



def _default_inspect_command(remote_path: str) -> str:
    quoted = json.dumps(str(remote_path))
    interesting = repr(list(_PROJECT_SENTINELS + _SOURCE_SENTINELS))
    script = f"""import os
root = {quoted}
entries = []
for name in sorted(os.listdir(root))[:40]:
    entries.append(name)
interesting_set = set({interesting})
interesting = [name for name in entries if name in interesting_set]
print('top_entries=' + ','.join(entries[:15]))
print('interesting=' + ','.join(interesting[:15]))
"""
    return "python3 - <<'PY'\n" + script + "PY"


def _remote_investigate_project(
    ctx: ToolContext,
    alias: str,
    roots: List[str] | None = None,
    preferred_path: str = '',
    fetch_mode: str = 'source_only',
    exclude_heavy_dirs: bool = True,
    snapshot_kind: str = 'auto',
    destination_label: str = '',
    inspect_command: str = '',
    max_depth: int = 6,
    max_results: int = 30,
) -> str:
    discover_payload = _decode_payload(
        _remote_project_discover(
            ctx,
            alias=alias,
            roots=roots,
            max_depth=max_depth,
            max_results=max_results,
        )
    )
    selection = _select_candidate(discover_payload, prefer_path=preferred_path)
    inspect_payload = _decode_payload(
        remote_command_exec(
            ctx,
            alias=alias,
            command=(inspect_command.strip() or _default_inspect_command(selection['path'])),
            cwd=selection['path'],
            execution_mode='read_only',
            timeout_sec=30,
            max_output_chars=6000,
        )
    )
    fetch_payload = _decode_payload(
        _remote_project_fetch(
            ctx,
            alias=alias,
            remote_path=selection['path'],
            mode=fetch_mode,
            exclude_heavy_dirs=exclude_heavy_dirs,
            snapshot_kind=snapshot_kind,
            destination_label=destination_label,
        )
    )
    tree_kind = _classify_tree(selection, fetch_payload)
    profile = _profile_project(selection, fetch_payload, inspect_payload)
    verdict = _investigation_verdict(selection, tree_kind, fetch_payload)
    payload = {
        'status': 'ok',
        'target': fetch_payload.get('target') or discover_payload.get('target') or {'alias': alias},
        'selection': {
            'preferred_path': str(preferred_path or '').strip(),
            'selected_remote_path': selection['path'],
            'candidate_count': len(discover_payload.get('items') or discover_payload.get('results') or []),
            'selected_candidate': selection,
        },
        'steps': [
            {'key': 'discover', 'tool': 'remote_project_discover', 'status': discover_payload.get('status') or 'unknown', 'payload': discover_payload},
            {'key': 'inspect', 'tool': 'remote_command_exec', 'status': inspect_payload.get('status') or 'unknown', 'payload': inspect_payload},
            {'key': 'fetch', 'tool': 'remote_project_fetch', 'status': fetch_payload.get('status') or 'unknown', 'payload': fetch_payload},
        ],
        'tree': tree_kind,
        'manifest': {
            'path': fetch_payload.get('manifest_path'),
            'local_snapshot_dir': fetch_payload.get('local_snapshot_dir'),
            'local_files_dir': fetch_payload.get('local_files_dir'),
            'selection': fetch_payload.get('selection') or {},
            'integrity': fetch_payload.get('integrity') or {},
            'files_preview': fetch_payload.get('files_preview') or [],
        },
        'project_summary': profile,
        'verdict': verdict,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)



def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            'remote_investigate_project',
            'Run a full read-only remote investigation flow: discover a likely project root on an SSH target, inspect it safely, fetch a local snapshot, build a manifest-backed summary, and return a compact operator verdict.',
            {
                'alias': {'type': 'string', 'description': 'Registered SSH target alias'},
                'roots': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Optional remote roots to search for projects before selection'},
                'preferred_path': {'type': 'string', 'description': 'Optional exact remote path to prefer if discovery finds multiple candidates'},
                'fetch_mode': {'type': 'string', 'enum': ['full', 'source_only'], 'description': 'How aggressively to materialize the selected tree', 'default': 'source_only'},
                'exclude_heavy_dirs': {'type': 'boolean', 'description': 'Exclude heavy/cache/build directories during fetch', 'default': True},
                'snapshot_kind': {'type': 'string', 'enum': ['auto', 'source_snapshot', 'deployment_artifact_snapshot'], 'description': 'Override or auto-detect snapshot classification', 'default': 'auto'},
                'destination_label': {'type': 'string', 'description': 'Optional local snapshot label'},
                'inspect_command': {'type': 'string', 'description': 'Optional read-only remote command for extra profiling; defaults to lightweight directory introspection'},
                'max_depth': {'type': 'integer', 'description': 'Project discovery search depth', 'default': 6},
                'max_results': {'type': 'integer', 'description': 'Maximum discovery candidates to inspect', 'default': 30},
            },
            ['alias'],
            _remote_investigate_project,
            is_code_tool=True,
            timeout_sec=900,
        )
    ]
