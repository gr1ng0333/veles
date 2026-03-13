import json
import pathlib
import subprocess

import pytest

from ouroboros.tools.project_bootstrap import _project_commit, _project_file_write, _project_init, _project_push, _project_server_register, _project_status
from ouroboros.tools.project_composite_flows import _project_deploy_and_verify
from ouroboros.tools.project_deploy import _project_deploy_apply
from ouroboros.tools.project_github_dev import _project_branch_checkout, _project_issue_comment, _project_issue_create, _project_issue_list, _project_pr_comment, _project_pr_create, _project_pr_get, _project_pr_list, _project_pr_merge
from ouroboros.tools.project_operational_snapshot import _project_operational_snapshot
from ouroboros.tools.project_overview import _project_overview
from ouroboros.tools.project_pr_update import _project_pr_review_list, _project_pr_review_submit
from ouroboros.tools.project_remote_awareness import _project_branch_compare, _project_git_fetch
from ouroboros.tools.project_server_management import _project_server_validate
from ouroboros.tools.project_server_observability import _project_deploy_status, _project_service_logs
from ouroboros.tools.registry import ToolContext


def _ctx(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path)


@pytest.fixture(autouse=True)
def _projects_root_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VELES_PROJECTS_ROOT", str(tmp_path / "projects"))


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


