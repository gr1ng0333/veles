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
from ouroboros.tools.project_server_info import _project_server_get, _project_server_remove
from ouroboros.tools.project_branch_info import _project_branch_delete, _project_branch_get, _project_branch_list, _project_branch_rename
from ouroboros.tools.project_remote_awareness import _project_branch_compare, _project_git_fetch
from ouroboros.tools.project_issue_update import (
    _project_issue_assign,
    _project_issue_close,
    _project_issue_label_add,
    _project_issue_label_remove,
    _project_issue_reopen,
    _project_issue_unassign,
    _project_issue_update,
)
from ouroboros.tools.project_pr_update import (
    _project_pr_close,
    _project_pr_reopen,
    _project_pr_review_list,
    _project_pr_review_submit,
)
from ouroboros.tools.project_github_dev import (
    _project_branch_checkout,
    _project_issue_comment,
    _project_issue_create,
    _project_issue_get,
    _project_issue_list,
    _project_pr_comment,
    _project_pr_create,
    _project_pr_get,
    _project_pr_list,
    _project_pr_merge,
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


def test_project_server_get_rejects_unknown_alias(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")

    with pytest.raises(ValueError, match='project server alias not found'):
        _project_server_get(_ctx(tmp_path), name='demo-api', alias='prod')


def test_project_server_remove_rejects_unknown_alias(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")

    with pytest.raises(ValueError, match='project server alias not found'):
        _project_server_remove(_ctx(tmp_path), name='demo-api', alias='prod')


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


def test_project_server_get_reads_registered_server(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='prod',
        host='example.com',
        user='deploy',
        ssh_key_path='~/id_test',
        deploy_path='/srv/demo-api',
        label='Production',
    )

    payload = json.loads(_project_server_get(_ctx(tmp_path), name='demo-api', alias='prod'))

    assert payload['status'] == 'ok'
    assert payload['server']['alias'] == 'prod'
    assert payload['server']['label'] == 'Production'
    assert payload['server']['host'] == 'example.com'
    assert payload['server']['deploy_path'] == '/srv/demo-api'
    assert payload['registry']['count'] == 1
    assert payload['registry']['aliases'] == ['prod']


def test_project_server_get_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_server_get" in names


def test_project_server_remove_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_server_remove" in names


def test_project_server_remove_deletes_registered_server(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='prod',
        host='example.com',
        user='deploy',
        ssh_key_path='~/id_test',
        deploy_path='/srv/demo-api',
        label='Production',
    )
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='staging',
        host='staging.example.com',
        user='deploy',
        ssh_key_path='~/id_test',
        deploy_path='/srv/demo-api-staging',
        label='Staging',
    )

    payload = json.loads(_project_server_remove(_ctx(tmp_path), name='demo-api', alias='prod'))

    assert payload['status'] == 'ok'
    assert payload['removed_server']['alias'] == 'prod'
    assert payload['removed_server']['label'] == 'Production'
    assert payload['registry']['count'] == 1
    assert payload['registry']['aliases'] == ['staging']


def test_project_server_remove_leaves_empty_registry_file_when_last_server_removed(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='prod',
        host='example.com',
        user='deploy',
        ssh_key_path='~/id_test',
        deploy_path='/srv/demo-api',
    )

    payload = json.loads(_project_server_remove(_ctx(tmp_path), name='demo-api', alias='prod'))

    assert payload['status'] == 'ok'
    assert payload['registry']['count'] == 0
    assert payload['registry']['aliases'] == []
    assert payload['registry']['exists'] is True


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


def test_project_branch_list_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_branch_list" in names


def test_project_branch_get_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_branch_get" in names


def test_project_branch_list_reads_local_branches_with_origin_context(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "checkout", "-b", "feature/auth"], cwd=repo_dir, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/demo-api.git"], cwd=repo_dir, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo_dir, check=True)
    subprocess.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], cwd=repo_dir, check=True)

    payload = json.loads(_project_branch_list(_ctx(tmp_path), name="demo-api"))

    assert payload['status'] == 'ok'
    assert payload['branches']['current'] == 'main'
    assert payload['branches']['default'] == 'main'
    assert payload['branches']['count'] == 2
    names = {item['name']: item for item in payload['branches']['items']}
    assert set(names.keys()) == {'main', 'feature/auth'}
    assert names['main']['current'] is True
    assert names['main']['default'] is True
    assert names['main']['remote_ref'] == 'origin/main'
    assert names['main']['ahead_behind']['available'] is True
    assert names['main']['ahead_behind']['ahead'] == 0
    assert names['main']['ahead_behind']['behind'] == 0
    assert names['feature/auth']['current'] is False
    assert names['feature/auth']['remote_ref'] == ''
    assert names['feature/auth']['ahead_behind']['available'] is False


