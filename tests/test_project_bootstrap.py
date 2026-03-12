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
