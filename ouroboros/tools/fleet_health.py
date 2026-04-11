from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.remote_service import _remote_server_health, _remote_xray_status
from ouroboros.tools.ssh_targets import _load_registry, _public_target_view
from ouroboros.tools.xui_panel import _xui_panel_status
from ouroboros.utils import utc_now_iso

_DEFAULT_MAX_WORKERS = 6
_MAX_MAX_WORKERS = 32


def _tool_entry(
    name: str,
    description: str,
    properties: Dict[str, Any],
    required: List[str],
    handler,
    is_code_tool: bool = False,
) -> ToolEntry:
    return ToolEntry(
        name=name,
        schema={
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
        handler=handler,
        is_code_tool=is_code_tool,
        timeout_sec=180,
    )


def _normalize_string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(',')
    elif isinstance(value, list):
        items = value
    else:
        raise ValueError('expected a list of strings or a comma-separated string')
    result: List[str] = []
    for item in items:
        text = str(item or '').strip()
        if text:
            result.append(text)
    return result


def _normalize_max_workers(value: Any) -> int:
    if value is None:
        return _DEFAULT_MAX_WORKERS
    try:
        workers = int(value)
    except Exception as exc:
        raise ValueError('max_workers must be an integer') from exc
    if workers < 1 or workers > _MAX_MAX_WORKERS:
        raise ValueError(f'max_workers must be between 1 and {_MAX_MAX_WORKERS}')
    return workers


def _selected_targets(ctx: ToolContext, aliases: List[str], tags: List[str]) -> List[Dict[str, Any]]:
    registry = _load_registry(ctx)
    targets: List[Dict[str, Any]] = []
    alias_filter = {item.lower() for item in aliases}
    tag_filter = {item.lower() for item in tags}
    for alias in sorted((registry.get('targets') or {}).keys()):
        record = registry['targets'][alias]
        public = _public_target_view(record)
        candidate_aliases = {str(public.get('alias') or '').lower()}
        candidate_aliases.update(str(item).lower() for item in (public.get('legacy_aliases') or []))
        candidate_tags = {str(item).lower() for item in (public.get('tags') or [])}
        if alias_filter and not (candidate_aliases & alias_filter):
            continue
        if tag_filter and not tag_filter.issubset(candidate_tags):
            continue
        targets.append(public)
    return targets


def _decode_payload(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except Exception as exc:
            return {
                'status': 'error',
                'kind': 'invalid_json',
                'error': f'failed to decode JSON payload: {exc}',
                'raw': raw[:1000],
            }
        if isinstance(payload, dict):
            return payload
        return {
            'status': 'error',
            'kind': 'invalid_payload',
            'error': 'tool returned a non-object JSON payload',
            'raw': payload,
        }
    return {
        'status': 'error',
        'kind': 'invalid_payload',
        'error': f'unsupported payload type: {type(raw).__name__}',
    }


def _combine_verdict(current: str, incoming: str) -> str:
    rank = {'ok': 0, 'warn': 1, 'critical': 2}
    if rank.get(incoming, 0) > rank.get(current, 0):
        return incoming
    return current


def _issue(code: str, severity: str, message: str, source: str) -> Dict[str, str]:
    return {
        'code': code,
        'severity': severity,
        'message': message,
        'source': source,
    }


def _extract_state(payload: Dict[str, Any], candidates: List[str]) -> str:
    for key in candidates:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


def _inspect_health(payload: Dict[str, Any]) -> tuple[str, List[Dict[str, str]], Dict[str, Any]]:
    issues: List[Dict[str, str]] = []
    verdict = 'ok'
    health_verdict = str(payload.get('verdict') or '').strip().lower()
    status = str(payload.get('status') or '').strip().lower()
    if status != 'ok' or health_verdict in {'critical', 'error', 'failed'}:
        verdict = 'critical'
        issues.append(_issue('ssh_health_failed', 'critical', payload.get('error') or 'SSH/server health check failed', 'ssh_health'))
    elif health_verdict in {'warn', 'warning', 'degraded'}:
        verdict = 'warn'
        issues.append(_issue('ssh_health_warn', 'warn', payload.get('summary') or 'SSH/server health reported warnings', 'ssh_health'))
    summary = {
        'status': status or 'unknown',
        'verdict': health_verdict or ('critical' if verdict == 'critical' else 'ok'),
    }
    return verdict, issues, summary


def _infer_xray_state(payload: Dict[str, Any]) -> str:
    explicit = _extract_state(payload, ['xray_state', 'service_state', 'state', 'active_state', 'status_text'])
    if explicit:
        return explicit

    process_present = payload.get('xray_process_present')
    services = payload.get('services') if isinstance(payload.get('services'), list) else []
    managed_by = str(payload.get('managed_by') or '').strip()

    def _service_running(entry: Dict[str, Any]) -> bool:
        service = entry.get('service') if isinstance(entry.get('service'), dict) else {}
        active_state = str(service.get('active_state') or '').strip().lower()
        sub_state = str(service.get('sub_state') or '').strip().lower()
        return active_state == 'active' and sub_state in {'running', 'listening', 'exited'}

    if isinstance(process_present, bool) and process_present:
        return 'running'

    if managed_by:
        for entry in services:
            if not isinstance(entry, dict):
                continue
            if str(entry.get('service_name') or '').strip() == managed_by and _service_running(entry):
                return 'running'

    for entry in services:
        if not isinstance(entry, dict):
            continue
        if str(entry.get('service_name') or '').strip() == 'xray.service' and _service_running(entry):
            return 'running'

    if isinstance(process_present, bool) and not process_present:
        return 'not_running'
    return ''


def _inspect_xray(payload: Dict[str, Any]) -> tuple[str, List[Dict[str, str]], Dict[str, Any]]:
    issues: List[Dict[str, str]] = []
    verdict = 'ok'
    status = str(payload.get('status') or '').strip().lower()
    managed_by = str(payload.get('managed_by') or '').strip()
    state = _infer_xray_state(payload)
    if status != 'ok':
        verdict = 'warn'
        issues.append(_issue('xray_check_failed', 'warn', payload.get('error') or 'Xray diagnostic failed', 'xray'))
    elif not state:
        verdict = 'warn'
        issues.append(_issue('xray_state_unknown', 'warn', 'Xray state could not be derived from diagnostic payload', 'xray'))
    elif state.lower() not in {'active', 'running', 'ok'}:
        verdict = 'warn'
        issues.append(_issue('xray_not_running', 'warn', f'Xray state is {state}', 'xray'))
    summary = {
        'status': status or 'unknown',
        'state': state or 'unknown',
        'managed_by': managed_by or '',
    }
    return verdict, issues, summary


def _panel_expected(target: Dict[str, Any]) -> bool:
    return str(target.get('panel_type') or '').strip().lower() == '3x-ui'


def _inspect_panel(target: Dict[str, Any], payload: Dict[str, Any] | None) -> tuple[str, List[Dict[str, str]], Dict[str, Any]]:
    issues: List[Dict[str, str]] = []
    verdict = 'ok'
    if not _panel_expected(target):
        return verdict, issues, {'status': 'not_applicable'}

    if not str(target.get('panel_url') or '').strip():
        verdict = 'warn'
        issues.append(_issue('panel_url_missing', 'warn', '3x-ui target has no panel_url configured', 'panel'))
        return verdict, issues, {'status': 'missing_config', 'reason': 'panel_url_missing'}

    if not bool(target.get('has_panel_credentials')):
        verdict = 'warn'
        issues.append(_issue('panel_credentials_missing', 'warn', '3x-ui target has no panel credentials configured', 'panel'))
        return verdict, issues, {'status': 'missing_config', 'reason': 'panel_credentials_missing'}

    payload = payload or {}
    status = str(payload.get('status') or '').strip().lower()
    if status != 'ok':
        verdict = 'warn'
        issues.append(_issue('panel_check_failed', 'warn', payload.get('error') or '3x-ui panel check failed', 'panel'))

    inbounds = payload.get('inbounds') if isinstance(payload.get('inbounds'), list) else []
    enabled_inbounds = sum(1 for inbound in inbounds if isinstance(inbound, dict) and inbound.get('enable') is not False)
    disabled_inbounds = max(len(inbounds) - enabled_inbounds, 0)
    summary = {
        'status': status or 'unknown',
        'inbounds_total': len(inbounds),
        'enabled_inbounds': enabled_inbounds,
        'disabled_inbounds': disabled_inbounds,
    }
    return verdict, issues, summary


def _probe_target(ctx: ToolContext, target: Dict[str, Any], include_panel: bool, include_xray: bool) -> Dict[str, Any]:
    alias = str(target.get('alias') or '')
    health_payload = _decode_payload(_remote_server_health(ctx, alias=alias))
    health_verdict, issues, health_summary = _inspect_health(health_payload)

    result: Dict[str, Any] = {
        'alias': alias,
        'label': target.get('label') or alias,
        'host': target.get('host') or '',
        'provider': target.get('provider') or '',
        'location': target.get('location') or '',
        'tags': target.get('tags') or [],
        'panel_type': target.get('panel_type') or '',
        'panel_url': target.get('panel_url') or '',
        'verdict': health_verdict,
        'issues': issues,
        'checks': {
            'ssh_health': health_summary,
        },
        'raw': {
            'ssh_health': health_payload,
        },
    }

    if include_xray:
        xray_payload = _decode_payload(_remote_xray_status(ctx, alias=alias))
        xray_verdict, xray_issues, xray_summary = _inspect_xray(xray_payload)
        result['verdict'] = _combine_verdict(result['verdict'], xray_verdict)
        result['issues'].extend(xray_issues)
        result['checks']['xray'] = xray_summary
        result['raw']['xray'] = xray_payload

    if include_panel:
        panel_payload = None
        if _panel_expected(target) and str(target.get('panel_url') or '').strip() and bool(target.get('has_panel_credentials')):
            panel_payload = _decode_payload(_xui_panel_status(ctx, alias=alias))
        panel_verdict, panel_issues, panel_summary = _inspect_panel(target, panel_payload)
        result['verdict'] = _combine_verdict(result['verdict'], panel_verdict)
        result['issues'].extend(panel_issues)
        result['checks']['panel'] = panel_summary
        if panel_payload is not None:
            result['raw']['panel'] = panel_payload

    return result


def fleet_health(
    ctx: ToolContext,
    aliases: Any = None,
    tags: Any = None,
    include_panel: bool = True,
    include_xray: bool = True,
    max_workers: Any = None,
) -> str:
    try:
        alias_list = _normalize_string_list(aliases)
        tag_list = _normalize_string_list(tags)
        workers = _normalize_max_workers(max_workers)
    except ValueError as exc:
        return json.dumps({'status': 'error', 'kind': 'invalid_arguments', 'error': str(exc)}, ensure_ascii=False)

    targets = _selected_targets(ctx, alias_list, tag_list)
    if not targets:
        return json.dumps(
            {
                'status': 'ok',
                'checked_at': utc_now_iso(),
                'filters': {'aliases': alias_list, 'tags': tag_list},
                'summary': {
                    'registered_targets': len((_load_registry(ctx).get('targets') or {})),
                    'matched_targets': 0,
                    'by_verdict': {'ok': 0, 'warn': 0, 'critical': 0},
                },
                'targets': [],
            },
            ensure_ascii=False,
        )

    results: List[Dict[str, Any]] = []
    worker_count = min(max(workers, 1), max(len(targets), 1))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(_probe_target, ctx, target, bool(include_panel), bool(include_xray)): target['alias']
            for target in targets
        }
        for future in as_completed(future_map):
            alias = future_map[future]
            try:
                results.append(future.result())
            except Exception as exc:
                target = next(item for item in targets if item['alias'] == alias)
                results.append(
                    {
                        'alias': alias,
                        'label': target.get('label') or alias,
                        'host': target.get('host') or '',
                        'provider': target.get('provider') or '',
                        'location': target.get('location') or '',
                        'tags': target.get('tags') or [],
                        'panel_type': target.get('panel_type') or '',
                        'panel_url': target.get('panel_url') or '',
                        'verdict': 'critical',
                        'issues': [_issue('fleet_probe_failed', 'critical', str(exc), 'fleet_health')],
                        'checks': {},
                        'raw': {},
                    }
                )

    results.sort(key=lambda item: item['alias'])
    summary_counts = {'ok': 0, 'warn': 0, 'critical': 0}
    for item in results:
        verdict = str(item.get('verdict') or 'ok')
        summary_counts[verdict] = summary_counts.get(verdict, 0) + 1

    overall_verdict = 'ok'
    if summary_counts.get('critical'):
        overall_verdict = 'critical'
    elif summary_counts.get('warn'):
        overall_verdict = 'warn'

    payload = {
        'status': 'ok',
        'checked_at': utc_now_iso(),
        'filters': {'aliases': alias_list, 'tags': tag_list},
        'summary': {
            'registered_targets': len((_load_registry(ctx).get('targets') or {})),
            'matched_targets': len(results),
            'by_verdict': summary_counts,
            'overall_verdict': overall_verdict,
            'include_panel': bool(include_panel),
            'include_xray': bool(include_xray),
        },
        'targets': results,
    }
    return json.dumps(payload, ensure_ascii=False)


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            name='fleet_health',
            description='Aggregate SSH health, Xray state, and 3x-ui panel status across registered servers with optional filtering by tags or aliases.',
            properties={
                'aliases': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'Optional list of target aliases to probe. Supports canonical and legacy aliases.',
                },
                'tags': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'Optional list of tags. A target must contain all requested tags to match.',
                },
                'include_panel': {
                    'type': 'boolean',
                    'description': 'Include 3x-ui panel checks when panel metadata is configured.',
                    'default': True,
                },
                'include_xray': {
                    'type': 'boolean',
                    'description': 'Include Xray core diagnostics via SSH.',
                    'default': True,
                },
                'max_workers': {
                    'type': 'integer',
                    'description': f'Parallel worker count for probing targets (1-{_MAX_MAX_WORKERS}).',
                    'default': _DEFAULT_MAX_WORKERS,
                },
            },
            required=[],
            handler=fleet_health,
        )
    ]