def test_project_branch_get_defaults_to_current_branch(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/demo-api.git"], cwd=repo_dir, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo_dir, check=True)
    subprocess.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], cwd=repo_dir, check=True)

    payload = json.loads(_project_branch_get(_ctx(tmp_path), name="demo-api"))

    assert payload['status'] == 'ok'
    assert payload['branch']['name'] == 'main'
    assert payload['branch']['current'] is True
    assert payload['branch']['default'] is True
    assert payload['branch']['remote_ref'] == 'origin/main'
    assert payload['branch']['ahead_behind']['available'] is True
    assert payload['github']['origin'] == 'https://github.com/acme/demo-api.git'


def test_project_branch_get_rejects_missing_local_branch(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")

    with pytest.raises(ValueError, match='local branch not found'):
        _project_branch_get(_ctx(tmp_path), name="demo-api", branch="missing")


def test_project_branch_rename_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_branch_rename" in names


def test_project_branch_rename_renames_local_branch_and_updates_current_when_active(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "checkout", "-b", "feature/auth"], cwd=repo_dir, check=True)

    payload = json.loads(_project_branch_rename(_ctx(tmp_path), name="demo-api", branch="feature/auth", new_branch="feature/login"))

    assert payload['status'] == 'ok'
    assert payload['branch']['old_name'] == 'feature/auth'
    assert payload['branch']['name'] == 'feature/login'
    assert payload['branch']['renamed'] is True
    assert payload['branch']['current_before'] == 'feature/auth'
    assert payload['branch']['current_after'] == 'feature/login'
    refs_old = subprocess.run(["git", "branch", "--list", "feature/auth"], cwd=repo_dir, check=True, capture_output=True, text=True)
    refs_new = subprocess.run(["git", "branch", "--list", "feature/login"], cwd=repo_dir, check=True, capture_output=True, text=True)
    current = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True)
    assert refs_old.stdout.strip() == ''
    assert 'feature/login' in refs_new.stdout
    assert current.stdout.strip() == 'feature/login'


def test_project_branch_rename_updates_default_branch_metadata_when_origin_head_matches(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/demo-api.git"], cwd=repo_dir, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo_dir, check=True)
    subprocess.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], cwd=repo_dir, check=True)

    payload = json.loads(_project_branch_rename(_ctx(tmp_path), name="demo-api", branch="main", new_branch="stable"))

    assert payload['status'] == 'ok'
    assert payload['branch']['default_before'] == 'main'
    assert payload['branch']['default_after'] == 'stable'
    assert payload['branch']['current_after'] == 'stable'


