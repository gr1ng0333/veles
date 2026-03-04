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


def _check_searxng(urls: list[str], timeout_sec: float) -> Dict[str, Any]:
    details = []
    active_url = None

    for url in urls:
        try:
            req = urllib.request.Request(url, method='GET')
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                status = getattr(resp, 'status', None) or resp.getcode()
            healthy = 200 <= int(status) < 400
            details.append({'url': url, 'ok': healthy, 'status': int(status)})
            if healthy and active_url is None:
                active_url = url
                break
        except Exception as e:
            details.append({'url': url, 'ok': False, 'error': str(e)})

    return {
        'healthy': active_url is not None,
        'active_url': active_url,
        'checked': urls,
        'details': details,
    }


def _vps_health_check(
    ctx: ToolContext,
    output_path: str = '/opt/veles-data/state/health_check.json',
    searxng_urls: list[str] | None = None,
    timeout_sec: float = 2.0,
) -> str:
    """Check VPS runtime health and persist JSON report."""
    try:
        urls = searxng_urls or [
            'http://127.0.0.1:8888',
            'http://127.0.0.1:8080',
            'http://localhost:8888',
            'http://localhost:8080',
        ]

        searxng = _check_searxng(urls, float(timeout_sec))

        # Disk stats
        disk_total, disk_used, disk_free = shutil.disk_usage('/')
        disk_used_pct = (disk_used / disk_total * 100.0) if disk_total else 0.0

        # RAM stats
        meminfo = _read_meminfo()
        mem_total = meminfo.get('MemTotal', 0)
        mem_available = meminfo.get('MemAvailable', 0)
        mem_used = max(mem_total - mem_available, 0)
        mem_used_pct = (mem_used / mem_total * 100.0) if mem_total else 0.0

        # Uptime
        with open('/proc/uptime', 'r', encoding='utf-8') as f:
            uptime_seconds = float(f.read().split()[0])

        overall_healthy = bool(
            searxng['healthy']
            and disk_used_pct < 95.0
            and mem_used_pct < 98.0
        )

        report = {
            'timestamp_utc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'searxng': searxng,
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

        searx_status = '✅' if searxng['healthy'] else '⚠️'
        disk_status = '✅' if disk_used_pct < 95.0 else '⚠️'
        ram_status = '✅' if mem_used_pct < 98.0 else '⚠️'
        overall_status = '✅' if overall_healthy else '⚠️'

        lines = [
            '## VPS Health Check',
            f"- **Timestamp (UTC):** {report['timestamp_utc']}",
            f"- **SearXNG:** {searx_status} {'reachable at ' + searxng['active_url'] if searxng['healthy'] else 'unreachable'}",
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

def get_tools():
    return [
        ToolEntry('codebase_health', {
            'name': 'codebase_health',
            'description': 'Get codebase complexity metrics: file sizes, longest functions, modules exceeding limits. Useful for self-assessment per Bible Principle 5 (Minimalism).',
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        }, _codebase_health),
        ToolEntry('vps_health_check', {
            'name': 'vps_health_check',
            'description': 'Check VPS runtime health (SearXNG, disk, RAM, uptime), persist JSON report, and return a markdown summary.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'output_path': {'type': 'string'},
                    'searxng_urls': {'type': 'array', 'items': {'type': 'string'}},
                    'timeout_sec': {'type': 'number'},
                },
                'required': [],
            },
        }, _vps_health_check),
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
