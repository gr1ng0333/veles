from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_FILE = Path("/opt/veles-data/state/codex_tokens.json")
ACCOUNTS_STATE_FILE = Path("/opt/veles-data/state/codex_accounts_state.json")
REFRESH_THRESHOLD_SEC = 3600
RATE_LIMIT_COOLDOWN_SEC = 600
RATE_LIMIT_REPEAT_WINDOW = 1800
RATE_LIMIT_ESCALATED_SEC = 3600

_accounts_lock = threading.Lock()
_accounts: List[Dict[str, Any]] = []
_active_idx: int = 0


def _load_tokens(prefix: str = "CODEX") -> Dict[str, str]:
    if prefix == "CODEX":
        access_key, refresh_key, expires_key, account_key = (
            "CODEX_ACCESS_TOKEN", "CODEX_REFRESH_TOKEN", "CODEX_TOKEN_EXPIRES", "CODEX_ACCOUNT_ID",
        )
    else:
        access_key = f"{prefix}_ACCESS"
        refresh_key = f"{prefix}_REFRESH"
        expires_key = f"{prefix}_EXPIRES"
        account_key = f"{prefix}_ACCOUNT_ID"

    tokens = {
        "access_token": os.environ.get(access_key, ""),
        "refresh_token": os.environ.get(refresh_key, ""),
        "expires": os.environ.get(expires_key, "0"),
        "account_id": os.environ.get(account_key, ""),
    }
    if prefix == "CODEX" and not tokens["access_token"] and TOKEN_FILE.exists():
        try:
            stored = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            tokens["access_token"] = stored.get("access_token", "")
            tokens["refresh_token"] = stored.get("refresh_token", "")
            tokens["expires"] = str(stored.get("expires", "0"))
            tokens["account_id"] = stored.get("account_id", tokens["account_id"])
        except Exception as e:
            log.warning("Failed to load codex tokens from file: %s", e)
    return tokens


def _save_tokens(tokens: Dict[str, str], prefix: str = "CODEX") -> None:
    if prefix == "CODEX":
        access_key, refresh_key, expires_key, account_key = (
            "CODEX_ACCESS_TOKEN", "CODEX_REFRESH_TOKEN", "CODEX_TOKEN_EXPIRES", "CODEX_ACCOUNT_ID",
        )
    else:
        access_key = f"{prefix}_ACCESS"
        refresh_key = f"{prefix}_REFRESH"
        expires_key = f"{prefix}_EXPIRES"
        account_key = f"{prefix}_ACCOUNT_ID"

    os.environ[access_key] = tokens["access_token"]
    os.environ[refresh_key] = tokens["refresh_token"]
    os.environ[expires_key] = str(tokens["expires"])
    if tokens.get("account_id"):
        os.environ[account_key] = tokens["account_id"]
    if prefix == "CODEX":
        try:
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_FILE.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
        except Exception as e:
            log.warning("Failed to save codex tokens to file: %s", e)