def test_project_branch_rename_rejects_missing_source_branch(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")

    with pytest.raises(ValueError, match='local branch not found'):
        _project_branch_rename(_ctx(tmp_path), name="demo-api", branch="missing", new_branch="feature/login")


def test_project_git_fetch_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_git_fetch" in names


def test_project_branch_compare_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_branch_compare" in names


def test_project_git_fetch_updates_remote_awareness_after_remote_advance(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    remote_dir = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote_dir)], cwd=repo_dir, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=remote_dir, check=True)

    clone_dir = tmp_path / "remote-work"
    subprocess.run(["git", "clone", str(remote_dir), str(clone_dir)], check=True)
    subprocess.run(["git", "config", "user.name", "Remote Bot"], cwd=clone_dir, check=True)
    subprocess.run(["git", "config", "user.email", "remote@example.com"], cwd=clone_dir, check=True)
    (clone_dir / "REMOTE.txt").write_text("remote change\n", encoding="utf-8")
    subprocess.run(["git", "add", "REMOTE.txt"], cwd=clone_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Remote advance"], cwd=clone_dir, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=clone_dir, check=True)

    before = json.loads(_project_status(_ctx(tmp_path), name="demo-api"))
    assert before["remote_awareness"]["available"] is True
    assert before["remote_awareness"]["branch"] == "main"
    assert before["remote_awareness"]["ahead_behind"]["available"] is True
    assert before["remote_awareness"]["ahead_behind"]["ahead"] == 0
    assert before["remote_awareness"]["ahead_behind"]["behind"] == 0

    payload = json.loads(_project_git_fetch(_ctx(tmp_path), name="demo-api"))

    assert payload["status"] == "ok"
    assert payload["fetch"]["remote"] == "origin"
    assert payload["fetch"]["remote_url"] == str(remote_dir)
    assert payload["remote"]["before"]["ahead_behind"]["behind"] == 0
    assert payload["remote"]["after"]["ahead_behind"]["behind"] == 1
    assert payload["remote"]["after"]["compare"]["available"] is True
    assert payload["remote"]["after"]["compare"]["remote"]["unique_commits"][0]["subject"] == "Remote advance"


def test_project_branch_compare_reports_ahead_behind_and_unique_commits(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    remote_dir = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote_dir)], cwd=repo_dir, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=remote_dir, check=True)

    clone_dir = tmp_path / "remote-work"
    subprocess.run(["git", "clone", str(remote_dir), str(clone_dir)], check=True)
    subprocess.run(["git", "config", "user.name", "Remote Bot"], cwd=clone_dir, check=True)
    subprocess.run(["git", "config", "user.email", "remote@example.com"], cwd=clone_dir, check=True)
    (clone_dir / "REMOTE.txt").write_text("remote change\n", encoding="utf-8")
    subprocess.run(["git", "add", "REMOTE.txt"], cwd=clone_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Remote advance"], cwd=clone_dir, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=clone_dir, check=True)

    _project_git_fetch(_ctx(tmp_path), name="demo-api")
    _project_file_write(_ctx(tmp_path), name="demo-api", path="LOCAL.txt", content="local change\n")
    _project_commit(_ctx(tmp_path), name="demo-api", message="Local advance")

    payload = json.loads(_project_branch_compare(_ctx(tmp_path), name="demo-api"))

    assert payload["status"] == "ok"
    assert payload["github"]["origin"] == str(remote_dir)
    assert payload["branch"]["branch"] == "main"
    assert payload["branch"]["remote_ref"] == "origin/main"
    assert payload["branch"]["ahead_behind"]["available"] is True
    assert payload["branch"]["ahead_behind"]["ahead"] == 1
    assert payload["branch"]["ahead_behind"]["behind"] == 1
    assert payload["branch"]["compare"]["available"] is True
    assert payload["branch"]["compare"]["local"]["subject"] == "Local advance"
    assert payload["branch"]["compare"]["remote"]["subject"] == "Remote advance"
    assert payload["branch"]["compare"]["local"]["unique_commits"][0]["subject"] == "Local advance"
    assert payload["branch"]["compare"]["remote"]["unique_commits"][0]["subject"] == "Remote advance"


def test_project_branch_compare_requires_remote_tracking_branch(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/demo-api.git"], cwd=repo_dir, check=True)

    with pytest.raises(ValueError, match='remote tracking branch not found: origin/main; run project_git_fetch first'):
        _project_branch_compare(_ctx(tmp_path), name="demo-api")


def test_project_branch_rename_rejects_existing_target_branch(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "checkout", "-b", "feature/auth"], cwd=repo_dir, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo_dir, check=True)

    with pytest.raises(ValueError, match='local branch already exists'):
        _project_branch_rename(_ctx(tmp_path), name="demo-api", branch="feature/auth", new_branch="main")


def test_project_branch_rename_rejects_same_name(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")

    with pytest.raises(ValueError, match='new_branch must differ from branch'):
        _project_branch_rename(_ctx(tmp_path), name="demo-api", branch="main", new_branch="main")


def test_project_branch_rename_rejects_invalid_target_name(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")

    with pytest.raises(ValueError, match='must not contain whitespace'):
        _project_branch_rename(_ctx(tmp_path), name="demo-api", branch="main", new_branch="bad branch")


def test_project_branch_delete_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_branch_delete" in names


def test_project_branch_delete_removes_merged_branch(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "checkout", "-b", "feature/auth"], cwd=repo_dir, check=True)
    (repo_dir / "feature.txt").write_text("done\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Add feature"], cwd=repo_dir, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "merge", "--no-ff", "feature/auth", "-m", "Merge feature"], cwd=repo_dir, check=True)

    payload = json.loads(_project_branch_delete(_ctx(tmp_path), name="demo-api", branch="feature/auth"))

    assert payload['status'] == 'ok'
    assert payload['branch']['name'] == 'feature/auth'
    assert payload['branch']['deleted'] is True
    assert payload['branch']['force'] is False
    refs = subprocess.run(["git", "branch", "--list", "feature/auth"], cwd=repo_dir, check=True, capture_output=True, text=True)
    assert refs.stdout.strip() == ''


def test_project_branch_delete_refuses_active_branch(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")

    with pytest.raises(ValueError, match='cannot delete the active branch'):
        _project_branch_delete(_ctx(tmp_path), name="demo-api", branch="main")


def test_project_branch_delete_refuses_default_branch_even_if_not_active(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "checkout", "-b", "feature/auth"], cwd=repo_dir, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/demo-api.git"], cwd=repo_dir, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], cwd=repo_dir, check=True)

    with pytest.raises(ValueError, match='cannot delete the default branch'):
        _project_branch_delete(_ctx(tmp_path), name="demo-api", branch="main")


def test_project_branch_delete_refuses_unmerged_branch_without_force(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "checkout", "-b", "feature/auth"], cwd=repo_dir, check=True)
    (repo_dir / "feature.txt").write_text("done\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Add feature"], cwd=repo_dir, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo_dir, check=True)

    with pytest.raises(ValueError, match='branch is not fully merged'):
        _project_branch_delete(_ctx(tmp_path), name="demo-api", branch="feature/auth")


def test_project_branch_delete_force_removes_unmerged_branch(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "checkout", "-b", "feature/auth"], cwd=repo_dir, check=True)
    (repo_dir / "feature.txt").write_text("done\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Add feature"], cwd=repo_dir, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo_dir, check=True)

    payload = json.loads(_project_branch_delete(_ctx(tmp_path), name="demo-api", branch="feature/auth", force=True))

    assert payload['status'] == 'ok'
    assert payload['branch']['force'] is True
    refs = subprocess.run(["git", "branch", "--list", "feature/auth"], cwd=repo_dir, check=True, capture_output=True, text=True)
    assert refs.stdout.strip() == ''


