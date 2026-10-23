"""
Ouroboros — client for the ocbot job API (the "child" coding agent).

ocbot runs OpenCode on free models and exposes its budgeted, relay-based jobs on
a loopback HTTP API (spec 7, /home/ocbot/app/docs/JOB_API.md). Veles delegates
short tasks there, sleeps, and is woken by child_watch when a job finishes.

Uses urllib only (no requests dependency).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_URL = "http://127.0.0.1:4097/api/v1"
DEFAULT_TOKEN_FILE = "/etc/ocbot/jobapi.token"
DEFAULT_EXCHANGE_DIR = "/home/ocbot/exchange"
TIMEOUT_SEC = 30

# The bearer token must never pass through an ambient HTTP(S)_PROXY.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ChildApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        super().__init__(f"child API {status} {code}: {message}")


def api_url() -> str:
    return os.environ.get("VELES_CHILD_API_URL", "").strip().rstrip("/") or DEFAULT_URL


def exchange_dir() -> Path:
    return Path(os.environ.get("VELES_CHILD_EXCHANGE_DIR", "").strip() or DEFAULT_EXCHANGE_DIR)


def _token() -> str:
    path = Path(os.environ.get("VELES_CHILD_TOKEN_FILE", "").strip() or DEFAULT_TOKEN_FILE)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ChildApiError(0, "token_unavailable", f"cannot read {path}: {type(exc).__name__}") from exc
    if not token:
        raise ChildApiError(0, "token_unavailable", f"{path} is empty")
    return token


def request(method: str, path: str, *, body: Any = None, query: Optional[Dict[str, Any]] = None,
            headers: Optional[Dict[str, str]] = None, timeout: float = TIMEOUT_SEC) -> Dict[str, Any]:
    url = api_url() + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None and v != ""})
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers={
        "Authorization": "Bearer " + _token(),
        "Content-Type": "application/json",
        **(headers or {}),
    })
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()[:2000].decode("utf-8", errors="replace")
        code, message = f"http_{exc.code}", raw
        try:
            err = json.loads(raw).get("error") or {}
            code, message = str(err.get("code") or code), str(err.get("message") or message)
        except (ValueError, AttributeError):
            pass
        raise ChildApiError(exc.code, code, message) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ChildApiError(0, "unreachable", f"{method} {path}: {exc}") from exc
    return json.loads(payload) if payload else {}
