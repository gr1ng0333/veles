"""remote_network_diag — network diagnostics on remote servers over SSH.

Tools:
  remote_ping         — ICMP ping to target host (packet loss, latency)
  remote_traceroute   — traceroute to target host (hops, latency per hop)
  remote_port_check   — TCP port reachability check (nc/bash fallback)
  remote_dns_lookup   — DNS resolution for a hostname (A/AAAA/MX records)
  remote_vpn_status   — WireGuard / OpenVPN interface status
  remote_iptables_summary — active firewall rules (iptables -L summary)
  remote_netstat      — active connections and listening ports (ss/netstat)

All tools share the same SSH infrastructure as remote_execution.py:
registered SSH targets, _bootstrap_session, _base_ssh_command.
Execution mode is read_only for all diagnostics.

Usage:
  remote_ping(alias="myserver", host="8.8.8.8", count=5)
  remote_traceroute(alias="myserver", host="1.1.1.1", max_hops=20)
  remote_port_check(alias="myserver", host="1.1.1.1", port=443)
  remote_dns_lookup(alias="myserver", hostname="google.com", record_type="A")
  remote_vpn_status(alias="myserver")
  remote_iptables_summary(alias="myserver", table="filter")
  remote_netstat(alias="myserver", state_filter="LISTEN")
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.ssh_targets import (
    SshConnectionError,
    _base_ssh_command,
    _bootstrap_session,
    _get_target_record,
)

_DEFAULT_TIMEOUT = 30
_DEFAULT_PING_COUNT = 4
_DEFAULT_MAX_HOPS = 20


# ── internal helpers ──────────────────────────────────────────────────────────

def _run_remote(
    ctx: ToolContext,
    alias: str,
    command: str,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Execute a read-only command on the remote host via SSH. Returns raw dict."""
    record = _get_target_record(ctx, alias)
    alias_norm = record["alias"]
    try:
        _bootstrap_session(ctx, alias_norm)
        ssh_cmd = _base_ssh_command(ctx, record)
        ssh_cmd.append(command)
        result = subprocess.run(
            ssh_cmd,
            cwd=ctx.repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "status": "ok" if result.returncode == 0 else "nonzero",
            "exit_code": result.returncode,
            "stdout": (result.stdout or "").strip(),
            "stderr": (result.stderr or "").strip(),
            "target": alias_norm,
            "command": command,
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "kind": "timeout",
            "error": f"Command timed out after {timeout}s",
            "target": alias_norm,
            "command": command,
        }
    except SshConnectionError as e:
        return {
            "status": "error",
            "kind": e.kind,
            "error": e.message,
            "target": alias_norm,
            "command": command,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error",
            "kind": "unexpected",
            "error": str(e),
            "target": alias_norm,
            "command": command,
        }


