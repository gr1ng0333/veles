"""Tests for remote_network_diag — network diagnostics over SSH.

All tests use mocking so no real SSH connection is required.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.tools.remote_network_diag import (
    _handle_remote_dns_lookup,
    _handle_remote_iptables_summary,
    _handle_remote_netstat,
    _handle_remote_ping,
    _handle_remote_port_check,
    _handle_remote_traceroute,
    _handle_remote_vpn_status,
    get_tools,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.repo_dir = "/opt/veles"
    ctx.drive_path = MagicMock(return_value=MagicMock())
    return ctx


def _mock_run_remote(stdout: str = "", stderr: str = "", exit_code: int = 0):
    """Return a patch for _run_remote that yields a controlled result."""
    return {
        "status": "ok" if exit_code == 0 else "nonzero",
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "target": "testserver",
        "command": "(mocked)",
    }


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def test_get_tools_returns_all_seven():
    tools = get_tools()
    names = {t.name for t in tools}
    assert names == {
        "remote_ping",
        "remote_traceroute",
        "remote_port_check",
        "remote_dns_lookup",
        "remote_vpn_status",
        "remote_iptables_summary",
        "remote_netstat",
    }


def test_all_tools_have_schemas():
    for tool in get_tools():
        assert "name" in tool.schema
        assert "parameters" in tool.schema
        assert "description" in tool.schema


# ---------------------------------------------------------------------------
# remote_ping
# ---------------------------------------------------------------------------

PING_OUTPUT = (
    "PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.\n"
    "64 bytes from 8.8.8.8: icmp_seq=1 ttl=118 time=1.23 ms\n"
    "--- 8.8.8.8 ping statistics ---\n"
    "4 packets transmitted, 4 received, 0% packet loss, time 3003ms\n"
    "rtt min/avg/max/mdev = 1.0/1.23/1.5/0.1 ms\n"
)


def test_remote_ping_success():
    ctx = _make_ctx()
    with patch(
        "ouroboros.tools.remote_network_diag._run_remote",
        return_value=_mock_run_remote(stdout=PING_OUTPUT),
    ):
        result = json.loads(_handle_remote_ping(ctx, alias="testserver", host="8.8.8.8", count=4))
    assert result["status"] == "ok"
    assert result["host"] == "8.8.8.8"
    assert "0% packet loss" in result.get("packet_loss", "")
    assert result.get("rtt_avg") is not None


def test_remote_ping_missing_args():
    ctx = _make_ctx()
    result = json.loads(_handle_remote_ping(ctx, alias="", host="8.8.8.8"))
    assert result["status"] == "error"
    result2 = json.loads(_handle_remote_ping(ctx, alias="srv", host=""))
    assert result2["status"] == "error"


# ---------------------------------------------------------------------------
# remote_traceroute
# ---------------------------------------------------------------------------

TRACE_OUTPUT = (
    "traceroute to 1.1.1.1 (1.1.1.1), 20 hops max\n"
    " 1  gateway (192.168.1.1)  0.5 ms  0.4 ms  0.4 ms\n"
    " 2  10.0.0.1 (10.0.0.1)   1.2 ms  1.1 ms  1.3 ms\n"
    " 3  1.1.1.1 (1.1.1.1)     5.0 ms  4.9 ms  5.2 ms\n"
)


def test_remote_traceroute_success():
    ctx = _make_ctx()
    with patch(
        "ouroboros.tools.remote_network_diag._run_remote",
        return_value=_mock_run_remote(stdout=TRACE_OUTPUT),
    ):
        result = json.loads(_handle_remote_traceroute(ctx, alias="testserver", host="1.1.1.1"))
    assert result["status"] == "ok"
    assert result["host"] == "1.1.1.1"
    assert result["hops_parsed"] == 3


def test_remote_traceroute_missing_args():
    ctx = _make_ctx()
    result = json.loads(_handle_remote_traceroute(ctx, alias="", host="1.1.1.1"))
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# remote_port_check
# ---------------------------------------------------------------------------

def test_remote_port_check_open():
    ctx = _make_ctx()
    with patch(
        "ouroboros.tools.remote_network_diag._run_remote",
        return_value=_mock_run_remote(stdout="open"),
    ):
        result = json.loads(_handle_remote_port_check(ctx, alias="testserver", host="1.1.1.1", port=443))
    assert result["reachable"] is True
    assert result["port"] == 443


def test_remote_port_check_closed():
    ctx = _make_ctx()
    with patch(
        "ouroboros.tools.remote_network_diag._run_remote",
        return_value=_mock_run_remote(stdout="closed"),
    ):
        result = json.loads(_handle_remote_port_check(ctx, alias="testserver", host="1.1.1.1", port=9999))
    assert result["reachable"] is False


def test_remote_port_check_missing_args():
    ctx = _make_ctx()
    result = json.loads(_handle_remote_port_check(ctx, alias="", host="1.1.1.1"))
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# remote_dns_lookup
# ---------------------------------------------------------------------------

def test_remote_dns_lookup_success():
    ctx = _make_ctx()
    with patch(
        "ouroboros.tools.remote_network_diag._run_remote",
        return_value=_mock_run_remote(stdout="142.250.185.46"),
    ):
        result = json.loads(_handle_remote_dns_lookup(ctx, alias="testserver", hostname="google.com"))
    assert result["status"] == "ok"
    assert result["hostname"] == "google.com"
    assert "142.250.185.46" in result["records"]


def test_remote_dns_lookup_invalid_type_falls_back_to_A():
    ctx = _make_ctx()
    with patch(
        "ouroboros.tools.remote_network_diag._run_remote",
        return_value=_mock_run_remote(stdout="1.2.3.4"),
    ):
        result = json.loads(_handle_remote_dns_lookup(ctx, alias="srv", hostname="x.com", record_type="INVALID"))
    assert result["record_type"] == "A"


# ---------------------------------------------------------------------------
# remote_vpn_status
# ---------------------------------------------------------------------------

WG_OUTPUT = (
    "interface: wg0\n"
    "  public key: abc123\n"
    "  listening port: 51820\n"
    "peer: xyz789\n"
    "  allowed ips: 10.0.0.0/24\n"
)


def test_remote_vpn_status_wireguard_active():
    ctx = _make_ctx()
    call_count = {"n": 0}

    def _mock_run(ctx, alias, cmd, timeout=30):
        n = call_count["n"]
        call_count["n"] += 1
        if n == 0:  # wg show
            return _mock_run_remote(stdout=WG_OUTPUT)
        elif n == 1:  # tun/tap
            return _mock_run_remote(stdout="none")
        else:  # wg interfaces
            return _mock_run_remote(stdout="4: wg0: <POINTOPOINT,NOARP,UP,LOWER_UP>")

    with patch("ouroboros.tools.remote_network_diag._run_remote", side_effect=_mock_run):
        result = json.loads(_handle_remote_vpn_status(ctx, alias="testserver"))
    assert result["status"] == "ok"
    assert result["wireguard"]["active"] is True


def test_remote_vpn_status_missing_alias():
    ctx = _make_ctx()
    result = json.loads(_handle_remote_vpn_status(ctx, alias=""))
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# remote_iptables_summary
# ---------------------------------------------------------------------------

IPTABLES_OUTPUT = (
    "Chain INPUT (policy ACCEPT)\n"
    "target     prot opt source               destination\n"
    "ACCEPT     tcp  --  0.0.0.0/0            0.0.0.0/0            tcp dpt:22\n"
    "Chain FORWARD (policy DROP)\n"
    "target     prot opt source               destination\n"
    "Chain OUTPUT (policy ACCEPT)\n"
)


def test_remote_iptables_summary_success():
    ctx = _make_ctx()
    call_count = {"n": 0}

    def _mock_run(ctx, alias, cmd, timeout=20):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _mock_run_remote(stdout=IPTABLES_OUTPUT)
        return _mock_run_remote(stdout="nft_not_found")

    with patch("ouroboros.tools.remote_network_diag._run_remote", side_effect=_mock_run):
        result = json.loads(_handle_remote_iptables_summary(ctx, alias="testserver"))
    assert result["status"] == "ok"
    assert "INPUT" in result["iptables_output"]


def test_remote_iptables_summary_invalid_table_fallback():
    ctx = _make_ctx()

    def _mock_run(ctx, alias, cmd, timeout=20):
        return _mock_run_remote(stdout="iptables_unavailable")

    with patch("ouroboros.tools.remote_network_diag._run_remote", side_effect=_mock_run):
        result = json.loads(_handle_remote_iptables_summary(ctx, alias="srv", table="badtable"))
    # falls back to 'filter'
    assert result["table"] == "filter"


# ---------------------------------------------------------------------------
# remote_netstat
# ---------------------------------------------------------------------------

NETSTAT_OUTPUT = (
    "Netid  State   Recv-Q  Send-Q  Local Address:Port  Peer Address:Port\n"
    "tcp    LISTEN  0       128     0.0.0.0:22           0.0.0.0:*      users:((\"sshd\",pid=123))\n"
    "tcp    LISTEN  0       128     0.0.0.0:80           0.0.0.0:*      users:((\"nginx\",pid=456))\n"
)


def test_remote_netstat_success():
    ctx = _make_ctx()
    with patch(
        "ouroboros.tools.remote_network_diag._run_remote",
        return_value=_mock_run_remote(stdout=NETSTAT_OUTPUT),
    ):
        result = json.loads(_handle_remote_netstat(ctx, alias="testserver"))
    assert result["status"] == "ok"
    assert result["connections_parsed"] >= 2


def test_remote_netstat_missing_alias():
    ctx = _make_ctx()
    result = json.loads(_handle_remote_netstat(ctx, alias=""))
    assert result["status"] == "error"