def test_project_issue_list_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_issue_list" in names


def test_project_issue_get_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_issue_get" in names


def test_project_issue_create_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_issue_create" in names


def test_project_issue_comment_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_issue_comment" in names


def test_project_issue_list_reads_github_issues(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/demo-api.git"], cwd=repo_dir, check=True)

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        assert cwd == repo_dir
        assert args == [
            "issue", "list",
            "--state", "open",
            "--limit", "5",
            "--json", "number,title,state,url,author,labels",
        ]
        payload = [
            {
                "number": 12,
                "title": "Broken login",
                "state": "OPEN",
                "url": "https://github.com/acme/demo-api/issues/12",
                "author": {"login": "alice"},
                "labels": [{"name": "bug"}],
            }
        ]
        return subprocess.CompletedProcess(["gh", *args], 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)

    payload = json.loads(_project_issue_list(_ctx(tmp_path), name="demo-api", limit=5))

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['state'] == 'open'
    assert payload['github']['limit'] == 5
    assert payload['github']['issues'][0]['number'] == 12
    assert payload['github']['issues'][0]['title'] == 'Broken login'


def test_project_issue_get_reads_one_github_issue(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/demo-api.git"], cwd=repo_dir, check=True)

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        assert cwd == repo_dir
        assert args == [
            "issue", "view", "7",
            "--json", "number,title,body,state,url,author,labels,comments",
        ]
        payload = {
            "number": 7,
            "title": "Need healthcheck",
            "body": "Please add /health",
            "state": "OPEN",
            "url": "https://github.com/acme/demo-api/issues/7",
            "author": {"login": "bob"},
            "labels": [{"name": "enhancement"}],
            "comments": [{"author": {"login": "alice"}, "body": "working on it"}],
        }
        return subprocess.CompletedProcess(["gh", *args], 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)

    payload = json.loads(_project_issue_get(_ctx(tmp_path), name="demo-api", number=7))

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue']['number'] == 7
    assert payload['github']['issue']['body'] == 'Please add /health'
    assert payload['github']['issue']['comments'][0]['body'] == 'working on it'


def test_project_issue_create_returns_url_and_uses_stdin_for_body(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/demo-api.git"], cwd=repo_dir, check=True)

    calls = []

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout, "input": input_data})
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="https://github.com/acme/demo-api/issues/9\n", stderr="")

    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)

    payload = json.loads(_project_issue_create(_ctx(tmp_path), name="demo-api", title="Need /health", body="Please add a health endpoint"))

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue']['title'] == 'Need /health'
    assert payload['github']['issue']['body_provided'] is True
    assert payload['github']['issue']['url'] == 'https://github.com/acme/demo-api/issues/9'
    assert calls[0]['args'][:2] == ['issue', 'create']
    assert '--title=Need /health' in calls[0]['args']
    assert '--body-file=-' in calls[0]['args']
    assert calls[0]['input'] == 'Please add a health endpoint'


def test_project_issue_comment_passes_body_via_stdin(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/demo-api.git"], cwd=repo_dir, check=True)

    calls = []

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout, "input": input_data})
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="comment added\n", stderr="")

    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)

    payload = json.loads(_project_issue_comment(_ctx(tmp_path), name="demo-api", number=9, body="I am taking this"))

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue_comment']['number'] == 9
    assert payload['github']['issue_comment']['body'] == 'I am taking this'
    assert payload['github']['issue_comment']['result'] == 'comment added'
    assert calls[0]['args'] == ['issue', 'comment', '9', '--body-file', '-']
    assert calls[0]['input'] == 'I am taking this'


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


def test_project_pr_comment_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_pr_comment" in names


def test_project_pr_merge_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_pr_merge" in names


def test_project_pr_close_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_pr_close" in names


def test_project_pr_reopen_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_pr_reopen" in names


def test_project_pr_review_list_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_pr_review_list" in names


def test_project_pr_review_submit_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_pr_review_submit" in names


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


def test_project_pr_comment_passes_body_via_stdin(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/demo-api.git"], cwd=repo_dir, check=True)

    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout, "input": input_data})
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="https://github.com/acme/demo-api/pull/8#issuecomment-1\n", stderr="")

    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)

    payload = json.loads(_project_pr_comment(_ctx(tmp_path), name="demo-api", number=8, body="Looks good"))

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['pull_request_comment']['number'] == 8
    assert payload['github']['pull_request_comment']['body'] == 'Looks good'
    assert payload['github']['pull_request_comment']['result'] == 'https://github.com/acme/demo-api/pull/8#issuecomment-1'
    assert calls[0]['args'] == ['pr', 'comment', '8', '--body-file', '-']
    assert calls[0]['input'] == 'Looks good'


