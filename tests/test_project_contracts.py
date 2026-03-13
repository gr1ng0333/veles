import json
import pathlib

import pytest

from ouroboros.tools.project_bootstrap import _project_init, _project_server_register, _project_server_run
from ouroboros.tools.project_deploy import _project_deploy_apply
from ouroboros.tools.project_server_info import _project_server_get
from ouroboros.tools.project_server_management import _project_server_validate
from ouroboros.tools.project_server_observability import _project_deploy_status, _project_service_status
from ouroboros.tools.registry import ToolContext


def _ctx(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path)


@pytest.fixture(autouse=True)
def _projects_root_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VELES_PROJECTS_ROOT", str(tmp_path / "projects"))


def _assert_repo_shape(payload: dict, project_name: str):
    repo = payload["repo"]
    assert set(repo) >= {"path", "branch", "sha"}
    assert repo["path"].endswith(project_name)
    assert repo["branch"]
    assert repo["sha"]


def _assert_server_shape(server: dict, alias: str):
    assert set(server) >= {
        "alias", "label", "host", "port", "user", "auth",
        "ssh_key_path", "deploy_path", "created_at", "updated_at",
    }
    assert server["alias"] == alias


def test_project_tool_result_shapes_are_consistent_across_stage3(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    _project_server_register(
        _ctx(tmp_path),
        name="demo-api",
        alias="prod",
        host="example.com",
        user="deploy",
        ssh_key_path="/tmp/id_demo",
        deploy_path="/srv/demo-api",
        label="Production",
    )

    def fake_run_ssh(args, timeout):
        return __import__("subprocess").CompletedProcess(["ssh"], 0, stdout="ok\n", stderr="")

    def fake_validate_remote_text(server, command, timeout):
        if "systemctl show" in command:
            stdout = (
                "LoadState=loaded\n"
                "ActiveState=active\n"
                "SubState=running\n"
                "UnitFileState=enabled\n"
                "Result=success\n"
                "FragmentPath=/etc/systemd/system/demo-api.service\n"
            )
        else:
            stdout = (
                "SSH_OK=1\n"
                "WHOAMI=deploy\n"
                "SYSTEMCTL=present\n"
                "DEPLOY_EXISTS=1\n"
                "DEPLOY_WRITABLE=1\n"
                "PARENT_EXISTS=1\n"
                "PARENT_WRITABLE=1\n"
            )
        return __import__("subprocess").CompletedProcess(["ssh"], 0, stdout=stdout, stderr="")

    def fake_sync(ctx, **kwargs):
        return json.dumps({
            "status": "ok",
            "sync": {"file_count": 2, "files": ["README.md", "src/demo_api/main.py"]},
            "result": {"ok": True, "exit_code": 0},
        })

    def fake_service(ctx, **kwargs):
        action = kwargs["action"]
        if action == "status":
            return json.dumps({
                "status": "ok",
                "service": {
                    "name": "demo-api",
                    "unit_name": "demo-api.service",
                    "active_state": "active",
                    "sub_state": "running",
                    "result_state": "success",
                    "exists": True,
                    "running": True,
                },
                "result": {"ok": True, "exit_code": 0},
            })
        return json.dumps({
            "status": "ok",
            "service": {"action": action, "unit_name": "demo-api.service"},
            "result": {"ok": True, "exit_code": 0},
        })

    monkeypatch.setattr("ouroboros.tools.project_bootstrap._run_ssh", fake_run_ssh)
    monkeypatch.setattr("ouroboros.tools.project_server_management._run_remote_text", fake_validate_remote_text)
    monkeypatch.setattr("ouroboros.tools.project_server_observability._run_remote_text", fake_validate_remote_text)
    monkeypatch.setattr("ouroboros.tools.project_deploy._project_server_sync", fake_sync)
    monkeypatch.setattr("ouroboros.tools.project_service._project_service_control", fake_service)

    server_get = json.loads(_project_server_get(_ctx(tmp_path), name="demo-api", alias="prod"))
    _assert_repo_shape(server_get, "demo-api")
    _assert_server_shape(server_get["server"], "prod")

    server_run = json.loads(_project_server_run(_ctx(tmp_path), name="demo-api", alias="prod", command="pwd"))
    _assert_repo_shape(server_run, "demo-api")
    _assert_server_shape(server_run["server"], "prod")

    validate = json.loads(_project_server_validate(_ctx(tmp_path), name="demo-api", alias="prod", service_name="demo-api", sudo=False))
    _assert_repo_shape(validate, "demo-api")
    _assert_server_shape(validate["server"], "prod")

    service_status = json.loads(_project_service_status(_ctx(tmp_path), name="demo-api", alias="prod", service_name="demo-api", sudo=False))
    _assert_repo_shape(service_status, "demo-api")
    _assert_server_shape(service_status["server"], "prod")

    apply_payload = json.loads(
        _project_deploy_apply(
            _ctx(tmp_path),
            name="demo-api",
            alias="prod",
            service_name="demo-api",
            mode="update",
            dry_run=False,
            sudo=False,
        )
    )
    _assert_repo_shape(apply_payload, "demo-api")
    _assert_server_shape(apply_payload["server"], "prod")

    deploy_status = json.loads(_project_deploy_status(_ctx(tmp_path), name="demo-api", alias="prod", service_name="demo-api", sudo=False))
    _assert_repo_shape(deploy_status, "demo-api")
    _assert_server_shape(deploy_status["server"], "prod")
    assert deploy_status["last_deploy"]["outcome"]["target"]["alias"] == "prod"



def test_stage3_read_side_helpers_share_meaningful_working_tree_filter():
    from ouroboros.tools.project_read_side import _working_tree_signal

    status_payload = {
        'working_tree': {
            'entries': [
                {'path': '.veles/servers.json', 'status': '??'},
                {'path': '.veles/deploy-state.json', 'status': '??'},
                {'path': 'README.md', 'status': ' M'},
                {'path': 'src/app.py', 'status': '??'},
            ]
        }
    }

    signal = _working_tree_signal(status_payload)
    assert signal['clean'] is False
    assert signal['changed_count'] == 2
    assert [item['path'] for item in signal['entries']] == ['README.md', 'src/app.py']


def test_stage3_shared_github_helpers_keep_overview_and_operational_snapshot_in_sync(tmp_path, monkeypatch):
    from ouroboros.tools.project_bootstrap import _project_init
    from ouroboros.tools.project_operational_snapshot import _project_operational_snapshot
    from ouroboros.tools.project_overview import _project_overview

    _project_init(_ctx(tmp_path), name="Demo API", language="python")

    def fake_project_github_slug(repo_dir):
        return 'acme/demo-api'

    def fake_run_project_gh_json(repo_dir, args, timeout=120):
        if args[:2] == ['issue', 'list']:
            if '--json' in args and 'number,title,state,url,author,labels' in args:
                return [
                    {'number': 1, 'title': 'Issue one', 'state': 'OPEN', 'url': 'https://github.com/acme/demo-api/issues/1', 'author': {'login': 'veles'}, 'labels': []},
                    {'number': 2, 'title': 'Issue two', 'state': 'OPEN', 'url': 'https://github.com/acme/demo-api/issues/2', 'author': {'login': 'veles'}, 'labels': []},
                ]
            return [{'number': 1}, {'number': 2}]
        if args[:2] == ['pr', 'list']:
            if '--json' in args and 'number,title,state,headRefName,baseRefName,url,isDraft,author' in args:
                return [
                    {'number': 7, 'title': 'PR one', 'state': 'OPEN', 'headRefName': 'feature/x', 'baseRefName': 'main', 'url': 'https://github.com/acme/demo-api/pull/7', 'isDraft': False, 'author': {'login': 'veles'}},
                ]
            return [{'number': 7}]
        raise AssertionError(f'unexpected gh args: {args}')

    monkeypatch.setattr('ouroboros.tools.project_github_dev._project_github_slug', fake_project_github_slug)
    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_project_gh_json', fake_run_project_gh_json)
    monkeypatch.setattr('ouroboros.tools.project_read_side._project_github_slug', fake_project_github_slug)
    monkeypatch.setattr('ouroboros.tools.project_read_side._run_project_gh_json', fake_run_project_gh_json)

    overview = json.loads(_project_overview(_ctx(tmp_path), name='demo-api', issue_limit=10, pr_limit=10))
    snapshot = json.loads(_project_operational_snapshot(_ctx(tmp_path), name='demo-api', issue_limit=10, pr_limit=10))

    assert overview['github']['configured'] is True
    assert overview['github']['available'] is True
    assert overview['github']['repo'] == 'acme/demo-api'
    assert overview['github']['issues']['returned_count'] == 2
    assert overview['github']['pull_requests']['returned_count'] == 1

    assert snapshot['github']['configured'] is True
    assert snapshot['github']['available'] is True
    assert snapshot['github']['repo'] == 'acme/demo-api'
    assert snapshot['github']['open_issue_count'] == 2
    assert snapshot['github']['open_pull_request_count'] == 1
