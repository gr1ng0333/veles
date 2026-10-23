"""
Ouroboros — Claudexor transport for codex/* models.

Routes Codex calls through a local Claudexor daemon (github.com/razzant/claudexor),
which owns ChatGPT login, account pooling, quota and the Codex client version.
Uses the daemon's caller-owned model operation: we keep the prompt, tools and loop;
the daemon makes exactly one generation per call.

Selected by VELES_CODEX_TRANSPORT=claudexor (default: proxy, i.e. codex_proxy.py).
Uses urllib only (no requests dependency).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = Path.home() / ".claudexor" / "v3"
PROTOCOL_HEADER = "X-Claudexor-Protocol-Major"
PROTOCOL_MAJOR = "3"
SOURCE_ID = "codex"
CONTROL_TIMEOUT_SEC = 60
POLL_INTERVAL_SEC = 0.5
DEFAULT_OPERATION_TIMEOUT_SEC = 900

_MESSAGE_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")
_ALLOWED_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}


class ClaudexorUnavailable(RuntimeError):
    """The daemon cannot be reached or discovered; no generation was dispatched."""


class ClaudexorModelError(RuntimeError):
    """The daemon answered, but the model operation failed or its outcome is unknown."""

    def __init__(self, code: str, message: str, operation_id: str = ""):
        self.code = code
        self.operation_id = operation_id
        super().__init__(f"Claudexor {code}: {message}" + (f" (operation {operation_id})" if operation_id else ""))


def selected_codex_transport() -> str:
    value = os.environ.get("VELES_CODEX_TRANSPORT", "").strip().lower()
    return "claudexor" if value == "claudexor" else "proxy"


def _config_dir() -> Path:
    override = os.environ.get("CLAUDEXOR_CONFIG_DIR", "").strip()
    return Path(override) if override else DEFAULT_CONFIG_DIR


def _discover() -> Tuple[str, str]:
    """Return (base_url, bearer token) from the daemon's control-api descriptor."""
    descriptor = _config_dir() / "daemon" / "control-api.json"
    try:
        raw = json.loads(descriptor.read_text(encoding="utf-8"))
        host = str(raw.get("host") or "").strip()
        port = int(raw.get("port") or 0)
        token = Path(str(raw.get("tokenPath") or "")).read_text(encoding="utf-8").strip()
    except (OSError, ValueError, TypeError) as exc:
        raise ClaudexorUnavailable(f"Claudexor daemon not discovered at {descriptor}: {type(exc).__name__}") from exc
    # The token grants the whole /v2 control API: never send it off the loopback.
    if host not in ("127.0.0.1", "::1", "localhost") or port <= 0 or not token:
        raise ClaudexorUnavailable(f"Claudexor descriptor is not a usable loopback endpoint: {descriptor}")
    return f"http://{host}:{port}", token


# The bearer token must never pass through an ambient HTTP(S)_PROXY.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(base: str, token: str, method: str, path: str, *, body: Any = None, raw: Optional[bytes] = None,
             headers: Optional[Dict[str, str]] = None, want_bytes: bool = False) -> Any:
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    req = urllib.request.Request(base + path, method=method, data=data, headers={
        "Authorization": f"Bearer {token}",
        PROTOCOL_HEADER: PROTOCOL_MAJOR,
        "Content-Type": "application/json",
        **(headers or {}),
    })
    try:
        with _OPENER.open(req, timeout=CONTROL_TIMEOUT_SEC) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:500].decode("utf-8", errors="replace")
        code, message = f"http_{exc.code}", detail
        try:
            problem = json.loads(detail)
            code, message = str(problem.get("code") or code), str(problem.get("message") or message)
        except ValueError:
            pass
        raise ClaudexorModelError(code, message) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ClaudexorUnavailable(f"Claudexor control {method} {path.split('?')[0]} failed: {exc}") from exc
    if want_bytes:
        return payload
    return json.loads(payload) if payload else None


