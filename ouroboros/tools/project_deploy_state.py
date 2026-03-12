from __future__ import annotations

import json
import pathlib
from typing import Any, Dict

from ouroboros.tools.project_bootstrap import _utc_now_iso


def _project_deploy_state_path(repo_dir: pathlib.Path) -> pathlib.Path:
    return repo_dir / '.veles' / 'deploy-state.json'


def _read_project_deploy_state(repo_dir: pathlib.Path) -> Dict[str, Any] | None:
    path = _project_deploy_state_path(repo_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as e:
        raise RuntimeError('project deploy state file contains invalid JSON') from e
    if not isinstance(payload, dict):
        raise RuntimeError('project deploy state file must contain a JSON object')
    return payload


def _build_project_deploy_outcome(payload: Dict[str, Any]) -> Dict[str, Any]:
    summary = payload.get('summary') or {}
    recipe = payload.get('recipe') or {}
    service = recipe.get('service') or {}
    server = payload.get('server') or {}
    project = payload.get('project') or {}
    steps = payload.get('steps') or []
    step_statuses = {
        str(step.get('key') or ''): str(step.get('status') or 'unknown')
        for step in steps
        if str(step.get('key') or '').strip()
    }
    return {
        'kind': 'project_deploy_outcome',
        'recorded_at': _utc_now_iso(),
        'applied_at': payload.get('applied_at') or '',
        'status': payload.get('status') or 'unknown',
        'failed_step': payload.get('failed_step') or '',
        'project': {
            'name': project.get('name') or '',
            'path': project.get('path') or '',
        },
        'target': {
            'alias': server.get('alias') or '',
            'host': server.get('host') or '',
            'port': server.get('port'),
            'user': server.get('user') or '',
            'deploy_path': server.get('deploy_path') or '',
        },
        'deploy': {
            'mode': payload.get('mode') or '',
            'runtime': summary.get('runtime') or '',
            'service_name': summary.get('service_name') or service.get('name') or '',
            'unit_name': summary.get('unit_name') or service.get('unit_name') or '',
            'lifecycle_action': summary.get('lifecycle_action') or '',
            'status_ok': summary.get('status_ok'),
            'sync_file_count': summary.get('sync_file_count'),
            'step_keys': [step.get('key') for step in steps],
            'step_statuses': step_statuses,
        },
    }


def _record_project_deploy_outcome(repo_dir: pathlib.Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    outcome = _build_project_deploy_outcome(payload)
    path = _project_deploy_state_path(repo_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(outcome, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {
        'path': str(path),
        'exists': True,
        'outcome': outcome,
    }
