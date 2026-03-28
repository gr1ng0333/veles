"""
Ouroboros — Copilot Pro OAuth account management.

Manages GitHub PAT → Copilot API token exchange and multi-account rotation.
Tracks per-account usage quota (model cost multipliers).
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
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

# ---------------------------------------------------------------------------
# Copilot Pro quota constants
# ---------------------------------------------------------------------------

COPILOT_MODEL_COST: Dict[str, float] = {
    "claude-haiku-4.5": 0.33,
    "claude-sonnet-4.6": 1.0,
    "claude-opus-4.6": 3.0,
}
COPILOT_MONTHLY_QUOTA = 300  # units per account per month

# Default subscription dates for existing accounts
_DEFAULT_PURCHASED_AT = "2026-03-15"
_DEFAULT_SUBSCRIPTION_UNTIL = "2026-04-15"

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
    # Strip surrounding quotes — bash source or .env loaders may leave them
    if raw and len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1]
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
            log.warning("COPILOT_ACCOUNTS JSON parse failed: %s — trying token extraction", e)
            # Fallback: extract GitHub tokens directly (handles bash-stripped JSON
            # where 'source .env' removes inner double-quotes from unquoted values)
            tokens = re.findall(r'gh[a-z]_[A-Za-z0-9_]+', raw)
            for t in tokens:
                accounts.append({
                    "github_token": t,
                    "copilot_token": "",
                    "expires_at": 0,
                    "cooldown_until": 0.0,
                    "dead": False,
                    "last_429_at": 0.0,
                    "request_timestamps": [],
                })
            if accounts:
                log.info("Extracted %d Copilot token(s) from COPILOT_ACCOUNTS (non-JSON fallback)", len(accounts))
            else:
                log.error(
                    "COPILOT_ACCOUNTS set but no tokens found. "
                    "Wrap JSON in single quotes in .env: COPILOT_ACCOUNTS='[{\"github_token\":\"ghu_...\"}]'"
                )

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
                        # Restore quota tracking fields
                        accounts[i]["purchased_at"] = s.get("purchased_at", _DEFAULT_PURCHASED_AT)
                        accounts[i]["subscription_until"] = s.get("subscription_until", _DEFAULT_SUBSCRIPTION_UNTIL)
                        accounts[i]["usage_units"] = float(s.get("usage_units", 0.0))
                        accounts[i]["last_reset"] = s.get("last_reset", "")
                        accounts[i]["usage_history"] = s.get("usage_history", [])
        except Exception as e:
            log.warning("Failed to load Copilot accounts state: %s", e)

    # Ensure quota tracking fields exist with defaults
    for acc in accounts:
        acc.setdefault("purchased_at", _DEFAULT_PURCHASED_AT)
        acc.setdefault("subscription_until", _DEFAULT_SUBSCRIPTION_UNTIL)
        acc.setdefault("usage_units", 0.0)
        acc.setdefault("last_reset", "")
        acc.setdefault("usage_history", [])

    return accounts


def _save_accounts_state(accounts: List[Dict[str, Any]]) -> None:
    global _active_idx
    try:
        ACCOUNTS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        cutoff_7d = now - 604800
        serializable = []
        for i, acc in enumerate(accounts):
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
                # Quota tracking fields
                "idx": i,
                "purchased_at": acc.get("purchased_at", _DEFAULT_PURCHASED_AT),
                "subscription_until": acc.get("subscription_until", _DEFAULT_SUBSCRIPTION_UNTIL),
                "usage_units": acc.get("usage_units", 0.0),
                "last_reset": acc.get("last_reset", ""),
                "usage_history": acc.get("usage_history", []),
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


def _shortest_cooldown_remaining() -> float:
    """Return seconds until the earliest account exits cooldown. 0 if none on cooldown."""
    with _accounts_lock:
        now = time.time()
        remaining = []
        for acc in _accounts:
            if acc.get("dead"):
                continue
            cd = acc.get("cooldown_until", 0)
            if cd > now:
                remaining.append(cd - now)
        return min(remaining) if remaining else 0.0


def _apply_soft_cooldown(seconds: int) -> float:
    """Apply a temporary cooldown to all live accounts when the backend reports exhaustion with no wait hint."""
    with _accounts_lock:
        _init_accounts()
        if not _accounts:
            return 0.0
        now = time.time()
        target = now + max(seconds, 0)
        changed = False
        for acc in _accounts:
            if acc.get("dead"):
                continue
            if acc.get("cooldown_until", 0) < target:
                acc["cooldown_until"] = target
                changed = True
        if changed:
            _save_accounts_state(_accounts)
            return float(max(seconds, 0))
        return 0.0


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
                # Quota tracking
                "usage_units": acc.get("usage_units", 0.0),
                "purchased_at": acc.get("purchased_at", ""),
                "subscription_until": acc.get("subscription_until", ""),
            })
        return result


# ---------------------------------------------------------------------------
# Quota tracking
# ---------------------------------------------------------------------------

def _model_cost(model: str) -> float:
    """Return the cost multiplier for a Copilot model. Defaults to 1.0."""
    # Exact match first
    if model in COPILOT_MODEL_COST:
        return COPILOT_MODEL_COST[model]
    # Fuzzy match: strip version separators (claude-sonnet-4-5 → claude-sonnet-4.5)
    normalized = model.replace("-", ".")
    for key, cost in COPILOT_MODEL_COST.items():
        if key.replace("-", ".") == normalized:
            return cost
        # Partial match on base name (e.g. "claude-sonnet-4" in "claude-sonnet-4-5")
        if key.replace(".", "-") in model or model.replace(".", "-") in key.replace(".", "-"):
            return cost
    return 1.0


def _check_monthly_reset(acc: Dict[str, Any]) -> None:
    """Reset usage_units if we're in a new month since last_reset."""
    today = datetime.date.today()
    current_month_str = today.strftime("%Y-%m-01")
    last_reset = acc.get("last_reset", "")
    if last_reset != current_month_str:
        old_units = acc.get("usage_units", 0.0)
        acc["usage_units"] = 0.0
        acc["usage_history"] = []
        acc["last_reset"] = current_month_str
        if old_units > 0:
            log.info(
                "copilot_quota_reset: reset %.1f units (last_reset %s → %s)",
                old_units, last_reset or "none", current_month_str,
            )