def _ok(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


# ── tools ─────────────────────────────────────────────────────────────────────

def _handle_remote_ping(
    ctx: ToolContext,
    alias: str = "",
    host: str = "",
    count: int = _DEFAULT_PING_COUNT,
    timeout_sec: int = _DEFAULT_TIMEOUT,
) -> str:
    if not alias or not host:
        return _ok({"status": "error", "error": "alias and host are required"})
    count = max(1, min(int(count), 20))
    timeout_sec = max(5, min(int(timeout_sec), 60))
    cmd = f"ping -c {count} -W 2 {host} 2>&1"
    raw = _run_remote(ctx, alias, cmd, timeout=timeout_sec + 5)
    out = raw.get("stdout", "")

    # Parse summary line: "5 packets transmitted, 5 received, 0% packet loss"
    packet_loss: Optional[str] = None
    rtt_avg: Optional[str] = None
    for line in out.splitlines():
        if "packet loss" in line:
            parts = line.split(",")
            for p in parts:
                p = p.strip()
                if "packet loss" in p:
                    packet_loss = p
                    break
        if "rtt min" in line or "round-trip min" in line:
            # format: rtt min/avg/max/mdev = 0.1/0.2/0.3/0.05 ms
            try:
                rtt_avg = line.split("=")[1].strip().split("/")[1] + " ms"
            except (IndexError, AttributeError):
                pass

    result: Dict[str, Any] = {
        "status": raw["status"],
        "target": raw.get("target"),
        "host": host,
        "count": count,
    }
    if packet_loss is not None:
        result["packet_loss"] = packet_loss
    if rtt_avg is not None:
        result["rtt_avg"] = rtt_avg
    result["raw_output"] = out[:2000] if out else raw.get("stderr", "")
    if raw["status"] == "error":
        result["error"] = raw.get("error")
    return _ok(result)


def _handle_remote_traceroute(
    ctx: ToolContext,
    alias: str = "",
    host: str = "",
    max_hops: int = _DEFAULT_MAX_HOPS,
    timeout_sec: int = 45,
) -> str:
    if not alias or not host:
        return _ok({"status": "error", "error": "alias and host are required"})
    max_hops = max(1, min(int(max_hops), 30))
    timeout_sec = max(10, min(int(timeout_sec), 90))
    # traceroute preferred, fallback to tracepath
    cmd = f"command -v traceroute >/dev/null 2>&1 && traceroute -m {max_hops} -w 2 {host} 2>&1 || tracepath -m {max_hops} {host} 2>&1"
    raw = _run_remote(ctx, alias, cmd, timeout=timeout_sec + 5)
    out = raw.get("stdout", "") or raw.get("stderr", "")

    hops: List[Dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if not parts:
            continue
        # Try to extract hop number and RTTs
        try:
            hop_num = int(parts[0])
            hop_info: Dict[str, Any] = {"hop": hop_num, "raw": line}
            rtts = [p for p in parts if p.endswith("ms")]
            if rtts:
                hop_info["rtt_samples"] = rtts
            hops.append(hop_info)
        except (ValueError, IndexError):
            pass

    return _ok({
        "status": raw["status"],
        "target": raw.get("target"),
        "host": host,
        "max_hops": max_hops,
        "hops_parsed": len(hops),
        "hops": hops[:max_hops],
        "raw_output": out[:3000],
        **({"error": raw.get("error")} if raw["status"] == "error" else {}),
    })


def _handle_remote_port_check(
    ctx: ToolContext,
    alias: str = "",
    host: str = "",
    port: int = 80,
    timeout_sec: int = 10,
) -> str:
    if not alias or not host:
        return _ok({"status": "error", "error": "alias and host are required"})
    port = max(1, min(int(port), 65535))
    timeout_sec = max(3, min(int(timeout_sec), 30))
    # Use bash /dev/tcp as universal fallback — works without nc
    cmd = (
        f"(timeout {timeout_sec} bash -c 'echo >/dev/tcp/{host}/{port}' 2>/dev/null"
        f" && echo 'open') || echo 'closed'"
    )
    raw = _run_remote(ctx, alias, cmd, timeout=timeout_sec + 5)
    out = (raw.get("stdout") or "").strip()
    reachable = "open" in out.lower()
    return _ok({
        "status": raw["status"] if raw["status"] == "error" else "ok",
        "target": raw.get("target"),
        "host": host,
        "port": port,
        "reachable": reachable,
        "result": out,
        **({"error": raw.get("error")} if raw["status"] == "error" else {}),
    })


def _handle_remote_dns_lookup(
    ctx: ToolContext,
    alias: str = "",
    hostname: str = "",
    record_type: str = "A",
    timeout_sec: int = 15,
) -> str:
    if not alias or not hostname:
        return _ok({"status": "error", "error": "alias and hostname are required"})
    record_type = record_type.upper().strip()
    valid_types = {"A", "AAAA", "MX", "TXT", "CNAME", "NS", "SOA", "PTR"}
    if record_type not in valid_types:
        record_type = "A"
    # dig preferred, nslookup fallback
    cmd = (
        f"command -v dig >/dev/null 2>&1"
        f" && dig +short {hostname} {record_type} 2>&1"
        f" || nslookup -type={record_type} {hostname} 2>&1"
    )
    raw = _run_remote(ctx, alias, cmd, timeout=timeout_sec + 5)
    out = (raw.get("stdout") or "").strip()
    records = [line.strip() for line in out.splitlines() if line.strip()]
    return _ok({
        "status": raw["status"] if raw["status"] == "error" else "ok",
        "target": raw.get("target"),
        "hostname": hostname,
        "record_type": record_type,
        "records": records,
        "raw_output": out[:1000],
        **({"error": raw.get("error")} if raw["status"] == "error" else {}),
    })


def _handle_remote_vpn_status(
    ctx: ToolContext,
    alias: str = "",
    timeout_sec: int = 15,
) -> str:
    if not alias:
        return _ok({"status": "error", "error": "alias is required"})
    # WireGuard
    wg_cmd = "command -v wg >/dev/null 2>&1 && wg show 2>&1 || echo 'wg_not_found'"
    wg_raw = _run_remote(ctx, alias, wg_cmd, timeout=timeout_sec)
    wg_out = (wg_raw.get("stdout") or "").strip()

    # OpenVPN
    ovpn_cmd = "ip link show | grep -E 'tun[0-9]|tap[0-9]' 2>&1 || echo 'none'"
    ovpn_raw = _run_remote(ctx, alias, ovpn_cmd, timeout=timeout_sec)
    ovpn_out = (ovpn_raw.get("stdout") or "").strip()

    # WireGuard interfaces via ip
    wg_iface_cmd = "ip link show | grep -E 'wg[0-9]' 2>&1 || echo 'none'"
    wg_iface_raw = _run_remote(ctx, alias, wg_iface_cmd, timeout=timeout_sec)
    wg_iface_out = (wg_iface_raw.get("stdout") or "").strip()

    wg_active = "wg_not_found" not in wg_out and bool(wg_out)
    tun_interfaces = [
        line.strip() for line in ovpn_out.splitlines()
        if line.strip() and "none" not in line
    ]
    wg_interfaces = [
        line.strip() for line in wg_iface_out.splitlines()
        if line.strip() and "none" not in line
    ]

    return _ok({
        "status": "ok",
        "target": alias,
        "wireguard": {
            "active": wg_active,
            "interfaces": wg_interfaces,
            "raw": wg_out[:1000] if wg_active else "(not running or not installed)",
        },
        "openvpn": {
            "tun_tap_interfaces": tun_interfaces,
            "active": bool(tun_interfaces),
        },
    })


def _handle_remote_iptables_summary(
    ctx: ToolContext,
    alias: str = "",
    table: str = "filter",
    timeout_sec: int = 20,
) -> str:
    if not alias:
        return _ok({"status": "error", "error": "alias is required"})
    valid_tables = {"filter", "nat", "mangle", "raw"}
    table = table.lower().strip()
    if table not in valid_tables:
        table = "filter"
    cmd = f"iptables -t {table} -L -n --line-numbers 2>&1 || echo 'iptables_unavailable'"
    raw = _run_remote(ctx, alias, cmd, timeout=timeout_sec + 5)
    out = (raw.get("stdout") or "").strip()

    # Also try nftables as modern alternative
    nft_cmd = "command -v nft >/dev/null 2>&1 && nft list ruleset 2>&1 | head -50 || echo 'nft_not_found'"
    nft_raw = _run_remote(ctx, alias, nft_cmd, timeout=timeout_sec)
    nft_out = (nft_raw.get("stdout") or "").strip()

    return _ok({
        "status": "ok",
        "target": alias,
        "table": table,
        "iptables_unavailable": "iptables_unavailable" in out,
        "iptables_output": out[:3000],
        "nftables": {
            "available": "nft_not_found" not in nft_out and bool(nft_out),
            "output": nft_out[:2000] if "nft_not_found" not in nft_out else None,
        },
    })


def _handle_remote_netstat(
    ctx: ToolContext,
    alias: str = "",
    state_filter: str = "",
    timeout_sec: int = 15,
) -> str:
    if not alias:
        return _ok({"status": "error", "error": "alias is required"})
    # ss preferred (modern), netstat as fallback
    state_filter = (state_filter or "").upper().strip()
    if state_filter and state_filter not in {"LISTEN", "ESTABLISHED", "TIME_WAIT", "CLOSE_WAIT", "SYN_SENT"}:
        state_filter = ""
    state_expr = f"state {state_filter.lower()}" if state_filter else ""
    cmd = (
        f"command -v ss >/dev/null 2>&1"
        f" && ss -tulnp {state_expr} 2>&1"
        f" || netstat -tulnp 2>&1 | head -60"
    )
    raw = _run_remote(ctx, alias, cmd, timeout=timeout_sec + 5)
    out = (raw.get("stdout") or "").strip()

    # Parse listening ports from ss output
    listening: List[Dict[str, str]] = []
    for line in out.splitlines():
        if line.startswith("Netid") or line.startswith("Proto"):
            continue
        parts = line.split()
        if len(parts) >= 5:
            try:
                listening.append({
                    "proto": parts[0],
                    "local_addr": parts[4] if len(parts) > 4 else "",
                    "process": parts[-1] if len(parts) > 5 else "",
                })
            except (IndexError, ValueError):
                pass

    return _ok({
        "status": raw["status"] if raw["status"] == "error" else "ok",
        "target": raw.get("target"),
        "state_filter": state_filter or "all",
        "connections_parsed": len(listening),
        "connections": listening[:50],
        "raw_output": out[:3000],
        **({"error": raw.get("error")} if raw["status"] == "error" else {}),
    })


# ── registry ──────────────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="remote_ping",
            schema={
                "name": "remote_ping",
                "description": (
                    "Run ICMP ping to a host from a registered remote server. "
                    "Returns packet loss percentage and average RTT."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string", "description": "SSH target alias (registered via ssh_target_register)"},
                        "host": {"type": "string", "description": "Target hostname or IP to ping"},
                        "count": {"type": "integer", "description": "Number of ping packets (default: 4, max: 20)", "default": 4},
                        "timeout_sec": {"type": "integer", "description": "Total timeout in seconds (default: 30)", "default": 30},
                    },
                    "required": ["alias", "host"],
                },
            },
            handler=_handle_remote_ping,
        ),
        ToolEntry(
            name="remote_traceroute",
            schema={
                "name": "remote_traceroute",
                "description": (
                    "Run traceroute from a registered remote server to a target host. "
                    "Shows hop-by-hop latency. Uses traceroute or tracepath as fallback."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string", "description": "SSH target alias"},
                        "host": {"type": "string", "description": "Target hostname or IP"},
                        "max_hops": {"type": "integer", "description": "Maximum TTL/hops (default: 20, max: 30)", "default": 20},
                        "timeout_sec": {"type": "integer", "description": "Total timeout in seconds (default: 45)", "default": 45},
                    },
                    "required": ["alias", "host"],
                },
            },
            handler=_handle_remote_traceroute,
        ),
        ToolEntry(
            name="remote_port_check",
            schema={
                "name": "remote_port_check",
                "description": (
                    "Check if a TCP port is reachable from a registered remote server. "
                    "Uses bash /dev/tcp for zero-dependency port checking."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string", "description": "SSH target alias"},
                        "host": {"type": "string", "description": "Target hostname or IP"},
                        "port": {"type": "integer", "description": "TCP port to check (1-65535)"},
                        "timeout_sec": {"type": "integer", "description": "Connection timeout in seconds (default: 10)", "default": 10},
                    },
                    "required": ["alias", "host", "port"],
                },
            },
            handler=_handle_remote_port_check,
        ),
        ToolEntry(
            name="remote_dns_lookup",
            schema={
                "name": "remote_dns_lookup",
                "description": (
                    "Perform DNS lookup from a registered remote server. "
                    "Supports A, AAAA, MX, TXT, CNAME, NS record types. "
                    "Uses dig (preferred) or nslookup as fallback."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string", "description": "SSH target alias"},
                        "hostname": {"type": "string", "description": "Hostname to resolve"},
                        "record_type": {
                            "type": "string",
                            "description": "DNS record type (default: A)",
                            "enum": ["A", "AAAA", "MX", "TXT", "CNAME", "NS", "SOA", "PTR"],
                            "default": "A",
                        },
                        "timeout_sec": {"type": "integer", "description": "Timeout in seconds (default: 15)", "default": 15},
                    },
                    "required": ["alias", "hostname"],
                },
            },
            handler=_handle_remote_dns_lookup,
        ),
        ToolEntry(
            name="remote_vpn_status",
            schema={
                "name": "remote_vpn_status",
                "description": (
                    "Check VPN status on a registered remote server. "
                    "Reports WireGuard (wg show) and OpenVPN/tun/tap interface status."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string", "description": "SSH target alias"},
                        "timeout_sec": {"type": "integer", "description": "Timeout in seconds (default: 15)", "default": 15},
                    },
                    "required": ["alias"],
                },
            },
            handler=_handle_remote_vpn_status,
        ),
        ToolEntry(
            name="remote_iptables_summary",
            schema={
                "name": "remote_iptables_summary",
                "description": (
                    "Show active firewall rules on a registered remote server. "
                    "Queries iptables for the specified table and nftables if available."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string", "description": "SSH target alias"},
                        "table": {
                            "type": "string",
                            "description": "iptables table to inspect (default: filter)",
                            "enum": ["filter", "nat", "mangle", "raw"],
                            "default": "filter",
                        },
                        "timeout_sec": {"type": "integer", "description": "Timeout in seconds (default: 20)", "default": 20},
                    },
                    "required": ["alias"],
                },
            },
            handler=_handle_remote_iptables_summary,
        ),
        ToolEntry(
            name="remote_netstat",
            schema={
                "name": "remote_netstat",
                "description": (
                    "List active connections and listening ports on a registered remote server. "
                    "Uses 'ss' (modern) with netstat fallback. Optionally filter by connection state."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string", "description": "SSH target alias"},
                        "state_filter": {
                            "type": "string",
                            "description": "Filter by connection state (LISTEN, ESTABLISHED, TIME_WAIT, CLOSE_WAIT, SYN_SENT, or empty for all)",
                            "default": "",
                        },
                        "timeout_sec": {"type": "integer", "description": "Timeout in seconds (default: 15)", "default": 15},
                    },
                    "required": ["alias"],
                },
            },
            handler=_handle_remote_netstat,
        ),
    ]