def test_project_pr_merge_uses_requested_method_and_delete_branch(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/demo-api.git"], cwd=repo_dir, check=True)

    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout, "input": input_data})
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="Merged pull request #8\n", stderr="")

    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)

    payload = json.loads(
        _project_pr_merge(
            _ctx(tmp_path),
            name="demo-api",
            number=8,
            method="squash",
            delete_branch=True,
        )
    )

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['pull_request_merge']['number'] == 8
    assert payload['github']['pull_request_merge']['method'] == 'squash'
    assert payload['github']['pull_request_merge']['delete_branch'] is True
    assert payload['github']['pull_request_merge']['result'] == 'Merged pull request #8'
    assert calls[0]['args'] == ['pr', 'merge', '8', '--squash', '--delete-branch']
    assert calls[0]['input'] is None




def test_project_pr_close_calls_gh(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/demo-api.git"], cwd=repo_dir, check=True)

    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout, "input": input_data})
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="Closed pull request #8\n", stderr="")

    monkeypatch.setattr('ouroboros.tools.project_pr_update._run_gh', fake_run_gh)

    payload = json.loads(_project_pr_close(_ctx(tmp_path), name="demo-api", number=8))

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['pull_request_close']['number'] == 8
    assert payload['github']['pull_request_close']['result'] == 'Closed pull request #8'
    assert calls[0]['args'] == ['pr', 'close', '8']


def test_project_pr_reopen_calls_gh(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/demo-api.git"], cwd=repo_dir, check=True)

    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout, "input": input_data})
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="Reopened pull request #8\n", stderr="")

    monkeypatch.setattr('ouroboros.tools.project_pr_update._run_gh', fake_run_gh)

    payload = json.loads(_project_pr_reopen(_ctx(tmp_path), name="demo-api", number=8))

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['pull_request_reopen']['number'] == 8
    assert payload['github']['pull_request_reopen']['result'] == 'Reopened pull request #8'
    assert calls[0]['args'] == ['pr', 'reopen', '8']


def test_project_pr_review_list_reads_reviews(tmp_path, monkeypatch):
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
                'reviews': [
                    {'id': 'r1', 'author': {'login': 'veles'}, 'state': 'APPROVED', 'body': 'ok'},
                    {'id': 'r2', 'author': {'login': 'andrey'}, 'state': 'COMMENTED', 'body': 'nit'},
                ]
            }),
            stderr="",
        )

    monkeypatch.setattr('ouroboros.tools.project_pr_update._run_gh', fake_run_gh)

    payload = json.loads(_project_pr_review_list(_ctx(tmp_path), name="demo-api", number=8))

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['pull_request_reviews']['number'] == 8
    assert payload['github']['pull_request_reviews']['count'] == 2
    assert payload['github']['pull_request_reviews']['items'][0]['state'] == 'APPROVED'
    assert calls[0]['args'] == ['pr', 'view', '8', '--json', 'reviews']


def test_project_pr_review_submit_approve_calls_gh(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/demo-api.git"], cwd=repo_dir, check=True)

    calls = []

    def fake_run_gh(args, cwd, timeout, input_data=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout, "input": input_data})
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="Review submitted\n", stderr="")

    monkeypatch.setattr('ouroboros.tools.project_pr_update._run_gh', fake_run_gh)

    payload = json.loads(_project_pr_review_submit(_ctx(tmp_path), name="demo-api", number=8, event='approve', body='Ship it'))

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['pull_request_review_submit']['number'] == 8
    assert payload['github']['pull_request_review_submit']['event'] == 'approve'
    assert payload['github']['pull_request_review_submit']['body'] == 'Ship it'
    assert calls[0]['args'] == ['pr', 'review', '8', '--approve', '--body-file', '-']
    assert calls[0]['input'] == 'Ship it'


def test_project_pr_review_submit_rejects_unknown_event(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/demo-api.git"], cwd=repo_dir, check=True)

    with pytest.raises(ValueError, match='event must be one of: comment, approve, request_changes'):
        _project_pr_review_submit(_ctx(tmp_path), name="demo-api", number=8, event='dismiss')

def test_project_pr_merge_rejects_unknown_method(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/demo-api.git"], cwd=repo_dir, check=True)

    with pytest.raises(ValueError, match='method must be one of: merge, squash, rebase'):
        _project_pr_merge(_ctx(tmp_path), name="demo-api", number=8, method="fast-forward")


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
    assert payload["remote_awareness"] == {
        "available": False,
        "reason": "origin remote not configured",
    }


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
    assert payload["remote_awareness"]["available"] is True
    assert payload["remote_awareness"]["branch"] == "main"
    assert payload["remote_awareness"]["remote_ref"] == ""
    assert payload["remote_awareness"]["ahead_behind"]["available"] is False


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


def test_project_issue_label_add_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_issue_label_add" in names


def test_project_issue_label_remove_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_issue_label_remove" in names


def test_project_issue_assign_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_issue_assign" in names


def test_project_issue_unassign_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_issue_unassign" in names


def test_project_issue_update_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_issue_update" in names


def test_project_issue_close_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_issue_close" in names


def test_project_issue_reopen_registered():
    tmp = pathlib.Path("/tmp")
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    assert "project_issue_reopen" in names


def test_project_issue_update_passes_title_and_body_via_stdin(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/demo-api.git"], cwd=repo_dir, check=True)

    calls = []

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout, "input": input_data})
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="issue updated\n", stderr="")

    monkeypatch.setattr('ouroboros.tools.project_issue_update._run_gh', fake_run_gh)

    payload = json.loads(
        _project_issue_update(
            _ctx(tmp_path),
            name="demo-api",
            number=9,
            title="Need /ready",
            body="Please rename /health to /ready",
        )
    )

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue_update']['number'] == 9
    assert payload['github']['issue_update']['title'] == 'Need /ready'
    assert payload['github']['issue_update']['body_provided'] is True
    assert payload['github']['issue_update']['result'] == 'issue updated'
    assert calls[0]['args'] == ['issue', 'edit', '9', '--title=Need /ready', '--body-file', '-']
    assert calls[0]['input'] == 'Please rename /health to /ready'