def track_copilot_usage(account_idx: int, model: str) -> None:
    """Call after each successful Copilot request to track quota usage."""
    with _accounts_lock:
        if account_idx >= len(_accounts):
            return
        acc = _accounts[account_idx]
        _check_monthly_reset(acc)
        cost = _model_cost(model)
        acc["usage_units"] = acc.get("usage_units", 0.0) + cost
        history = acc.setdefault("usage_history", [])
        history.append({
            "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model": model,
            "cost": cost,
        })
        # Keep history bounded — last 2000 entries
        if len(history) > 2000:
            acc["usage_history"] = history[-1000:]
        _save_accounts_state(_accounts)
    log.debug(
        "copilot_usage_tracked account=#%d model=%s cost=%.2f total=%.1f/%d",
        account_idx, model, cost, acc.get("usage_units", 0), COPILOT_MONTHLY_QUOTA,
    )


def copilot_accounts_status_text() -> str:
    """Format Copilot accounts status for Telegram display."""
    statuses = get_accounts_status(force_reload=True)
    if not statuses:
        return "🤖 Copilot Accounts: не настроены"
    lines = [f"🤖 Copilot Accounts: {len(statuses)} шт."]
    for st in statuses:
        i = st["index"]
        if st["dead"]:
            lines.append(f"💀 #{i}: dead")
            continue
        units = st.get("usage_units", 0.0)
        free_pct = max(0, (1 - units / COPILOT_MONTHLY_QUOTA) * 100) if COPILOT_MONTHLY_QUOTA else 0
        sub_until = st.get("subscription_until", "")
        sub_label = ""
        if sub_until:
            try:
                dt = datetime.date.fromisoformat(sub_until)
                sub_label = f" | sub until {dt.strftime('%b %d')}"
            except ValueError:
                sub_label = f" | sub until {sub_until}"
        if st["in_cooldown"]:
            mins = st["cooldown_remaining"] // 60
            icon = "⏳"
            lines.append(f"{icon} #{i}: {units:.1f}/{COPILOT_MONTHLY_QUOTA} units ({free_pct:.0f}% free) | cooldown {mins}m{sub_label}")
        else:
            icon = "✅"
            lines.append(f"{icon} #{i}: {units:.1f}/{COPILOT_MONTHLY_QUOTA} units ({free_pct:.0f}% free){sub_label}")
    return "\n".join(lines)
