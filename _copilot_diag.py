#!/usr/bin/env python3
"""Copilot billing & multipart content diagnostic — 8 tests."""

from __future__ import annotations
import json, os, ssl, sys, time, uuid, urllib.request, urllib.error
from pathlib import Path

# --- Bootstrap veles env ---
sys.path.insert(0, "/opt/veles")
for line in Path("/opt/veles/.env").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    os.environ[k.strip()] = v

from ouroboros.copilot_proxy_accounts import (
    _init_accounts, _get_active_account, _ensure_copilot_token,
)

_init_accounts(force=True)
acc, idx = _get_active_account()
token = _ensure_copilot_token(acc, idx, urllib.request.urlopen)
api_base = acc.get("copilot_api_base", "https://api.individual.githubcopilot.com")
endpoint = api_base.rstrip("/") + "/chat/completions"

print(f"[init] account #{idx}, api_base={api_base}")

# --- Long system text (~20k chars, ~5k tokens) ---
LONG_TEXT = ("This is a long system prompt block used for diagnostic testing. " * 50 + "\n") * 20
print(f"[init] LONG_TEXT length = {len(LONG_TEXT)} chars")

RESULTS_FILE = Path("/tmp/_copilot_diag_results.txt")
results = []


def do_request(payload: dict, initiator: str = "user", interaction_id: str | None = None):
    """Send request, return (status, response_dict, response_headers_dict)."""
    body = json.dumps(payload).encode()
    iid = interaction_id or str(uuid.uuid4())
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "GitHubCopilotChat/0.29.1",
        "Editor-Version": "vscode/1.96.0",
        "Editor-Plugin-Version": "copilot-chat/0.24.0",
        "Copilot-Integration-Id": "vscode-chat",
        "Openai-Organization": "github-copilot",
        "Openai-Intent": "conversation-agent",
        "X-Initiator": initiator,
        "X-Interaction-Type": "conversation-agent",
        "X-Request-Id": str(uuid.uuid4()),
        "X-Interaction-Id": iid,
    }
    req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=180, context=ctx) as resp:
            status = resp.status
            resp_headers = dict(resp.headers)
            raw = resp.read().decode("utf-8")
        return status, json.loads(raw), resp_headers
    except urllib.error.HTTPError as e:
        body_preview = ""
        try:
            body_preview = e.read().decode(errors="replace")[:2000]
        except Exception:
            pass
        resp_headers = dict(e.headers) if e.headers else {}
        return e.code, {"error": body_preview}, resp_headers


def run_test(name, model, sys_msg, initiator="user", extra_log=False):
    """Run a single test and record results."""
    payload = {
        "model": model,
        "messages": [
            sys_msg,
            {"role": "user", "content": "Say hello in one word."},
        ],
        "max_tokens": 100,
        "stream": False,
    }
    print(f"\n{'='*60}")
    print(f"[{name}] model={model} initiator={initiator}")
    print(f"  system format: {type(sys_msg.get('content')).__name__}"
          f" (len={len(json.dumps(sys_msg))})")

    status, data, hdrs = do_request(payload, initiator=initiator)
    usage = data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", "?")
    cached_tokens = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0) if isinstance(usage.get("prompt_tokens_details"), dict) else 0
    completion_tokens = usage.get("completion_tokens", "?")

    content = ""
    choices = data.get("choices", [])
    if choices:
        content = choices[0].get("message", {}).get("content", "")[:200]

    row = {
        "test": name,
        "model": model,
        "initiator": initiator,
        "http": status,
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": usage.get("total_tokens", "?"),
        "content_preview": content[:80],
        "full_usage": usage,
    }

    print(f"  HTTP {status}")
    print(f"  prompt_tokens={prompt_tokens}  cached={cached_tokens}  completion={completion_tokens}")
    print(f"  content: {content[:100]}")

    if extra_log or status != 200:
        row["response_headers"] = hdrs
        print(f"  --- ALL RESPONSE HEADERS ---")
        for k, v in sorted(hdrs.items()):
            print(f"    {k}: {v}")

    if status != 200:
        err = data.get("error", "")[:500]
        print(f"  ERROR: {err}")
        row["error"] = err

    results.append(row)
    return row


# ============================================================
# Plain string system messages
# ============================================================
SYS_PLAIN = {"role": "system", "content": LONG_TEXT}

# Test 1: Sonnet plain
run_test("T1_sonnet_plain", "claude-sonnet-4.6", SYS_PLAIN)
time.sleep(2)

