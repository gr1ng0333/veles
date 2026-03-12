import json
import pathlib
import subprocess

import pytest

from ouroboros.tools.project_bootstrap import (
    _normalize_project_name,
    _project_commit,
    _project_file_read,
    _project_file_write,
    _project_github_create,
    _project_init,
    _project_push,
    _project_server_register,
    _project_server_list,
    _project_server_run,
    _project_status,
)
from ouroboros.tools.project_github_dev import (
    _project_branch_checkout,
    _project_pr_create,
    _project_pr_get,
    _project_pr_list,
)
from ouroboros.tools.registry import ToolContext, ToolRegistry


def _ctx(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path)


@pytest.fixture(autouse=True)
def _projects_root_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VELES_PROJECTS_ROOT", str(tmp_path / "projects"))


def test_project_bootstrap_tool_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_init" in names


def test_normalize_project_name_slugifies_and_rejects_empty():
    assert _normalize_project_name("My Cool App") == "my-cool-app"
    with pytest.raises(ValueError):
        _normalize_project_name("")


def test_project_init_python_creates_repo_and_first_commit(tmp_path):
    payload = json.loads(
        _project_init(
            _ctx(tmp_path),
            name="Demo API",
            language="python",
            description="Small python service",
        )
    )
    repo_path = pathlib.Path(payload["repo"]["path"])
    assert payload["status"] == "ok"
    assert payload["project"]["name"] == "demo-api"
    assert payload["project"]["language"] == "python"
    assert payload["repo"]["branch"] == "main"
    assert (repo_path / ".git").exists()
    assert (repo_path / "README.md").exists()
    assert (repo_path / "requirements.txt").exists()
    assert (repo_path / "src" / "demo_api" / "main.py").exists()
    assert payload["commit_message"] == "Bootstrap demo-api"


def test_project_init_node_template_contains_package_json(tmp_path):
    payload = json.loads(_project_init(_ctx(tmp_path), name="demo-node", language="node"))
    repo_path = pathlib.Path(payload["repo"]["path"])
    package_json = json.loads((repo_path / "package.json").read_text(encoding="utf-8"))
    assert package_json["name"] == "demo-node"
    assert package_json["scripts"]["start"] == "node src/index.js"
    assert (repo_path / "src" / "index.js").exists()


def test_project_init_refuses_existing_project(tmp_path):
    _project_init(_ctx(tmp_path), name="demo-static", language="static")
    with pytest.raises(ValueError):
        _project_init(_ctx(tmp_path), name="demo-static", language="static")


def test_project_github_create_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_github_create" in names


def test_project_file_write_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_file_write" in names


def test_project_file_read_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_file_read" in names


def test_project_commit_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_commit" in names


def test_project_push_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_push" in names


def test_project_server_register_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_server_register" in names


def test_project_server_run_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_server_run" in names


def test_project_server_list_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_server_list" in names


def test_project_status_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_status" in names


