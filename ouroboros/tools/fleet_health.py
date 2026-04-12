from __future__ import annotations

import json
import shlex
import subprocess
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List
from urllib.parse import urlparse

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.remote_service import _remote_server_health, _remote_xray_status
from ouroboros.tools.ssh_targets import _base_ssh_command, _bootstrap_session, _get_target_record, _load_registry
from ouroboros.tools.xui_panel import _xui_panel_status
from ouroboros.utils import utc_now_iso

_DEFAULT_MAX_WORKERS = 6
_MAX_MAX_WORKERS = 32
_EXTENDED_TIMEOUT_SEC = 90
_EXTENDED_TARGETS = ['google.com', 'microsoft.com', 'cloudflare.com', 'github.com']
_MODULE_ORDER = [
    'conntrack_status',
    'kernel_health',
    'ulimit_check',
    'network_tuning_check',
    'tls_connectivity_check',
    'dns_check',
    'xray_log_analysis',
    'certificate_check',
    'haproxy_check',
    'connection_stats',
    'firewall_check',
    'disk_io_check',
    'double_chain_check',
    'reality_check',
    'backup_freshness',
]


REMOTE_EXTENDED_CHECK_SCRIPT = r"""
import glob
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

CONFIG = __CONFIG_JSON__
XRAY_CONFIG_CANDIDATES = [
    '/usr/local/x-ui/bin/config.json',
    '/etc/x-ui/xray/config.json',
]
XUI_DB_CANDIDATES = [
    '/etc/x-ui/x-ui.db',
    '/usr/local/x-ui/x-ui.db',
]
SEVERITY_ORDER = {'ok': 0, 'skip': 0, 'warn': 1, 'critical': 2}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def shell_join(command):
    return ' '.join(shlex_quote(part) for part in command)


def shlex_quote(value):
    import shlex
    return shlex.quote(str(value))


def run(command, timeout=10, shell=False):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=shell,
        )
    except subprocess.TimeoutExpired:
        return {'ok': False, 'timeout': True, 'returncode': None, 'stdout': '', 'stderr': 'timeout'}
    return {
        'ok': result.returncode == 0,
        'timeout': False,
        'returncode': result.returncode,
        'stdout': (result.stdout or '').strip(),
        'stderr': (result.stderr or '').strip(),
    }


def command_exists(name):
    return shutil.which(name) is not None


def read_text(path):
    try:
        return Path(path).read_text(encoding='utf-8', errors='replace').strip()
    except Exception:
        return ''


def read_int(path):
    try:
        return int(read_text(path).split()[0])
    except Exception:
        return None


def proc_sysctl(key):
    path = '/proc/sys/' + key.replace('.', '/').replace('-', '_')
    return read_text(path)


def to_int(value):
    try:
        return int(str(value).strip())
    except Exception:
        return None


def to_float(value):
    try:
        return float(str(value).strip())
    except Exception:
        return None


def parse_size_to_bytes(text):
    value = str(text or '').strip()
    if not value:
        return None
    parts = value.split()
    if len(parts) == 1:
        number = ''.join(ch for ch in parts[0] if ch.isdigit() or ch == '.')
        suffix = ''.join(ch for ch in parts[0] if ch.isalpha())
    else:
        number, suffix = parts[0], parts[1]
    try:
        base = float(number)
    except Exception:
        return None
    suffix = suffix.strip().upper().rstrip('B')
    multipliers = {
        '': 1,
        'K': 1024,
        'M': 1024 ** 2,
        'G': 1024 ** 3,
        'T': 1024 ** 4,
    }
    return int(base * multipliers.get(suffix, 1))


def make_module(name):
    return {'module': name, 'status': 'ok', 'summary': '', 'metrics': {}, 'issues': []}


def worsen(module, status):
    current = module['status']
    if SEVERITY_ORDER.get(status, 0) > SEVERITY_ORDER.get(current, 0):
        module['status'] = status


def add_issue(module, status, code, message):
    worsen(module, status)
    module['issues'].append({'severity': status, 'code': code, 'message': message})


def finish(module, summary):
    module['summary'] = summary
    return module


def load_xray_config():
    for path in XRAY_CONFIG_CANDIDATES:
        try:
            if Path(path).exists():
                return path, json.loads(Path(path).read_text(encoding='utf-8', errors='replace'))
        except Exception:
            continue
    return '', None


def iter_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from iter_dicts(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_dicts(item)


def parse_iso_time(value):
    text = str(value or '').strip()
    if not text:
        return None
    for candidate in (text, text.replace('Z', '+00:00')):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return None


def tcp_and_tls_probe(host, port=443, timeout=5):
    result = {'host': host, 'port': port, 'status': 'error', 'tcp_connect': None, 'tls_handshake': None, 'error': ''}
    start = time.monotonic()
    sock = None
    try:
        addr = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][-1]
        sock = socket.create_connection(addr, timeout=timeout)
        result['tcp_connect'] = round(time.monotonic() - start, 3)
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        tls_start = time.monotonic()
        wrapped = context.wrap_socket(sock, server_hostname=host)
        result['tls_handshake'] = round(time.monotonic() - tls_start, 3)
        wrapped.close()
        result['status'] = 'ok'
        return result
    except Exception as exc:
        if result['tcp_connect'] is not None and result['tls_handshake'] is None:
            result['status'] = 'tls_failed'
        else:
            result['status'] = 'connect_failed'
        result['error'] = str(exc)
        return result
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass


def count_zombies():
    result = run(['ps', 'axo', 'stat='], timeout=5)
    if not result['ok']:
        return None
    count = 0
    for line in result['stdout'].splitlines():
        if 'Z' in line:
            count += 1
    return count


def parse_open_files_limit(pid):
    limits_path = Path(f'/proc/{pid}/limits')
    if not limits_path.exists():
        return None
    text = limits_path.read_text(encoding='utf-8', errors='replace')
    for line in text.splitlines():
        if 'Max open files' in line:
            parts = [part for part in line.split() if part]
            for item in parts:
                value = to_int(item)
                if value is not None:
                    return value
    return None


def parse_fd_open_count(pid):
    try:
        return len(list(Path(f'/proc/{pid}/fd').iterdir()))
    except Exception:
        return None


def first_pid():
    for command in (
        ['pgrep', '-f', '/usr/local/x-ui/x-ui'],
        ['pgrep', '-x', 'x-ui'],
    ):
        result = run(command, timeout=5)
        if result['ok'] and result['stdout']:
            line = result['stdout'].splitlines()[0].strip()
            value = to_int(line)
            if value is not None:
                return value
    return None


def sample_cpu():
    text = read_text('/proc/stat')
    if not text:
        return None
    line = text.splitlines()[0]
    parts = [to_int(item) for item in line.split()[1:8]]
    if any(item is None for item in parts):
        return None
    return parts


def io_wait_pct():
    first = sample_cpu()
    if not first:
        return None
    time.sleep(0.5)
    second = sample_cpu()
    if not second:
        return None
    first_total = sum(first)
    second_total = sum(second)
    total_delta = second_total - first_total
    if total_delta <= 0:
        return None
    iowait_delta = second[4] - first[4]
    return round((iowait_delta / total_delta) * 100, 2)


def directory_size_bytes(path):
    result = run(['du', '-sb', path], timeout=10)
    if not result['ok'] or not result['stdout']:
        return None
    return to_int(result['stdout'].split()[0])


def journal_disk_usage_bytes():
    result = run(['journalctl', '--disk-usage'], timeout=10)
    if not result['stdout']:
        return None
    tail = result['stdout'].split(':', 1)[-1].strip()
    return parse_size_to_bytes(tail)


def ss_lines(*extra):
    result = run(['ss', *extra], timeout=10)
    if not result['ok']:
        return []
    return [line for line in result['stdout'].splitlines() if line.strip()]


def parse_remote_host(field):
    value = str(field or '').strip()
    if not value or value in {'Peer', 'Address:Port'}:
        return ''
    if value.startswith('['):
        value = value[1:]
    if ']' in value:
        value = value.split(']', 1)[0]
    if ':' in value:
        value = value.rsplit(':', 1)[0]
    return value


def collect_listening_ports():
    ports = set()
    for line in ss_lines('-tln'):
        parts = line.split()
        if len(parts) < 4:
            continue
        local_addr = parts[3]
        if ':' in local_addr:
            try:
                ports.add(int(local_addr.rsplit(':', 1)[-1]))
            except Exception:
                pass
    return sorted(ports)


def extract_dns_config(config):
    if not isinstance(config, dict):
        return []
    dns = config.get('dns')
    if isinstance(dns, dict):
        servers = dns.get('servers') or []
        return [str(item) for item in servers if str(item).strip()]
    return []


def extract_outbound_address(config):
    if not isinstance(config, dict):
        return ''
    for outbound in config.get('outbounds') or []:
        if not isinstance(outbound, dict):
            continue
        settings = outbound.get('settings') or {}
        vnext = settings.get('vnext') or []
        if vnext and isinstance(vnext[0], dict):
            address = str(vnext[0].get('address') or '').strip()
            if address:
                return address
        servers = settings.get('servers') or []
        if servers and isinstance(servers[0], dict):
            address = str(servers[0].get('address') or '').strip()
            if address:
                return address
        address = str(settings.get('address') or '').strip()
        if address:
            return address
    return ''


def extract_reality_dest(config):
    for node in iter_dicts(config):
        reality = node.get('realitySettings') if isinstance(node, dict) else None
        if isinstance(reality, dict):
            dest = str(reality.get('dest') or '').strip()
            if dest:
                return dest
    return ''


def parse_cert_file(path):
    try:
        decoded = ssl._ssl._test_decode_cert(path)
        not_after = decoded.get('notAfter')
        expires = datetime.strptime(not_after, '%b %d %H:%M:%S %Y %Z').replace(tzinfo=timezone.utc)
        return {'path': path, 'expires_at': expires.isoformat(), 'days_left': int((expires - datetime.now(timezone.utc)).total_seconds() // 86400)}
    except Exception:
        return None


def fetch_panel_certificate(port):
    if not port:
        return None
    try:
        pem = ssl.get_server_certificate(('127.0.0.1', int(port)))
    except Exception:
        return None
    with tempfile.NamedTemporaryFile('w+', delete=False) as handle:
        handle.write(pem)
        temp_path = handle.name
    try:
        return parse_cert_file(temp_path)
    finally:
        try:
            os.unlink(temp_path)
        except Exception:
            pass


def module_conntrack_status():
    module = make_module('conntrack_status')
    count = read_int('/proc/sys/net/netfilter/nf_conntrack_count')
    maximum = read_int('/proc/sys/net/netfilter/nf_conntrack_max')
    established_timeout = to_int(proc_sysctl('net.netfilter.nf_conntrack_tcp_timeout_established'))
    time_wait_timeout = to_int(proc_sysctl('net.netfilter.nf_conntrack_tcp_timeout_time_wait'))
    ratio = None
    if count is not None and maximum:
        ratio = round(count / maximum, 3)
        if ratio > 0.9:
            add_issue(module, 'critical', 'conntrack_near_exhaustion', f'nf_conntrack usage is {ratio:.0%}')
        elif ratio > 0.7:
            add_issue(module, 'warn', 'conntrack_high', f'nf_conntrack usage is {ratio:.0%}')
    if established_timeout is not None and established_timeout > 3600:
        add_issue(module, 'warn', 'conntrack_established_timeout_high', f'established timeout is {established_timeout}s')
    if time_wait_timeout is not None and time_wait_timeout > 120:
        add_issue(module, 'warn', 'conntrack_time_wait_timeout_high', f'time_wait timeout is {time_wait_timeout}s')
    module['metrics'] = {
        'count': count,
        'max': maximum,
        'usage_ratio': ratio,
        'tcp_timeout_established': established_timeout,
        'tcp_timeout_time_wait': time_wait_timeout,
    }
    if count is None or maximum is None:
        module['status'] = 'skip'
        return finish(module, 'conntrack counters are unavailable on this host')
    return finish(module, f'conntrack {count}/{maximum} ({ratio:.0%})' if ratio is not None else 'conntrack counters collected')


def module_kernel_health():
    module = make_module('kernel_health')
    oom_24h = run(['journalctl', '-k', '--since', '24 hours ago', '--no-pager'], timeout=15)
    oom_lines = []
    if oom_24h['stdout']:
        oom_lines = [line for line in oom_24h['stdout'].splitlines() if ('oom' in line.lower() or 'out of memory' in line.lower())]
    if oom_lines:
        add_issue(module, 'critical', 'kernel_oom_recent', f'OOM events in last 24h: {len(oom_lines)}')
    kernel_hour = run(['journalctl', '-k', '--since', '1 hour ago', '--no-pager'], timeout=15)
    error_lines = []
    if kernel_hour['stdout']:
        error_lines = [line for line in kernel_hour['stdout'].splitlines() if any(token in line.lower() for token in ('error', 'panic', 'segfault'))]
    if error_lines:
        add_issue(module, 'warn', 'kernel_errors_recent', f'kernel errors in last hour: {len(error_lines)}')
    zombies = count_zombies()
    if zombies is not None and zombies > 10:
        add_issue(module, 'warn', 'zombie_processes_high', f'zombie processes: {zombies}')
    module['metrics'] = {
        'oom_events_24h': len(oom_lines),
        'kernel_errors_1h': len(error_lines),
        'zombie_processes': zombies,
    }
    return finish(module, f'oom={len(oom_lines)}, kernel_errors={len(error_lines)}, zombies={zombies if zombies is not None else "n/a"}')


def module_ulimit_check():
    module = make_module('ulimit_check')
    pid = first_pid()
    if not pid:
        module['status'] = 'skip'
        return finish(module, 'x-ui process was not found')
    limit = parse_open_files_limit(pid)
    open_fds = parse_fd_open_count(pid)
    ratio = None
    if limit and open_fds is not None:
        ratio = round(open_fds / limit, 3)
        if ratio > 0.8:
            add_issue(module, 'critical', 'open_files_near_limit', f'open files usage is {ratio:.0%}')
        elif ratio > 0.5:
            add_issue(module, 'warn', 'open_files_high', f'open files usage is {ratio:.0%}')
    if limit is not None and limit < 65535:
        add_issue(module, 'warn', 'open_files_limit_low', f'open files limit is {limit}')
    module['metrics'] = {'xui_pid': pid, 'open_files_limit': limit, 'open_files_open': open_fds, 'usage_ratio': ratio}
    return finish(module, f'fd usage={open_fds if open_fds is not None else "n/a"}/{limit if limit is not None else "n/a"}')


def module_network_tuning_check():
    module = make_module('network_tuning_check')
    congestion = proc_sysctl('net.ipv4.tcp_congestion_control')
    fastopen = to_int(proc_sysctl('net.ipv4.tcp_fastopen'))
    rmem_max = to_int(proc_sysctl('net.core.rmem_max'))
    wmem_max = to_int(proc_sysctl('net.core.wmem_max'))
    mtu_probing = to_int(proc_sysctl('net.ipv4.tcp_mtu_probing'))
    syncookies = to_int(proc_sysctl('net.ipv4.tcp_syncookies'))
    if congestion and congestion.lower() != 'bbr':
        add_issue(module, 'warn', 'bbr_disabled', f'tcp_congestion_control={congestion}')
    if fastopen is not None and fastopen != 3:
        add_issue(module, 'warn', 'tcp_fastopen_not_full', f'tcp_fastopen={fastopen}')
    if (rmem_max is not None and rmem_max < 16777216) or (wmem_max is not None and wmem_max < 16777216):
        add_issue(module, 'warn', 'tcp_buffers_low', f'rmem_max={rmem_max}, wmem_max={wmem_max}')
    if mtu_probing is not None and mtu_probing != 1:
        add_issue(module, 'warn', 'mtu_probing_disabled', f'tcp_mtu_probing={mtu_probing}')
    if syncookies is not None and syncookies != 1:
        add_issue(module, 'warn', 'syncookies_disabled', f'tcp_syncookies={syncookies}')
    module['metrics'] = {
        'tcp_congestion_control': congestion,
        'tcp_fastopen': fastopen,
        'rmem_max': rmem_max,
        'wmem_max': wmem_max,
        'tcp_mtu_probing': mtu_probing,
        'tcp_syncookies': syncookies,
    }
    return finish(module, f'cc={congestion or "n/a"}, fastopen={fastopen}, syncookies={syncookies}')


def module_tls_connectivity_check():
    module = make_module('tls_connectivity_check')
    targets = []
    failures = 0
    tls_failures = 0
    for host in CONFIG.get('tls_targets') or []:
        probe = tcp_and_tls_probe(host, 443, timeout=5)
        targets.append(probe)
        if probe['status'] != 'ok':
            failures += 1
            if probe['status'] == 'tls_failed':
                tls_failures += 1
    if targets:
        if failures == len(targets):
            add_issue(module, 'critical', 'tls_connectivity_down', 'all TLS probes failed')
        elif failures > 0:
            add_issue(module, 'warn', 'tls_connectivity_partial', f'{failures}/{len(targets)} TLS probes failed')
        elif tls_failures > 0:
            add_issue(module, 'warn', 'tls_handshake_partial', f'{tls_failures}/{len(targets)} TLS handshakes failed')
    module['metrics'] = {'targets': targets, 'failed_targets': failures, 'tls_failed_targets': tls_failures}
    return finish(module, f'{len(targets) - failures}/{len(targets)} outbound TLS targets reachable')


def module_dns_check(config):
    module = make_module('dns_check')
    system_hosts = {}
    system_failures = 0
    for host in ('google.com', 'microsoft.com'):
        try:
            addrs = sorted({item[4][0] for item in socket.getaddrinfo(host, None)})
            system_hosts[host] = addrs[:5]
        except Exception as exc:
            system_failures += 1
            system_hosts[host] = {'error': str(exc)}
    if system_failures:
        add_issue(module, 'critical', 'system_dns_failed', f'system resolver failed for {system_failures} hosts')
    dig_results = {}
    if command_exists('dig'):
        for server in ('8.8.8.8', '1.1.1.1', '77.88.8.8'):
            result = run(['dig', '+short', 'google.com', f'@{server}'], timeout=8)
            answers = [line for line in result['stdout'].splitlines() if line.strip()]
            dig_results[server] = answers
        if not dig_results.get('8.8.8.8') and dig_results.get('77.88.8.8'):
            add_issue(module, 'warn', 'dns_hijacking_suspected', '8.8.8.8 fails while 77.88.8.8 works')
    else:
        dig_results['status'] = 'dig unavailable'
    dns_servers = extract_dns_config(config)
    if not dns_servers:
        add_issue(module, 'warn', 'xray_dns_not_configured', 'dns block is absent in Xray config')
    module['metrics'] = {'system_dns': system_hosts, 'dig': dig_results, 'xray_dns_servers': dns_servers}
    return finish(module, f'system_dns_failures={system_failures}, xray_dns_servers={len(dns_servers)}')


def module_xray_log_analysis():
    module = make_module('xray_log_analysis')
    recent_errors = run(['journalctl', '-u', 'x-ui', '--no-pager', '-n', '100', '--since', '1 hour ago'], timeout=15)
    error_lines = []
    if recent_errors['stdout']:
        error_lines = [line for line in recent_errors['stdout'].splitlines() if any(token in line.lower() for token in ('error', 'fail', 'panic', 'fatal'))]
    restarts = run(['journalctl', '-u', 'x-ui', '--no-pager', '--since', '24 hours ago'], timeout=15)
    restart_count = 0
    if restarts['stdout']:
        restart_count = sum(1 for line in restarts['stdout'].splitlines() if 'Started x-ui' in line)
    bind_issue = any('address already in use' in line.lower() for line in error_lines)
    open_files_issue = any('too many open files' in line.lower() for line in error_lines)
    if restart_count > 5:
        add_issue(module, 'critical', 'xui_restarts_high', f'x-ui restarted {restart_count} times in 24h')
    elif restart_count > 2:
        add_issue(module, 'warn', 'xui_restarts_warn', f'x-ui restarted {restart_count} times in 24h')
    if bind_issue:
        add_issue(module, 'critical', 'xui_bind_conflict', 'address already in use found in x-ui logs')
    if open_files_issue:
        add_issue(module, 'critical', 'xui_open_files_exhausted', 'too many open files found in x-ui logs')
    if error_lines and not bind_issue and not open_files_issue:
        add_issue(module, 'warn', 'xui_recent_errors', f'{len(error_lines)} recent error lines in x-ui logs')
    module['metrics'] = {
        'recent_error_lines': error_lines[-20:],
        'restart_count_24h': restart_count,
        'bind_issue': bind_issue,
        'too_many_open_files': open_files_issue,
    }
    return finish(module, f'restarts_24h={restart_count}, recent_errors={len(error_lines)}')


def module_certificate_check(panel_port, panel_scheme):
    module = make_module('certificate_check')
    certificates = []
    if str(panel_scheme or '').lower() == 'https' and panel_port:
        cert = fetch_panel_certificate(panel_port)
        if cert:
            cert['source'] = 'panel_local'
            certificates.append(cert)
    for path in glob.glob('/etc/letsencrypt/live/*/cert.pem'):
        cert = parse_cert_file(path)
        if cert:
            cert['source'] = 'letsencrypt'
            certificates.append(cert)
    for path in glob.glob(str(Path.home() / '.acme.sh' / '*' / 'fullchain.cer')):
        cert = parse_cert_file(path)
        if cert:
            cert['source'] = 'acme.sh'
            certificates.append(cert)
    if not certificates:
        add_issue(module, 'warn', 'certificates_not_found', 'no local certificates were found')
    else:
        min_days = min(item['days_left'] for item in certificates)
        if min_days < 3:
            add_issue(module, 'critical', 'certificate_expiring_soon', f'certificate expires in {min_days} days')
        elif min_days < 14:
            add_issue(module, 'warn', 'certificate_expiring_warn', f'certificate expires in {min_days} days')
    module['metrics'] = {'certificates': certificates}
    return finish(module, f'certificates_found={len(certificates)}')


def module_haproxy_check():
    module = make_module('haproxy_check')
    config_path = Path('/etc/haproxy/haproxy.cfg')
    service_probe = run(['systemctl', 'is-active', 'haproxy'], timeout=8)
    installed = config_path.exists() or service_probe['returncode'] in {0, 3}
    if not installed:
        module['status'] = 'skip'
        return finish(module, 'haproxy is not installed')
    active = service_probe['stdout'].strip() == 'active'
    if not active:
        add_issue(module, 'critical', 'haproxy_inactive', f'haproxy state={service_probe["stdout"] or service_probe["stderr"] or "unknown"}')
    config_check = run(['haproxy', '-c', '-f', str(config_path)], timeout=10) if command_exists('haproxy') and config_path.exists() else {'ok': False, 'stdout': '', 'stderr': 'haproxy binary or config missing'}
    if command_exists('haproxy') and config_path.exists() and not config_check['ok']:
        add_issue(module, 'critical', 'haproxy_invalid_config', config_check['stderr'] or config_check['stdout'] or 'haproxy config check failed')
    stats_output = ''
    if command_exists('socat') and Path('/var/run/haproxy/admin.sock').exists():
        stats = run(['bash', '-lc', 'echo "show stat" | socat stdio /var/run/haproxy/admin.sock'], timeout=10)
        stats_output = stats['stdout']
        if 'DOWN' in stats_output:
            add_issue(module, 'critical', 'haproxy_backend_down', 'haproxy stats report DOWN backend')
    listen_probe = run(['bash', '-lc', 'ss -tlnp | grep haproxy'], timeout=10)
    module['metrics'] = {
        'installed': installed,
        'active': active,
        'config_ok': config_check.get('ok', False),
        'stats_has_down': 'DOWN' in stats_output if stats_output else False,
        'listening': bool(listen_probe['stdout']),
    }
    return finish(module, f'installed={installed}, active={active}')


def module_connection_stats(required_ports):
    module = make_module('connection_stats')
    lines = ss_lines('-tn')
    state_summary = run(['ss', '-s'], timeout=10)
    syn_recv_lines = ss_lines('-tn', 'state', 'syn-recv')
    time_wait_lines = ss_lines('-tn', 'state', 'time-wait')
    active_connections = 0
    remote_counts = Counter()
    for line in lines:
        parts = line.split()
        if len(parts) < 5 or parts[0] == 'State':
            continue
        local = parts[3]
        remote = parts[4]
        port = to_int(local.rsplit(':', 1)[-1]) if ':' in local else None
        if required_ports and port in required_ports:
            active_connections += 1
            host = parse_remote_host(remote)
            if host:
                remote_counts[host] += 1
    top_ip = remote_counts.most_common(1)[0] if remote_counts else None
    if active_connections > 5000:
        add_issue(module, 'warn', 'active_connections_high', f'active connections={active_connections}')
    if len(syn_recv_lines) > 100:
        add_issue(module, 'critical', 'syn_recv_high', f'SYN_RECV connections={len(syn_recv_lines)}')
    if len(time_wait_lines) > 10000:
        add_issue(module, 'warn', 'time_wait_high', f'TIME_WAIT connections={len(time_wait_lines)}')
    if top_ip and top_ip[1] > 200:
        add_issue(module, 'warn', 'single_ip_flood', f'{top_ip[0]} has {top_ip[1]} connections')
    module['metrics'] = {
        'required_ports': required_ports,
        'ss_summary': state_summary['stdout'],
        'active_connections': active_connections,
        'syn_recv_connections': len(syn_recv_lines),
        'time_wait_connections': len(time_wait_lines),
        'top_remote_ips': remote_counts.most_common(10),
    }
    return finish(module, f'active={active_connections}, syn_recv={len(syn_recv_lines)}, time_wait={len(time_wait_lines)}')


def module_firewall_check(required_ports):
    module = make_module('firewall_check')
    ufw = run(['ufw', 'status'], timeout=8) if command_exists('ufw') else {'stdout': '', 'stderr': 'ufw unavailable', 'ok': False}
    nft = run(['nft', 'list', 'ruleset'], timeout=10) if command_exists('nft') else {'stdout': '', 'stderr': 'nft unavailable', 'ok': False}
    iptables = run(['iptables', '-L', '-n'], timeout=10) if command_exists('iptables') else {'stdout': '', 'stderr': 'iptables unavailable', 'ok': False}
    firewall_active = False
    accepts_everything = False
    missing_ports = []
    if ufw['stdout']:
        text = ufw['stdout'].lower()
        firewall_active = 'status: active' in text
        if firewall_active:
            for port in required_ports:
                if str(port) not in ufw['stdout']:
                    missing_ports.append(port)
            if 'allow in on' not in text and 'deny' not in text and 'reject' not in text:
                accepts_everything = True
    elif nft['stdout']:
        firewall_active = True
        accepts_everything = 'accept' in nft['stdout'].lower() and 'drop' not in nft['stdout'].lower() and 'reject' not in nft['stdout'].lower()
        for port in required_ports:
            if str(port) not in nft['stdout']:
                missing_ports.append(port)
    elif iptables['stdout']:
        firewall_active = True
        accepts_everything = 'policy accept' in iptables['stdout'].lower() and 'drop' not in iptables['stdout'].lower() and 'reject' not in iptables['stdout'].lower()
        for port in required_ports:
            if str(port) not in iptables['stdout']:
                missing_ports.append(port)
    if not firewall_active:
        add_issue(module, 'warn', 'firewall_inactive', 'no active firewall detected')
    if firewall_active and missing_ports:
        add_issue(module, 'critical', 'firewall_missing_required_ports', f'no explicit rules found for ports: {missing_ports}')
    if firewall_active and accepts_everything:
        add_issue(module, 'warn', 'firewall_all_accept', 'firewall rules appear to accept everything')
    module['metrics'] = {
        'required_ports': required_ports,
        'ufw_active': 'status: active' in ufw['stdout'].lower() if ufw['stdout'] else False,
        'nft_present': bool(nft['stdout']),
        'iptables_present': bool(iptables['stdout']),
        'missing_ports': missing_ports,
    }
    return finish(module, f'firewall_active={firewall_active}, missing_ports={missing_ports}')


def module_disk_io_check():
    module = make_module('disk_io_check')
    iowait = io_wait_pct()
    log_size = directory_size_bytes('/var/log')
    journal_size = journal_disk_usage_bytes()
    xui_db_size = None
    for path in XUI_DB_CANDIDATES:
        if Path(path).exists():
            xui_db_size = Path(path).stat().st_size
            break
    if iowait is not None and iowait > 20:
        add_issue(module, 'warn', 'iowait_high', f'iowait={iowait}%')
    if log_size is not None and log_size > 5 * 1024 ** 3:
        add_issue(module, 'warn', 'var_log_large', f'/var/log size={log_size} bytes')
    if journal_size is not None and journal_size > 2 * 1024 ** 3:
        add_issue(module, 'warn', 'journal_large', f'journal usage={journal_size} bytes')
    if xui_db_size is not None and xui_db_size > 100 * 1024 ** 2:
        add_issue(module, 'warn', 'xui_db_large', f'x-ui.db size={xui_db_size} bytes')
    module['metrics'] = {
        'iowait_pct': iowait,
        'var_log_bytes': log_size,
        'journal_bytes': journal_size,
        'xui_db_bytes': xui_db_size,
    }
    return finish(module, f'iowait={iowait}, var_log={log_size}, journal={journal_size}')


def module_double_chain_check(config):
    module = make_module('double_chain_check')
    outbound = extract_outbound_address(config)
    if not outbound:
        module['status'] = 'skip'
        return finish(module, 'no outbound server address found in Xray config')
    tcp_probe = tcp_and_tls_probe(outbound, 443, timeout=5)
    if tcp_probe['status'] == 'connect_failed':
        add_issue(module, 'critical', 'double_chain_outbound_unreachable', f'outbound {outbound}:443 is unreachable')
    elif tcp_probe['status'] == 'tls_failed':
        add_issue(module, 'warn', 'double_chain_tls_failed', f'outbound {outbound}:443 TCP works but TLS failed')
    module['metrics'] = {'outbound_address': outbound, 'probe': tcp_probe}
    return finish(module, f'outbound={outbound}, status={tcp_probe["status"]}')


def module_reality_check(config):
    module = make_module('reality_check')
    dest = extract_reality_dest(config)
    if not dest:
        module['status'] = 'skip'
        return finish(module, 'no Reality dest found in Xray config')
    host, _, port_text = dest.partition(':')
    port = to_int(port_text) or 443
    probe = tcp_and_tls_probe(host, port, timeout=5)
    if probe['status'] != 'ok':
        add_issue(module, 'critical', 'reality_dest_unreachable', f'Reality dest {dest} is unreachable')
    elif (probe.get('tls_handshake') or 0) > 3:
        add_issue(module, 'warn', 'reality_dest_slow', f'Reality dest {dest} TLS handshake is slow ({probe.get("tls_handshake")}s)')
    module['metrics'] = {'reality_dest': dest, 'probe': probe}
    return finish(module, f'dest={dest}, status={probe["status"]}')


def module_backup_freshness():
    module = make_module('backup_freshness')
    latest = None
    backup_root = Path('/root/backups')
    if backup_root.exists():
        for path in backup_root.rglob('*.db'):
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if latest is None or mtime > latest['mtime']:
                latest = {'path': str(path), 'mtime': mtime}
    cron = run(['crontab', '-l'], timeout=8)
    cron_present = False
    if cron['stdout']:
        cron_present = any(token in cron['stdout'].lower() for token in ('backup', 'x-ui'))
    if latest is None:
        add_issue(module, 'critical', 'backup_missing', 'no database backups found in /root/backups')
    else:
        age_hours = round((datetime.now(timezone.utc) - latest['mtime']).total_seconds() / 3600, 2)
        if age_hours > 24:
            add_issue(module, 'warn', 'backup_stale', f'latest backup is {age_hours}h old')
        latest['age_hours'] = age_hours
    if not cron_present:
        add_issue(module, 'warn', 'backup_cron_missing', 'no backup cron entry found')
    module['metrics'] = {
        'latest_backup': {
            'path': latest['path'],
            'mtime': latest['mtime'].isoformat(),
            'age_hours': latest.get('age_hours'),
        } if latest else None,
        'backup_cron_present': cron_present,
    }
    return finish(module, f'latest_backup={latest["path"] if latest else "none"}, cron={cron_present}')


def main():
    panel_port = CONFIG.get('panel_port')
    panel_scheme = CONFIG.get('panel_scheme')
    required_ports = list(dict.fromkeys([port for port in (CONFIG.get('known_ports') or []) if isinstance(port, int)] + ([panel_port] if panel_port else [])))
    config_path, xray_config = load_xray_config()
    modules = {}
    modules['conntrack_status'] = module_conntrack_status()
    modules['kernel_health'] = module_kernel_health()
    modules['ulimit_check'] = module_ulimit_check()
    modules['network_tuning_check'] = module_network_tuning_check()
    modules['tls_connectivity_check'] = module_tls_connectivity_check()
    modules['dns_check'] = module_dns_check(xray_config)
    modules['xray_log_analysis'] = module_xray_log_analysis()
    modules['certificate_check'] = module_certificate_check(panel_port, panel_scheme)
    modules['haproxy_check'] = module_haproxy_check()
    modules['connection_stats'] = module_connection_stats(required_ports)
    modules['firewall_check'] = module_firewall_check(required_ports)
    modules['disk_io_check'] = module_disk_io_check()
    modules['double_chain_check'] = module_double_chain_check(xray_config)
    modules['reality_check'] = module_reality_check(xray_config)
    modules['backup_freshness'] = module_backup_freshness()

    counts = {'ok': 0, 'skip': 0, 'warn': 0, 'critical': 0}
    overall = 'ok'
    issues = []
    for name, module in modules.items():
        status = module.get('status') or 'ok'
        counts[status] = counts.get(status, 0) + 1
        if status == 'critical':
            overall = 'critical'
        elif status == 'warn' and overall != 'critical':
            overall = 'warn'
        for issue in module.get('issues') or []:
            enriched = dict(issue)
            enriched['module'] = name
            issues.append(enriched)

    payload = {
        'status': 'ok',
        'checked_at': now_iso(),
        'overall_verdict': overall,
        'module_status_counts': counts,
        'xray_config_path': config_path,
        'known_ports': required_ports,
        'modules': modules,
        'issues': issues,
    }
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == '__main__':
    main()
"""


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
    if value is None or value == '':
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
    targets = list((registry.get('targets') or {}).values())
    if aliases:
        alias_set = {item.lower() for item in aliases}
        targets = [item for item in targets if str(item.get('alias') or '').lower() in alias_set]
    if tags:
        tag_set = {item.lower() for item in tags}
        targets = [
            item
            for item in targets
            if tag_set.issubset({str(tag).lower() for tag in (item.get('tags') or [])})
        ]
    return sorted(targets, key=lambda item: str(item.get('alias') or ''))