def _do_refresh(refresh_token: str, auth_endpoint: str, urlopen) -> Optional[Dict[str, str]]:
    now = time.time()
    try:
        body = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        }).encode()
        req = __import__("urllib.request").request.Request(
            auth_endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        return {
            "access_token": data.get("access_token", ""),
            "refresh_token": data.get("refresh_token", refresh_token),
            "expires": str(int(now + int(data.get("expires_in", 864000)))),
        }
    except Exception as e:
        log.error("OAuth refresh failed: %s", e)
        return None


def refresh_token_if_needed(auth_endpoint: str, urlopen, prefix: str = "CODEX") -> str:
    tokens = _load_tokens(prefix)
    expires = float(tokens.get("expires") or 0)
    now = time.time()
    remaining = max(0, expires - now)
    is_consciousness = prefix != "CODEX"

    if tokens["access_token"] and (expires - now) > REFRESH_THRESHOLD_SEC:
        return tokens["access_token"]
    if not tokens["refresh_token"]:
        log.warning("Codex token expired and no refresh token available (prefix=%s)", prefix)
        return tokens["access_token"]

    if is_consciousness:
        log.info("consciousness_token_proactive_refresh expires_in=%ds", int(remaining))
    else:
        log.info("Refreshing Codex OAuth token (prefix=%s, expires in %.0fs)", prefix, remaining)

    result = _do_refresh(tokens["refresh_token"], auth_endpoint, urlopen)
    if result:
        tokens.update(result)
        _save_tokens(tokens, prefix)
        new_expires = float(result.get("expires", 0))
        if is_consciousness:
            log.info("consciousness_token_refreshed expires_in=%ds", int(new_expires - time.time()))
        else:
            log.info("Codex OAuth token refreshed successfully (prefix=%s)", prefix)
        return tokens["access_token"]

    if is_consciousness:
        log.error("consciousness_token_refresh_failed error=refresh_returned_none")
    return tokens["access_token"]


def _tolerant_json_loads(raw: str) -> Any:
    s = raw.strip()
    if s.startswith("﻿"):
        s = s[1:]
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        inner = s[1:-1]
        if s[0] == '"':
            inner = inner.replace('\"', '"')
        if inner.lstrip().startswith(("[", "{")):
            s = inner
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    fixed = s.replace("'", '"')
    fixed = re.sub(r'(?<=[{,])\s*([A-Za-z_]\w*)\s*:', r' "\1":', fixed)
    fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
    return json.loads(fixed)


def _load_accounts() -> List[Dict[str, Any]]:
    raw = os.environ.get("CODEX_ACCOUNTS", "")
    accounts: List[Dict[str, Any]] = []
    if raw:
        try:
            parsed = _tolerant_json_loads(raw)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and item.get("refresh"):
                        accounts.append({
                            "access": item.get("access", ""),
                            "refresh": item["refresh"],
                            "expires": float(item.get("expires", 0)),
                            "cooldown_until": 0.0,
                            "dead": False,
                            "last_429_at": 0.0,
                            "request_timestamps": [],
                            "quota": {},
                        })
            if accounts:
                log.info("Loaded %d Codex accounts from CODEX_ACCOUNTS", len(accounts))
            else:
                log.warning("CODEX_ACCOUNTS parsed but contained 0 valid accounts (need 'refresh' key)")
        except (json.JSONDecodeError, ValueError) as e:
            log.error("Failed to parse CODEX_ACCOUNTS: %s  |  raw[:200]=%s", e, raw[:200])
    if ACCOUNTS_STATE_FILE.exists():
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
                        if s.get("access"):
                            accounts[i]["access"] = s["access"]
                        if s.get("refresh"):
                            accounts[i]["refresh"] = s["refresh"]
                        if s.get("expires"):
                            accounts[i]["expires"] = float(s["expires"])
                        accounts[i]["cooldown_until"] = float(s.get("cooldown_until", 0))
                        accounts[i]["last_429_at"] = float(s.get("last_429_at", 0))
                        if s.get("quota") and isinstance(s["quota"], dict):
                            accounts[i]["quota"] = s["quota"]
                        raw_ts = s.get("request_timestamps", [])
                        if isinstance(raw_ts, list):
                            cutoff = time.time() - 604800
                            accounts[i]["request_timestamps"] = [
                                t for t in raw_ts if isinstance(t, (int, float)) and t > cutoff
                            ]
        except Exception as e:
            log.warning("Failed to load accounts state: %s", e)
    return accounts


def _save_accounts_state(accounts: List[Dict[str, Any]]) -> None:
    global _active_idx
    try:
        ACCOUNTS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        serializable = []
        now = time.time()
        cutoff_7d = now - 604800
        for acc in accounts:
            raw_ts = acc.get("request_timestamps", [])
            pruned = [t for t in raw_ts if isinstance(t, (int, float)) and t > cutoff_7d]
            acc["request_timestamps"] = pruned
            serializable.append({
                "access": acc.get("access", ""),
                "refresh": acc.get("refresh", ""),
                "expires": acc.get("expires", 0),
                "cooldown_until": acc.get("cooldown_until", 0),
                "dead": acc.get("dead", False),
                "last_429_at": acc.get("last_429_at", 0),
                "request_timestamps": pruned,
                "quota": acc.get("quota", {}),
            })
        state_obj = {"active_idx": _active_idx, "accounts": serializable}
        ACCOUNTS_STATE_FILE.write_text(json.dumps(state_obj, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Failed to save accounts state: %s", e)


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
                for i, acc in enumerate(_accounts):
                    if not acc["dead"] and acc["cooldown_until"] < now:
                        _active_idx = i
                        break
        else:
            for i, acc in enumerate(_accounts):
                if not acc["dead"] and acc["cooldown_until"] < now:
                    _active_idx = i
                    break
        log.info("Codex multi-account: %d accounts loaded, active=#%d", len(_accounts), _active_idx)


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
                log.info("Codex account rotation: switched to #%d", idx)
                return acc, idx
        log.error("All Codex accounts exhausted (dead or on cooldown)")
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
                "Codex account #%d rate-limited, cooldown %ds (retry_after=%d, repeated=%s)",
                account_idx, cooldown, retry_after,
                (now - last_429) < RATE_LIMIT_REPEAT_WINDOW if last_429 else False,
            )
            _save_accounts_state(_accounts)


def _on_dead_account(account_idx: int) -> None:
    with _accounts_lock:
        if account_idx < len(_accounts):
            _accounts[account_idx]["dead"] = True
            log.error("Codex account #%d marked dead", account_idx)
            _save_accounts_state(_accounts)


def _refresh_account(acc: Dict[str, Any], account_idx: int, auth_endpoint: str, urlopen) -> str:
    now = time.time()
    if acc["access"] and (acc["expires"] - now) > REFRESH_THRESHOLD_SEC:
        return acc["access"]
    if not acc["refresh"]:
        log.warning("Account #%d: no refresh token", account_idx)
        return acc["access"]
    log.info("Refreshing account #%d token (expires in %.0fs)", account_idx, max(0, acc["expires"] - now))
    result = _do_refresh(acc["refresh"], auth_endpoint, urlopen)
    if result:
        with _accounts_lock:
            acc["access"] = result["access_token"]
            acc["refresh"] = result["refresh_token"]
            acc["expires"] = float(result["expires"])
            _save_accounts_state(_accounts)
        log.info("Account #%d token refreshed", account_idx)
        return acc["access"]
    return acc["access"]


def _is_multi_account() -> bool:
    with _accounts_lock:
        _init_accounts()
        return len(_accounts) > 0


def get_account_usage(acc: Dict[str, Any]) -> Dict[str, int]:
    now = time.time()
    timestamps = acc.get("request_timestamps", [])
    last_5h = sum(1 for t in timestamps if now - t < 18000)
    last_7d = sum(1 for t in timestamps if now - t < 604800)
    return {"5h": last_5h, "7d": last_7d}


def _record_successful_request(account_idx: int) -> None:
    with _accounts_lock:
        if account_idx < len(_accounts):
            _accounts[account_idx].setdefault("request_timestamps", []).append(time.time())
            _save_accounts_state(_accounts)


def _update_account_quota(account_idx: int, quota: Dict[str, Any]) -> None:
    """Store real Codex quota data (from x-codex-* response headers) for an account."""
    if not quota:
        return
    with _accounts_lock:
        if account_idx < len(_accounts):
            quota["updated_at"] = time.time()
            _accounts[account_idx]["quota"] = quota
            _save_accounts_state(_accounts)


def force_switch_account(target_idx: int = -1) -> Dict[str, Any]:
    global _active_idx
    with _accounts_lock:
        _init_accounts()
        if not _accounts:
            return {"ok": False, "active_idx": -1, "total": 0, "message": "No Codex accounts configured"}
        now = time.time()
        if target_idx >= 0:
            if target_idx >= len(_accounts):
                return {"ok": False, "active_idx": _active_idx, "total": len(_accounts), "message": f"Index {target_idx} out of range (0-{len(_accounts)-1})"}
            acc = _accounts[target_idx]
            if acc["dead"]:
                return {"ok": False, "active_idx": _active_idx, "total": len(_accounts), "message": f"Account #{target_idx} is dead"}
            _active_idx = target_idx
            _save_accounts_state(_accounts)
            log.info("Force-switched to Codex account #%d", target_idx)
            return {"ok": True, "active_idx": target_idx, "total": len(_accounts), "message": f"Switched to account #{target_idx}"}
        for i in range(len(_accounts)):
            idx = (_active_idx + 1 + i) % len(_accounts)
            acc = _accounts[idx]
            if not acc["dead"] and acc["cooldown_until"] < now:
                _active_idx = idx
                _save_accounts_state(_accounts)
                log.info("Force-rotated to Codex account #%d", idx)
                return {"ok": True, "active_idx": idx, "total": len(_accounts), "message": f"Rotated to account #{idx}"}
        return {"ok": False, "active_idx": _active_idx, "total": len(_accounts), "message": "All other accounts dead or on cooldown"}


def bootstrap_refresh_missing_access_tokens(auth_endpoint: str, urlopen) -> Dict[str, Any]:
    refreshed: List[int] = []
    failed: List[int] = []
    skipped: List[int] = []

    with _accounts_lock:
        _init_accounts(force=True)
        total = len(_accounts)

    if total == 0:
        return {"total": 0, "refreshed": refreshed, "failed": failed, "skipped": skipped}

    for idx in range(total):
        with _accounts_lock:
            acc = _accounts[idx]
            if acc.get("dead"):
                skipped.append(idx)
                continue
            if acc.get("access"):
                skipped.append(idx)
                continue
            if not acc.get("refresh"):
                failed.append(idx)
                continue
        token = _refresh_account(acc, idx, auth_endpoint, urlopen)
        if token:
            refreshed.append(idx)
        else:
            failed.append(idx)

    return {"total": total, "refreshed": refreshed, "failed": failed, "skipped": skipped}



def get_accounts_status(force_reload: bool = False) -> List[Dict[str, Any]]:
    with _accounts_lock:
        _init_accounts(force=force_reload)
        now = time.time()
        result = []
        for i, acc in enumerate(_accounts):
            usage = get_account_usage(acc)
            quota = acc.get("quota", {})
            entry: Dict[str, Any] = {
                "index": i,
                "active": i == _active_idx,
                "dead": acc.get("dead", False),
                "cooldown_until": acc.get("cooldown_until", 0),
                "in_cooldown": acc.get("cooldown_until", 0) > now,
                "cooldown_remaining": max(0, int(acc.get("cooldown_until", 0) - now)),
                "has_access": bool(acc.get("access")),
                "has_refresh": bool(acc.get("refresh")),
                "requests_5h": usage["5h"],
                "requests_7d": usage["7d"],
                "last_429_at": acc.get("last_429_at", 0),
            }
            # Real OpenAI quota from x-codex-* headers
            if quota:
                entry["quota_5h_used_pct"] = quota.get("primary_used_percent")
                entry["quota_7d_used_pct"] = quota.get("secondary_used_percent")
                entry["quota_plan"] = quota.get("plan_type", "")
                entry["quota_5h_reset_at"] = quota.get("primary_reset_at")
                entry["quota_7d_reset_at"] = quota.get("secondary_reset_at")
                entry["quota_updated_at"] = quota.get("updated_at", 0)
            result.append(entry)
        return result
