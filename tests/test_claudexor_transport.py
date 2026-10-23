"""Tests for the Claudexor transport of codex/* models (ouroboros/claudexor_transport.py)."""
from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ouroboros import claudexor_transport as cx

TOKEN = "test-token"


class FakeDaemon:
    """Minimal /v2 uploads + model-operations surface with a scripted result."""

    def __init__(self, result: dict):
        self.result = result
        self.uploaded = b""
        self.requests: list[tuple[str, str]] = []
        self.acked = False
        self.polls = 0

    def handler(self):
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body=None, raw=None):
                data = raw if raw is not None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _route(self, method):
                daemon.requests.append((method, self.path))
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._send(401, {"code": "unauthorized", "message": "bad token"})
                body = self._body()
                result_bytes = json.dumps(daemon.result).encode()
                ref = {"resourceId": "res-out", "sizeBytes": len(result_bytes),
                       "sha256": "sha256:" + hashlib.sha256(result_bytes).hexdigest()}
                if method == "POST" and self.path == "/v2/uploads":
                    return self._send(201, {"uploadId": "upl-1", "state": "open"})
                if method == "PUT" and self.path == "/v2/uploads/upl-1/bytes":
                    daemon.uploaded = body
                    return self._send(200, {"state": "uploaded"})
                if method == "POST" and self.path == "/v2/uploads/upl-1/finalize":
                    return self._send(201, {"resourceId": "res-in", "sizeBytes": len(daemon.uploaded),
                                            "sha256": "sha256:" + hashlib.sha256(daemon.uploaded).hexdigest()})
                if method == "POST" and self.path == "/v2/model-operations":
                    return self._send(202, {"id": "job-1", "state": "running", "response": {"state": "absent"}})
                if method == "GET" and self.path == "/v2/model-operations/job-1":
                    daemon.polls += 1
                    return self._send(200, {"id": "job-1", "state": "succeeded",
                                            "response": {"state": "ready", "ref": ref}})
                if method == "GET" and self.path == "/v2/model-operations/job-1/result":
                    return self._send(200, raw=result_bytes)
                if method == "POST" and self.path == "/v2/model-operations/job-1/ack":
                    daemon.acked = json.loads(body)["sha256"] == ref["sha256"]
                    return self._send(200, {"id": "job-1", "state": "succeeded"})
                return self._send(404, {"code": "not_found", "message": self.path})

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_PUT(self):
                self._route("PUT")

        return Handler


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    def start(result: dict) -> FakeDaemon:
        fake = FakeDaemon(result)
        server = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        (tmp_path / "daemon").mkdir()
        (tmp_path / "token").write_text(TOKEN)
        (tmp_path / "daemon" / "control-api.json").write_text(json.dumps(
            {"host": "127.0.0.1", "port": server.server_address[1], "tokenPath": str(tmp_path / "token")}))
        monkeypatch.setenv("CLAUDEXOR_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(cx, "POLL_INTERVAL_SEC", 0.01)
        return fake

    servers: list = []
    yield start
    for server in servers:
        server.shutdown()


TOOL_RESULT = {
    "outcome": "completed",
    "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "get_time", "arguments": "{}"}}],
                "nativeContinuation": {"format": "codex.responses.v1", "payload": []}},
    "route": {"source": "codex", "credentialProfileId": "codex-default", "model": "gpt-6.1-sol"},
    "usage": {"input_tokens": 56, "output_tokens": 14, "cached_input_tokens": 8},
}


def test_default_transport_is_proxy(monkeypatch):
    monkeypatch.delenv("VELES_CODEX_TRANSPORT", raising=False)
    assert cx.selected_codex_transport() == "proxy"
    monkeypatch.setenv("VELES_CODEX_TRANSPORT", "Claudexor")
    assert cx.selected_codex_transport() == "claudexor"


def test_call_returns_chat_message_and_usage(daemon):
    fake = daemon(TOOL_RESULT)
    tools = [{"type": "function", "function": {"name": "get_time", "parameters": {"type": "object"}}}]
    msg, usage = cx.call_codex_claudexor([{"role": "user", "content": "time?"}], tools=tools,
                                         model="gpt-6.1-sol", reasoning_effort="high")
    assert msg == {"role": "assistant", "content": None, "tool_calls": TOOL_RESULT["message"]["tool_calls"]}
    assert usage["prompt_tokens"] == 56 and usage["completion_tokens"] == 14 and usage["cached_tokens"] == 8
    assert usage["cost"] == 0.0 and usage["transport"] == "claudexor"
    assert usage["claudexor_profile"] == "codex-default"
    sent = json.loads(fake.uploaded)
    assert sent["source"] == "codex" and sent["model"] == "gpt-6.1-sol"
    assert sent["account"] == {"mode": "auto"}
    assert sent["options"] == {"reasoningEffort": "high", "cacheKey": "veles"}
    assert sent["tools"] == tools
    assert fake.acked