def _decode_payload(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = str(raw or '').strip()
    if not text:
        return {'status': 'error', 'error': 'empty payload'}
    try:
        payload = json.loads(text)
    except Exception:
        return {'status': 'error', 'error': f'invalid json payload: {text[:200]}'}
    if not isinstance(payload, dict):
        return {'status': 'error', 'error': 'payload is not a JSON object'}
    return payload


def _issue(code: str, severity: str, message: str, source: str) -> Dict[str, str]:
    return {
        'code': code,
        'severity': severity,
        'message': str(message or '').strip(),
        'source': source,
    }


def _combine_verdict(left: str, right: str) -> str:
    order = {'ok': 0, 'warn': 1, 'critical': 2}
    left_norm = str(left or 'ok').strip().lower()
    right_norm = str(right or 'ok').strip().lower()
    return left_norm if order.get(left_norm, 0) >= order.get(right_norm, 0) else right_norm


def _infer_xray_state(payload: Dict[str, Any]) -> str:
    for key in ('state', 'xray_state', 'status_text'):
        value = str(payload.get(key) or '').strip()
        if value:
            return value
    if isinstance(payload.get('xray'), dict):
        nested = str((payload.get('xray') or {}).get('state') or '').strip()
        if nested:
            return nested
    return ''


def _panel_expected(target: Dict[str, Any]) -> bool:
    panel_type = str(target.get('panel_type') or '').strip().lower()
    return panel_type in {'3x-ui', '3xui', 'x-ui', 'xui'}


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
        'summary': payload.get('summary') or '',
    }
    return verdict, issues, summary


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