def test_project_deploy_operational_loop_scenario_smoke(tmp_path, monkeypatch):
    from ouroboros.tools.project_server_management import _project_server_validate
    from ouroboros.tools.project_server_observability import _project_deploy_status, _project_service_logs

    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / 'projects' / 'demo-api'
    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='prod',
        host='example.com',
        user='deploy',
        ssh_key_path='/tmp/id_demo',
        deploy_path='/srv/demo-api',
        label='Production',
    )

    validate_calls = []

    def fake_validate_remote_text(server, command, timeout):
        validate_calls.append(command)
        if 'systemctl show' in command:
            stdout = (
                'LoadState=loaded\n'
                'ActiveState=inactive\n'
                'SubState=dead\n'
                'UnitFileState=enabled\n'
                'Result=success\n'
                'FragmentPath=/etc/systemd/system/demo-api.service\n'
            )
            return subprocess.CompletedProcess(['ssh'], 0, stdout=stdout, stderr='')
        stdout = (
            'SSH_OK=1\n'
            'WHOAMI=deploy\n'
            'SYSTEMCTL=present\n'
            'DEPLOY_EXISTS=0\n'
            'DEPLOY_WRITABLE=0\n'
            'PARENT_EXISTS=1\n'
            'PARENT_WRITABLE=1\n'
        )
        return subprocess.CompletedProcess(['ssh'], 0, stdout=stdout, stderr='')

    monkeypatch.setattr('ouroboros.tools.project_server_management._run_remote_text', fake_validate_remote_text)

    validate_payload = json.loads(
        _project_server_validate(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            timeout=25,
            sudo=False,
        )
    )

    assert validate_payload['status'] == 'ok'
    assert validate_payload['validation']['ok'] is True
    assert validate_payload['validation']['checks']['deploy_path_ready'] is True
    assert validate_payload['validation']['checks']['service_unit_exists'] is True
    assert len(validate_calls) == 2

    apply_calls = []

    def fake_sync(ctx, **kwargs):
        apply_calls.append(('sync', kwargs))
        return json.dumps({
            'status': 'ok',
            'sync': {'file_count': 4, 'files': ['README.md', 'requirements.txt', 'src/demo_api/main.py', 'src/demo_api/__init__.py']},
            'result': {'ok': True, 'exit_code': 0},
        })

    def fake_run(ctx, **kwargs):
        apply_calls.append(('setup', kwargs))
        return json.dumps({
            'status': 'ok',
            'command': {'raw': kwargs['command']},
            'result': {'ok': True, 'exit_code': 0, 'stdout': 'setup ok\n'},
        })

    def fake_service(ctx, **kwargs):
        apply_calls.append((kwargs['action'], kwargs))
        action = kwargs['action']
        if action == 'status':
            return json.dumps({
                'status': 'ok',
                'service': {
                    'name': 'demo-api',
                    'unit_name': 'demo-api.service',
                    'active_state': 'active',
                    'sub_state': 'running',
                    'result_state': 'success',
                    'exists': True,
                    'running': True,
                },
                'result': {'ok': True, 'exit_code': 0},
            })
        return json.dumps({
            'status': 'ok',
            'service': {'action': action, 'unit_name': 'demo-api.service'},
            'result': {'ok': True, 'exit_code': 0},
        })

    monkeypatch.setattr('ouroboros.tools.project_deploy._project_server_sync', fake_sync)
    monkeypatch.setattr('ouroboros.tools.project_bootstrap._project_server_run', fake_run)
    monkeypatch.setattr('ouroboros.tools.project_service._project_service_control', fake_service)

    apply_payload = json.loads(
        _project_deploy_apply(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            mode='update',
            delete=True,
            sync_timeout=90,
            service_timeout=60,
            status_timeout=20,
        )
    )

    assert apply_payload['status'] == 'ok'
    assert [step['key'] for step in apply_payload['steps']] == ['sync', 'setup', 'install_service', 'restart', 'status']
    assert [name for name, _ in apply_calls] == ['sync', 'setup', 'install', 'restart', 'status']
    assert apply_payload['execution']['ok_steps'] == 5
    assert apply_payload['execution']['failed_step'] == ''
    assert apply_payload['deploy_record']['exists'] is True
    assert apply_payload['deploy_record']['outcome']['deploy']['mode'] == 'update'
    assert apply_payload['deploy_record']['outcome']['deploy']['lifecycle_action'] == 'restart'
    assert repo_dir.joinpath('.veles', 'deploy-state.json').exists()

    status_calls = []

    def fake_status_ssh_text(args, timeout):
        command = args[-1]
        status_calls.append(command)
        if 'journalctl' in command:
            return subprocess.CompletedProcess(['ssh', *args], 0, stdout='line1\nline2\nline3\n', stderr='')
        if 'systemctl show' in command:
            stdout = (
                'LoadState=loaded\n'
                'ActiveState=active\n'
                'SubState=running\n'
                'UnitFileState=enabled\n'
                'FragmentPath=/etc/systemd/system/demo-api.service\n'
                'ExecMainPID=4321\n'
                'ExecMainStatus=0\n'
                'Result=success\n'
                'enabled\n'
            )
            return subprocess.CompletedProcess(['ssh', *args], 0, stdout=stdout, stderr='')
        stdout = (
            'DEPLOY_EXISTS=1\n'
            'DEPLOY_REALPATH=/srv/demo-api\n'
            'DEPLOY_TOP_LEVEL_COUNT=4\n'
            'DEPLOY_WRITABLE=1\n'
            'DEPLOY_GIT=0\n'
        )
        return subprocess.CompletedProcess(['ssh', *args], 0, stdout=stdout, stderr='')

    monkeypatch.setattr('ouroboros.tools.project_server_observability._run_ssh_text', fake_status_ssh_text)

    status_payload = json.loads(
        _project_deploy_status(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            timeout=15,
            sudo=False,
        )
    )
    logs_payload = json.loads(
        _project_service_logs(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            lines=20,
            timeout=10,
            max_output_chars=12,
            sudo=False,
        )
    )

    assert status_payload['status'] == 'ok'
    assert status_payload['deploy']['exists'] is True
    assert status_payload['deploy']['top_level_entry_count'] == 4
    assert status_payload['last_deploy']['exists'] is True
    assert status_payload['last_deploy']['outcome']['status'] == 'ok'
    assert status_payload['last_deploy']['outcome']['deploy']['execution']['ok_steps'] == 5
    assert status_payload['service']['running'] is True
    assert status_payload['diagnostics']['severity'] == 'healthy'
    assert 'last deploy: ok' in status_payload['diagnostics']['summary']

    assert logs_payload['status'] == 'ok'
    assert logs_payload['logs']['empty'] is False
    assert logs_payload['result']['truncated'] is True
    assert logs_payload['logs']['content'] == 'line1\nline2\n'

    snapshot_payload = json.loads(
        _project_operational_snapshot(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
        )
    )

    assert snapshot_payload['selection']['runtime_included'] is True
    assert snapshot_payload['readiness']['local_clean'] is True
    assert snapshot_payload['readiness']['deploy_target_ready'] is True
    assert snapshot_payload['readiness']['service_running'] is True
    assert snapshot_payload['readiness']['rollout_ready'] is True
    assert snapshot_payload['risk_flags'] == []
    assert snapshot_payload['last_deploy']['status'] == 'ok'
    assert snapshot_payload['last_deploy']['deploy']['execution']['ok_steps'] == 5
    assert snapshot_payload['runtime']['diagnostics']['severity'] == 'healthy'
    assert 'attach a GitHub origin with project_github_create' in snapshot_payload['next_actions']

    assert any('systemctl show' in command for command in status_calls)
    assert any('journalctl' in command for command in status_calls)


