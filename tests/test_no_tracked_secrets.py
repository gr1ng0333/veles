from __future__ import annotations

import re
import subprocess
from pathlib import Path

SECRET_PATTERNS = {
    "telegram_bot_token": re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "github_token": re.compile(r"\bgh[opusr]_[A-Za-z0-9_]{30,}\b"),
    "github_fine_grained_token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "credential_in_url": re.compile(r"https?://[^\s/@:]+:[^\s/@]+@[^\s/]+"),
}

ALLOWLIST_MATCHES = {
    "sk-abcdefghijklmnopqrstuvwxyz1234",
}


def tracked_files() -> list[str]:
    output = subprocess.check_output(["git", "ls-files"], text=True)
    return [line for line in output.splitlines() if line]


def test_tracked_files_do_not_contain_secret_literals() -> None:
    findings: list[str] = []
    for file_name in tracked_files():
        path = Path(file_name)
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for name, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(text):
                if match.group(0) in ALLOWLIST_MATCHES:
                    continue
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{file_name}:{line}: {name}")

    assert findings == []
