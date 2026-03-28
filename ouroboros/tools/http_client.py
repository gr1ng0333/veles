"""HTTP client tool — lightweight REST API calls without browser or shell curl.

Growth tool: adds a native HTTP execution primitive for API interactions,
webhook testing, endpoint health checks, and JSON data retrieval.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

_DEFAULT_TIMEOUT = 30
_MAX_RESPONSE_BYTES = 512 * 1024  # 512 KB — prevent OOM on large responses
_DEFAULT_UA = "Veles-Agent/1.0"

# Methods that typically carry a request body
_BODY_METHODS = {"POST", "PUT", "PATCH"}


def _http_request(
    ctx: ToolContext,
    url: str,
    method: str = "GET",
    headers: str = "",
    body: str = "",
    content_type: str = "",
    timeout: int = _DEFAULT_TIMEOUT,
    follow_redirects: bool = True,
) -> str:
    """Execute an HTTP request and return status, headers, and body."""

    method = method.upper().strip()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        return json.dumps({"error": f"Unsupported HTTP method: {method}"})

    # Parse URL
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Build headers dict
    req_headers: Dict[str, str] = {"User-Agent": _DEFAULT_UA}
    if headers:
        try:
            parsed = json.loads(headers)
            if isinstance(parsed, dict):
                req_headers.update(parsed)
        except json.JSONDecodeError:
            # Try key: value format, one per line
            for line in headers.strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    req_headers[k.strip()] = v.strip()

    # Build body
    data: Optional[bytes] = None
    if body and method in _BODY_METHODS:
        if content_type:
            req_headers.setdefault("Content-Type", content_type)
        else:
            # Auto-detect JSON
            stripped = body.strip()
            if stripped.startswith(("{", "[")) and stripped.endswith(("}", "]")):
                req_headers.setdefault("Content-Type", "application/json")
            else:
                req_headers.setdefault("Content-Type", "text/plain; charset=utf-8")
        data = body.encode("utf-8")

    # Clamp timeout
    timeout = max(5, min(timeout, 60))

    # Execute
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)

    start_ts = time.monotonic()
    try:
        # Handle redirects
        if not follow_redirects:
            import http.client
            parsed_url = urllib.parse.urlparse(url)
            conn_cls = http.client.HTTPSConnection if parsed_url.scheme == "https" else http.client.HTTPConnection
            conn = conn_cls(parsed_url.hostname, parsed_url.port, timeout=timeout)
            try:
                path = parsed_url.path or "/"
                if parsed_url.query:
                    path += "?" + parsed_url.query
                conn.request(method, path, body=data, headers=req_headers)
                resp = conn.getresponse()
                elapsed = time.monotonic() - start_ts
                resp_body = resp.read(_MAX_RESPONSE_BYTES)
                resp_headers = dict(resp.getheaders())
                return _format_response(
                    status=resp.status,
                    reason=resp.reason,
                    headers=resp_headers,
                    body=resp_body,
                    elapsed=elapsed,
                    url=url,
                    method=method,
                )
            finally:
                conn.close()

        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            elapsed = time.monotonic() - start_ts
            resp_body = resp.read(_MAX_RESPONSE_BYTES)
            resp_headers = dict(resp.headers.items())
            return _format_response(
                status=resp.status,
                reason=resp.reason,
                headers=resp_headers,
                body=resp_body,
                elapsed=elapsed,
                url=resp.url,
                method=method,
            )

    except urllib.error.HTTPError as e:
        elapsed = time.monotonic() - start_ts
        try:
            err_body = e.read(_MAX_RESPONSE_BYTES)
        except Exception:
            err_body = b""
        resp_headers = dict(e.headers.items()) if e.headers else {}
        return _format_response(
            status=e.code,
            reason=str(e.reason),
            headers=resp_headers,
            body=err_body,
            elapsed=elapsed,
            url=url,
            method=method,
        )

    except urllib.error.URLError as e:
        elapsed = time.monotonic() - start_ts
        return json.dumps({
            "error": f"Connection failed: {e.reason}",
            "url": url,
            "method": method,
            "elapsed_sec": round(elapsed, 3),
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        elapsed = time.monotonic() - start_ts
        return json.dumps({
            "error": f"{type(e).__name__}: {e}",
            "url": url,
            "method": method,
            "elapsed_sec": round(elapsed, 3),
        }, ensure_ascii=False, indent=2)


def _format_response(
    status: int,
    reason: str,
    headers: Dict[str, str],
    body: bytes,
    elapsed: float,
    url: str,
    method: str,
) -> str:
    """Format HTTP response into a compact readable result."""
    ct = headers.get("Content-Type", headers.get("content-type", ""))

    # Try to decode body as text
    body_text: Optional[str] = None
    body_json: Any = None
    is_binary = False

    try:
        decoded = body.decode("utf-8")
        # Try parsing as JSON for pretty output
        if "json" in ct.lower() or (decoded.strip().startswith(("{", "["))):
            try:
                body_json = json.loads(decoded)
            except json.JSONDecodeError:
                body_text = decoded
        else:
            body_text = decoded
    except UnicodeDecodeError:
        is_binary = True

    result: Dict[str, Any] = {
        "status": status,
        "reason": reason,
        "method": method,
        "url": url,
        "elapsed_sec": round(elapsed, 3),
        "content_type": ct,
        "content_length": len(body),
    }

    # Include select response headers
    interesting_headers = {}
    for key in ("Location", "Set-Cookie", "X-RateLimit-Remaining",
                "X-RateLimit-Limit", "Retry-After", "ETag",
                "Cache-Control", "WWW-Authenticate", "Server"):
        # Case-insensitive lookup
        for hk, hv in headers.items():
            if hk.lower() == key.lower():
                interesting_headers[key] = hv
                break
    if interesting_headers:
        result["headers"] = interesting_headers

    if body_json is not None:
        # Truncate large JSON for readability
        json_str = json.dumps(body_json, ensure_ascii=False, indent=2)
        if len(json_str) > 8000:
            result["body_json_truncated"] = json_str[:8000] + "\n... (truncated)"
        else:
            result["body_json"] = body_json
    elif body_text is not None:
        if len(body_text) > 8000:
            result["body_text_truncated"] = body_text[:8000] + "\n... (truncated)"
        else:
            result["body_text"] = body_text
    elif is_binary:
        result["body_binary"] = f"<binary {len(body)} bytes>"

    return json.dumps(result, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="http_request",
            schema={
                "name": "http_request",
                "description": (
                    "Make an HTTP request to any URL. Supports GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS. "
                    "Use for REST API calls, webhook testing, endpoint health checks, JSON data retrieval. "
                    "Lighter than browser automation, more structured than shell curl."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "Target URL (https:// auto-prefixed if missing).",
                        },
                        "method": {
                            "type": "string",
                            "description": "HTTP method: GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS. Default: GET.",
                            "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                        },
                        "headers": {
                            "type": "string",
                            "description": 'Request headers as JSON object (e.g. {"Authorization": "Bearer xxx"}) or key: value lines.',
                        },
                        "body": {
                            "type": "string",
                            "description": "Request body (for POST/PUT/PATCH). JSON auto-detected.",
                        },
                        "content_type": {
                            "type": "string",
                            "description": "Explicit Content-Type header for body. Auto-detected if omitted.",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Timeout in seconds (5-60, default 30).",
                        },
                        "follow_redirects": {
                            "type": "boolean",
                            "description": "Follow HTTP redirects. Default: true.",
                        },
                    },
                    "required": ["url"],
                },
            },
            handler=_http_request,
            timeout_sec=60,
        ),
    ]
