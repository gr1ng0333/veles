import json
import pathlib

from ouroboros.tools.external_repo_github import _external_repo_memory_append_note, _external_repo_memory_get, _external_repo_memory_update, _github_repo_slug
from ouroboros.tools.registry import ToolContext



def _register_repo(tmp_path: pathlib.Path, alias: str = 'demo') -> pathlib.Path:
    repo_dir = tmp_path / 'repos' / alias
    repo_dir.mkdir(parents=True)
    (tmp_path / 'state').mkdir(exist_ok=True)
    import subprocess
    subprocess.run(['git', 'init', '-b', 'main'], cwd=repo_dir, check=True, capture_output=True, text=True)
    subprocess.run(['git', 'remote', 'add', 'origin', 'git@github.com:gr1ng0333/avito-sniper-bot.git'], cwd=repo_dir, check=True, capture_output=True, text=True)
    registry = tmp_path / 'state' / 'external_repos.json'
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({'repos': {alias: {'path': str(repo_dir), 'origin': 'git@github.com:gr1ng0333/avito-sniper-bot.git'}}}), encoding='utf-8')
    return repo_dir


def test_github_repo_slug_supports_https_and_ssh():
    assert _github_repo_slug('git@github.com:gr1ng0333/avito-sniper-bot.git') == 'gr1ng0333/avito-sniper-bot'
    assert _github_repo_slug('https://github.com/gr1ng0333/avito-sniper-bot.git') == 'gr1ng0333/avito-sniper-bot'


def test_external_repo_memory_template_create_update_append(tmp_path):
    _register_repo(tmp_path)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    initial = _external_repo_memory_get(ctx, 'demo')
    assert '# External Repo Memory — demo' in initial
    assert 'gr1ng0333/avito-sniper-bot' in initial

    payload = '# External Repo Memory — demo\n\n## Project Summary\n- test payload\n'
    result = json.loads(_external_repo_memory_update(ctx, 'demo', payload))
    assert result['status'] == 'ok'

    _external_repo_memory_append_note(ctx, 'demo', 'checked worktree')
    stored = _external_repo_memory_get(ctx, 'demo')
    assert 'test payload' in stored
    assert 'checked worktree' in stored