def test_project_file_read_reads_utf8_content_inside_local_project(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_file_write(
        _ctx(tmp_path),
        name="demo-api",
        path="src/demo_api/config.json",
        content='{"port": 8080}\n',
    )

    payload = json.loads(
        _project_file_read(
            _ctx(tmp_path),
            name="demo-api",
            path="src/demo_api/config.json",
        )
    )

    assert payload["status"] == "ok"
    assert payload["file"]["path"] == "src/demo_api/config.json"
    assert payload["file"]["truncated"] is False
    assert payload["content"] == '{"port": 8080}\n'


def test_project_file_read_truncates_when_max_chars_is_small(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_file_write(
        _ctx(tmp_path),
        name="demo-api",
        path="README.md",
        content="abcdefghij",
    )

    payload = json.loads(
        _project_file_read(
            _ctx(tmp_path),
            name="demo-api",
            path="README.md",
            max_chars=5,
        )
    )

    assert payload["status"] == "ok"
    assert payload["file"]["truncated"] is True
    assert payload["file"]["max_chars"] == 5
    assert payload["content"] == "abcde"


def test_project_file_read_truncates_with_marker_for_normal_limits(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_file_write(
        _ctx(tmp_path),
        name="demo-api",
        path="README.md",
        content="abcdefghijklmnopqrstuvwxyz",
    )

    payload = json.loads(
        _project_file_read(
            _ctx(tmp_path),
            name="demo-api",
            path="README.md",
            max_chars=20,
        )
    )

    assert payload["status"] == "ok"
    assert payload["file"]["truncated"] is True
    assert len(payload["content"]) == 20
    assert "...(truncated)..." in payload["content"]
    assert payload["content"].startswith("a")
    assert payload["content"].endswith("yz")


def test_project_file_read_rejects_missing_project_file(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    with pytest.raises(ValueError):
        _project_file_read(_ctx(tmp_path), name="demo-api", path="missing.txt")


def test_project_file_write_updates_file_inside_local_project(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    payload = json.loads(
        _project_file_write(
            _ctx(tmp_path),
            name="demo-api",
            path="src/demo_api/config.json",
            content='{"port": 8080}\n',
        )
    )
    repo_path = pathlib.Path(payload["project"]["path"])
    assert payload["status"] == "ok"
    assert payload["file"]["path"] == "src/demo_api/config.json"
    assert (repo_path / "src" / "demo_api" / "config.json").read_text(encoding="utf-8") == '{"port": 8080}\n'


def test_project_file_write_rejects_missing_project(tmp_path):
    with pytest.raises(ValueError):
        _project_file_write(_ctx(tmp_path), name="missing", path="README.md", content="hello")


def test_project_file_write_rejects_path_escape(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    with pytest.raises(ValueError):
        _project_file_write(_ctx(tmp_path), name="demo-api", path="../escape.txt", content="nope")


def test_project_github_create_attaches_origin_and_reports_slug(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")

    def fake_run_gh(args, cwd, timeout=120):
        assert args[:2] == ["repo", "create"]
        assert "--source" in args
        assert "--remote" in args
        subprocess.run(["git", "remote", "add", "origin", "git@github.com:veles/demo-api.git"], cwd=cwd, check=True)
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="created\n", stderr="")

    monkeypatch.setattr("ouroboros.tools.project_bootstrap._run_gh", fake_run_gh)

    payload = json.loads(
        _project_github_create(
            _ctx(tmp_path),
            name="demo-api",
            owner="veles",
            private=False,
            description="Demo repo",
        )
    )
    repo_path = pathlib.Path(payload["project"]["path"])
    assert payload["status"] == "ok"
    assert payload["github"]["slug"] == "veles/demo-api"
    assert payload["github"]["private"] is False
    assert payload["github"]["remote"] == "git@github.com:veles/demo-api.git"
    remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo_path, check=True, capture_output=True, text=True)
    assert remote.stdout.strip() == "git@github.com:veles/demo-api.git"


def test_project_github_create_refuses_when_origin_already_exists(tmp_path):
    _project_init(_ctx(tmp_path), name="demo-static", language="static")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-static"
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:veles/demo-static.git"], cwd=repo_dir, check=True)

    with pytest.raises(ValueError):
        _project_github_create(_ctx(tmp_path), name="demo-static")


def test_project_github_create_requires_existing_local_project(tmp_path):
    with pytest.raises(ValueError):
        _project_github_create(_ctx(tmp_path), name="missing-project")


def test_project_branch_checkout_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_branch_checkout" in names


def test_project_branch_checkout_creates_and_switches_new_branch(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")

    payload = json.loads(
        _project_branch_checkout(
            _ctx(tmp_path),
            name="demo-api",
            branch="feature/auth",
        )
    )

    assert payload['status'] == 'ok'
    assert payload['branch']['action'] == 'created'
    assert payload['branch']['created'] is True
    assert payload['branch']['switched'] is True
    assert payload['branch']['previous'] == 'main'
    assert payload['branch']['current'] == 'feature/auth'
    assert payload['branch']['base'] == 'main'
    assert payload['repo']['branch'] == 'feature/auth'


def test_project_branch_checkout_switches_existing_branch_when_clean(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "checkout", "-b", "feature/auth"], cwd=repo_dir, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo_dir, check=True)

    payload = json.loads(
        _project_branch_checkout(
            _ctx(tmp_path),
            name="demo-api",
            branch="feature/auth",
            create=False,
        )
    )

    assert payload['status'] == 'ok'
    assert payload['branch']['action'] == 'switched'
    assert payload['branch']['created'] is False
    assert payload['branch']['switched'] is True
    assert payload['branch']['previous'] == 'main'
    assert payload['branch']['current'] == 'feature/auth'


def test_project_branch_checkout_refuses_switch_with_dirty_working_tree(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "checkout", "-b", "feature/auth"], cwd=repo_dir, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo_dir, check=True)
    (repo_dir / "README.md").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(ValueError, match='working tree must be clean'):
        _project_branch_checkout(
            _ctx(tmp_path),
            name="demo-api",
            branch="feature/auth",
            create=False,
        )


def test_project_pr_list_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_pr_list" in names


def test_project_pr_get_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_pr_get" in names


def test_project_pr_create_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_pr_create" in names


def test_project_pr_create_uses_current_branch_and_reports_url(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/demo-api.git"], cwd=repo_dir, check=True)
    subprocess.run(["git", "checkout", "-b", "feature/auth"], cwd=repo_dir, check=True)

    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout, "input": input_data})
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="https://github.com/acme/demo-api/pull/7\n", stderr="")

    from ouroboros.tools.project_github_dev import _git as real_git

    def fake_git(args, cwd, timeout=20):
        if args[:3] == ["ls-remote", "--heads", "origin"]:
            return subprocess.CompletedProcess(["git", *args], 0, stdout="abc	refs/heads/feature/auth\n", stderr="")
        return real_git(args, cwd, timeout)

    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)
    monkeypatch.setattr('ouroboros.tools.project_github_dev._git', fake_git)

    payload = json.loads(_project_pr_create(_ctx(tmp_path), name="demo-api", title="Add auth"))

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['pull_request']['base'] == 'main'
    assert payload['github']['pull_request']['head'] == 'feature/auth'
    assert payload['github']['pull_request']['url'] == 'https://github.com/acme/demo-api/pull/7'
    assert calls[0]['args'][:2] == ['pr', 'create']
    assert '--base=main' in calls[0]['args']
    assert '--head=feature/auth' in calls[0]['args']
    assert calls[0]['input'] is None


def test_project_pr_create_requires_pushed_head_branch(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/demo-api.git"], cwd=repo_dir, check=True)

    with pytest.raises(ValueError, match='head branch is not pushed to origin'):
        _project_pr_create(_ctx(tmp_path), name="demo-api", title="Add auth")


def test_project_pr_list_reads_remote_prs(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/demo-api.git"], cwd=repo_dir, check=True)

    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout, "input": input_data})
        return subprocess.CompletedProcess(
            ["gh", *args],
            0,
            stdout=json.dumps([
                {
                    "number": 7,
                    "title": "Add auth",
                    "state": "OPEN",
                    "headRefName": "feature/auth",
                    "baseRefName": "main",
                    "url": "https://github.com/acme/demo-api/pull/7",
                    "isDraft": False,
                    "author": {"login": "veles"},
                }
            ]),
            stderr="",
        )

    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)

    payload = json.loads(_project_pr_list(_ctx(tmp_path), name="demo-api", state="open", limit=5))

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['state'] == 'open'
    assert payload['github']['limit'] == 5
    assert payload['github']['pull_requests'][0]['number'] == 7
    assert calls[0]['args'][:2] == ['pr', 'list']
    assert '--state' in calls[0]['args']
    assert '--limit' in calls[0]['args']
    assert '--json' in calls[0]['args']


