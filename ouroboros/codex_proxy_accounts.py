from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

# ── Paths ───────────────────────────────────────────────────────────────
# Single source of truth for all Codex account data (tokens + state).
# Survives restarts AND crashes (atomic writes via tmp+rename).
ACCOUNTS_FILE = Path("/opt/veles-data/state/codex_accounts.json")

# Legacy paths — read on migration only, then ignored
_LEGACY_STATE_FILE = Path("/opt/veles-data/state/codex_accounts_state.json")
_LEGACY_TOKEN_FILE = Path("/opt/veles-data/state/codex_tokens.json")

# ── Timing constants ────────────────────────────────────────────────────
REFRESH_THRESHOLD_SEC = 3600        # refresh access when <1h left
RATE_LIMIT_COOLDOWN_SEC = 600       # 10 min after 429
RATE_LIMIT_REPEAT_WINDOW = 1800     # 30 min window for repeated 429
RATE_LIMIT_ESCALATED_SEC = 3600     # 1h if repeated 429
RATE_LIMIT_EXHAUSTED_SEC = 7200     # 2h if quota fully used
QUOTA_REFRESH_MIN_INTERVAL = 300    # skip quota probe if fresher than 5m
AUTH_FAILURE_CODES = {401, 403}

_accounts_lock = threading.Lock()
_accounts: List[Dict[str, Any]] = []
_active_idx: int = 0


# ════════════════════════════════════════════════════════════════════════
#  ATOMIC PERSISTENCE
# ════════════════════════════════════════════════════════════════════════

def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically: write to tmp file then os.replace.
    Survives crashes — either old file or new file, never partial."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ════════════════════════════════════════════════════════════════════════
#  ACCOUNT LOADING — single file, with .env seed fallback
# ════════════════════════════════════════════════════════════════════════

def _tolerant_json_loads(raw: str) -> Any:
    """Parse JSON that may have single quotes, unquoted keys, trailing commas, BOM."""
    s = raw.strip()
    if s.startswith("\ufeff"):
        s = s[1:]
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        inner = s[1:-1]
        if s[0] == '"':
            inner = inner.replace('\\"', '"')
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


def _empty_account(refresh: str) -> Dict[str, Any]:
    """Create a fresh account dict from a refresh token."""
    return {
        "access": "",
        "refresh": refresh,
        "expires": 0.0,
        "cooldown_until": 0.0,
        "dead": False,
        "last_429_at": 0.0,
        "request_timestamps": [],
        "quota": {},
        "last_error": {},
    }


def _parse_env_seed() -> List[Dict[str, Any]]:
    """Parse CODEX_ACCOUNTS from .env as initial seed (refresh tokens only)."""
    raw = os.environ.get("CODEX_ACCOUNTS", "")
    if not raw:
        return []
    try:
        parsed = _tolerant_json_loads(raw)
        if not isinstance(parsed, list):
            return []
        accounts = []
        for item in parsed:
            if isinstance(item, dict) and item.get("refresh"):
                accounts.append(_empty_account(item["refresh"]))
        if accounts:
            log.info("Parsed %d Codex accounts from CODEX_ACCOUNTS env (seed)", len(accounts))
        return accounts
    except (json.JSONDecodeError, ValueError) as e:
        log.error("Failed to parse CODEX_ACCOUNTS env: %s  |  raw[:200]=%s", e, raw[:200])
        return []


