"""
Ouroboros — Copilot Pro OAuth account management.

Manages GitHub PAT → Copilot API token exchange and multi-account rotation.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

TOKEN_EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"
ACCOUNTS_STATE_FILE = Path("/opt/veles-data/state/copilot_accounts_state.json")
REFRESH_THRESHOLD_SEC = 300  # re-exchange if < 5 min until expiry
RATE_LIMIT_COOLDOWN_SEC = 600
RATE_LIMIT_REPEAT_WINDOW = 1800
RATE_LIMIT_ESCALATED_SEC = 3600

_accounts_lock = threading.Lock()
_accounts: List[Dict[str, Any]] = []
_active_idx: int = 0


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------

COPILOT_DEFAULT_API_BASE = "https://api.individual.githubcopilot.com"


def _exchange_token(github_token: str, urlopen) -> Optional[Dict[str, Any]]:
    """Exchange GitHub PAT for a short-lived Copilot API token."""
    try:
        req = __import__("urllib.request").request.Request(
            TOKEN_EXCHANGE_URL,
            headers={
                "Authorization": f"token {github_token}",
                "Accept": "application/json",
                "User-Agent": "GitHubCopilotChat/0.29.1",
            },
            method="GET",
        )
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        token = data.get("token", "")
        expires_at = int(data.get("expires_at", 0))
        if not token:
            log.error("[copilot_token_exchange] Empty token in response")
            return None
        endpoints = data.get("endpoints", {})
        api_base = endpoints.get("api", "") if isinstance(endpoints, dict) else ""
        return {
            "copilot_token": token,
            "expires_at": expires_at,
            "copilot_api_base": api_base or COPILOT_DEFAULT_API_BASE,
        }
    except Exception as e:
        log.error("[copilot_token_exchange] Token exchange failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Account loading / persistence
# ---------------------------------------------------------------------------

def _load_accounts() -> List[Dict[str, Any]]:
    """Load accounts from COPILOT_ACCOUNTS env var (or single COPILOT_GITHUB_TOKEN)."""
    raw = os.environ.get("COPILOT_ACCOUNTS", "")
    accounts: List[Dict[str, Any]] = []

    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and item.get("github_token"):
                        accounts.append({
                            "github_token": item["github_token"],
                            "copilot_token": "",
                            "expires_at": 0,
                            "cooldown_until": 0.0,
                            "dead": False,
                            "last_429_at": 0.0,
                            "request_timestamps": [],
                        })
            if accounts:
                log.info("Loaded %d Copilot accounts from COPILOT_ACCOUNTS", len(accounts))
            else:
                log.warning("COPILOT_ACCOUNTS parsed but contained 0 valid accounts (need 'github_token' key)")
        except (json.JSONDecodeError, ValueError) as e:
            log.error("Failed to parse COPILOT_ACCOUNTS: %s  |  raw[:200]=%s", e, raw[:200])

    # Fallback: single account from COPILOT_GITHUB_TOKEN
    if not accounts:
        single_token = os.environ.get("COPILOT_GITHUB_TOKEN", "")
        if single_token:
            accounts.append({
                "github_token": single_token,
                "copilot_token": "",
                "expires_at": 0,
                "cooldown_until": 0.0,
                "dead": False,
                "last_429_at": 0.0,
                "request_timestamps": [],
            })
            log.info("Loaded 1 Copilot account from COPILOT_GITHUB_TOKEN")

    # Restore persisted state
    if accounts and ACCOUNTS_STATE_FILE.exists():
        try:
            raw_state = json.loads(ACCOUNTS_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(raw_state, dict):
                state = raw_state.get("accounts", [])
            elif isinstance(raw_state, list):
                state = raw_state
            else:
                state = []
            if isinstance(state, list):
                for i, s in enumerate(state):
                    if i < len(accounts) and isinstance(s, dict):
                        if s.get("copilot_token"):
                            accounts[i]["copilot_token"] = s["copilot_token"]
                        if s.get("expires_at"):
                            accounts[i]["expires_at"] = int(s["expires_at"])
                        accounts[i]["cooldown_until"] = float(s.get("cooldown_until", 0))
                        accounts[i]["last_429_at"] = float(s.get("last_429_at", 0))
                        raw_ts = s.get("request_timestamps", [])
                        if isinstance(raw_ts, list):
                            cutoff = time.time() - 604800
                            accounts[i]["request_timestamps"] = [
                                t for t in raw_ts if isinstance(t, (int, float)) and t > cutoff
                            ]
        except Exception as e:
            log.warning("Failed to load Copilot accounts state: %s", e)

    return accounts


def _save_accounts_state(accounts: List[Dict[str, Any]]) -> None:
    global _active_idx
    try:
        ACCOUNTS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        cutoff_7d = now - 604800
        serializable = []
        for acc in accounts:
            pruned = [t for t in acc.get("request_timestamps", [])
                      if isinstance(t, (int, float)) and t > cutoff_7d]
            acc["request_timestamps"] = pruned
            serializable.append({
                "copilot_token": acc.get("copilot_token", ""),
                "expires_at": acc.get("expires_at", 0),
                "cooldown_until": acc.get("cooldown_until", 0),
                "dead": acc.get("dead", False),
                "last_429_at": acc.get("last_429_at", 0),
                "request_timestamps": pruned,
            })
        state_obj = {"active_idx": _active_idx, "accounts": serializable}
        ACCOUNTS_STATE_FILE.write_text(json.dumps(state_obj, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Failed to save Copilot accounts state: %s", e)


# ---------------------------------------------------------------------------
# Account init / rotation
# ---------------------------------------------------------------------------

def _init_accounts(force: bool = False) -> None:
    global _accounts, _active_idx
    if _accounts and not force:
        return
    _accounts = _load_accounts()
    if _accounts:
        restored_idx = -1
        if ACCOUNTS_STATE_FILE.exists():
            try:
                raw_state = json.loads(ACCOUNTS_STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(raw_state, dict):
                    restored_idx = int(raw_state.get("active_idx", -1))
            except Exception:
                pass
        now = time.time()
        if 0 <= restored_idx < len(_accounts):
            acc = _accounts[restored_idx]
            if not acc["dead"] and acc["cooldown_until"] < now:
                _active_idx = restored_idx
            else:
                for i, a in enumerate(_accounts):
                    if not a["dead"] and a["cooldown_until"] < now:
                        _active_idx = i
                        break
        else:
            for i, a in enumerate(_accounts):
                if not a["dead"] and a["cooldown_until"] < now:
                    _active_idx = i
                    break
        log.info("Copilot multi-account: %d accounts loaded, active=#%d", len(_accounts), _active_idx)


def _get_active_account() -> Optional[Tuple[Dict[str, Any], int]]:
    global _active_idx
    with _accounts_lock:
        _init_accounts()
        if not _accounts:
            return None
        now = time.time()
        acc = _accounts[_active_idx]
        if not acc["dead"] and acc["cooldown_until"] < now:
            return acc, _active_idx
        for i in range(len(_accounts)):
            idx = (_active_idx + 1 + i) % len(_accounts)
            acc = _accounts[idx]
            if not acc["dead"] and acc["cooldown_until"] < now:
                _active_idx = idx
                log.info("Copilot account rotation: switched to #%d", idx)
                return acc, idx
        log.error("[copilot_account_dead] All Copilot accounts exhausted (dead or on cooldown)")
        return None


def _on_rate_limit(account_idx: int, retry_after: int = 0) -> None:
    with _accounts_lock:
        if account_idx < len(_accounts):
            acc = _accounts[account_idx]
            now = time.time()
            last_429 = acc.get("last_429_at", 0)
            acc["last_429_at"] = now
            if retry_after > 0:
                cooldown = retry_after
            elif (now - last_429) < RATE_LIMIT_REPEAT_WINDOW:
                cooldown = RATE_LIMIT_ESCALATED_SEC
            else:
                cooldown = RATE_LIMIT_COOLDOWN_SEC
            acc["cooldown_until"] = now + cooldown
            log.warning(
                "[copilot_account_cooldown] Account #%d rate-limited, cooldown %ds "
                "(retry_after=%d, repeated=%s)",
                account_idx, cooldown, retry_after,
                (now - last_429) < RATE_LIMIT_REPEAT_WINDOW if last_429 else False,
            )
            _save_accounts_state(_accounts)


def _on_dead_account(account_idx: int) -> None:
    with _accounts_lock:
        if account_idx < len(_accounts):
            _accounts[account_idx]["dead"] = True
            log.error("[copilot_account_dead] Account #%d marked dead", account_idx)
            _save_accounts_state(_accounts)


def _ensure_copilot_token(acc: Dict[str, Any], account_idx: int, urlopen) -> str:
    """Ensure account has a valid Copilot API token; exchange if needed."""
    now = time.time()
    if acc["copilot_token"] and (acc["expires_at"] - now) > REFRESH_THRESHOLD_SEC:
        return acc["copilot_token"]

    if not acc["github_token"]:
        log.warning("Account #%d: no GitHub token", account_idx)
        return ""

    log.info("[copilot_token_exchange] Exchanging token for account #%d", account_idx)
    result = _exchange_token(acc["github_token"], urlopen)
    if result and result["copilot_token"]:
        with _accounts_lock:
            acc["copilot_token"] = result["copilot_token"]
            acc["expires_at"] = result["expires_at"]
            acc["copilot_api_base"] = result.get("copilot_api_base", COPILOT_DEFAULT_API_BASE)
            _save_accounts_state(_accounts)
        log.info(
            "[copilot_token_exchange] Account #%d token exchanged, expires_at=%d, api_base=%s",
            account_idx, result["expires_at"], acc["copilot_api_base"],
        )
        return acc["copilot_token"]
    return ""


def _is_multi_account() -> bool:
    with _accounts_lock:
        _init_accounts()
        return len(_accounts) > 1


def _record_successful_request(account_idx: int) -> None:
    with _accounts_lock:
        if account_idx < len(_accounts):
            _accounts[account_idx].setdefault("request_timestamps", []).append(time.time())
            _save_accounts_state(_accounts)


# ---------------------------------------------------------------------------
# Status / diagnostics
# ---------------------------------------------------------------------------

def get_account_usage(acc: Dict[str, Any]) -> Dict[str, int]:
    now = time.time()
    timestamps = acc.get("request_timestamps", [])
    return {
        "5h": sum(1 for t in timestamps if now - t < 18000),
        "7d": sum(1 for t in timestamps if now - t < 604800),
    }


def get_accounts_status(force_reload: bool = True) -> List[Dict[str, Any]]:
    with _accounts_lock:
        _init_accounts(force=force_reload)
        now = time.time()
        result = []
        for i, acc in enumerate(_accounts):
            usage = get_account_usage(acc)
            result.append({
                "index": i,
                "active": i == _active_idx,
                "dead": acc.get("dead", False),
                "cooldown_until": acc.get("cooldown_until", 0),
                "in_cooldown": acc.get("cooldown_until", 0) > now,
                "cooldown_remaining": max(0, int(acc.get("cooldown_until", 0) - now)),
                "has_token": bool(acc.get("copilot_token")),
                "has_github_token": bool(acc.get("github_token")),
                "requests_5h": usage["5h"],
                "requests_7d": usage["7d"],
                "last_429_at": acc.get("last_429_at", 0),
            })
        return result