def test_stage3_full_cycle_scenario_smoke(tmp_path, monkeypatch):
    _project_init(_ctx(tmp_path), name="Demo API", language="python")
    repo_dir = pathlib.Path(_ctx(tmp_path).drive_root) / "projects" / "demo-api"
    remote_dir = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote_dir)], cwd=repo_dir, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=remote_dir, check=True)

    _project_server_register(
        _ctx(tmp_path),
        name='demo-api',
        alias='prod',
        host='example.com',
        user='deploy',
        ssh_key_path='/tmp/id_demo',
        deploy_path='/srv/demo-api',
        label='Production',
    )

    gh_calls = []
    gh_state = {
        'issues': [],
        'pull_requests': [],
        'merged_numbers': [],
    }

    def fake_run_gh(args, cwd, timeout=120, input_data=None):
        gh_calls.append({'args': list(args), 'cwd': cwd, 'timeout': timeout, 'input': input_data})
        if args[:2] == ['issue', 'list']:
            return subprocess.CompletedProcess(['gh', *args], 0, stdout=json.dumps(gh_state['issues']), stderr='')
        if args[:2] == ['issue', 'create']:
            issue = {
                'number': 1,
                'title': next((a.split('=', 1)[1] for a in args if a.startswith('--title=')), ''),
                'state': 'OPEN',
                'url': 'https://github.com/acme/demo-api/issues/1',
                'author': {'login': 'veles'},
                'labels': [],
            }
            gh_state['issues'] = [issue]
            return subprocess.CompletedProcess(['gh', *args], 0, stdout=issue['url'] + '\n', stderr='')
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
            gh_state['pull_requests'] = [pr]
            return subprocess.CompletedProcess(['gh', *args], 0, stdout=pr['url'] + '\n', stderr='')
        if args[:2] == ['pr', 'list']:
            state_value = next((args[i + 1] for i, value in enumerate(args[:-1]) if value == '--state'), 'open')
            prs = gh_state['pull_requests']
            if state_value == 'open':
                prs = [pr for pr in prs if pr['state'] == 'OPEN']
            elif state_value == 'merged':
                prs = [pr for pr in prs if pr['state'] == 'MERGED']
            return subprocess.CompletedProcess(['gh', *args], 0, stdout=json.dumps(prs), stderr='')
        if args[:2] == ['pr', 'merge']:
            pr = gh_state['pull_requests'][0]
            pr['state'] = 'MERGED'
            gh_state['merged_numbers'].append(int(args[2]))
            subprocess.run(['git', 'checkout', 'main'], cwd=cwd, check=True)
            subprocess.run(['git', 'merge', '--ff-only', pr['headRefName']], cwd=cwd, check=True)
            subprocess.run(['git', 'push', 'origin', 'main'], cwd=cwd, check=True)
            return subprocess.CompletedProcess(['gh', *args], 0, stdout='merged\n', stderr='')
        raise AssertionError(f'unexpected gh args: {args}')

    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_gh', fake_run_gh)
    monkeypatch.setattr('ouroboros.tools.project_github_dev._project_github_slug', lambda repo_dir: 'acme/demo-api')
    monkeypatch.setattr('ouroboros.tools.project_read_side._project_github_slug', lambda repo_dir: 'acme/demo-api')

    def fake_run_project_gh_json(repo_dir, args, timeout=120):
        if args[:2] == ['issue', 'list']:
            return gh_state['issues']
        if args[:2] == ['pr', 'list']:
            state_value = next((args[i + 1] for i, value in enumerate(args[:-1]) if value == '--state'), 'open')
            prs = gh_state['pull_requests']
            if state_value == 'open':
                return [pr for pr in prs if pr['state'] == 'OPEN']
            if state_value == 'merged':
                return [pr for pr in prs if pr['state'] == 'MERGED']
            return prs
        raise AssertionError(f'unexpected gh json args: {args}')

    monkeypatch.setattr('ouroboros.tools.project_github_dev._run_project_gh_json', fake_run_project_gh_json)
    monkeypatch.setattr('ouroboros.tools.project_read_side._run_project_gh_json', fake_run_project_gh_json)

    branch_payload = json.loads(_project_branch_checkout(_ctx(tmp_path), name='demo-api', branch='feature/full-cycle', base='main'))
    assert branch_payload['branch']['created'] is True

    _project_file_write(_ctx(tmp_path), name='demo-api', path='README.md', content='# demo-api\n\nFull cycle smoke\n')
    commit_payload = json.loads(_project_commit(_ctx(tmp_path), name='demo-api', message='Add Stage 3 full cycle docs'))
    assert commit_payload['status'] == 'ok'

    push_payload = json.loads(_project_push(_ctx(tmp_path), name='demo-api', branch='feature/full-cycle'))
    assert push_payload['status'] == 'ok'

    issue_payload = json.loads(_project_issue_create(_ctx(tmp_path), name='demo-api', title='Ship Stage 3 full cycle', body='Need one end-to-end system contract'))
    assert issue_payload['status'] == 'ok'

    pr_payload = json.loads(_project_pr_create(_ctx(tmp_path), name='demo-api', title='Stage 3 full cycle smoke', body='Implements issue #1'))
    assert pr_payload['status'] == 'ok'

    merge_payload = json.loads(_project_pr_merge(_ctx(tmp_path), name='demo-api', number=7, method='squash', delete_branch=True))
    assert merge_payload['status'] == 'ok'

    fetch_payload = json.loads(_project_git_fetch(_ctx(tmp_path), name='demo-api'))
    assert fetch_payload['status'] == 'ok'

    compare_payload = json.loads(_project_branch_compare(_ctx(tmp_path), name='demo-api', branch='main'))
    assert compare_payload['branch']['ahead_behind']['available'] is True
    assert compare_payload['branch']['ahead_behind']['ahead'] == 0
    assert compare_payload['branch']['ahead_behind']['behind'] == 0

    status_payload = json.loads(_project_status(_ctx(tmp_path), name='demo-api'))
    assert status_payload['remote_awareness']['available'] is True
    assert status_payload['remote_awareness']['branch'] == 'main'
    assert status_payload['remote_awareness']['ahead_behind']['ahead'] == 0
    assert status_payload['remote_awareness']['ahead_behind']['behind'] == 0

    deploy_calls = []

    def fake_project_deploy_apply(ctx, **kwargs):
        deploy_calls.append(('deploy_apply', kwargs))
        return json.dumps({
            'status': 'ok',
            'project': {'name': 'demo-api', 'path': str(repo_dir)},
            'server': {'alias': 'prod'},
            'execution': {'failed_step': '', 'last_step_key': 'status', 'ok_steps': 5},
            'summary': {'service_name': 'demo-api'},
            'deploy_record': {
                'exists': True,
                'outcome': {
                    'status': 'ok',
                    'deploy': {
                        'mode': 'update',
                        'lifecycle_action': 'restart',
                        'execution': {'ok_steps': 5},
                    },
                },
            },
        })

    def fake_project_operational_snapshot(ctx, **kwargs):
        deploy_calls.append(('operational_snapshot', kwargs))
        return json.dumps({
            'status': 'ok',
            'project': {'name': 'demo-api', 'path': str(repo_dir)},
            'selection': {'alias': 'prod', 'service_name': 'demo-api', 'runtime_included': True},
            'readiness': {
                'local_clean': True,
                'github_ready': True,
                'deploy_target_ready': True,
                'service_running': True,
                'rollout_ready': True,
                'blocked_reasons': [],
            },
            'risk_flags': [],
            'next_actions': [],
            'runtime': {'diagnostics': {'severity': 'healthy'}},
            'github': {
                'configured': True,
                'available': True,
                'repo': 'acme/demo-api',
                'open_issue_count': 1,
                'open_pull_request_count': 0,
            },
            'last_deploy': {
                'status': 'ok',
                'deploy': {'execution': {'ok_steps': 5}},
            },
        })

    monkeypatch.setattr('ouroboros.tools.project_composite_flows._project_deploy_apply', fake_project_deploy_apply)
    monkeypatch.setattr('ouroboros.tools.project_composite_flows._project_operational_snapshot', fake_project_operational_snapshot)

    verify_payload = json.loads(
        _project_deploy_and_verify(
            _ctx(tmp_path),
            name='demo-api',
            alias='prod',
            service_name='demo-api',
            mode='update',
        )
    )

    assert verify_payload['status'] == 'ok'
    assert [step['tool'] for step in verify_payload['steps']] == ['project_deploy_apply', 'project_operational_snapshot']
    assert verify_payload['verdict']['healthy'] is True
    assert verify_payload['verdict']['rollout_ready'] is True
    assert verify_payload['verdict']['service_running'] is True
    assert verify_payload['verification']['github']['open_pull_request_count'] == 0
    assert verify_payload['verification']['github']['open_issue_count'] == 1
    assert verify_payload['verification']['last_deploy']['deploy']['execution']['ok_steps'] == 5

    gh_pairs = [(call['args'][0], call['args'][1]) for call in gh_calls]
    assert ('issue', 'create') in gh_pairs
    assert ('pr', 'create') in gh_pairs
    assert ('pr', 'merge') in gh_pairs
    assert gh_state['merged_numbers'] == [7]
    assert [name for name, _ in deploy_calls] == ['deploy_apply', 'operational_snapshot']
