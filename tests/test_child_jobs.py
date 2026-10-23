"""Tests for the child (ocbot) job tools and child_watch, against a fake job API."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from ouroboros import child_api
from ouroboros.tools import child_jobs
from supervisor.child_watch import ChildWatch, WAKE_TYPES

TOKEN = "child-token"


def git(*args, cwd=None):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class FakeOcbot:
    """Enough of /api/v1 for the tools: exchanges, jobs, close, events."""

    def __init__(self, exchange_dir: Path):
        self.exchange_dir = exchange_dir
        self.jobs = {}
        self.events = []
        self.requests = []

    def job(self, num, **kw):
        base = {"job_id": f"job_{num:012x}", "num": num, "origin": "veles", "title": "T", "project": "proj",
                "state": "queued", "reason": "", "stopped_by": "", "branch": f"job/J{num}", "base_ref": "",
                "verify_cmd": "", "verify": {}, "legs_used": 0, "max_legs": 4, "followups": 0, "source": {},
                "leg": {}, "parent_ref": ""}
        base.update(kw)
        return base

    def handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def route(self, method):
                url = urlparse(self.path)
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
                fake.requests.append((method, url.path, body, dict(self.headers)))
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self.send(401, {"error": {"code": "unauthorized", "message": "no"}})
                path = url.path.removeprefix("/api/v1")
                if method == "POST" and path == "/exchanges":
                    repo = fake.exchange_dir / f"{body['name']}.git"
                    if not repo.exists():
                        git("init", "-q", "--bare", "-b", "main", str(repo))
                    return self.send(201, {"name": body["name"], "path": str(repo), "created": True})
                if method == "POST" and path == "/jobs":
                    num = len(fake.jobs) + 1
                    exchange = fake.exchange_dir / f"{body['source']['exchange']}.git"
                    base = git("rev-parse", f"refs/heads/{body['source']['base']}", cwd=exchange)
                    job = fake.job(num, title=body.get("title") or "T", project=body["source"]["exchange"],
                                   source=body["source"], base_ref=base, verify_cmd=body.get("verify_cmd", ""),
                                   parent_ref=body.get("parent_ref", ""))
                    fake.jobs[job["job_id"]] = job
                    return self.send(201, {"job": job})
                if path.startswith("/jobs/"):
                    parts = path.split("/")
                    job = fake.jobs.get(parts[2]) or next((j for j in fake.jobs.values() if str(j["num"]) == parts[2]), None)
                    if not job:
                        return self.send(404, {"error": {"code": "not_found", "message": "no"}})
                    if len(parts) == 3:
                        return self.send(200, {"job": job})
                    if parts[3] == "close":
                        job.update(state="closed", resolution=body["resolution"])
                        return self.send(200, {"job": job})
                    if parts[3] == "followup":
                        if job.get("stopped_by") == "owner":
                            return self.send(409, {"error": {"code": "stopped_by_owner", "message": "owner"}})
                        job.update(state="queued", followups=job["followups"] + 1)
                        return self.send(200, {"job": job})
                    if parts[3] == "report":
                        return self.send(200, {"job": job, "report": "status: done\nok", "handoff": "", "verify": {"cmd": "x", "exit": 0, "tail": "OK"},
                                               "diff_stat": " a.py | 1 +", "commits": "abc feat"})
                if method == "GET" and path == "/events":
                    q = parse_qs(url.query)
                    after = int(q.get("after", ["0"])[0])
                    limit = int(q.get("limit", ["100"])[0])
                    rows = [e for e in fake.events if e["seq"] > after][:limit]
                    return self.send(200, {"events": rows, "next": rows[-1]["seq"] if rows else after})
                return self.send(404, {"error": {"code": "not_found", "message": path}})

            def do_GET(self):
                self.route("GET")

            def do_POST(self):
                self.route("POST")

        return H


@pytest.fixture
def ocbot(tmp_path, monkeypatch):
    exchange_dir = tmp_path / "exchange"
    exchange_dir.mkdir()
    fake = FakeOcbot(exchange_dir)
    server = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    (tmp_path / "token").write_text(TOKEN)
    monkeypatch.setenv("VELES_CHILD_API_URL", f"http://127.0.0.1:{server.server_address[1]}/api/v1")
    monkeypatch.setenv("VELES_CHILD_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setenv("VELES_CHILD_EXCHANGE_DIR", str(exchange_dir))
    monkeypatch.setenv("VELES_CHILD_VERIFY_USER", "")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    yield fake
    server.shutdown()


def make_repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git("add", ".", cwd=repo)
    git("commit", "-qm", "base", cwd=repo)
    return repo


def ctx(tmp_path):
    return SimpleNamespace(drive_root=tmp_path / "drive")


def child_commit(fake, job, content, message="feat: mul"):
    """Simulate ocbot publishing job/J<n> into the exchange."""
    import tempfile
    exchange = fake.exchange_dir / f"{job['project']}.git"
    work = Path(tempfile.mkdtemp(dir=exchange.parent)) / "w"
    git("clone", "-q", str(exchange), str(work))
    (work / "calc.py").write_text(content)
    git("commit", "-qam", message, cwd=work)
    git("push", "-q", "origin", f"+HEAD:refs/heads/{job['branch']}", cwd=work)


def test_tools_registered_with_valid_schemas():
    names = [t.name for t in child_jobs.get_tools()]
    assert names == list(child_jobs.CHILD_TOOL_NAMES)
    for tool in child_jobs.get_tools():
        assert set(tool.schema) == {"name", "description", "parameters"} and tool.schema["name"] == tool.name


def test_delegate_pushes_base_and_links_repo(tmp_path, ocbot):
    repo = make_repo(tmp_path)
    c = ctx(tmp_path)
    out = child_jobs._child_delegate(c, str(repo), "Добавь mul", title="mul", acceptance=["тест"],
                                     verify_cmd="python3 -c 'import calc'", parent_ref="plan:x/step:1")
    assert out.startswith("✅"), out
    job = next(iter(ocbot.jobs.values()))
    assert job["base_ref"] == git("rev-parse", "HEAD", cwd=repo) and job["parent_ref"] == "plan:x/step:1"
    post = [r for r in ocbot.requests if r[1] == "/api/v1/jobs"][0]
    assert post[3]["Idempotency-Key"].startswith("veles-") and post[2]["acceptance"] == ["тест"]
    links = json.loads((tmp_path / "drive/state/child_jobs.json").read_text())
    assert links[job["job_id"]] == {"repo": str(repo), "base": "main", "exchange": "proj"}
    safe = subprocess.run(["git", "config", "--global", "--get-all", "safe.directory"], capture_output=True, text=True).stdout
    assert str(ocbot.exchange_dir / "proj.git") in safe


def test_accept_verifies_merges_and_closes(tmp_path, ocbot):
    repo = make_repo(tmp_path)
    c = ctx(tmp_path)
    child_jobs._child_delegate(c, str(repo), "Добавь mul", verify_cmd="python3 -c 'import calc; assert calc.mul(2,3)==6'")
    job = next(iter(ocbot.jobs.values()))
    child_commit(ocbot, job, "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    job["state"] = "done"
    out = child_jobs._child_accept(c, job["job_id"], verify_only=True)
    assert "exit 0" in out and job["state"] == "done"
    out = child_jobs._child_accept(c, job["job_id"])
    assert out.startswith("✅"), out
    assert "def mul" in (repo / "calc.py").read_text() and job["resolution"] == "accepted"
    assert git("log", "-1", "--format=%s", cwd=repo).startswith("merge child J1")
    assert not git("worktree", "list", "--porcelain", cwd=repo).count("veles-child")


def test_accept_refuses_failed_verify_dirty_tree_and_not_done(tmp_path, ocbot):
    repo = make_repo(tmp_path)
    c = ctx(tmp_path)
    child_jobs._child_delegate(c, str(repo), "x", verify_cmd="python3 -c 'import calc; calc.mul'")
    job = next(iter(ocbot.jobs.values()))
    child_commit(ocbot, job, "def add(a, b):\n    return a - b\n")
    assert "done" in child_jobs._child_accept(c, job["job_id"])  # still queued
    job["state"] = "done"
    out = child_jobs._child_accept(c, job["job_id"])
    assert "проверка упала" in out and job["state"] == "done" and "a - b" not in (repo / "calc.py").read_text()
    child_commit(ocbot, job, "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n", "fix")
    (repo / "calc.py").write_text("dirty\n")
    out = child_jobs._child_accept(c, job["job_id"])
    assert "незакоммиченные" in out and job["state"] == "done"


def test_verify_env_has_no_secrets(tmp_path, ocbot, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
    repo = make_repo(tmp_path)
    c = ctx(tmp_path)
    child_jobs._child_delegate(c, str(repo), "x", verify_cmd="env | grep -c sk-secret; test -z \"$OPENROUTER_API_KEY\"")
    job = next(iter(ocbot.jobs.values()))
    child_commit(ocbot, job, "x = 1\n")
    job["state"] = "done"
    out = child_jobs._child_accept(c, job["job_id"], verify_only=True)
    assert "exit 0" in out


def test_followup_refused_after_owner_stop(tmp_path, ocbot):
    repo = make_repo(tmp_path)
    c = ctx(tmp_path)
    child_jobs._child_delegate(c, str(repo), "x")
    job = next(iter(ocbot.jobs.values()))
    job.update(state="stopped", stopped_by="owner")
    assert "stopped_by_owner" in child_jobs._child_followup(c, job["job_id"], "ещё")
    assert "REPORT.md" in child_jobs._child_report(c, job["job_id"])


def test_report_distinguishes_not_run_from_not_set(tmp_path, ocbot, monkeypatch):
    repo = make_repo(tmp_path)
    c = ctx(tmp_path)
    child_jobs._child_delegate(c, str(repo), "x", verify_cmd="python3 -m unittest")
    job = next(iter(ocbot.jobs.values()))

    def report_without_verify(*a, **kw):
        return {"job": job, "report": "", "handoff": "", "verify": {}, "diff_stat": "", "commits": ""}

    monkeypatch.setattr(child_jobs.child_api, "request", lambda m, p, **kw: report_without_verify())
    out = child_jobs._child_report(c, job["job_id"])
    assert "python3 -m unittest → ещё не запускалась" in out and "не задана" not in out
    job["verify_cmd"] = ""
    assert "Проверка ocbot: не задана" in child_jobs._child_report(c, job["job_id"])


def test_api_errors_are_text_not_exceptions(tmp_path, ocbot, monkeypatch):
    monkeypatch.setenv("VELES_CHILD_TOKEN_FILE", str(tmp_path / "missing"))
    assert child_jobs._child_jobs(ctx(tmp_path)).startswith("⚠️")


def event(seq, typ, origin="veles", **data):
    return {"seq": seq, "ts": 0, "type": typ, "job_id": f"job_{seq:012x}", "num": seq, "origin": origin,
            "parent_ref": "plan:p/step:1", "data": data}


def test_child_watch_cursor_wake_and_no_replay(tmp_path, ocbot):
    ocbot.events = [event(1, "job.created"), event(2, "job.done", published="abc")]
    clock = SimpleNamespace(t=1000.0)
    watch = ChildWatch(tmp_path / "drive", clock=lambda: clock.t)
    tasks, cursor, notice = watch.poll(42)
    # First run starts from the end of the feed: history is not replayed.
    assert tasks == [] and cursor is None and watch._load()["after"] == 2
    ocbot.events += [event(3, "job.leg_started"), event(4, "job.needs_attention", reason="Лимит заходов"),
                     event(5, "job.done", origin="owner"), event(6, "job.stopped", by="owner")]
    assert watch.poll(42) == ([], None, None)  # interval not elapsed
    clock.t += 11
    tasks, cursor, notice = watch.poll(42)
    assert [t["child_event_seq"] for t in tasks] == [4, 6] and cursor == 6 and notice is None
    assert tasks[0]["type"] == "task" and tasks[0]["priority"] == 0 and tasks[0]["chat_id"] == 42
    assert "[child_report]" in tasks[0]["text"] and "Лимит заходов" in tasks[0]["text"] and "child_accept" in tasks[0]["text"]
    assert "Остановил: владелец" in tasks[1]["text"]
    # Not committed yet: a crash before enqueueing re-delivers the same events.
    clock.t += 11
    assert [t["child_event_seq"] for t in watch.poll(42)[0]] == [4, 6]
    watch.commit(cursor)
    clock.t += 11
    assert watch.poll(42) == ([], None, None)
    assert WAKE_TYPES == {"job.done", "job.needs_attention", "job.stopped", "job.failed"}


def test_child_watch_outage_notice_once_and_missing_token(tmp_path, ocbot, monkeypatch):
    clock = SimpleNamespace(t=1000.0)
    calls = []

    def down(*a, **kw):
        calls.append(a)
        raise child_api.ChildApiError(0, "unreachable", "refused")

    watch = ChildWatch(tmp_path / "drive", clock=lambda: clock.t, request=down)
    notices = []
    for _ in range(80):
        clock.t += 11
        notices.append(watch.poll(42)[2])
    assert [n for n in notices if n] and sum(1 for n in notices if n) == 1
    watch.request = lambda *a, **kw: {"events": [], "next": 0}
    clock.t += 11
    assert watch.poll(42)[2] == "✅ Младший (ocbot) снова на связи."

    def no_token(*a, **kw):
        raise child_api.ChildApiError(0, "token_unavailable", "missing")

    quiet = ChildWatch(tmp_path / "drive2", clock=lambda: clock.t, request=no_token)
    for _ in range(80):
        clock.t += 11
        assert quiet.poll(42) == ([], None, None)