def _migrate_legacy_state(accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge runtime state from legacy codex_accounts_state.json if it exists."""
    if not _LEGACY_STATE_FILE.exists():
        return accounts
    try:
        raw_state = json.loads(_LEGACY_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(raw_state, dict):
            state_list = raw_state.get("accounts", [])
        elif isinstance(raw_state, list):
            state_list = raw_state
        else:
            return accounts
        if not isinstance(state_list, list):
            return accounts

        now = time.time()
        cutoff = now - 604800
        for i, s in enumerate(state_list):
            if i >= len(accounts) or not isinstance(s, dict):
                continue
            # Only take state that's newer/better than seed
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
            if s.get("last_error") and isinstance(s["last_error"], dict):
                accounts[i]["last_error"] = s["last_error"]
            raw_ts = s.get("request_timestamps", [])
            if isinstance(raw_ts, list):
                accounts[i]["request_timestamps"] = [
                    t for t in raw_ts if isinstance(t, (int, float)) and t > cutoff
                ]
        log.info("Migrated state from legacy %s", _LEGACY_STATE_FILE)
        return accounts
    except Exception as e:
        log.warning("Failed to migrate legacy state: %s", e)
        return accounts


def _load_accounts() -> List[Dict[str, Any]]:
    """Load accounts from the single source of truth file.

    Priority:
    1. ACCOUNTS_FILE exists → use it (already has tokens + state)
    2. Legacy state file exists → seed from .env, merge legacy state, save to new file
    3. Neither exists → seed from .env, save to new file
    """
    if ACCOUNTS_FILE.exists():
        try:
            data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                accs = data.get("accounts", [])
                if isinstance(accs, list) and accs:
                    now = time.time()
                    cutoff = now - 604800
                    for acc in accs:
                        raw_ts = acc.get("request_timestamps", [])
                        if isinstance(raw_ts, list):
                            acc["request_timestamps"] = [
                                t for t in raw_ts if isinstance(t, (int, float)) and t > cutoff
                            ]
                    log.info("Loaded %d Codex accounts from %s", len(accs), ACCOUNTS_FILE)

                    # Check if .env has MORE accounts than the file (user added new ones)
                    env_accounts = _parse_env_seed()
                    if len(env_accounts) > len(accs):
                        # Append new accounts from .env seed
                        for i in range(len(accs), len(env_accounts)):
                            accs.append(env_accounts[i])
                        log.info("Added %d new accounts from .env seed", len(env_accounts) - len(accs))
                        _atomic_write_json(ACCOUNTS_FILE, {"active_idx": data.get("active_idx", 0), "accounts": accs})

                    return accs
        except Exception as e:
            log.warning("Failed to load %s: %s", ACCOUNTS_FILE, e)

    # No accounts file yet — seed from .env
    accounts = _parse_env_seed()
    if not accounts:
        return []

    # Try merging legacy state
    accounts = _migrate_legacy_state(accounts)

    # Save to new canonical file
    try:
        _atomic_write_json(ACCOUNTS_FILE, {"active_idx": 0, "accounts": accounts})
        log.info("Created %s with %d accounts", ACCOUNTS_FILE, len(accounts))
    except Exception as e:
        log.warning("Failed to create %s: %s", ACCOUNTS_FILE, e)

    return accounts


def _save_accounts_state(accounts: List[Dict[str, Any]]) -> None:
    """Save current state to the single source of truth file."""
    global _active_idx
    try:
        now = time.time()
        cutoff_7d = now - 604800
        serializable = []
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
                "last_error": acc.get("last_error", {}),
            })
        _atomic_write_json(ACCOUNTS_FILE, {"active_idx": _active_idx, "accounts": serializable})
    except Exception as e:
        log.warning("Failed to save accounts state: %s", e)


# ════════════════════════════════════════════════════════════════════════
#  LEGACY SINGLE-TOKEN SUPPORT (_load_tokens / _save_tokens)
#  Used by consciousness prefix and single-account fallback
# ════════════════════════════════════════════════════════════════════════

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
    if prefix == "CODEX" and not tokens["access_token"] and _LEGACY_TOKEN_FILE.exists():
        try:
            stored = json.loads(_LEGACY_TOKEN_FILE.read_text(encoding="utf-8"))
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
            _LEGACY_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            _LEGACY_TOKEN_FILE.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
        except Exception as e:
            log.warning("Failed to save codex tokens to file: %s", e)


# ════════════════════════════════════════════════════════════════════════
#  OAUTH REFRESH
# ════════════════════════════════════════════════════════════════════════

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


# ════════════════════════════════════════════════════════════════════════
#  MULTI-ACCOUNT MANAGEMENT
# ════════════════════════════════════════════════════════════════════════

def _init_accounts(force: bool = False) -> None:
    global _accounts, _active_idx
    if _accounts and not force:
        return
    _accounts = _load_accounts()
    if _accounts:
        restored_idx = -1
        if ACCOUNTS_FILE.exists():
            try:
                raw_state = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
                if isinstance(raw_state, dict):
                    restored_idx = int(raw_state.get("active_idx", -1))
            except Exception:
                pass
        now = time.time()
        if 0 <= restored_idx < len(_accounts):
            acc = _accounts[restored_idx]
            if not acc.get("dead") and acc.get("cooldown_until", 0) < now:
                _active_idx = restored_idx
            else:
                for i, a in enumerate(_accounts):
                    if not a.get("dead") and a.get("cooldown_until", 0) < now:
                        _active_idx = i
                        break
        else:
            for i, a in enumerate(_accounts):
                if not a.get("dead") and a.get("cooldown_until", 0) < now:
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
        if not acc.get("dead") and acc.get("cooldown_until", 0) < now:
            return acc, _active_idx
        for i in range(len(_accounts)):
            idx = (_active_idx + 1 + i) % len(_accounts)
            acc = _accounts[idx]
            if not acc.get("dead") and acc.get("cooldown_until", 0) < now:
                _active_idx = idx
                log.info("Codex account rotation: switched to #%d", idx)
                return acc, idx
        log.error("All Codex accounts exhausted (dead or on cooldown)")
        return None


def _on_rate_limit(account_idx: int, retry_after: int = 0, reason: str = "rate_limited") -> None:
    with _accounts_lock:
        if account_idx < len(_accounts):
            acc = _accounts[account_idx]
            now = time.time()
            last_429 = acc.get("last_429_at", 0)
            acc["last_429_at"] = now
            repeated = (now - last_429) < RATE_LIMIT_REPEAT_WINDOW if last_429 else False
            if retry_after > 0:
                cooldown = retry_after
            elif reason == "usage_limit_reached":
                cooldown = RATE_LIMIT_EXHAUSTED_SEC
            elif repeated:
                cooldown = RATE_LIMIT_ESCALATED_SEC
            else:
                cooldown = RATE_LIMIT_COOLDOWN_SEC
            acc["cooldown_until"] = now + cooldown
            log.warning(
                "Codex account #%d rate-limited, reason=%s, cooldown %ds (retry_after=%d, repeated=%s)",
                account_idx, reason, cooldown, retry_after, repeated,
            )
            _save_accounts_state(_accounts)


def _on_dead_account(account_idx: int) -> None:
    with _accounts_lock:
        if account_idx < len(_accounts):
            _accounts[account_idx]["dead"] = True
            log.error("Codex account #%d marked dead", account_idx)
            _save_accounts_state(_accounts)


def _refresh_account(acc: Dict[str, Any], account_idx: int, auth_endpoint: str, urlopen) -> str:
    """Refresh access token ONLY if it's expired or about to expire.
    This is the ONLY place that consumes refresh tokens for multi-account."""
    now = time.time()
    # If access token still has >1h of life — don't touch refresh at all
    if acc.get("access") and (acc.get("expires", 0) - now) > REFRESH_THRESHOLD_SEC:
        return acc["access"]
    if not acc.get("refresh"):
        log.warning("Account #%d: no refresh token", account_idx)
        return acc.get("access", "")
    log.info("Refreshing account #%d token (expires in %.0fs)", account_idx, max(0, acc.get("expires", 0) - now))
    result = _do_refresh(acc["refresh"], auth_endpoint, urlopen)
    if result:
        with _accounts_lock:
            acc["access"] = result["access_token"]
            acc["refresh"] = result["refresh_token"]
            acc["expires"] = float(result["expires"])
            _save_accounts_state(_accounts)
        log.info("Account #%d token refreshed, new refresh saved", account_idx)
        return acc["access"]
    return acc.get("access", "")


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
            _accounts[account_idx]["last_error"] = {}
            _save_accounts_state(_accounts)


# ════════════════════════════════════════════════════════════════════════
#  HTTP ERROR CLASSIFICATION
# ════════════════════════════════════════════════════════════════════════

def _body_snippet(raw: Any, limit: int = 500) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode(errors="replace")[:limit]
    return str(raw)[:limit]


def _parse_json_body(raw: Any) -> Dict[str, Any]:
    snippet = _body_snippet(raw, limit=4000)
    if not snippet:
        return {}
    try:
        parsed = json.loads(snippet)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def classify_codex_http_failure(status_code: int, headers: Optional[Dict[str, Any]] = None, body: Any = None) -> Dict[str, Any]:
    hdrs = dict(headers or {})
    lowered = {str(k).lower(): v for k, v in hdrs.items()}
    parsed = _parse_json_body(body)
    error_obj = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
    error_code = str(parsed.get("error_code") or error_obj.get("code") or "").strip()
    error_type = str(parsed.get("type") or error_obj.get("type") or "").strip()
    error_message = str(
        parsed.get("message")
        or parsed.get("error") if isinstance(parsed.get("error"), str) else ""
        or error_obj.get("message")
        or ""
    ).strip()

    primary_used = lowered.get("x-codex-primary-used-percent")
    secondary_used = lowered.get("x-codex-secondary-used-percent")

    category = "http_error"
    reason = "http_error"
    retry_after = 0
    ra = lowered.get("retry-after")
    if isinstance(ra, str) and ra.isdigit():
        retry_after = int(ra)

    if status_code == 429:
        category = "rate_limit"
        exhausted = False
        try:
            exhausted = int(str(secondary_used)) >= 100 or int(str(primary_used)) >= 100
        except Exception:
            exhausted = False
        lowered_blob = f"{error_code} {error_type} {error_message}".lower()
        if exhausted or "usage_limit" in lowered_blob or "limit reached" in lowered_blob:
            reason = "usage_limit_reached"
        elif retry_after > 0:
            reason = "retry_after"
        else:
            reason = "rate_limited"
    elif status_code in AUTH_FAILURE_CODES:
        category = "auth"
        reason = "unauthorized" if status_code == 401 else "forbidden"

    return {
        "status_code": int(status_code),
        "category": category,
        "reason": reason,
        "retry_after": retry_after,
        "error_code": error_code,
        "error_type": error_type,
        "message": error_message[:500],
        "body_preview": _body_snippet(body),
        "primary_used_percent": primary_used,
        "secondary_used_percent": secondary_used,
        "classified_at": time.time(),
    }


def _set_last_error(account_idx: int, error: Dict[str, Any]) -> None:
    with _accounts_lock:
        if account_idx < len(_accounts):
            _accounts[account_idx]["last_error"] = dict(error or {})
            _save_accounts_state(_accounts)


def _clear_last_error(account_idx: int) -> None:
    with _accounts_lock:
        if account_idx < len(_accounts) and _accounts[account_idx].get("last_error"):
            _accounts[account_idx]["last_error"] = {}
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
                return {"ok": False, "active_idx": _active_idx, "total": len(_accounts),
                        "message": f"Index {target_idx} out of range (0-{len(_accounts)-1})"}
            acc = _accounts[target_idx]
            if acc.get("dead"):
                return {"ok": False, "active_idx": _active_idx, "total": len(_accounts),
                        "message": f"Account #{target_idx} is dead"}
            _active_idx = target_idx
            _save_accounts_state(_accounts)
            log.info("Force-switched to Codex account #%d", target_idx)
            return {"ok": True, "active_idx": target_idx, "total": len(_accounts),
                    "message": f"Switched to account #{target_idx}"}
        for i in range(len(_accounts)):
            idx = (_active_idx + 1 + i) % len(_accounts)
            acc = _accounts[idx]
            if not acc.get("dead") and acc.get("cooldown_until", 0) < now:
                _active_idx = idx
                _save_accounts_state(_accounts)
                log.info("Force-rotated to Codex account #%d", idx)
                return {"ok": True, "active_idx": idx, "total": len(_accounts),
                        "message": f"Rotated to account #{idx}"}
        return {"ok": False, "active_idx": _active_idx, "total": len(_accounts),
                "message": "All other accounts dead or on cooldown"}


# ════════════════════════════════════════════════════════════════════════
#  BOOTSTRAP & QUOTA (called at startup / on /accounts)
# ════════════════════════════════════════════════════════════════════════

def bootstrap_refresh_missing_access_tokens(auth_endpoint: str, urlopen) -> Dict[str, Any]:
    """Refresh ONLY accounts that have NO valid access token.
    Does NOT touch accounts with a live access token — their refresh stays intact."""
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
            # KEY FIX: skip if access token exists AND is not expired
            now = time.time()
            if acc.get("access") and (acc.get("expires", 0) - now) > REFRESH_THRESHOLD_SEC:
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


def refresh_all_quotas() -> Dict[int, Optional[Dict[str, Any]]]:
    """Probe each live account for quota headers WITHOUT refreshing tokens.

    Only sends a minimal request to accounts that already have a valid access token.
    Does NOT call _refresh_account — never consumes refresh tokens.
    """
    import ssl
    import urllib.error
    import urllib.request

    CODEX_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"

    with _accounts_lock:
        _init_accounts()
        snapshot = [(i, dict(acc)) for i, acc in enumerate(_accounts)]

    now = time.time()
    results: Dict[int, Optional[Dict[str, Any]]] = {}

    for idx, acc in snapshot:
        if acc.get("dead"):
            results[idx] = None
            continue
        if acc.get("cooldown_until", 0) > now:
            results[idx] = None
            continue

        # Skip if quota was updated recently
        existing_q = acc.get("quota") or {}
        if existing_q.get("updated_at", 0) > now - QUOTA_REFRESH_MIN_INTERVAL:
            results[idx] = existing_q
            continue

        # KEY CHANGE: only probe if we already have a valid access token
        access = acc.get("access", "")
        if not access or acc.get("expires", 0) < now:
            # No valid access — skip, don't burn refresh token for quota check
            results[idx] = acc.get("quota") or None
            continue

        # Minimal Codex request just for headers
        payload = {
            "model": "gpt-5.4",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "Hi"}]}],
            "instructions": "Reply with one word.",
            "store": False,
            "stream": True,
        }
        try:
            body = json.dumps(payload).encode()
            headers = {
                "Authorization": f"Bearer {access}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }
            req = __import__("urllib.request").request.Request(
                CODEX_ENDPOINT, data=body, headers=headers, method="POST",
            )
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                resp_headers = dict(resp.headers)
                resp.read()

            quota: Dict[str, Any] = {}
            for k, v in resp_headers.items():
                kl = k.lower()
                if not kl.startswith("x-codex-"):
                    continue
                key = kl[len("x-codex-"):].replace("-", "_")
                if v.isdigit():
                    quota[key] = int(v)
                else:
                    try:
                        quota[key] = float(v)
                    except (ValueError, TypeError):
                        quota[key] = v

            if quota:
                _update_account_quota(idx, quota)
                results[idx] = quota
                log.info("Refreshed quota for account #%d: %s", idx, quota)
            else:
                results[idx] = None
        except urllib.error.HTTPError as e:
            body_preview = ""
            try:
                body_preview = e.read().decode(errors="replace")
            except Exception:
                pass
            diagnostic = classify_codex_http_failure(e.code, dict(getattr(e, "headers", {}) or {}), body_preview)
            _set_last_error(idx, diagnostic)
            if diagnostic["category"] == "rate_limit":
                _on_rate_limit(idx, retry_after=diagnostic.get("retry_after", 0), reason=diagnostic.get("reason", "rate_limited"))
                q = {
                    k: diagnostic[k]
                    for k in ("primary_used_percent", "secondary_used_percent")
                    if diagnostic.get(k) is not None
                }
                if q:
                    _update_account_quota(idx, q)
                log.warning("Quota refresh hit rate limit for account #%d: %s", idx, diagnostic)
            else:
                log.warning("Quota refresh HTTP failure for account #%d: %s", idx, diagnostic)
            results[idx] = None
        except Exception as e:
            log.warning("Failed to refresh quota for account #%d: %s", idx, e)
            results[idx] = None

        time.sleep(1.5)

    return results


# ════════════════════════════════════════════════════════════════════════
#  STATUS REPORTING
# ════════════════════════════════════════════════════════════════════════

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
                "is_active": i == _active_idx,
                "dead": acc.get("dead", False),
                "cooldown_until": acc.get("cooldown_until", 0),
                "in_cooldown": acc.get("cooldown_until", 0) > now,
                "cooldown_remaining": max(0, int(acc.get("cooldown_until", 0) - now)),
                "has_access": bool(acc.get("access")),
                "has_refresh": bool(acc.get("refresh")),
                "requests_5h": usage["5h"],
                "requests_7d": usage["7d"],
                "usage_5h": usage["5h"],
                "usage_7d": usage["7d"],
                "last_429_at": acc.get("last_429_at", 0),
            }
            last_error = acc.get("last_error") or {}
            if last_error:
                entry["last_error"] = dict(last_error)
                entry["last_error_category"] = last_error.get("category", "")
                entry["last_error_reason"] = last_error.get("reason", "")
                entry["last_error_status_code"] = last_error.get("status_code", 0)
            if quota:
                entry["quota_5h_used_pct"] = quota.get("primary_used_percent")
                entry["quota_7d_used_pct"] = quota.get("secondary_used_percent")
                entry["quota_plan"] = quota.get("plan_type", "")
                entry["quota_5h_reset_at"] = quota.get("primary_reset_at")
                entry["quota_7d_reset_at"] = quota.get("secondary_reset_at")
                entry["quota_updated_at"] = quota.get("updated_at", 0)
            result.append(entry)
        return result