def _prepare_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Send copy in canonical chat shape: only standard keys, no cache markers."""
    prepared: List[Dict[str, Any]] = []
    for message in messages:
        clean = {key: copy.deepcopy(message[key]) for key in _MESSAGE_KEYS if key in message}
        content = clean.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
        if clean.get("role") == "assistant" and not clean.get("content") and not clean.get("tool_calls"):
            clean["content"] = ""
        prepared.append(clean)
    return prepared


def _build_request(messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]],
                   model: str, reasoning_effort: str) -> Dict[str, Any]:
    profile = os.environ.get("VELES_CLAUDEXOR_PROFILE", "").strip()
    options: Dict[str, Any] = {}
    effort = str(reasoning_effort or "").strip().lower()
    if effort in _ALLOWED_EFFORTS:
        options["reasoningEffort"] = effort
    return {
        "source": SOURCE_ID,
        "model": model,
        "account": {"mode": "pin", "profileId": profile} if profile else {"mode": "auto"},
        "messages": _prepare_messages(messages),
        "tools": copy.deepcopy(tools or []),
        "toolChoice": "auto",
        "options": options,
    }


def _upload(base: str, token: str, request: Dict[str, Any], key: str) -> Dict[str, Any]:
    data = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    created = _request(base, token, "POST", "/v2/uploads", body={
        "purpose": "model", "kind": "file", "mime": "application/json",
        "name": f"model-request-{digest[len('sha256:'):]}.json", "sizeBytes": len(data),
    }, headers={"Idempotency-Key": f"veles-upload-{key}"})
    upload_id = created["uploadId"]
    _request(base, token, "PUT", f"/v2/uploads/{upload_id}/bytes", raw=data,
             headers={"Content-Type": "application/octet-stream"})
    resource = _request(base, token, "POST", f"/v2/uploads/{upload_id}/finalize",
                        body={"expectedSha256": digest}, headers={"Idempotency-Key": f"veles-finalize-{key}"})
    if resource.get("sha256") != digest or resource.get("sizeBytes") != len(data):
        raise ClaudexorModelError("model_payload_integrity_error", "finalized upload differs from request bytes")
    return {name: resource[name] for name in ("resourceId", "sha256", "sizeBytes")}


def _await_operation(base: str, token: str, operation: Dict[str, Any]) -> Dict[str, Any]:
    timeout = float(os.environ.get("VELES_CLAUDEXOR_TIMEOUT_SEC", DEFAULT_OPERATION_TIMEOUT_SEC))
    operation_id = operation["id"]
    deadline = time.monotonic() + timeout
    while operation.get("state") in ("queued", "running"):
        if time.monotonic() > deadline:
            try:
                _request(base, token, "POST", f"/v2/model-operations/{operation_id}/control",
                         body={"action": "cancel", "reasonCode": "caller_timeout"})
            except (ClaudexorUnavailable, ClaudexorModelError):
                log.warning("Claudexor cancel failed for %s", operation_id, exc_info=True)
            raise ClaudexorModelError("caller_timeout", f"no terminal state after {timeout:.0f}s", operation_id)
        time.sleep(POLL_INTERVAL_SEC)
        operation = _request(base, token, "GET", f"/v2/model-operations/{operation_id}")
    return operation


def _read_result(base: str, token: str, operation: Dict[str, Any]) -> Dict[str, Any]:
    operation_id = operation["id"]
    response = operation.get("response") or {}
    if response.get("state") != "ready":
        problem = operation.get("problem") or {}
        raise ClaudexorModelError(str(problem.get("code") or operation.get("state") or "unknown"),
                                  str(problem.get("message") or "operation ended without a response"), operation_id)
    ref = response["ref"]
    raw = _request(base, token, "GET", f"/v2/model-operations/{operation_id}/result", want_bytes=True)
    if len(raw) != ref["sizeBytes"] or "sha256:" + hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise ClaudexorModelError("model_payload_integrity_error", "result does not match its digest", operation_id)
    result = json.loads(raw.decode("utf-8"))
    try:
        _request(base, token, "POST", f"/v2/model-operations/{operation_id}/ack", body={"sha256": ref["sha256"]})
    except (ClaudexorUnavailable, ClaudexorModelError):
        # The result is already ours; an unacknowledged one expires on the daemon side.
        log.warning("Claudexor ack failed for %s", operation_id, exc_info=True)
    return result


def _to_chat_message(result: Dict[str, Any], operation_id: str) -> Dict[str, Any]:
    if result.get("outcome") == "failed":
        problem = result.get("problem") or {}
        raise ClaudexorModelError(str(problem.get("code") or "failed"), str(problem.get("message") or ""), operation_id)
    message = result.get("message")
    if not isinstance(message, dict):
        raise ClaudexorModelError("response_rejected", "terminal response contained no model message", operation_id)
    msg: Dict[str, Any] = {"role": "assistant", "content": message.get("content")}
    if message.get("tool_calls"):
        msg["tool_calls"] = message["tool_calls"]
    if result.get("outcome") == "incomplete":
        log.warning("Claudexor operation %s returned an incomplete response", operation_id)
    return msg


def _to_usage(result: Dict[str, Any], model: str) -> Dict[str, Any]:
    counters = result.get("usage") or {}
    prompt_tokens = int(counters.get("input_tokens") or 0)
    completion_tokens = int(counters.get("output_tokens") or 0)
    cached_tokens = int(counters.get("cached_input_tokens") or 0)

    from ouroboros.pricing import estimate_cost
    shadow_cost = estimate_cost(model=model, prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens, cached_tokens=cached_tokens)
    route = result.get("route") or {}
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cost": 0.0,  # Subscription via Claudexor
        "shadow_cost": round(shadow_cost, 6),
        "transport": "claudexor",
        "claudexor_profile": route.get("credentialProfileId", ""),
    }


def call_codex_claudexor(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    model: str = "gpt-6.1-sol",
    reasoning_effort: str = "medium",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """One Codex generation through Claudexor. Same contract as codex_proxy.call_codex()."""
    base, token = _discover()
    key = uuid.uuid4().hex
    request = _build_request(messages, tools, model, reasoning_effort)
    ref = _upload(base, token, request, key)
    operation = _request(base, token, "POST", "/v2/model-operations", body={"request": ref},
                         headers={"Idempotency-Key": f"veles-op-{key}"})
    operation = _await_operation(base, token, operation)
    result = _read_result(base, token, operation)
    return _to_chat_message(result, operation["id"]), _to_usage(result, model)


def call_codex_selected(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    model: str = "gpt-6.1-sol",
    reasoning_effort: str = "medium",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Codex call on the transport chosen by VELES_CODEX_TRANSPORT.

    With claudexor selected, an unreachable daemon falls back to the legacy proxy;
    model errors from a reachable daemon are raised as-is.
    """
    from ouroboros.codex_proxy import call_codex

    if selected_codex_transport() == "claudexor":
        try:
            return call_codex_claudexor(messages, tools=tools, model=model, reasoning_effort=reasoning_effort)
        except ClaudexorUnavailable as exc:
            log.warning("Claudexor unavailable, falling back to codex proxy: %s", exc)
    return call_codex(messages, tools=tools, model=model, reasoning_effort=reasoning_effort)
