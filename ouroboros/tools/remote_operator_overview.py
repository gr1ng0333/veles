from __future__ import annotations

import json
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.ssh_targets import _load_registry, _public_target_view


_READ_ONLY_TOOLS = [
    'remote_list_dir',
    'remote_stat',
    'remote_read_file',
    'remote_find',
    'remote_grep',
    'remote_project_discover',
    'remote_command_exec',
    'remote_service_status',
    'remote_service_logs',
    'remote_service_list',
    'remote_server_health',
    'remote_xray_status',
]

_MUTATING_TOOLS = [
    'remote_mkdir',
    'remote_write_file',
    'remote_service_action',
    'remote_project_fetch',
]

_COMPOSITE_TOOLS = [
    'remote_investigate_project',
]

_TARGET_TOOLS = [
    'ssh_target_register',
    'ssh_target_list',
    'ssh_target_get',
    'ssh_key_generate',
    'ssh_key_list',
    'ssh_key_deploy',
    'ssh_session_bootstrap',
    'ssh_target_ping',
]


def _tool_entry(name: str, description: str, properties: Dict[str, Any], required: List[str], handler, is_code_tool: bool = False) -> ToolEntry:
    return ToolEntry(
        name=name,
        schema={
            'name': name,
            'description': description,
            'parameters': {
                'type': 'object',
                'properties': properties,
                'required': required,
            },
        },
        handler=handler,
        is_code_tool=is_code_tool,
    )


def _registry_targets(ctx: ToolContext) -> List[Dict[str, Any]]:
    registry = _load_registry(ctx)
    items: List[Dict[str, Any]] = []
    for alias in sorted((registry.get('targets') or {}).keys()):
        record = registry['targets'][alias]
        public = _public_target_view(record)
        items.append({
            'alias': public['alias'],
            'label': public.get('label') or public['alias'],
            'host': public['host'],
            'port': public['port'],
            'user': public['user'],
            'auth_mode': public['auth_mode'],
            'default_remote_root': public.get('default_remote_root') or '',
            'known_projects_paths': public.get('known_projects_paths') or [],
            'known_services': public.get('known_services') or [],
            'known_ports': public.get('known_ports') or [],
            'known_tls_domains': public.get('known_tls_domains') or [],
            'has_recommended_root': bool((public.get('default_remote_root') or '').strip() or (public.get('known_projects_paths') or [])),
        })
    return items


def _recommended_workflows(target_count: int) -> List[Dict[str, Any]]:
    register_hint = 'Register a target first with ssh_target_register.' if target_count == 0 else 'Bootstrap or ping an existing target alias.'
    return [
        {
            'key': 'target_bootstrap',
            'when': 'First contact with a remote machine',
            'steps': ['ssh_target_register', 'ssh_key_generate', 'ssh_key_deploy', 'ssh_session_bootstrap', 'ssh_target_ping'],
            'summary': register_hint + ' Generate a key once, deploy it, then use key-based sessions by default.',
        },
        {
            'key': 'read_only_investigation',
            'when': 'Need to inspect a remote host without changing it',
            'steps': ['remote_project_discover', 'remote_list_dir', 'remote_read_file', 'remote_command_exec(read_only)'],
            'summary': 'Prefer read-side tools and read_only command execution before any fetch or mutation.',
        },
        {
            'key': 'project_materialization',
            'when': 'Need a local snapshot with manifest/integrity data',
            'steps': ['remote_project_discover', 'remote_project_fetch'],
            'summary': 'Use source_only + exclude_heavy_dirs for the default honest source snapshot path.',
        },
        {
            'key': 'composite_investigation',
            'when': 'Need one end-to-end operator verdict',
            'steps': ['remote_investigate_project'],
            'summary': 'Runs discover -> inspect -> fetch -> summary as one transparent workflow.',
        },
        {
            'key': 'service_ops',
            'when': 'Need to inspect or control systemd services',
            'steps': ['remote_service_status', 'remote_service_logs', 'remote_service_list', 'remote_service_action'],
            'summary': 'Use service-specific tools for status, logs, list and controlled restart/start/stop/reload actions.',
        },
        {
            'key': 'server_health',
            'when': 'Need one structured snapshot of server health',
            'steps': ['remote_server_health'],
            'summary': 'Checks uptime, load, disk, memory, expected ports, expected services, and TLS domains from the target registry.',
        },
    ]


def remote_capabilities_overview(ctx: ToolContext) -> str:
    targets = _registry_targets(ctx)
    target_count = len(targets)
    payload = {
        'status': 'ok',
        'summary': {
            'registered_target_count': target_count,
            'has_registered_targets': bool(target_count),
            'default_mode': 'read_only_first',
            'operator_entrypoint': 'remote_capabilities_overview',
            'compat_alias': 'remote_operator_overview',
        },
        'targets': targets,
        'capability_map': {
            'targets_and_sessions': _TARGET_TOOLS,
            'read_only_filesystem': [
                'remote_list_dir',
                'remote_stat',
                'remote_read_file',
                'remote_find',
                'remote_grep',
                'remote_project_discover',
            ],
            'command_execution': ['remote_command_exec'],
            'network_diagnostics': ['remote_ping', 'remote_traceroute', 'remote_port_check', 'remote_dns_lookup', 'remote_vpn_status', 'remote_iptables_summary', 'remote_netstat'],
            'materialization': ['remote_mkdir', 'remote_write_file', 'remote_project_fetch'],
            'composite_workflow': ['remote_investigate_project'],
        },
        'policy': {
            'default_execution_mode': 'read_only',
            'read_only_tools': _READ_ONLY_TOOLS,
            'mutating_tools': _MUTATING_TOOLS,
            'composite_tools': _COMPOSITE_TOOLS,
            'notes': [
                'Use read-side tools first; do not jump to mutating remote actions without evidence.',
                'remote_command_exec is read_only by default and only allows a guarded inspection command set.',
                'Treat deployment-looking trees as artifacts until source integrity is confirmed.',
            ],
        },
        'recommended_workflows': _recommended_workflows(target_count),
        'next_actions': [
            'Register a remote host with ssh_target_register.' if target_count == 0 else 'Pick a target alias and bootstrap a session with ssh_session_bootstrap.',
            'Use remote_project_discover before fetch when the real project root is not yet known.',
            'Use remote_xray_status when Xray may be spawned by x-ui.service instead of a standalone xray.service.',
            'Use remote_investigate_project when you want one compact operator verdict instead of manual tool stitching.',
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def remote_operator_overview(ctx: ToolContext) -> str:
    return remote_capabilities_overview(ctx)


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            'remote_capabilities_overview',
            'Return an operator-facing overview of the SSH/remote contour: registered targets, tool layers, policy boundaries, and recommended investigation workflows.',
            {},
            [],
            remote_capabilities_overview,
        ),
        _tool_entry(
            'remote_operator_overview',
            'Backward-compatible alias for remote_capabilities_overview.',
            {},
            [],
            remote_operator_overview,
        ),
    ]
