from pathlib import Path
from subprocess import CompletedProcess

from ouroboros.tools.git import _acquire_copilot_write_lock, _release_copilot_write_lock
from ouroboros.tools.registry import ToolContext
from supervisor import git_ops


def _ctx(tmp_path: Path, *, transport: str = 'copilot', task_id: str = 'task-1') -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path, write_transport=transport, task_id=task_id)


def test_copilot_write_lock_is_transport_scoped(tmp_path):
    ctx = _ctx(tmp_path, transport='codex')
    msg = _acquire_copilot_write_lock(ctx)
    assert msg is None
    assert not (tmp_path / 'locks' / 'copilot-write.lock').exists()


def test_copilot_write_lock_acquire_and_release(tmp_path):
    ctx = _ctx(tmp_path, transport='copilot', task_id='copilot-task')
    msg = _acquire_copilot_write_lock(ctx)
    lock_path = tmp_path / 'locks' / 'copilot-write.lock'
    assert msg is None
    assert lock_path.exists()
    assert ctx.copilot_write_lock_acquired is True

    _release_copilot_write_lock(ctx)
    assert not lock_path.exists()
    assert ctx.copilot_write_lock_acquired is False


def test_ensure_copilot_rescue_ref_if_needed_skips_clean_repo(monkeypatch):
    monkeypatch.setattr(
        git_ops,
        '_collect_copilot_rescue_repo_state',
        lambda: {'dirty_lines': [], 'unpushed_lines': [], 'warnings': []},
    )

    ok, ref_or_err, pushed = git_ops.ensure_copilot_rescue_ref_if_needed(task_id='task-1', reason='clean')
    assert ok is True
    assert ref_or_err == ''
    assert pushed is False


def test_ensure_copilot_rescue_ref_if_needed_pushes_when_dirty(monkeypatch):
    monkeypatch.setattr(
        git_ops,
        '_collect_copilot_rescue_repo_state',
        lambda: {'dirty_lines': [' M ouroboros/agent.py'], 'unpushed_lines': [], 'warnings': ['dirty_working_tree']},
    )
    monkeypatch.setattr(
        git_ops,
        '_push_copilot_rescue_ref',
        lambda task_id, reason='': (True, 'refs/veles-rescue/task-1/20260328T000000Z'),
    )

    ok, ref_or_err, pushed = git_ops.ensure_copilot_rescue_ref_if_needed(task_id='task-1', reason='dirty')
    assert ok is True
    assert ref_or_err == 'refs/veles-rescue/task-1/20260328T000000Z'
    assert pushed is True


def test_push_copilot_rescue_ref_pushes_snapshot_commit_not_tag(tmp_path, monkeypatch):
    rescue_dir = tmp_path / 'rescue-snapshot'
    rescue_dir.mkdir(parents=True)
    (rescue_dir / 'changes.diff').write_text('diff --git a/x b/x\n', encoding='utf-8')
    nested = rescue_dir / 'untracked'
    nested.mkdir()
    (nested / 'note.txt').write_text('hello', encoding='utf-8')

    commands = []

    def fake_run(cmd, cwd=None, capture_output=None, text=None):
        commands.append(cmd)
        if cmd[:4] == ['git', 'remote', 'get-url', 'origin']:
            return CompletedProcess(cmd, 0, stdout='git@github.com:gr1ng0333/veles.git\n', stderr='')
        if cmd[:3] == ['git', 'rev-parse', 'HEAD']:
            return CompletedProcess(cmd, 0, stdout='abc123\n', stderr='')
        return CompletedProcess(cmd, 0, stdout='', stderr='')

    monkeypatch.setattr(git_ops, '_collect_copilot_rescue_repo_state', lambda: {
        'current_branch': 'veles',
        'dirty_lines': [' M ouroboros/agent.py'],
        'unpushed_lines': ['abc123 local commit'],
        'warnings': ['dirty_working_tree', 'unpushed_commits'],
    })
    monkeypatch.setattr(git_ops, '_create_rescue_snapshot', lambda branch, reason, repo_state: {'path': str(rescue_dir)})
    monkeypatch.setattr(git_ops.subprocess, 'run', fake_run)
    monkeypatch.setattr(git_ops, 'append_jsonl', lambda *args, **kwargs: None)

    ok, ref = git_ops._push_copilot_rescue_ref(task_id='task-1', reason='unit-test')
    assert ok is True
    assert ref.startswith('refs/veles-rescue/task-1/')
    assert any(cmd[:2] == ['git', 'init'] for cmd in commands)
    assert any(cmd[:2] == ['git', 'commit'] for cmd in commands)
    assert any(cmd[:2] == ['git', 'push'] and any(part.startswith('HEAD:refs/veles-rescue/task-1/') for part in cmd) for cmd in commands)
    assert not any(cmd[:2] == ['git', 'tag'] for cmd in commands)
