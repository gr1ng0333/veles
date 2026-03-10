import json
import pathlib
import subprocess
import tempfile

from ouroboros.tools.external_repos import (
    _external_repo_git_diff,
    _external_repo_git_status,
    _external_repo_list,
    _external_repo_list_files,
    _external_repo_read,
    _external_repo_register,
    _external_repo_run_shell,
    _external_repo_search,
    _external_repo_sync,
)
from ouroboros.tools.registry import ToolContext, ToolRegistry


def make_git_repo() -> pathlib.Path:
    tmp = pathlib.Path(tempfile.mkdtemp())
    subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "veles@example.com"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.name", "Veles Test"], cwd=tmp, check=True)
    (tmp / "README.md").write_text("hello external repo\n", encoding="utf-8")
    (tmp / "src").mkdir()
    (tmp / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp, check=True, capture_output=True, text=True)
    return tmp


def make_ctx() -> ToolContext:
    tmp = pathlib.Path(tempfile.mkdtemp())
    return ToolContext(repo_dir=tmp, drive_root=tmp, current_chat_id=12345)


def test_external_repo_tools_are_registered():
    tmp = pathlib.Path(tempfile.mkdtemp())
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {t["function"]["name"] for t in registry.schemas()}
    expected = {
        "external_repo_register",
        "external_repo_list",
        "external_repo_sync",
        "external_repo_read",
        "external_repo_list_files",
        "external_repo_search",
        "external_repo_run_shell",
        "external_repo_git_status",
        "external_repo_git_diff",
    }
    assert expected.issubset(names)


def test_external_repo_register_and_list():
    ctx = make_ctx()
    repo = make_git_repo()
    raw = _external_repo_register(ctx, alias="demo", repo_path=str(repo), notes="sample")
    payload = json.loads(raw)
    assert payload["status"] == "ok"
    listed = json.loads(_external_repo_list(ctx))
    assert listed["repos"][0]["alias"] == "demo"
    assert listed["repos"][0]["notes"] == "sample"


def test_external_repo_read_list_search_and_git_status():
    ctx = make_ctx()
    repo = make_git_repo()
    _external_repo_register(ctx, alias="demo", repo_path=str(repo))

    text = _external_repo_read(ctx, alias="demo", path="README.md")
    assert "hello external repo" in text

    files = json.loads(_external_repo_list_files(ctx, alias="demo", dir="src"))
    assert "src/app.py" in files

    search = json.loads(_external_repo_search(ctx, alias="demo", query="hello"))
    assert search["count"] >= 1
    assert any("README.md" in row or "src/app.py" in row for row in search["results"])

    status = _external_repo_git_status(ctx, alias="demo")
    assert "##" in status or "No commits yet" in status


def test_external_repo_run_shell_and_diff():
    ctx = make_ctx()
    repo = make_git_repo()
    _external_repo_register(ctx, alias="demo", repo_path=str(repo))

    run = json.loads(_external_repo_run_shell(ctx, alias="demo", cmd=["python3", "-c", "print('ok')"]))
    assert run["returncode"] == 0
    assert "ok" in run["stdout"]

    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    diff = _external_repo_git_diff(ctx, alias="demo")
    assert "changed" in diff or "README.md" in diff


def test_external_repo_sync_handles_repo_without_origin():
    ctx = make_ctx()
    repo = make_git_repo()
    _external_repo_register(ctx, alias="demo", repo_path=str(repo))
    payload = json.loads(_external_repo_sync(ctx, alias="demo"))
    assert payload["status"] in {"ok", "error"}
    assert payload["alias"] == "demo"


def test_external_repo_register_rejects_relative_path():
    ctx = make_ctx()
    try:
        _external_repo_register(ctx, alias="demo", repo_path="relative/path")
    except ValueError as e:
        assert "absolute path" in str(e)
    else:
        assert False, "expected ValueError for relative repo_path"