def test_project_pr_get_reads_one_remote_pr(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/demo-api.git"], cwd=repo_dir, check=True)

    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout, "input": input_data})
        return subprocess.CompletedProcess(
            ["gh", *args],
            0,
            stdout=json.dumps({
                "number": 8,
                "title": "Add auth",
                "body": "Detailed body",
                "state": "OPEN",
                "headRefName": "feature/auth",
                "baseRefName": "main",
                "url": "https://github.com/acme/demo-api/pull/8",
                "isDraft": False,
                "author": {"login": "veles"},
                "commits": [{"oid": "abc"}],
                "comments": [{"id": "note-1"}],
            }),
            stderr="",
        )

    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)

    payload = json.loads(_project_pr_get(_ctx(tmp_path), name="demo-api", number=8))

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['pull_request']['number'] == 8
    assert payload['github']['pull_request']['body'] == 'Detailed body'
    assert payload['github']['pull_request']['commits'][0]['oid'] == 'abc'
    assert calls[0]['args'][:3] == ['pr', 'view', '8']
    assert '--json' in calls[0]['args']


def test_project_pr_create_passes_body_via_stdin(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/demo-api.git"], cwd=repo_dir, check=True)
    subprocess.run(["git", "checkout", "-b", "feature/body"], cwd=repo_dir, check=True)

    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout, "input": input_data})
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="https://github.com/acme/demo-api/pull/8\n", stderr="")

    from ouroboros.tools.project_github_dev import _git as real_git

    def fake_git(args, cwd, timeout=20):
        if args[:3] == ["ls-remote", "--heads", "origin"]:
            return subprocess.CompletedProcess(["git", *args], 0, stdout="abc\trefs/heads/feature/body\n", stderr="")
        return real_git(args, cwd, timeout)

    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)
    monkeypatch.setattr('ouroboros.tools.project_github_dev._git', fake_git)

    payload = json.loads(
        _project_pr_create(
            _ctx(tmp_path),
            name="demo-api",
            title="Add auth",
            body="Detailed PR body",
            base="main",
            head="feature/body",
        )
    )

    assert payload['status'] == 'ok'
    assert payload['github']['pull_request']['body_provided'] is True
    assert payload['github']['pull_request']['url'] == 'https://github.com/acme/demo-api/pull/8'
    assert '--body-file=-' in calls[0]['args']
    assert calls[0]['input'] == 'Detailed PR body'