def _inspect_panel(target: Dict[str, Any], payload: Dict[str, Any] | None) -> tuple[str, List[Dict[str, str]], Dict[str, Any]]:
    issues: List[Dict[str, str]] = []
    verdict = 'ok'
    if not _panel_expected(target):
        return 'ok', issues, {'status': 'skipped', 'reason': 'panel_not_expected'}
    if not str(target.get('panel_url') or '').strip():
        verdict = 'warn'
        issues.append(_issue('panel_url_missing', 'warn', 'panel_url is not configured in registry', 'panel'))
        return verdict, issues, {'status': 'missing_config', 'reason': 'panel_url_missing'}
    if not bool(target.get("has_panel_credentials") or (target.get("panel_username") and target.get("panel_password"))):
        verdict = 'warn'
        issues.append(_issue('panel_credentials_missing', 'warn', 'panel credentials are not configured in registry', 'panel'))
        return verdict, issues, {'status': 'missing_config', 'reason': 'panel_credentials_missing'}
    if not payload:
        verdict = 'warn'
        issues.append(_issue('panel_payload_missing', 'warn', 'panel monitoring was expected but not executed', 'panel'))
        return verdict, issues, {'status': 'missing_payload'}
    status = str(payload.get('status') or '').strip().lower()
    panel_verdict = str(payload.get('verdict') or '').strip().lower()
    if status != 'ok' or panel_verdict in {'warn', 'warning', 'critical', 'error', 'failed'}:
        verdict = 'warn' if status == 'ok' else 'critical'
        issues.append(_issue('panel_check_failed', verdict, payload.get('error') or payload.get('summary') or '3x-ui panel check reported a problem', 'panel'))
    summary = {
        'status': status or 'unknown',
        'verdict': panel_verdict or ('warn' if verdict != 'ok' else 'ok'),
        'inbounds': payload.get('inbounds_count'),
        'enabled_inbounds': payload.get('enabled_inbounds_count'),
    }
    return verdict, issues, summary


