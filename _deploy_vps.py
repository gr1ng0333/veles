"""One-shot VPS deployment helper for Veles.

This script is intentionally secret-free. Historical versions of this file
contained deployment credentials and service tokens; never put secrets back
into tracked source. Provide credentials through environment variables when
using this legacy helper. Prefer the newer remote-operation tools for real
work; this file remains only as a minimal bootstrap helper.
"""
from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass

import paramiko


REQUIRED_ENV = (
    "VELES_DEPLOY_HOST",
    "VELES_DEPLOY_USER",
    "VELES_DEPLOY_PASSWORD",
    "OPENROUTER_API_KEY",
    "TELEGRAM_BOT_TOKEN",
)


@dataclass(frozen=True)
class DeployConfig:
    host: str
    user: str
    password: str
    openrouter_api_key: str
    telegram_bot_token: str
    allow_unknown_host: bool


class MissingConfig(RuntimeError):
    pass


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingConfig(f"Missing required environment variable: {name}")
    return value


def load_config() -> DeployConfig:
    return DeployConfig(
        host=_require_env("VELES_DEPLOY_HOST"),
        user=_require_env("VELES_DEPLOY_USER"),
        password=_require_env("VELES_DEPLOY_PASSWORD"),
        openrouter_api_key=_require_env("OPENROUTER_API_KEY"),
        telegram_bot_token=_require_env("TELEGRAM_BOT_TOKEN"),
        allow_unknown_host=os.environ.get("VELES_DEPLOY_ALLOW_UNKNOWN_HOST") == "1",
    )


def ssh_connect(config: DeployConfig) -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    if config.allow_unknown_host:
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    ssh.connect(config.host, username=config.user, password=config.password, timeout=15)
    return ssh


def run(ssh: paramiko.SSHClient, cmd: str, label: str = "", timeout: int = 120, *, sensitive: bool = False) -> tuple[int, str, str]:
    if label:
        print(f"\n{'=' * 60}")
        print(f"  {label}")
        print(f"{'=' * 60}")
    if sensitive:
        print("$ <sensitive command redacted>")
    else:
        print(f"$ {cmd[:200]}{'...' if len(cmd) > 200 else ''}")
    _stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    if out.strip() and not sensitive:
        print(out[-3000:] if len(out) > 3000 else out)
    if err.strip() and not sensitive:
        for line in err.strip().split("\n"):
            if "WARNING" not in line and "DEPRECATION" not in line:
                print(f"  [stderr] {line}")
    if rc != 0:
        print(f"  [exit code: {rc}]")
    return rc, out, err


def build_env_file(config: DeployConfig) -> str:
    """Build the remote .env content without storing secrets in git."""
    entries = {
        "OPENROUTER_API_KEY": config.openrouter_api_key,
        "TELEGRAM_BOT_TOKEN": config.telegram_bot_token,
        "OUROBOROS_MODEL": os.environ.get("OUROBOROS_MODEL", "codex/gpt-5.5"),
        "OUROBOROS_MODEL_CODE": os.environ.get("OUROBOROS_MODEL_CODE", "codex/gpt-5.5"),
        "OUROBOROS_MODEL_LIGHT": os.environ.get("OUROBOROS_MODEL_LIGHT", "codex/gpt-5.4-mini"),
        "OUROBOROS_MODEL_BACKGROUND": os.environ.get("OUROBOROS_MODEL_BACKGROUND", "codex/gpt-5.4-mini"),
    }
    return "\n".join(f"{key}={value}" for key, value in entries.items()) + "\n"


def write_remote_file(ssh: paramiko.SSHClient, path: str, content: str, mode: int = 0o600) -> None:
    sftp = ssh.open_sftp()
    try:
        with sftp.file(path, "w") as remote_file:
            remote_file.write(content)
        sftp.chmod(path, stat.S_IMODE(mode))
    finally:
        sftp.close()


def main() -> int:
    try:
        config = load_config()
    except MissingConfig as exc:
        print(exc, file=sys.stderr)
        print("Required env vars: " + ", ".join(REQUIRED_ENV), file=sys.stderr)
        return 2

    step = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    ssh = ssh_connect(config)
    print(f"Connected to {config.host}")

    if step <= 0:
        run(ssh, "mkdir -p /opt/veles /opt/veles-data/{state,logs,memory/knowledge}", "Create directories")

    if step <= 1:
        run(
            ssh,
            "test -d /opt/veles/.git || git clone https://github.com/gr1ng0333/veles.git /opt/veles",
            "Clone repository if missing",
            timeout=300,
        )
        run(ssh, "cd /opt/veles && git fetch origin && git checkout veles && git pull --ff-only", "Update repository", timeout=300)

    if step <= 2:
        write_remote_file(ssh, "/opt/veles/.env", build_env_file(config), 0o600)
        print("Wrote /opt/veles/.env via SFTP with mode 0600")

    if step <= 3:
        run(ssh, "systemctl restart veles.service && systemctl --no-pager --lines=20 status veles.service", "Restart Veles")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