def test_profile_pin_from_env(daemon, monkeypatch):
    fake = daemon(TOOL_RESULT)
    monkeypatch.setenv("VELES_CLAUDEXOR_PROFILE", "codex-work")
    cx.call_codex_claudexor([{"role": "user", "content": "hi"}], model="gpt-6.1-sol")
    assert json.loads(fake.uploaded)["account"] == {"mode": "pin", "profileId": "codex-work"}


def test_failed_outcome_raises_model_error(daemon):
    daemon({"outcome": "failed", "problem": {"code": "subscription_window_exhausted", "message": "resets at 18:00"}})
    with pytest.raises(cx.ClaudexorModelError) as info:
        cx.call_codex_claudexor([{"role": "user", "content": "hi"}], model="gpt-6.1-sol")
    assert info.value.code == "subscription_window_exhausted"


def test_prepare_messages_strips_host_metadata():
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}]},
        {"role": "user", "content": "hi", "_task_id": "t1"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}], "reasoning": "x"},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    prepared = cx._prepare_messages(messages)
    assert prepared[0]["content"] == [{"type": "text", "text": "rules"}]
    assert prepared[1] == {"role": "user", "content": "hi"}
    assert "reasoning" not in prepared[2]
    assert prepared[3] == {"role": "tool", "tool_call_id": "c1", "content": "ok"}
    assert "cache_control" in messages[0]["content"][0]  # caller's history untouched


def test_prepare_tools_matches_strict_daemon_shape():
    params = {"type": "object", "properties": {}}
    tools = [
        {"name": "legacy", "type": "function",
         "function": {"name": "legacy", "description": "d", "parameters": params}},
        {"type": "function", "function": {"type": "function",
                                          "function": {"name": "wrapped", "parameters": params}}},
        {"type": "function", "function": {"name": "", "parameters": params}},
        {"type": "function", "function": {"name": "no_params"}},
    ]
    assert cx._prepare_tools(tools) == [
        {"type": "function", "function": {"name": "legacy", "parameters": params, "description": "d"}},
        {"type": "function", "function": {"name": "wrapped", "parameters": params}},
    ]


def test_prepare_messages_normalizes_tool_calls():
    messages = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "index": 0, "function": {"name": "f", "arguments": {"a": 1}}}]},
        {"role": "assistant", "content": "x", "tool_calls": [], "name": None}]
    prepared = cx._prepare_messages(messages)
    assert prepared[0]["tool_calls"] == [
        {"id": "c1", "type": "function", "function": {"name": "f", "arguments": '{"a": 1}'}}]
    assert prepared[1] == {"role": "assistant", "content": "x"}


def test_unreachable_daemon_falls_back_to_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("VELES_CODEX_TRANSPORT", "claudexor")
    monkeypatch.setenv("CLAUDEXOR_CONFIG_DIR", str(tmp_path / "missing"))
    calls = {}

    def fake_call_codex(messages, tools=None, model="", reasoning_effort="medium", **_):
        calls["model"] = model
        return {"role": "assistant", "content": "proxy"}, {"prompt_tokens": 0}

    monkeypatch.setattr("ouroboros.codex_proxy.call_codex", fake_call_codex)
    msg, _ = cx.call_codex_selected([{"role": "user", "content": "hi"}], model="gpt-6.1-sol")
    assert msg["content"] == "proxy" and calls["model"] == "gpt-6.1-sol"


def test_non_loopback_descriptor_is_refused(tmp_path, monkeypatch):
    (tmp_path / "daemon").mkdir()
    (tmp_path / "token").write_text(TOKEN)
    (tmp_path / "daemon" / "control-api.json").write_text(json.dumps(
        {"host": "10.0.0.5", "port": 1234, "tokenPath": str(tmp_path / "token")}))
    monkeypatch.setenv("CLAUDEXOR_CONFIG_DIR", str(tmp_path))
    with pytest.raises(cx.ClaudexorUnavailable):
        cx._discover()
