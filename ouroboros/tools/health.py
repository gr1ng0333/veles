"""Codebase and VPS health tools — complexity metrics and runtime self-assessment."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import shutil
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)


def _codebase_health(ctx: ToolContext) -> str:
    """Compute and format codebase health report."""
    try:
        from ouroboros.review import collect_sections, compute_complexity_metrics

        repo_dir = pathlib.Path(ctx.repo_dir)
        drive_root = pathlib.Path(os.environ.get("DRIVE_ROOT", "/content/drive/MyDrive/Ouroboros"))

        sections, stats = collect_sections(repo_dir, drive_root)
        metrics = compute_complexity_metrics(sections)

        # Format report
        lines = []
        lines.append("## Codebase Health Report\n")
        lines.append(f"**Analyzed:** {stats['files']} files, {stats['chars']:,} chars")
        lines.append(f"**Files:** {metrics['total_files']} ({metrics['py_files']} Python)")
        lines.append(f"**Total lines:** {metrics['total_lines']:,}")
        lines.append(f"**Functions:** {metrics['total_functions']}")
        lines.append(f"**Avg function length:** {metrics['avg_function_length']} lines")
        lines.append(f"**Max function length:** {metrics['max_function_length']} lines")

        # Largest files
        if metrics.get("largest_files"):
            lines.append("\n### Largest Files")
            for path, size in metrics["largest_files"][:10]:
                marker = " ⚠️ OVERSIZED" if size > 1000 else ""
                lines.append(f"  {path}: {size} lines{marker}")

        # Longest functions
        if metrics.get("longest_functions"):
            lines.append("\n### Longest Functions")
            for path, start, length in metrics["longest_functions"][:10]:
                marker = " ⚠️ OVERSIZED" if length > 150 else ""
                lines.append(f"  {path}:{start}: {length} lines{marker}")

        # Warnings
        oversized_funcs = metrics.get("oversized_functions", [])
        oversized_mods = metrics.get("oversized_modules", [])

        if oversized_funcs or oversized_mods:
            lines.append("\n### ⚠️ Bible Violations (Principle 5: Minimalism)")
            if oversized_funcs:
                lines.append(f"  Functions > 150 lines: {len(oversized_funcs)}")
                for path, start, length in oversized_funcs:
                    lines.append(f"    - {path}:{start} ({length} lines)")
            if oversized_mods:
                lines.append(f"  Modules > 1000 lines: {len(oversized_mods)}")
                for path, size in oversized_mods:
                    lines.append(f"    - {path} ({size} lines)")
        else:
            lines.append("\n✅ No Bible violations detected (all functions < 150 lines, all modules < 1000 lines)")

        return "\n".join(lines)

    except Exception as e:
        log.warning("codebase_health failed: %s", e, exc_info=True)
        return f"⚠️ Failed to compute codebase health: {e}"


def _read_meminfo() -> Dict[str, int]:
    info: Dict[str, int] = {}
    with open('/proc/meminfo', 'r', encoding='utf-8') as f:
        for line in f:
            if ':' not in line:
                continue
            key, rest = line.split(':', 1)
            parts = rest.strip().split()
            if not parts:
                continue
            try:
                # Values in /proc/meminfo are kB
                info[key] = int(parts[0]) * 1024
            except ValueError:
                continue
    return info


def _vps_health_check(
    ctx: ToolContext,
    output_path: str = '/opt/veles-data/state/health_check.json',
) -> str:
    """Check VPS runtime health and persist JSON report."""
    try:
        del ctx

        disk_total, disk_used, disk_free = shutil.disk_usage('/')
        disk_used_pct = (disk_used / disk_total * 100.0) if disk_total else 0.0

        meminfo = _read_meminfo()
        mem_total = meminfo.get('MemTotal', 0)
        mem_available = meminfo.get('MemAvailable', 0)
        mem_used = max(mem_total - mem_available, 0)
        mem_used_pct = (mem_used / mem_total * 100.0) if mem_total else 0.0

        with open('/proc/uptime', 'r', encoding='utf-8') as f:
            uptime_seconds = float(f.read().split()[0])

        serper_configured = bool(os.environ.get('SERPER_API_KEY', '').strip())

        overall_healthy = bool(
            serper_configured
            and disk_used_pct < 95.0
            and mem_used_pct < 98.0
        )

        report = {
            'timestamp_utc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'search_backend': {
                'provider': 'serper',
                'configured': serper_configured,
            },
            'disk': {
                'total_bytes': disk_total,
                'used_bytes': disk_used,
                'free_bytes': disk_free,
                'used_percent': round(disk_used_pct, 2),
                'total_gib': round(disk_total / (1024 ** 3), 2),
                'used_gib': round(disk_used / (1024 ** 3), 2),
                'free_gib': round(disk_free / (1024 ** 3), 2),
            },
            'ram': {
                'total_bytes': mem_total,
                'used_bytes': mem_used,
                'available_bytes': mem_available,
                'used_percent': round(mem_used_pct, 2),
                'total_gib': round(mem_total / (1024 ** 3), 2),
                'used_gib': round(mem_used / (1024 ** 3), 2),
                'available_gib': round(mem_available / (1024 ** 3), 2),
            },
            'uptime_seconds': uptime_seconds,
            'overall_healthy': overall_healthy,
        }

        out = pathlib.Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

        search_status = '✅' if serper_configured else '⚠️'
        disk_status = '✅' if disk_used_pct < 95.0 else '⚠️'
        ram_status = '✅' if mem_used_pct < 98.0 else '⚠️'
        overall_status = '✅' if overall_healthy else '⚠️'

        lines = [
            '## VPS Health Check',
            f"- **Timestamp (UTC):** {report['timestamp_utc']}",
            f"- **Search backend:** {search_status} Serper {'configured' if serper_configured else 'not configured'}",
            f"- **Disk:** {disk_status} {disk_used_pct:.2f}% used ({disk_used / (1024 ** 3):.2f} GiB / {disk_total / (1024 ** 3):.2f} GiB)",
            f"- **RAM:** {ram_status} {mem_used_pct:.2f}% used ({mem_used / (1024 ** 3):.2f} GiB / {mem_total / (1024 ** 3):.2f} GiB)",
            f"- **Uptime:** ✅ {uptime_seconds:.0f} sec",
            f"- **Overall:** {overall_status} {'healthy' if overall_healthy else 'degraded'}",
            f"- **Report written:** `{output_path}`",
        ]
        return '\n'.join(lines)

    except Exception as e:
        log.warning('vps_health_check failed: %s', e, exc_info=True)
        return f"⚠️ Failed to run VPS health check: {e}"


def _doctor(
    ctx: ToolContext,
    output_path: str = '/opt/veles-data/state/doctor_report.json',
    stale_identity_hours: float = 4.0,
) -> str:
    """Run consolidated diagnostics and persist JSON doctor report."""
    report: Dict[str, Any] = {
        'timestamp_utc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'checks': {},
        'overall_healthy': True,
    }

    def add_check(name: str, ok: bool, details: Dict[str, Any]) -> None:
        report['checks'][name] = {'ok': bool(ok), **details}
        if not ok:
            report['overall_healthy'] = False

    # 1) VERSION sync invariant (VERSION == pyproject == README mention)
    try:
        repo = pathlib.Path(ctx.repo_dir)
        version = (repo / 'VERSION').read_text(encoding='utf-8').strip()
        pyproject = (repo / 'pyproject.toml').read_text(encoding='utf-8')
        m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, flags=re.MULTILINE)
        py_ver = m.group(1).strip() if m else ''
        readme = (repo / 'README.md').read_text(encoding='utf-8')
        ok = bool(version and py_ver and version == py_ver and version in readme)
        add_check('version_sync', ok, {'version': version, 'pyproject_version': py_ver})
    except Exception as e:
        add_check('version_sync', False, {'error': str(e)})

    # 2) GitHub CLI availability
    gh_path = shutil.which('gh')
    add_check('github_cli', gh_path is not None, {'path': gh_path or ''})

    # 3) Identity freshness
    try:
        identity = pathlib.Path(ctx.drive_root) / 'memory' / 'identity.md'
        if identity.exists():
            age_hours = max((datetime.now(timezone.utc).timestamp() - identity.stat().st_mtime) / 3600.0, 0.0)
            ok = age_hours <= float(stale_identity_hours)
            add_check('identity_freshness', ok, {'age_hours': round(age_hours, 2), 'threshold_hours': float(stale_identity_hours)})
        else:
            add_check('identity_freshness', False, {'error': 'identity.md not found'})
    except Exception as e:
        add_check('identity_freshness', False, {'error': str(e)})

    # 4) Budget drift (if state file exists)
    try:
        state_path = pathlib.Path('/opt/veles-data/state/state.json')
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding='utf-8'))
            drift = float(state.get('budget_drift_pct') or 0.0)
            tracked = float(state.get('spent_usd') or 0.0)
            openrouter = float(state.get('openrouter_total_usd') or 0.0)
            ok = abs(drift) <= 20.0
            add_check('budget_drift', ok, {'drift_pct': round(drift, 2), 'tracked_usd': tracked, 'openrouter_usd': openrouter})
        else:
            add_check('budget_drift', False, {'error': 'state.json not found'})
    except Exception as e:
        add_check('budget_drift', False, {'error': str(e)})

    out = pathlib.Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    status = '✅' if report['overall_healthy'] else '⚠️'
    lines = [
        '## Doctor Report',
        f"- **Timestamp (UTC):** {report['timestamp_utc']}",
        f"- **Overall:** {status} {'healthy' if report['overall_healthy'] else 'attention needed'}",
    ]

    for name, item in report['checks'].items():
        icon = '✅' if item.get('ok') else '⚠️'
        if name == 'version_sync':
            lines.append(f"- **{name}:** {icon} VERSION={item.get('version','?')} pyproject={item.get('pyproject_version','?')}")
        elif name == 'github_cli':
            path = item.get('path') or 'not found'
            lines.append(f"- **{name}:** {icon} {path}")
        elif name == 'identity_freshness':
            if item.get('ok'):
                lines.append(f"- **{name}:** {icon} age={item.get('age_hours')}h (<= {item.get('threshold_hours')}h)")
            else:
                lines.append(f"- **{name}:** {icon} {item.get('error','stale')}")
        elif name == 'budget_drift':
            if 'drift_pct' in item:
                lines.append(f"- **{name}:** {icon} drift={item.get('drift_pct')}% (tracked=${item.get('tracked_usd')} vs OR=${item.get('openrouter_usd')})")
            else:
                lines.append(f"- **{name}:** {icon} {item.get('error','unknown')}")

    lines.append(f"- **Report written:** `{output_path}`")
    return '\n'.join(lines)


def _safe_read_json(path: pathlib.Path) -> tuple[str, Dict[str, Any]]:
    if not path.exists():
        return 'missing', {}
    try:
        return 'ok', json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        return 'error', {'error': str(e)}


def _monitor_snapshot(
    ctx: ToolContext,
    output_path: str = '/opt/veles-data/state/monitor_snapshot.json',
) -> str:
    """Build a consolidated runtime snapshot from state/doctor/health/queue/codex files."""
    state_dir = pathlib.Path('/opt/veles-data/state')
    ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    source_paths = {
        'state': state_dir / 'state.json',
        'doctor_report': state_dir / 'doctor_report.json',
        'health_check': state_dir / 'health_check.json',
        'codex_accounts_state': state_dir / 'codex_accounts_state.json',
        'queue_snapshot': state_dir / 'queue_snapshot.json',
    }

    report: Dict[str, Any] = {
        'timestamp_utc': ts,
        'overall_healthy': True,
        'sources': {},
    }

    state_data: Dict[str, Any] = {}
    doctor_data: Dict[str, Any] = {}
    health_data: Dict[str, Any] = {}
    codex_data: Dict[str, Any] = {}
    queue_data: Dict[str, Any] = {}

    for name, path in source_paths.items():
        status, data = _safe_read_json(path)
        entry: Dict[str, Any] = {'status': status, 'path': str(path)}

        if status == 'ok':
            if name == 'state':
                state_data = data
                total = float(data.get('total_usd') or data.get('budget_total_usd') or 0.0)
                spent = float(data.get('spent_usd') or 0.0)
                remaining = (total - spent) if total > 0 else None
                entry['summary'] = {
                    'current_branch': data.get('current_branch'),
                    'current_sha': data.get('current_sha'),
                    'spent_usd': spent,
                    'remaining_usd': round(remaining, 6) if remaining is not None else None,
                    'evolution_mode_enabled': bool(data.get('evolution_mode_enabled', False)),
                    'evolution_cycle': int(data.get('evolution_cycle') or 0),
                    'evolution_consecutive_failures': int(data.get('evolution_consecutive_failures') or 0),
                }
            elif name == 'doctor_report':
                doctor_data = data
                checks = data.get('checks') if isinstance(data.get('checks'), dict) else {}
                entry['summary'] = {
                    'overall_healthy': bool(data.get('overall_healthy', False)),
                    'checks': {k: bool(v.get('ok')) for k, v in checks.items() if isinstance(v, dict)},
                }
            elif name == 'health_check':
                health_data = data
                disk = data.get('disk') if isinstance(data.get('disk'), dict) else {}
                ram = data.get('ram') if isinstance(data.get('ram'), dict) else {}
                search_backend = data.get('search_backend') if isinstance(data.get('search_backend'), dict) else {}
                entry['summary'] = {
                    'overall_healthy': bool(data.get('overall_healthy', False)),
                    'disk_used_percent': disk.get('used_percent'),
                    'ram_used_percent': ram.get('used_percent'),
                    'search_backend_configured': bool(search_backend.get('configured', False)),
                }
            elif name == 'codex_accounts_state':
                codex_data = data
                accounts = data.get('accounts') if isinstance(data.get('accounts'), list) else []
                dead_count = sum(1 for a in accounts if isinstance(a, dict) and a.get('dead'))
                entry['summary'] = {
                    'accounts_count': len(accounts),
                    'active_idx': data.get('active_idx'),
                    'dead_count': dead_count,
                }
            elif name == 'queue_snapshot':
                queue_data = data
                pending = data.get('pending') if isinstance(data.get('pending'), list) else []
                running = data.get('running') if isinstance(data.get('running'), list) else []
                entry['summary'] = {
                    'pending_count': len(pending),
                    'running_count': len(running),
                    'reason': data.get('reason'),
                    'ts': data.get('ts'),
                }
        elif status == 'error':
            entry['error'] = data.get('error')

        report['sources'][name] = entry

    if report['sources'].get('doctor_report', {}).get('status') == 'ok':
        report['overall_healthy'] = report['overall_healthy'] and bool(doctor_data.get('overall_healthy', False))
    if report['sources'].get('health_check', {}).get('status') == 'ok':
        report['overall_healthy'] = report['overall_healthy'] and bool(health_data.get('overall_healthy', False))
    if report['sources'].get('state', {}).get('status') == 'ok':
        report['overall_healthy'] = report['overall_healthy'] and int(state_data.get('evolution_consecutive_failures') or 0) < 3

    out = pathlib.Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    def _icon(src_name: str) -> str:
        st = report['sources'].get(src_name, {}).get('status')
        return '✅' if st == 'ok' else ('⚪' if st == 'missing' else '⚠️')

    spent = None
    failures = None
    if report['sources'].get('state', {}).get('status') == 'ok':
        spent = state_data.get('spent_usd')
        failures = state_data.get('evolution_consecutive_failures')

    pending_count = None
    running_count = None
    if report['sources'].get('queue_snapshot', {}).get('status') == 'ok':
        pending_count = len(queue_data.get('pending') or [])
        running_count = len(queue_data.get('running') or [])

    codex_accounts = None
    if report['sources'].get('codex_accounts_state', {}).get('status') == 'ok':
        codex_accounts = len(codex_data.get('accounts') or [])

    overall_icon = '✅' if report['overall_healthy'] else '⚠️'
    lines = [
        '## Monitor Snapshot',
        f'- **Timestamp (UTC):** {ts}',
        f'- **Overall:** {overall_icon} {"healthy" if report["overall_healthy"] else "attention needed"}',
        '- **Sources:**',
        f'  - {_icon("state")} state: {report["sources"].get("state", {}).get("status")}',
        f'  - {_icon("doctor_report")} doctor_report: {report["sources"].get("doctor_report", {}).get("status")}',
        f'  - {_icon("health_check")} health_check: {report["sources"].get("health_check", {}).get("status")}',
        f'  - {_icon("codex_accounts_state")} codex_accounts_state: {report["sources"].get("codex_accounts_state", {}).get("status")}',
        f'  - {_icon("queue_snapshot")} queue_snapshot: {report["sources"].get("queue_snapshot", {}).get("status")}',
        f'- **Key metrics:** spent=${spent if spent is not None else "n/a"}, pending={pending_count if pending_count is not None else "n/a"}, running={running_count if running_count is not None else "n/a"}, evolution_failures={failures if failures is not None else "n/a"}, codex_accounts={codex_accounts if codex_accounts is not None else "n/a"}',
        f'- **Report written:** `{output_path}`',
    ]
    return '\n'.join(lines)

def get_tools():
    return [
        ToolEntry('codebase_health', {
            'name': 'codebase_health',
            'description': 'Get codebase complexity metrics: file sizes, longest functions, modules exceeding limits. Useful for self-assessment per Bible Principle 5 (Minimalism).',
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        }, _codebase_health),
        ToolEntry('vps_health_check', {
            'name': 'vps_health_check',
            'description': 'Check VPS runtime health (Serper config, disk, RAM, uptime), persist JSON report, and return a markdown summary.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'output_path': {'type': 'string'},
                },
                'required': [],
            },
        }, _vps_health_check),
        ToolEntry('monitor_snapshot', {
            'name': 'monitor_snapshot',
            'description': 'Consolidated runtime snapshot from state/doctor/health/queue/codex files.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'output_path': {'type': 'string'},
                },
                'required': [],
            },
        }, _monitor_snapshot),
        ToolEntry('doctor', {
            'name': 'doctor',
            'description': 'Run consolidated diagnostics (version sync, gh CLI availability, identity freshness, budget drift) and persist JSON report.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'output_path': {'type': 'string'},
                    'stale_identity_hours': {'type': 'number'},
                },
                'required': [],
            },
        }, _doctor),
    ]