def test_project_issue_close_calls_gh_close(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/demo-api.git"], cwd=repo_dir, check=True)

    calls = []

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout, "input": input_data})
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="issue closed\n", stderr="")

    monkeypatch.setattr('ouroboros.tools.project_issue_update._run_gh', fake_run_gh)

    payload = json.loads(_project_issue_close(_ctx(tmp_path), name="demo-api", number=11))

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue_close']['number'] == 11
    assert payload['github']['issue_close']['result'] == 'issue closed'
    assert calls[0]['args'] == ['issue', 'close', '11']
    assert calls[0]['input'] is None


def test_project_issue_reopen_calls_gh_reopen(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/demo-api.git"], cwd=repo_dir, check=True)

    calls = []

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout, "input": input_data})
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="issue reopened\n", stderr="")

    monkeypatch.setattr('ouroboros.tools.project_issue_update._run_gh', fake_run_gh)

    payload = json.loads(_project_issue_reopen(_ctx(tmp_path), name="demo-api", number=11))

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue_reopen']['number'] == 11
    assert payload['github']['issue_reopen']['result'] == 'issue reopened'
    assert calls[0]['args'] == ['issue', 'reopen', '11']
    assert calls[0]['input'] is None


def test_project_issue_label_add_calls_gh_edit_with_add_label(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/demo-api.git"], cwd=repo_dir, check=True)

    calls = []

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout, "input": input_data})
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="labels added\n", stderr="")

    monkeypatch.setattr('ouroboros.tools.project_issue_update._run_gh', fake_run_gh)

    payload = json.loads(_project_issue_label_add(_ctx(tmp_path), name="demo-api", number=7, labels=["bug", "backend"]))

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue_label_add']['number'] == 7
    assert payload['github']['issue_label_add']['labels'] == ['bug', 'backend']
    assert payload['github']['issue_label_add']['result'] == 'labels added'
    assert calls[0]['args'] == ['issue', 'edit', '7', '--add-label', 'bug', '--add-label', 'backend']
    assert calls[0]['input'] is None


def test_project_issue_label_remove_calls_gh_edit_with_remove_label(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/demo-api.git"], cwd=repo_dir, check=True)

    calls = []

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout, "input": input_data})
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="labels removed\n", stderr="")

    monkeypatch.setattr('ouroboros.tools.project_issue_update._run_gh', fake_run_gh)

    payload = json.loads(_project_issue_label_remove(_ctx(tmp_path), name="demo-api", number=7, labels="bug"))

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue_label_remove']['number'] == 7
    assert payload['github']['issue_label_remove']['labels'] == ['bug']
    assert payload['github']['issue_label_remove']['result'] == 'labels removed'
    assert calls[0]['args'] == ['issue', 'edit', '7', '--remove-label', 'bug']
    assert calls[0]['input'] is None


def test_project_issue_assign_calls_gh_edit_with_add_assignee(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/demo-api.git"], cwd=repo_dir, check=True)

    calls = []

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout, "input": input_data})
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="assignees added\n", stderr="")

    monkeypatch.setattr('ouroboros.tools.project_issue_update._run_gh', fake_run_gh)

    payload = json.loads(_project_issue_assign(_ctx(tmp_path), name="demo-api", number=5, assignees=["alice", "bob"]))

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue_assign']['number'] == 5
    assert payload['github']['issue_assign']['assignees'] == ['alice', 'bob']
    assert payload['github']['issue_assign']['result'] == 'assignees added'
    assert calls[0]['args'] == ['issue', 'edit', '5', '--add-assignee', 'alice', '--add-assignee', 'bob']
    assert calls[0]['input'] is None