def _inspect_extended(payload: Dict[str, Any]) -> tuple[str, List[Dict[str, str]], Dict[str, Any]]:
    issues: List[Dict[str, str]] = []
    status = str(payload.get('status') or '').strip().lower()
    if status != 'ok':
        issues.append(_issue('extended_checks_failed', 'warn', payload.get('error') or 'extended monitoring probe failed', 'extended'))
        return 'warn', issues, {'status': status or 'error', 'overall_verdict': 'warn', 'module_status_counts': {}}
    overall = str(payload.get('overall_verdict') or 'ok').strip().lower()
    for issue in payload.get('issues') or []:
        severity = str(issue.get('severity') or issue.get('status') or 'warn').strip().lower()
        if severity not in {'warn', 'critical'}:
            continue
        module = str(issue.get('module') or 'extended').strip()
        code = str(issue.get('code') or f'{module}_issue').strip()
        message = issue.get('message') or issue.get('summary') or f'{module} reported {severity}'
        issues.append(_issue(code, severity, message, module))
    summary = {
        'status': status,
        'overall_verdict': overall,
        'module_status_counts': payload.get('module_status_counts') or {},
        'modules': {
            name: {
                'status': module.get('status') or 'unknown',
                'summary': module.get('summary') or '',
            }
            for name, module in (payload.get('modules') or {}).items()
        },
    }
    return overall if overall in {'ok', 'warn', 'critical'} else 'warn', issues, summary