def test_project_commit_creates_new_git_commit_for_local_project(tmp_path):
    init_payload = json.loads(_project_init(_ctx(tmp_path), name="Demo API", language="python"))
    before_sha = init_payload["repo"]["sha"]
    _project_file_write(
        _ctx(tmp_path),
        name="demo-api",
        path="src/demo_api/config.json",
        content='{"port": 8080}\n',
    )

    payload = json.loads(_project_commit(_ctx(tmp_path), name="demo-api", message="Add config"))
    repo_path = pathlib.Path(payload["project"]["path"])
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, check=True, capture_output=True, text=True).stdout.strip()
    log_subject = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=repo_path, check=True, capture_output=True, text=True).stdout.strip()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo_path, check=True, capture_output=True, text=True).stdout.strip()

    assert payload["status"] == "ok"
    assert payload["commit_message"] == "Add config"
    assert payload["changes"]["count"] == 1
    assert payload["changes"]["paths"] == ["src/demo_api/config.json"]
    assert payload["repo"]["sha"] != before_sha
    assert payload["repo"]["sha"] == head
    assert log_subject == "Add config"
    assert status == ""


def test_project_commit_rejects_clean_repo(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    with pytest.raises(ValueError):
        _project_commit(_ctx(tmp_path), name="demo-api", message="Nothing changed")


def test_project_push_pushes_current_branch_to_origin(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_file_write(
        _ctx(tmp_path),
        name="demo-api",
        path="src/demo_api/config.json",
        content='{"port": 8080}\n',
    )
    _project_commit(_ctx(tmp_path), name="demo-api", message="Add config")

    projects_root = pathlib.Path(_ctx(tmp_path).drive_root) / "projects"
    repo_dir = projects_root / "demo-api"
    remote_dir = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote_dir)], cwd=repo_dir, check=True)

    payload = json.loads(_project_push(_ctx(tmp_path), name="demo-api"))
    remote_head = subprocess.run(
        ["git", f"--git-dir={remote_dir}", "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert payload["status"] == "ok"
    assert payload["push"]["remote"] == "origin"
    assert payload["push"]["branch"] == "main"
    assert payload["push"]["remote_url"] == str(remote_dir)
    assert payload["repo"]["sha"] == remote_head


def test_project_push_rejects_missing_origin(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    with pytest.raises(ValueError):
        _project_push(_ctx(tmp_path), name="demo-api")


def test_project_status_reports_clean_repo_and_latest_commit(tmp_path):
    init_payload = json.loads(_project_init(_ctx(tmp_path), name="Demo API", language="python"))

    payload = json.loads(_project_status(_ctx(tmp_path), name="demo-api"))

    assert payload["status"] == "ok"
    assert payload["project"]["name"] == "demo-api"
    assert payload["repo"]["branch"] == "main"
    assert payload["repo"]["sha"] == init_payload["repo"]["sha"]
    assert payload["working_tree"]["clean"] is True
    assert payload["working_tree"]["counts"] == {"total": 0, "staged": 0, "unstaged": 0, "untracked": 0}
    assert payload["working_tree"]["changes"] == []
    assert payload["latest_commit"]["sha"] == init_payload["repo"]["sha"]
    assert payload["latest_commit"]["subject"] == "Bootstrap demo-api"
    assert payload["remotes"] == []


def test_project_status_reports_untracked_and_remote_entries(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_file_write(
        _ctx(tmp_path),
        name="demo-api",
        path="notes/todo.txt",
        content="ship it\n",
    )

    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    remote_dir = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote_dir)], cwd=repo_dir, check=True)

    payload = json.loads(_project_status(_ctx(tmp_path), name="demo-api"))

    assert payload["working_tree"]["clean"] is False
    assert payload["working_tree"]["counts"]["total"] == 1
    assert payload["working_tree"]["counts"]["untracked"] == 1
    assert payload["working_tree"]["counts"]["staged"] == 0
    assert payload["working_tree"]["counts"]["unstaged"] == 0
    assert payload["working_tree"]["changes"] == [
        {"path": "notes/todo.txt", "index": "?", "worktree": "?", "kind": "untracked"}
    ]
    assert payload["remotes"] == [
        {"name": "origin", "url": str(remote_dir), "direction": "fetch"},
        {"name": "origin", "url": str(remote_dir), "direction": "push"},
    ]


def test_project_server_register_persists_validated_server_metadata(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")

    payload = json.loads(
        _project_server_register(
            _ctx(tmp_path),
            name="demo-api",
            alias="prod",
            host="203.0.113.10",
            user="deploy",
            ssh_key_path="~/.ssh/demo_prod",
            deploy_path="/srv/demo-api",
            port=2222,
            label="Production",
        )
    )

    repo_dir = pathlib.Path(payload["project"]["path"])
    registry_path = repo_dir / ".veles" / "servers.json"
    saved = json.loads(registry_path.read_text(encoding="utf-8"))

    assert payload["status"] == "ok"
    assert payload["server"]["alias"] == "prod"
    assert payload["server"]["host"] == "203.0.113.10"
    assert payload["server"]["user"] == "deploy"
    assert payload["server"]["port"] == 2222
    assert payload["server"]["deploy_path"] == "/srv/demo-api"
    assert payload["server"]["ssh_key_path"].endswith("/.ssh/demo_prod")
    assert payload["registry"]["count"] == 1
    assert payload["registry"]["aliases"] == ["prod"]
    assert saved[0]["alias"] == "prod"
    assert saved[0]["label"] == "Production"


def test_project_server_register_updates_existing_alias_instead_of_duplicating(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_server_register(
        _ctx(tmp_path),
        name="demo-api",
        alias="prod",
        host="203.0.113.10",
        user="deploy",
        ssh_key_path="/home/veles/.ssh/demo_prod",
        deploy_path="/srv/demo-api",
    )

    payload = json.loads(
        _project_server_register(
            _ctx(tmp_path),
            name="demo-api",
            alias="prod",
            host="demo.example.com",
            user="root",
            ssh_key_path="/home/veles/.ssh/demo_root",
            deploy_path="/opt/demo",
            label="Primary",
        )
    )

    repo_dir = pathlib.Path(payload["project"]["path"])
    saved = json.loads((repo_dir / ".veles" / "servers.json").read_text(encoding="utf-8"))

    assert payload["registry"]["count"] == 1
    assert payload["server"]["host"] == "demo.example.com"
    assert payload["server"]["user"] == "root"
    assert payload["server"]["deploy_path"] == "/opt/demo"
    assert saved == [saved[0]]
    assert saved[0]["alias"] == "prod"
    assert saved[0]["host"] == "demo.example.com"
    assert saved[0]["label"] == "Primary"


def test_project_server_register_rejects_relative_deploy_path(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    with pytest.raises(ValueError):
        _project_server_register(
            _ctx(tmp_path),
            name="demo-api",
            alias="prod",
            host="demo.example.com",
            user="deploy",
            ssh_key_path="/home/veles/.ssh/demo",
            deploy_path="srv/demo-api",
        )



def test_project_server_list_reports_empty_registry_when_missing(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")

    payload = json.loads(_project_server_list(_ctx(tmp_path), name='demo-api'))

    assert payload['status'] == 'ok'
    assert payload['registry']['count'] == 0
    assert payload['registry']['aliases'] == []
    assert payload['registry']['exists'] is False
    assert payload['servers'] == []



def test_project_server_list_returns_public_sorted_server_views(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='prod',
        host='prod.example.com',
        user='deploy',
        ssh_key_path='/home/veles/.ssh/prod',
        deploy_path='/srv/demo-api',
        label='Production',
    )
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='staging',
        host='staging.example.com',
        user='ubuntu',
        ssh_key_path='/home/veles/.ssh/staging',
        deploy_path='/srv/demo-api-staging',
        port=2222,
    )

    payload = json.loads(_project_server_list(_ctx(tmp_path), name='demo-api'))

    assert payload['status'] == 'ok'
    assert payload['registry']['count'] == 2
    assert payload['registry']['aliases'] == ['prod', 'staging']
    assert payload['registry']['exists'] is True
    assert [item['alias'] for item in payload['servers']] == ['prod', 'staging']
    assert payload['servers'][0] == {
        'alias': 'prod',
        'label': 'Production',
        'host': 'prod.example.com',
        'port': 22,
        'user': 'deploy',
        'auth': 'ssh_key_path',
        'ssh_key_path': '/home/veles/.ssh/prod',
        'deploy_path': '/srv/demo-api',
        'created_at': payload['servers'][0]['created_at'],
        'updated_at': payload['servers'][0]['updated_at'],
    }
    assert payload['servers'][1]['alias'] == 'staging'
    assert payload['servers'][1]['port'] == 2222
    assert payload['servers'][1]['label'] == ''


def test_project_server_run_executes_command_via_registered_alias(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_server_register(
        _ctx(tmp_path),
        name="demo-api",
        alias="prod",
        host="demo.example.com",
        user="deploy",
        ssh_key_path="/home/veles/.ssh/demo",
        deploy_path="/srv/demo-api",
        port=2222,
    )

    seen = {}

    def fake_run_ssh(args, timeout):
        seen['args'] = args
        seen['timeout'] = timeout
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout='ok\n', stderr='')

    monkeypatch.setattr('ouroboros.tools.project_bootstrap._run_ssh', fake_run_ssh)

    payload = json.loads(
        _project_server_run(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            command='uname -a',
            timeout=45,
        )
    )

    assert payload['status'] == 'ok'
    assert payload['server']['alias'] == 'prod'
    assert payload['command']['raw'] == 'uname -a'
    assert payload['command']['timeout_seconds'] == 45
    assert payload['result']['ok'] is True
    assert payload['result']['exit_code'] == 0
    assert payload['result']['stdout'] == 'ok\n'
    assert payload['result']['stderr'] == ''
    assert payload['result']['output'] == 'ok\n'
    assert payload['result']['truncated'] is False
    assert seen['timeout'] == 45
    assert seen['args'][:10] == [
        '-i', '/home/veles/.ssh/demo',
        '-p', '2222',
        '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', 'IdentitiesOnly=yes',
    ]
    assert seen['args'][10:] == ['deploy@demo.example.com', '--', 'uname -a']


def test_project_server_run_reports_nonzero_exit_and_clips_output(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_server_register(
        _ctx(tmp_path),
        name="demo-api",
        alias="prod",
        host="demo.example.com",
        user="deploy",
        ssh_key_path="/home/veles/.ssh/demo",
        deploy_path="/srv/demo-api",
    )

    def fake_run_ssh(args, timeout):
        return subprocess.CompletedProcess(['ssh', *args], 17, stdout='abcdef', stderr='ghijkl')

    monkeypatch.setattr('ouroboros.tools.project_bootstrap._run_ssh', fake_run_ssh)

    payload = json.loads(
        _project_server_run(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            command='failing-command',
            max_output_chars=8,
        )
    )

    assert payload['status'] == 'error'
    assert payload['result']['ok'] is False
    assert payload['result']['exit_code'] == 17
    assert payload['result']['stdout'] == 'abcdef'
    assert payload['result']['stderr'] == 'ghijkl'
    assert payload['result']['output'] == 'abcdefgh'
    assert payload['result']['truncated'] is True
    assert payload['result']['max_output_chars'] == 8
    assert len(payload['result']['output']) <= len('abcdefghijkl')


def test_project_server_run_rejects_unknown_alias(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    with pytest.raises(ValueError):
        _project_server_run(_ctx(tmp_path), name='demo-api', alias='missing', command='pwd')