def test_project_issue_unassign_calls_gh_edit_with_remove_assignee(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/demo-api.git"], cwd=repo_dir, check=True)

    calls = []

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout, "input": input_data})
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="assignees removed\n", stderr="")

    monkeypatch.setattr('ouroboros.tools.project_issue_update._run_gh', fake_run_gh)

    payload = json.loads(_project_issue_unassign(_ctx(tmp_path), name="demo-api", number=5, assignees="alice"))

    assert payload['status'] == 'ok'
    assert payload['github']['repo'] == 'acme/demo-api'
    assert payload['github']['issue_unassign']['number'] == 5
    assert payload['github']['issue_unassign']['assignees'] == ['alice']
    assert payload['github']['issue_unassign']['result'] == 'assignees removed'
    assert calls[0]['args'] == ['issue', 'edit', '5', '--remove-assignee', 'alice']
    assert calls[0]['input'] is None


def test_project_issue_label_add_rejects_empty_labels(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/demo-api.git"], cwd=repo_dir, check=True)

    with pytest.raises(ValueError, match='labels must contain at least one non-empty value'):
        _project_issue_label_add(_ctx(tmp_path), name="demo-api", number=1, labels=["", "   "])


def test_project_issue_assign_rejects_whitespace_in_assignee(tmp_path):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/demo-api.git"], cwd=repo_dir, check=True)

    with pytest.raises(ValueError, match='assignees entries must not contain whitespace'):
        _project_issue_assign(_ctx(tmp_path), name="demo-api", number=1, assignees=["alice smith"])


def test_project_github_dev_loop_scenario_smoke(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    remote_dir = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote_dir)], cwd=repo_dir, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=remote_dir, check=True)

    gh_calls = []
    state = {
        'issues': [
            {
                'number': 1,
                'title': 'Existing issue',
                'state': 'OPEN',
                'url': 'https://github.com/acme/demo-api/issues/1',
            }
        ],
        'issue_comments': [],
        'pull_requests': [],
        'reviews': [
            {
                'id': 'review-1',
                'author': {'login': 'review-bot'},
                'state': 'COMMENTED',
                'body': 'Looks good',
            }
        ],
        'pr_comments': [],
    }

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        gh_calls.append({'args': list(args), 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        if args[:2] == ['issue', 'create']:
            issue = {
                'number': 2,
                'title': next((a.split('=', 1)[1] for a in args if a.startswith('--title=')), ''),
                'state': 'OPEN',
                'url': 'https://github.com/acme/demo-api/issues/2',
            }
            state['issues'].append(issue)
            return subprocess.CompletedProcess(['gh', *args], 0, stdout=issue['url'] + '\n', stderr='')
        if args[:2] == ['issue', 'comment']:
            state['issue_comments'].append({'number': int(args[2]), 'body': input_data or ''})
            return subprocess.CompletedProcess(['gh', *args], 0, stdout='issue commented\n', stderr='')
        if args[:2] == ['issue', 'list']:
            return subprocess.CompletedProcess(['gh', *args], 0, stdout=json.dumps(state['issues']), stderr='')
        if args[:2] == ['pr', 'create']:
            pr = {
                'number': 7,
                'title': next((a.split('=', 1)[1] for a in args if a.startswith('--title=')), ''),
                'state': 'OPEN',
                'headRefName': next((a.split('=', 1)[1] for a in args if a.startswith('--head=')), ''),
                'baseRefName': next((a.split('=', 1)[1] for a in args if a.startswith('--base=')), ''),
                'url': 'https://github.com/acme/demo-api/pull/7',
                'isDraft': False,
                'author': {'login': 'veles'},
            }
            state['pull_requests'] = [pr]
            return subprocess.CompletedProcess(['gh', *args], 0, stdout=pr['url'] + '\n', stderr='')
        if args[:2] == ['pr', 'list']:
            return subprocess.CompletedProcess(['gh', *args], 0, stdout=json.dumps(state['pull_requests']), stderr='')
        if args[:3] == ['pr', 'view', '7']:
            if '--json' in args and 'reviews' in args:
                return subprocess.CompletedProcess(['gh', *args], 0, stdout=json.dumps({'reviews': state['reviews']}), stderr='')
            pr = state['pull_requests'][0]
            payload = {
                'number': pr['number'],
                'title': pr['title'],
                'body': 'Implements issue #2',
                'state': pr['state'],
                'url': pr['url'],
                'headRefName': pr['headRefName'],
                'baseRefName': pr['baseRefName'],
                'comments': state['pr_comments'],
                'commits': [
                    {
                        'oid': subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip(),
                        'messageHeadline': 'Add ready endpoint docs',
                    }
                ],
            }
            return subprocess.CompletedProcess(['gh', *args], 0, stdout=json.dumps(payload), stderr='')
        if args[:2] == ['pr', 'comment']:
            state['pr_comments'].append({'body': input_data or ''})
            return subprocess.CompletedProcess(['gh', *args], 0, stdout='pr commented\n', stderr='')
        if args[:2] == ['pr', 'review']:
            state['reviews'].append({'id': 'review-2', 'state': 'APPROVED', 'body': input_data or ''})
            return subprocess.CompletedProcess(['gh', *args], 0, stdout='review submitted\n', stderr='')
        if args[:2] == ['pr', 'merge']:
            state['pull_requests'][0]['state'] = 'MERGED'
            return subprocess.CompletedProcess(['gh', *args], 0, stdout='merged\n', stderr='')
        raise AssertionError(f'unexpected gh args: {args}')

    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)
    monkeypatch.setattr('ouroboros.tools.project_github_dev._project_github_slug', lambda repo_dir: 'acme/demo-api')
    monkeypatch.setattr('ouroboros.tools.project_issue_update._run_gh', fake_run_gh)
    monkeypatch.setattr('ouroboros.tools.project_issue_update._project_github_slug', lambda repo_dir: 'acme/demo-api')
    monkeypatch.setattr('ouroboros.tools.project_pr_update._run_gh', fake_run_gh)
    monkeypatch.setattr('ouroboros.tools.project_pr_update._project_github_slug', lambda repo_dir: 'acme/demo-api')

    branch_payload = json.loads(_project_branch_checkout(_ctx(tmp_path), name='demo-api', branch='feature/ready-endpoint', base='main'))
    assert branch_payload['branch']['created'] is True

    _project_file_write(_ctx(tmp_path), name='demo-api', path='README.md', content='# demo-api\n\nReady endpoint docs\n')
    commit_payload = json.loads(_project_commit(_ctx(tmp_path), name='demo-api', message='Add ready endpoint docs'))
    assert commit_payload['status'] == 'ok'

    push_payload = json.loads(_project_push(_ctx(tmp_path), name='demo-api', branch='feature/ready-endpoint'))
    assert push_payload['status'] == 'ok'

    issue_create = json.loads(_project_issue_create(_ctx(tmp_path), name='demo-api', title='Add /ready endpoint', body='Need readiness endpoint'))
    assert issue_create['github']['issue']['url'].endswith('/issues/2')
    issue_number = 2

    issue_comment = json.loads(_project_issue_comment(_ctx(tmp_path), name='demo-api', number=issue_number, body='Working on this now'))
    assert issue_comment['status'] == 'ok'

    issue_list = json.loads(_project_issue_list(_ctx(tmp_path), name='demo-api', state='open', limit=10))
    assert issue_list['github']['issues'][-1]['number'] == 2

    pr_create = json.loads(_project_pr_create(_ctx(tmp_path), name='demo-api', title='Add ready endpoint', body='Implements issue #2'))
    assert pr_create['github']['pull_request']['head'] == 'feature/ready-endpoint'

    pr_list = json.loads(_project_pr_list(_ctx(tmp_path), name='demo-api', state='open', limit=10))
    assert pr_list['github']['pull_requests'][0]['number'] == 7

    pr_get = json.loads(_project_pr_get(_ctx(tmp_path), name='demo-api', number=7))
    assert pr_get['github']['pull_request']['number'] == 7

    pr_comment = json.loads(_project_pr_comment(_ctx(tmp_path), name='demo-api', number=7, body='Please review'))
    assert pr_comment['status'] == 'ok'

    review_submit = json.loads(_project_pr_review_submit(_ctx(tmp_path), name='demo-api', number=7, event='approve', body='Looks good to me'))
    assert review_submit['github']['pull_request_review_submit']['event'] == 'approve'

    review_list = json.loads(_project_pr_review_list(_ctx(tmp_path), name='demo-api', number=7))
    assert review_list['github']['pull_request_reviews']['count'] == 2

    merge_payload = json.loads(_project_pr_merge(_ctx(tmp_path), name='demo-api', number=7, method='squash', delete_branch=True))
    assert merge_payload['status'] == 'ok'

    fetch_payload = json.loads(_project_git_fetch(_ctx(tmp_path), name='demo-api'))
    assert fetch_payload['status'] == 'ok'

    compare_payload = json.loads(_project_branch_compare(_ctx(tmp_path), name='demo-api', branch='feature/ready-endpoint'))
    assert compare_payload['branch']['ahead_behind']['available'] is True
    assert compare_payload['branch']['ahead_behind']['ahead'] == 0
    assert compare_payload['branch']['ahead_behind']['behind'] == 0

    status_payload = json.loads(_project_status(_ctx(tmp_path), name='demo-api'))
    assert status_payload['remote_awareness']['available'] is True
    assert status_payload['remote_awareness']['branch'] == 'feature/ready-endpoint'
    assert status_payload['remote_awareness']['ahead_behind']['ahead'] == 0
    assert status_payload['remote_awareness']['ahead_behind']['behind'] == 0

    called_pairs = [(call['args'][0], call['args'][1]) for call in gh_calls]
    assert ('issue', 'create') in called_pairs
    assert ('issue', 'comment') in called_pairs
    assert ('pr', 'create') in called_pairs
    assert ('pr', 'comment') in called_pairs
    assert ('pr', 'review') in called_pairs
    assert ('pr', 'merge') in called_pairs