def _parse_panel_port(panel_url: str) -> int | None:
    text = str(panel_url or '').strip()
    if not text:
        return None
    try:
        parsed = urlparse(text)
    except Exception:
        return None
    if parsed.port:
        return parsed.port
    if parsed.scheme == 'https':
        return 443
    if parsed.scheme == 'http':
        return 80
    return None


def _dedupe_ports(values: List[Any]) -> List[int]:
    ports: List[int] = []
    seen: set[int] = set()
    for value in values:
        try:
            port = int(value)
        except Exception:
            continue
        if port > 0 and port not in seen:
            seen.add(port)
            ports.append(port)
    return ports


def _run_extended_checks(ctx: ToolContext, alias: str, target: Dict[str, Any]) -> Dict[str, Any]:
    try:
        record = _get_target_record(ctx, alias)
        _bootstrap_session(ctx, alias)
        panel_url = str(target.get('panel_url') or '')
        config = {
            'alias': alias,
            'panel_url': panel_url,
            'panel_port': _parse_panel_port(panel_url),
            'panel_scheme': (urlparse(panel_url).scheme if panel_url else ''),
            'known_ports': _dedupe_ports(list(target.get('known_ports') or [])),
            'tls_targets': list(_EXTENDED_TARGETS),
        }
        script = textwrap.dedent(REMOTE_EXTENDED_CHECK_SCRIPT).replace('__CONFIG_JSON__', json.dumps(config, ensure_ascii=False))
        command = _base_ssh_command(ctx, record)
        completed = subprocess.run(
            [*command, 'python3', '-'],
            input=script,
            text=True,
            capture_output=True,
            timeout=_EXTENDED_TIMEOUT_SEC,
            cwd=str(ctx.repo_dir),
        )
    except subprocess.TimeoutExpired:
        return {'status': 'error', 'error': f'extended probe exceeded {_EXTENDED_TIMEOUT_SEC}s timeout'}
    except Exception as exc:
        return {'status': 'error', 'error': f'extended probe failed to start: {exc}'}

    stdout = (completed.stdout or '').strip()
    stderr = (completed.stderr or '').strip()
    if completed.returncode != 0:
        return {
            'status': 'error',
            'error': f'extended probe failed with rc={completed.returncode}: {stderr[:400] or stdout[:400] or "no output"}',
        }
    try:
        payload = json.loads(stdout)
    except Exception:
        return {'status': 'error', 'error': f'extended probe returned invalid JSON: {stdout[:400] or stderr[:400] or "empty output"}'}
    if not isinstance(payload, dict):
        return {'status': 'error', 'error': 'extended probe returned a non-object payload'}
    return payload


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
        if _panel_expected(target) and str(target.get('panel_url') or '').strip() and bool(target.get('has_panel_credentials') or (target.get('panel_username') and target.get('panel_password'))):
            panel_payload = _decode_payload(_xui_panel_status(ctx, alias=alias))
        panel_verdict, panel_issues, panel_summary = _inspect_panel(target, panel_payload)
        result['verdict'] = _combine_verdict(result['verdict'], panel_verdict)
        result['issues'].extend(panel_issues)
        result['checks']['panel'] = panel_summary
        if panel_payload is not None:
            result['raw']['panel'] = panel_payload

    extended_payload = _run_extended_checks(ctx, alias, target)
    extended_verdict, extended_issues, extended_summary = _inspect_extended(extended_payload)
    result['verdict'] = _combine_verdict(result['verdict'], extended_verdict)
    result['issues'].extend(extended_issues)
    result['checks']['extended'] = extended_summary
    result['raw']['extended'] = extended_payload
    return result


