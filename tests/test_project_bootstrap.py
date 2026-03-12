import json
import pathlib
import subprocess

import pytest

from ouroboros.tools.project_bootstrap import (
    _normalize_project_name,
    _project_github_create,
    _project_init,
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
