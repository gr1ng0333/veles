from __future__ import annotations

import json
from typing import Any, Dict, List

from ouroboros.tools.external_repos import _tool_entry
from ouroboros.tools.project_deploy import _project_deploy_apply
from ouroboros.tools.project_operational_snapshot import _decode_payload, _project_operational_snapshot
from ouroboros.tools.registry import ToolContext, ToolEntry


def _composite_verdict(deploy_payload: Dict[str, Any], snapshot_payload: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    deploy_status = str(deploy_payload.get('status') or '')
    execution = deploy_payload.get('execution') or {}
    readiness = snapshot_payload.get('readiness') or {}
    risk_flags = snapshot_payload.get('risk_flags') or []
    next_actions = snapshot_payload.get('next_actions') or []
    runtime = snapshot_payload.get('runtime') or {}
    diagnostics = runtime.get('diagnostics') or {}

    healthy = False
    if dry_run:
        healthy = bool(readiness.get('rollout_ready'))
    else:
        healthy = (
            deploy_status == 'ok'
            and bool(readiness.get('rollout_ready'))
            and str(diagnostics.get('severity') or 'healthy') not in {'warning', 'critical'}
        )

    return {
        'healthy': bool(healthy),
        'deploy_status': deploy_status,
        'failed_step': str(deploy_payload.get('failed_step') or execution.get('failed_step') or ''),
        'rollout_ready': bool(readiness.get('rollout_ready')),
        'service_running': readiness.get('service_running'),
        'blocked_reasons': readiness.get('blocked_reasons') or [],
        'risk_flags': risk_flags,
        'next_actions': next_actions,
    }


def _project_deploy_and_verify(
    ctx: ToolContext,
    name: str,
    alias: str,
    service_name: str,
    mode: str = 'install',
    runtime: str = 'auto',
    description: str = '',
    working_directory: str = '',
    exec_start: str = '',
    environment_file: str = '',
    environment: list[str] | None = None,
    user: str = '',
    restart: str = 'always',
    restart_sec: int = 3,
    wanted_by: str = 'multi-user.target',
    sync_timeout: int = 60,
    service_timeout: int = 60,
    status_timeout: int = 60,
    delete: bool = False,
    enable_on_install: bool = True,
    start_on_install: bool = False,
    sudo: bool = True,
    dry_run: bool = False,
    verify_issue_limit: int = 20,
    verify_pr_limit: int = 20,
) -> str:
    deploy_payload = _decode_payload(
        _project_deploy_apply(
            ctx,
            name=name,
            alias=alias,
            service_name=service_name,
            mode=mode,
            runtime=runtime,
            description=description,
            working_directory=working_directory,
            exec_start=exec_start,
            environment_file=environment_file,
            environment=environment,
            user=user,
            restart=restart,
            restart_sec=restart_sec,
            wanted_by=wanted_by,
            sync_timeout=sync_timeout,
            service_timeout=service_timeout,
            status_timeout=status_timeout,
            delete=delete,
            enable_on_install=enable_on_install,
            start_on_install=start_on_install,
            sudo=sudo,
            dry_run=dry_run,
        )
    )

    snapshot_payload = _decode_payload(
        _project_operational_snapshot(
            ctx,
            name=name,
            alias=alias,
            service_name=service_name,
            issue_limit=verify_issue_limit,
            pr_limit=verify_pr_limit,
        )
    )

    payload = {
        'status': 'ok' if deploy_payload.get('status') == 'ok' else 'error',
        'project': deploy_payload.get('project') or snapshot_payload.get('project') or {},
        'server': deploy_payload.get('server') or {},
        'selection': {
            'alias': str(alias or '').strip(),
            'service_name': str(service_name or '').strip(),
            'mode': str(mode or '').strip() or 'install',
            'dry_run': bool(dry_run),
        },
        'steps': [
            {
                'key': 'deploy_apply',
                'tool': 'project_deploy_apply',
                'status': deploy_payload.get('status') or 'unknown',
                'payload': deploy_payload,
            },
            {
                'key': 'verify_snapshot',
                'tool': 'project_operational_snapshot',
                'status': snapshot_payload.get('status') or 'unknown',
                'payload': snapshot_payload,
            },
        ],
        'deploy': deploy_payload,
        'verification': snapshot_payload,
        'verdict': _composite_verdict(deploy_payload, snapshot_payload, bool(dry_run)),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)



def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            'project_deploy_and_verify',
            'Run the existing typed deploy flow for a bootstrapped project and immediately verify the result through project_operational_snapshot, returning both layers plus a compact operator verdict.',
            {
                'name': {'type': 'string', 'description': 'Existing local project name under the projects root'},
                'alias': {'type': 'string', 'description': 'Registered server alias from the project-local .veles server registry'},
                'service_name': {'type': 'string', 'description': 'Systemd service name to deploy and verify'},
                'mode': {'type': 'string', 'description': 'Typed deploy flow to run', 'enum': ['install', 'start', 'update'], 'default': 'install'},
                'runtime': {'type': 'string', 'description': 'Runtime to plan for: auto, python, node, or static', 'default': 'auto'},
                'description': {'type': 'string', 'description': 'Optional systemd unit Description override'},
                'working_directory': {'type': 'string', 'description': 'Optional absolute working directory override inside deploy_path'},
                'exec_start': {'type': 'string', 'description': 'Optional ExecStart override; default depends on runtime'},
                'environment_file': {'type': 'string', 'description': 'Optional absolute EnvironmentFile path'},
                'environment': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Optional KEY=VALUE entries for the systemd unit environment', 'default': []},
                'user': {'type': 'string', 'description': 'Optional system user for the service'},
                'restart': {'type': 'string', 'description': 'Systemd Restart policy', 'default': 'always'},
                'restart_sec': {'type': 'integer', 'description': 'Delay before restart in seconds', 'default': 3},
                'wanted_by': {'type': 'string', 'description': 'WantedBy target for the install section', 'default': 'multi-user.target'},
                'sync_timeout': {'type': 'integer', 'description': 'Timeout for project_server_sync in seconds', 'default': 60},
                'service_timeout': {'type': 'integer', 'description': 'Timeout for project_service_control lifecycle actions in seconds', 'default': 60},
                'status_timeout': {'type': 'integer', 'description': 'Timeout for the final project_service_control status check in seconds', 'default': 60},
                'delete': {'type': 'boolean', 'description': 'Whether to wipe the remote deploy directory contents before sync', 'default': False},
                'enable_on_install': {'type': 'boolean', 'description': 'Whether install mode should enable the unit during the install step', 'default': True},
                'start_on_install': {'type': 'boolean', 'description': 'Whether install mode should also start the unit inside the install step before the explicit lifecycle action', 'default': False},
                'sudo': {'type': 'boolean', 'description': 'Whether remote service actions should use sudo systemctl', 'default': True},
                'dry_run': {'type': 'boolean', 'description': 'If true, return deploy preview plus verification snapshot without executing SSH sync or systemd actions', 'default': False},
                'verify_issue_limit': {'type': 'integer', 'description': 'Maximum number of open GitHub issues to count in the verification snapshot', 'default': 20},
                'verify_pr_limit': {'type': 'integer', 'description': 'Maximum number of open GitHub pull requests to count in the verification snapshot', 'default': 20},
            },
            ['name', 'alias', 'service_name'],
            _project_deploy_and_verify,
            is_code_tool=True,
        ),
    ]