def _build_summary(targets: List[Dict[str, Any]], registered_targets: int) -> Dict[str, Any]:
    by_verdict = {'ok': 0, 'warn': 0, 'critical': 0}
    overall = 'ok'
    module_totals: Dict[str, Dict[str, int]] = {
        name: {'ok': 0, 'skip': 0, 'warn': 0, 'critical': 0}
        for name in _MODULE_ORDER
    }
    total_issues = 0
    for item in targets:
        verdict = str(item.get('verdict') or 'ok').strip().lower()
        if verdict not in by_verdict:
            verdict = 'warn'
        by_verdict[verdict] += 1
        total_issues += len(item.get('issues') or [])
        if verdict == 'critical':
            overall = 'critical'
        elif verdict == 'warn' and overall != 'critical':
            overall = 'warn'
        extended = ((item.get('checks') or {}).get('extended') or {}).get('modules') or {}
        for name in _MODULE_ORDER:
            status = str((extended.get(name) or {}).get('status') or 'skip').strip().lower()
            if status not in {'ok', 'skip', 'warn', 'critical'}:
                status = 'warn'
            module_totals[name][status] += 1
    return {
        'registered_targets': registered_targets,
        'matched_targets': len(targets),
        'overall_verdict': overall,
        'by_verdict': by_verdict,
        'module_status_totals': module_totals,
        'issue_count': total_issues,
    }


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

    registry = _load_registry(ctx)
    targets = _selected_targets(ctx, alias_list, tag_list)
    registered_targets = len((registry.get('targets') or {}))
    if not targets:
        return json.dumps(
            {
                'status': 'ok',
                'checked_at': utc_now_iso(),
                'filters': {'aliases': alias_list, 'tags': tag_list, 'include_panel': bool(include_panel), 'include_xray': bool(include_xray)},
                'summary': {
                    'registered_targets': registered_targets,
                    'matched_targets': 0,
                    'overall_verdict': 'ok',
                    'by_verdict': {'ok': 0, 'warn': 0, 'critical': 0},
                    'module_status_totals': {name: {'ok': 0, 'skip': 0, 'warn': 0, 'critical': 0} for name in _MODULE_ORDER},
                    'issue_count': 0,
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
                        'issues': [_issue('fleet_probe_exception', 'critical', str(exc), 'fleet_health')],
                        'checks': {},
                        'raw': {},
                    }
                )
    results.sort(key=lambda item: str(item.get('alias') or ''))
    payload = {
        'status': 'ok',
        'checked_at': utc_now_iso(),
        'filters': {'aliases': alias_list, 'tags': tag_list, 'include_panel': bool(include_panel), 'include_xray': bool(include_xray)},
        'summary': _build_summary(results, registered_targets),
        'targets': results,
    }
    return json.dumps(payload, ensure_ascii=False)


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            name='fleet_health',
            description='Run parallel fleet diagnostics over registered SSH targets, combining SSH health, Xray state, 3x-ui panel status, and extended server diagnostics.',
            properties={
                'aliases': {
                    'type': ['array', 'string', 'null'],
                    'items': {'type': 'string'},
                    'description': 'Optional aliases to check. May be a list or comma-separated string.',
                },
                'tags': {
                    'type': ['array', 'string', 'null'],
                    'items': {'type': 'string'},
                    'description': 'Optional tag filter. Target must contain all requested tags.',
                },
                'include_panel': {
                    'type': 'boolean',
                    'description': 'Whether to query 3x-ui panel status when panel metadata is configured.',
                    'default': True,
                },
                'include_xray': {
                    'type': 'boolean',
                    'description': 'Whether to query Xray-specific diagnostics.',
                    'default': True,
                },
                'max_workers': {
                    'type': ['integer', 'string', 'null'],
                    'description': f'Maximum worker threads for parallel target checks (1-{_MAX_MAX_WORKERS}).',
                },
            },
            required=[],
            handler=fleet_health,
            is_code_tool=False,
        )
    ]