# Test 2: Opus plain
run_test("T2_opus_plain", "claude-opus-4.6", SYS_PLAIN)
time.sleep(5)

# ============================================================
# Multipart WITHOUT cache_control
# ============================================================
SYS_MULTI_NOCACHE = {
    "role": "system",
    "content": [
        {"type": "text", "text": LONG_TEXT},
        {"type": "text", "text": "Short dynamic part. This is ephemeral context."},
    ],
}

# Test 3: Sonnet multipart no cache_control
run_test("T3_sonnet_multi_nocache", "claude-sonnet-4.6", SYS_MULTI_NOCACHE)
time.sleep(2)

# Test 4: Opus multipart no cache_control
run_test("T4_opus_multi_nocache", "claude-opus-4.6", SYS_MULTI_NOCACHE)
time.sleep(5)

# ============================================================
# Multipart WITH cache_control (как context.py делает)
# ============================================================
SYS_MULTI_CACHE = {
    "role": "system",
    "content": [
        {"type": "text", "text": LONG_TEXT, "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        {"type": "text", "text": "Semi-stable part with some context.", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "Dynamic part — state, runtime, recent logs."},
    ],
}

# Test 5: Sonnet multipart with cache_control
run_test("T5_sonnet_multi_cache", "claude-sonnet-4.6", SYS_MULTI_CACHE)
time.sleep(2)

# Test 6: Opus multipart with cache_control
run_test("T6_opus_multi_cache", "claude-opus-4.6", SYS_MULTI_CACHE)
time.sleep(5)

# ============================================================
# Billing headers comparison
# ============================================================
SYS_SHORT = {"role": "system", "content": "You are a helpful assistant."}

# Test 7: Sonnet X-Initiator: user — full headers
run_test("T7_sonnet_billing_user", "claude-sonnet-4.6", SYS_SHORT, initiator="user", extra_log=True)
time.sleep(2)

# Test 8: Sonnet X-Initiator: agent — full headers
run_test("T8_sonnet_billing_agent", "claude-sonnet-4.6", SYS_SHORT, initiator="agent", extra_log=True)

# ============================================================
# Write results
# ============================================================
print("\n\n" + "=" * 80)
print("SUMMARY TABLE")
print("=" * 80)
print(f"{'Test':<28} {'Model':<20} {'SysFmt':<12} {'CacheCtl':<10} {'Initiator':<10} {'HTTP':<6} {'Prompt':<10} {'Cached':<10} {'Compl':<8}")
print("-" * 114)

for r in results:
    name = r["test"]
    # Derive format and cache_control from test name
    if "plain" in name:
        fmt, cc = "plain", "no"
    elif "nocache" in name:
        fmt, cc = "multipart", "no"
    elif "cache" in name:
        fmt, cc = "multipart", "yes"
    else:
        fmt, cc = "short", "no"
    print(f"{name:<28} {r['model']:<20} {fmt:<12} {cc:<10} {r['initiator']:<10} {r['http']:<6} {str(r['prompt_tokens']):<10} {str(r['cached_tokens']):<10} {str(r['completion_tokens']):<8}")

# Headers diff for T7 vs T8
t7 = next((r for r in results if r["test"] == "T7_sonnet_billing_user"), None)
t8 = next((r for r in results if r["test"] == "T8_sonnet_billing_agent"), None)
if t7 and t8 and "response_headers" in t7 and "response_headers" in t8:
    print("\n\nHEADER DIFF: T7 (user) vs T8 (agent)")
    print("=" * 60)
    all_keys = sorted(set(list(t7["response_headers"].keys()) + list(t8["response_headers"].keys())))
    for k in all_keys:
        v7 = t7["response_headers"].get(k, "<missing>")
        v8 = t8["response_headers"].get(k, "<missing>")
        marker = " <<< DIFF" if v7 != v8 else ""
        if marker or "ratelimit" in k.lower() or "github" in k.lower() or "billing" in k.lower() or "quota" in k.lower() or "request-id" in k.lower():
            print(f"  {k}:")
            print(f"    user:  {v7}")
            print(f"    agent: {v8}{marker}")

# Write full JSON results
output = {
    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "tests": results,
}
RESULTS_FILE.write_text(json.dumps(output, indent=2, default=str, ensure_ascii=False))
print(f"\n[done] Full results written to {RESULTS_FILE}")
