from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
from typing import Any, Dict, List

from ouroboros.utils import safe_relpath


from ouroboros.tools.external_repos import _ensure_external_repo_git_identity, _tool_entry
from ouroboros.tools.registry import ToolContext, ToolEntry

_DEFAULT_PROJECTS_ROOT = "/opt/repos"
_ALLOWED_LANGUAGES = {"python", "node", "static"}
_MAX_WRITE_CHARS = 500_000
_DEFAULT_READ_PREVIEW_CHARS = 20_000
_SERVER_REGISTRY_DIR = ".veles"
_SERVER_REGISTRY_FILE = "servers.json"

_DEFAULT_SERVER_RUN_TIMEOUT = 60
_MAX_SERVER_RUN_OUTPUT_CHARS = 20_000


def _find_project_server(repo_dir: pathlib.Path, alias: str) -> Dict[str, Any]:
    alias_name = _normalize_server_alias(alias)
    for item in _load_project_server_registry(repo_dir):
        if item.get('alias') == alias_name:
            return item
    raise ValueError(f"project server alias not found: {alias_name}")


def _clip_server_run_output(content: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ''
    return _clip_project_read_content(content, max_chars)


def _run_ssh(args: List[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["ssh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise RuntimeError("ssh client not found on VPS") from e


def _project_server_run(
    ctx: ToolContext,
    name: str,
    alias: str,
    command: str,
    timeout: int = _DEFAULT_SERVER_RUN_TIMEOUT,
    max_output_chars: int = _MAX_SERVER_RUN_OUTPUT_CHARS,
) -> str:
    repo_dir = _require_local_project(name)
    project_name = _normalize_project_name(name)
    server = _find_project_server(repo_dir, alias)

    raw_command = str(command or '').strip()
    if not raw_command:
        raise ValueError('command must be non-empty')

    try:
        timeout_value = int(timeout)
    except (TypeError, ValueError) as e:
        raise ValueError('timeout must be an integer') from e
    if timeout_value <= 0:
        raise ValueError('timeout must be > 0')

    try:
        max_chars = int(max_output_chars)
    except (TypeError, ValueError) as e:
        raise ValueError('max_output_chars must be an integer') from e
    if max_chars <= 0:
        raise ValueError('max_output_chars must be > 0')

    ssh_args = [
        '-i', server['ssh_key_path'],
        '-p', str(server['port']),
        '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', 'IdentitiesOnly=yes',
        f"{server['user']}@{server['host']}",
        '--',
        raw_command,
    ]
    res = _run_ssh(ssh_args, timeout=timeout_value)
    stdout = res.stdout or ''
    stderr = res.stderr or ''
    combined = stdout + stderr
    clipped = _clip_server_run_output(combined, max_chars)

    payload = {
        'status': 'ok' if res.returncode == 0 else 'error',
        'executed_at': _utc_now_iso(),
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'server': _public_server_view(server),
        'command': {
            'raw': raw_command,
            'transport': 'ssh',
            'timeout_seconds': timeout_value,
        },
        'result': {
            'ok': res.returncode == 0,
            'exit_code': res.returncode,
            'stdout': _clip_server_run_output(stdout, max_chars),
            'stderr': _clip_server_run_output(stderr, max_chars),
            'output': clipped,
            'truncated': clipped != combined,
            'max_output_chars': max_chars,
        },
        'repo': _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)




def _project_veles_dir(repo_dir: pathlib.Path) -> pathlib.Path:
    return repo_dir / _SERVER_REGISTRY_DIR


def _project_server_registry_path(repo_dir: pathlib.Path) -> pathlib.Path:
    return _project_veles_dir(repo_dir) / _SERVER_REGISTRY_FILE


def _load_project_server_registry(repo_dir: pathlib.Path) -> List[Dict[str, Any]]:
    path = _project_server_registry_path(repo_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"project server registry is invalid JSON: {path}") from e
    if not isinstance(data, list):
        raise ValueError("project server registry must be a JSON list")
    normalized: List[Dict[str, Any]] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"project server registry entry #{idx} must be an object")
        normalized.append(item)
    return normalized


def _save_project_server_registry(repo_dir: pathlib.Path, servers: List[Dict[str, Any]]) -> None:
    registry_path = _project_server_registry_path(repo_dir)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(servers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _normalize_server_alias(alias: str) -> str:
    raw = str(alias or "").strip().lower()
    if not raw:
        raise ValueError("server alias must be non-empty")
    if raw in {'.', '..'}:
        raise ValueError("invalid server alias")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", raw):
        raise ValueError("server alias may contain only lowercase letters, digits, dot, underscore, and dash")
    return raw


def _normalize_server_host(host: str) -> str:
    raw = str(host or "").strip()
    if not raw:
        raise ValueError("host must be non-empty")
    if any(ch.isspace() for ch in raw):
        raise ValueError("host must not contain whitespace")
    if raw in {'.', '..'} or '/' in raw or '@' in raw:
        raise ValueError("host must be a plain hostname or IP, without path/user prefix")
    return raw


def _normalize_server_user(user: str) -> str:
    raw = str(user or "").strip()
    if not raw:
        raise ValueError("user must be non-empty")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9._-]*", raw):
        raise ValueError("user contains unsupported characters")
    return raw


def _normalize_server_port(port: Any) -> int:
    try:
        value = int(port)
    except (TypeError, ValueError) as e:
        raise ValueError("port must be an integer") from e
    if value < 1 or value > 65535:
        raise ValueError("port must be between 1 and 65535")
    return value


def _normalize_server_auth(auth: str) -> str:
    raw = str(auth or "ssh_key_path").strip().lower()
    if raw != 'ssh_key_path':
        raise ValueError("auth must currently be 'ssh_key_path'")
    return raw


def _normalize_server_ssh_key_path(ssh_key_path: str) -> str:
    raw = str(ssh_key_path or '').strip()
    if not raw:
        raise ValueError("ssh_key_path must be non-empty")
    expanded = pathlib.Path(raw).expanduser()
    if not expanded.is_absolute():
        raise ValueError("ssh_key_path must be absolute or use ~/...")
    return str(expanded)


def _normalize_server_deploy_path(deploy_path: str) -> str:
    raw = str(deploy_path or '').strip()
    if not raw:
        raise ValueError("deploy_path must be non-empty")
    if not raw.startswith('/'):
        raise ValueError("deploy_path must be absolute")
    normalized = re.sub(r'/+', '/', raw)
    if normalized in {'/', '/.', '/..'}:
        raise ValueError("deploy_path must not point to filesystem root")
    return normalized.rstrip('/') or '/'


def _normalize_server_label(label: str) -> str:
    return str(label or '').strip()


def _public_server_view(server: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'alias': server['alias'],
        'label': server.get('label') or '',
        'host': server['host'],
        'port': server['port'],
        'user': server['user'],
        'auth': server['auth'],
        'ssh_key_path': server['ssh_key_path'],
        'deploy_path': server['deploy_path'],
        'created_at': server['created_at'],
        'updated_at': server.get('updated_at') or server['created_at'],
    }




def _utc_now_iso() -> str:
    return __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()


def _projects_root() -> pathlib.Path:
    root = pathlib.Path(os.getenv("VELES_PROJECTS_ROOT", _DEFAULT_PROJECTS_ROOT)).expanduser()
    return root.resolve()


def _normalize_project_name(name: str) -> str:
    raw = str(name or "").strip().lower()
    if not raw:
        raise ValueError("name must be non-empty")
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw)
    slug = re.sub(r"-{2,}", "-", slug).strip("-._")
    if not slug:
        raise ValueError("name must contain at least one latin letter or digit")
    if slug in {".", ".."}:
        raise ValueError("invalid project name")
    return slug


def _normalize_language(language: str) -> str:
    lang = str(language or "").strip().lower()
    if lang not in _ALLOWED_LANGUAGES:
        raise ValueError(f"language must be one of: {', '.join(sorted(_ALLOWED_LANGUAGES))}")
    return lang


def _project_dir(name: str) -> pathlib.Path:
    return (_projects_root() / _normalize_project_name(name)).resolve()


def _write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _clip_project_read_content(content: str, max_chars: int) -> str:
    marker = "...(truncated)..."
    if max_chars <= 0 or len(content) <= max_chars:
        return content
    if max_chars <= len(marker) + 2:
        return content[:max_chars]
    available = max_chars - len(marker)
    head = max(1, available // 2)
    tail = max(1, available - head)
    return content[:head] + marker + content[-tail:]


def _python_template(project_name: str, description: str) -> Dict[str, str]:
    package = project_name.replace("-", "_").replace(".", "_")
    return {
        "README.md": f"# {project_name}\n\n{description or 'New Python project bootstrapped by Veles.'}\n\n## Run\n\n```bash\npython -m src.{package}.main\n```\n",
        ".gitignore": "__pycache__/\n*.pyc\n.venv/\nvenv/\n.env\n.pytest_cache/\n.dist/\nbuild/\n",
        "requirements.txt": "",
        f"src/{package}/__init__.py": "",
        f"src/{package}/main.py": (
            'def main() -> None:\n'
            f'    print("{project_name} is alive")\n\n\n'
            'if __name__ == "__main__":\n'
            '    main()\n'
        ),
    }


def _node_template(project_name: str, description: str) -> Dict[str, str]:
    package_json = {
        "name": project_name,
        "version": "0.1.0",
        "private": True,
        "description": description or "New Node project bootstrapped by Veles.",
        "main": "src/index.js",
        "scripts": {"start": "node src/index.js"},
    }
    return {
        "README.md": f"# {project_name}\n\n{description or 'New Node project bootstrapped by Veles.'}\n\n## Run\n\n```bash\nnpm start\n```\n",
        ".gitignore": "node_modules/\n.env\ndist/\nbuild/\ncoverage/\n",
        "package.json": json.dumps(package_json, ensure_ascii=False, indent=2) + "\n",
        "src/index.js": f"console.log('{project_name} is alive');\n",
    }


def _static_template(project_name: str, description: str) -> Dict[str, str]:
    title = project_name.replace("-", " ").title()
    body = description or "Static site bootstrapped by Veles."
    return {
        "README.md": f"# {project_name}\n\n{body}\n\nOpen `index.html` in a browser or deploy it as a static site.\n",
        ".gitignore": ".DS_Store\n.env\n",
        "index.html": (
            "<!doctype html>\n"
            "<html lang='en'>\n"
            "<head>\n"
            "  <meta charset='utf-8'>\n"
            "  <meta name='viewport' content='width=device-width, initial-scale=1'>\n"
            f"  <title>{title}</title>\n"
            "  <link rel='stylesheet' href='styles.css'>\n"
            "</head>\n"
            "<body>\n"
            f"  <main><h1>{title}</h1><p>{body}</p></main>\n"
            "</body>\n"
            "</html>\n"
        ),
        "styles.css": "body { font-family: Arial, sans-serif; margin: 40px; color: #111; }\nmain { max-width: 720px; }\n",
    }


def _template_files(project_name: str, language: str, description: str) -> Dict[str, str]:
    if language == "python":
        return _python_template(project_name, description)
    if language == "node":
        return _node_template(project_name, description)
    return _static_template(project_name, description)


def _git(args: List[str], cwd: pathlib.Path, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _repo_info(repo_dir: pathlib.Path) -> Dict[str, str]:
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_dir).stdout.strip()
    sha = _git(["rev-parse", "HEAD"], repo_dir).stdout.strip()
    return {"path": str(repo_dir), "branch": branch, "sha": sha}


def _git_lines(repo_dir: pathlib.Path, args: List[str], timeout: int = 30) -> List[str]:
    res = _git(args, repo_dir, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or f"git {' '.join(args)} failed")
    return [line for line in (res.stdout or "").splitlines() if line.strip()]


def _project_server_register(
    ctx: ToolContext,
    name: str,
    alias: str,
    host: str,
    user: str,
    ssh_key_path: str,
    deploy_path: str,
    port: int = 22,
    label: str = "",
    auth: str = "ssh_key_path",
) -> str:
    repo_dir = _require_local_project(name)
    project_name = _normalize_project_name(name)
    alias_name = _normalize_server_alias(alias)
    host_name = _normalize_server_host(host)
    user_name = _normalize_server_user(user)
    port_number = _normalize_server_port(port)
    auth_kind = _normalize_server_auth(auth)
    key_path = _normalize_server_ssh_key_path(ssh_key_path)
    target_path = _normalize_server_deploy_path(deploy_path)
    server_label = _normalize_server_label(label)

    servers = _load_project_server_registry(repo_dir)
    now = _utc_now_iso()
    existing = None
    for item in servers:
        if item.get('alias') == alias_name:
            existing = item
            break

    if existing is None:
        existing = {
            'alias': alias_name,
            'created_at': now,
        }
        servers.append(existing)

    existing.update({
        'label': server_label,
        'host': host_name,
        'port': port_number,
        'user': user_name,
        'auth': auth_kind,
        'ssh_key_path': key_path,
        'deploy_path': target_path,
        'updated_at': now,
    })

    servers.sort(key=lambda item: item.get('alias', ''))
    _save_project_server_registry(repo_dir, servers)

    payload = {
        'status': 'ok',
        'registered_at': now,
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'server': _public_server_view(existing),
        'registry': {
            'path': str(_project_server_registry_path(repo_dir)),
            'count': len(servers),
            'aliases': [item.get('alias') for item in servers],
        },
        'repo': _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)



def _project_server_list(ctx: ToolContext, name: str) -> str:
    repo_dir = _require_local_project(name)
    project_name = _normalize_project_name(name)
    servers = _load_project_server_registry(repo_dir)

    payload = {
        'status': 'ok',
        'listed_at': _utc_now_iso(),
        'project': {
            'name': project_name,
            'path': str(repo_dir),
        },
        'registry': {
            'path': str(_project_server_registry_path(repo_dir)),
            'count': len(servers),
            'aliases': [item.get('alias') for item in servers],
            'exists': _project_server_registry_path(repo_dir).exists(),
        },
        'servers': [_public_server_view(item) for item in servers],
        'repo': _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _project_status(ctx: ToolContext, name: str) -> str:
    repo_dir = _require_local_project(name)
    project_name = _normalize_project_name(name)

    status_lines = _git_lines(repo_dir, ["status", "--porcelain", "--untracked-files=all"])
    remotes = []
    for line in _git_lines(repo_dir, ["remote", "-v"]):
        parts = line.split()
        if len(parts) >= 3:
            remotes.append({
                "name": parts[0],
                "url": parts[1],
                "direction": parts[2].strip("()"),
            })

    latest_commit = {"sha": None, "subject": None}
    latest_sha_lines = _git_lines(repo_dir, ["rev-parse", "HEAD"])
    latest_subject_lines = _git_lines(repo_dir, ["log", "-1", "--pretty=%s"])
    if latest_sha_lines:
        latest_commit["sha"] = latest_sha_lines[0]
    if latest_subject_lines:
        latest_commit["subject"] = latest_subject_lines[0]

    changes = []
    staged = 0
    unstaged = 0
    untracked = 0
    for line in status_lines:
        if len(line) < 4:
            continue
        index_flag = line[0]
        worktree_flag = line[1]
        rel_path = line[3:]
        if index_flag == '?' and worktree_flag == '?':
            kind = 'untracked'
            untracked += 1
        elif index_flag != ' ' and index_flag != '?':
            kind = 'staged'
            staged += 1
        else:
            kind = 'unstaged'
            unstaged += 1
        changes.append({
            "path": rel_path,
            "index": index_flag,
            "worktree": worktree_flag,
            "kind": kind,
        })

    payload = {
        "status": "ok",
        "checked_at": _utc_now_iso(),
        "project": {
            "name": project_name,
            "path": str(repo_dir),
        },
        "repo": _repo_info(repo_dir),
        "working_tree": {
            "clean": len(changes) == 0,
            "counts": {
                "total": len(changes),
                "staged": staged,
                "unstaged": unstaged,
                "untracked": untracked,
            },
            "changes": changes,
        },
        "latest_commit": latest_commit,
        "remotes": remotes,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _require_local_project(name: str) -> pathlib.Path:
    repo_dir = _project_dir(name)
    if not repo_dir.exists():
        raise ValueError(f"project does not exist: {repo_dir}")
    if not (repo_dir / ".git").exists():
        raise ValueError(f"project is not a git repository: {repo_dir}")
    return repo_dir


def _git_remote_url(repo_dir: pathlib.Path, remote: str = "origin") -> str:
    res = _git(["remote", "get-url", remote], repo_dir, timeout=30)
    if res.returncode != 0:
        return ""
    return (res.stdout or "").strip()


def _normalize_github_repo_name(name: str) -> str:
    raw = str(name or "").strip()
    if not raw:
        raise ValueError("github repo name must be non-empty")
    if "/" in raw or raw in {".", ".."}:
        raise ValueError("github repo name must not contain path separators")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", raw):
        raise ValueError("github repo name may contain only letters, digits, dot, underscore, and dash")
    return raw


def _normalize_github_owner(owner: str) -> str:
    raw = str(owner or "").strip()
    if not raw:
        return ""
    if "/" in raw or not re.fullmatch(r"[A-Za-z0-9-]+", raw):
        raise ValueError("github owner may contain only letters, digits, and dash")
    return raw


def _run_gh(
    args: List[str],
    cwd: pathlib.Path,
    timeout: int = 120,
    input_data: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["gh", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_data,
        )
    except FileNotFoundError as e:
        raise RuntimeError("gh CLI not found on VPS") from e


def _project_file_read(ctx: ToolContext, name: str, path: str, max_chars: int = _DEFAULT_READ_PREVIEW_CHARS) -> str:
    repo_dir = _require_local_project(name)
    rel = safe_relpath(path)
    target = (repo_dir / rel).resolve()
    try:
        target.relative_to(repo_dir.resolve())
    except ValueError as e:
        raise ValueError(f"path escapes project repository: {path}") from e
    if not target.exists() or not target.is_file():
        raise ValueError(f"project file does not exist: {rel}")

    try:
        requested_max = int(max_chars)
    except (TypeError, ValueError) as e:
        raise ValueError('max_chars must be an integer') from e
    if requested_max <= 0:
        raise ValueError('max_chars must be > 0')

    content = target.read_text(encoding='utf-8')
    preview = _clip_project_read_content(content, requested_max)
    payload = {
        'status': 'ok',
        'read_at': _utc_now_iso(),
        'project': {
            'name': _normalize_project_name(name),
            'path': str(repo_dir),
        },
        'file': {
            'path': rel,
            'bytes': target.stat().st_size,
            'chars': len(content),
            'truncated': preview != content,
            'max_chars': requested_max,
        },
        'content': preview,
        'repo': _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _project_file_write(ctx: ToolContext, name: str, path: str, content: str) -> str:
    repo_dir = _require_local_project(name)
    rel = safe_relpath(path)
    target = (repo_dir / rel).resolve()
    try:
        target.relative_to(repo_dir.resolve())
    except ValueError as e:
        raise ValueError(f"path escapes project repository: {path}") from e
    if len(content) > _MAX_WRITE_CHARS:
        raise ValueError(f"content too large (> {_MAX_WRITE_CHARS} chars)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    payload = {
        "status": "ok",
        "written_at": _utc_now_iso(),
        "project": {
            "name": _normalize_project_name(name),
            "path": str(repo_dir),
        },
        "file": {
            "path": rel,
            "bytes_written": len(content.encode("utf-8")),
        },
        "repo": _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _normalize_commit_message(message: str) -> str:
    normalized = str(message or "").strip()
    if not normalized:
        raise ValueError("commit message must be non-empty")
    return normalized


def _project_commit(ctx: ToolContext, name: str, message: str) -> str:
    repo_dir = _require_local_project(name)
    commit_message = _normalize_commit_message(message)

    status_res = _git(["status", "--porcelain"], repo_dir, timeout=30)
    if status_res.returncode != 0:
        raise RuntimeError(status_res.stderr.strip() or status_res.stdout.strip() or "git status failed")
    status_lines = [line for line in (status_res.stdout or "").splitlines() if line.strip()]
    if not status_lines:
        raise ValueError("project has no changes to commit")

    _ensure_external_repo_git_identity(repo_dir)

    add_res = _git(["add", "-A"], repo_dir, timeout=60)
    if add_res.returncode != 0:
        raise RuntimeError(add_res.stderr.strip() or add_res.stdout.strip() or "git add failed")

    commit_res = _git(["commit", "-m", commit_message], repo_dir, timeout=60)
    if commit_res.returncode != 0:
        raise RuntimeError(commit_res.stderr.strip() or commit_res.stdout.strip() or "git commit failed")

    payload = {
        "status": "ok",
        "committed_at": _utc_now_iso(),
        "project": {
            "name": _normalize_project_name(name),
            "path": str(repo_dir),
        },
        "commit_message": commit_message,
        "changes": {
            "count": len(status_lines),
            "paths": [line[3:] for line in status_lines if len(line) > 3],
        },
        "repo": _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)



def _project_push(ctx: ToolContext, name: str, remote: str = "origin", branch: str = "") -> str:
    repo_dir = _require_local_project(name)
    remote_name = str(remote or "").strip()
    if not remote_name:
        raise ValueError("remote must be non-empty")
    remote_url = _git_remote_url(repo_dir, remote_name)
    if not remote_url:
        raise ValueError(f"project has no {remote_name} remote configured")

    branch_name = str(branch or "").strip()
    if not branch_name:
        branch_res = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_dir, timeout=30)
        if branch_res.returncode != 0:
            raise RuntimeError(branch_res.stderr.strip() or branch_res.stdout.strip() or "git rev-parse failed")
        branch_name = (branch_res.stdout or "").strip()
    if not branch_name:
        raise RuntimeError("could not determine branch to push")

    push_res = _git(["push", remote_name, branch_name], repo_dir, timeout=180)
    if push_res.returncode != 0:
        raise RuntimeError(push_res.stderr.strip() or push_res.stdout.strip() or "git push failed")

    payload = {
        "status": "ok",
        "pushed_at": _utc_now_iso(),
        "project": {
            "name": _normalize_project_name(name),
            "path": str(repo_dir),
        },
        "push": {
            "remote": remote_name,
            "branch": branch_name,
            "remote_url": remote_url,
        },
        "repo": _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _project_github_create(
    ctx: ToolContext,
    name: str,
    github_name: str = "",
    owner: str = "",
    private: bool = True,
    description: str = "",
) -> str:
    repo_dir = _require_local_project(name)
    remote_url = _git_remote_url(repo_dir)
    if remote_url:
        raise ValueError(f"project already has origin remote: {remote_url}")

    project_name = _normalize_project_name(name)
    repo_name = _normalize_github_repo_name(github_name or project_name)
    owner_name = _normalize_github_owner(owner)
    repo_slug = f"{owner_name}/{repo_name}" if owner_name else repo_name
    visibility_flag = "--private" if bool(private) else "--public"

    args = [
        "repo",
        "create",
        repo_slug,
        "--source",
        str(repo_dir),
        "--remote",
        "origin",
        "--push",
        visibility_flag,
    ]
    desc = str(description or "").strip()
    if desc:
        args.extend(["--description", desc])

    create_res = _run_gh(args, cwd=repo_dir, timeout=180)
    if create_res.returncode != 0:
        raise RuntimeError(create_res.stderr.strip() or create_res.stdout.strip() or "gh repo create failed")

    remote_url = _git_remote_url(repo_dir)
    if not remote_url:
        raise RuntimeError("origin remote missing after gh repo create")

    payload = {
        "status": "ok",
        "created_at": _utc_now_iso(),
        "project": {
            "name": project_name,
            "path": str(repo_dir),
        },
        "github": {
            "owner": owner_name or None,
            "name": repo_name,
            "slug": repo_slug,
            "private": bool(private),
            "description": desc,
            "remote": remote_url,
        },
        "repo": _repo_info(repo_dir),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _project_init(ctx: ToolContext, name: str, language: str, description: str = "") -> str:
    project_name = _normalize_project_name(name)
    project_language = _normalize_language(language)
    repo_dir = _project_dir(project_name)
    if repo_dir.exists():
        raise ValueError(f"project already exists: {repo_dir}")

    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    files = _template_files(project_name, project_language, str(description or "").strip())
    for rel_path, content in files.items():
        _write_text(repo_dir / rel_path, content)

    init_res = _git(["init", "-b", "main"], repo_dir, timeout=60)
    if init_res.returncode != 0:
        raise RuntimeError(init_res.stderr.strip() or init_res.stdout.strip() or "git init failed")

    git_identity = _ensure_external_repo_git_identity(repo_dir)

    add_res = _git(["add", "."], repo_dir, timeout=60)
    if add_res.returncode != 0:
        raise RuntimeError(add_res.stderr.strip() or add_res.stdout.strip() or "git add failed")

    commit_message = f"Bootstrap {project_name}"
    commit_res = _git(["commit", "-m", commit_message], repo_dir, timeout=60)
    if commit_res.returncode != 0:
        raise RuntimeError(commit_res.stderr.strip() or commit_res.stdout.strip() or "git commit failed")

    payload = {
        "status": "ok",
        "created_at": _utc_now_iso(),
        "project": {
            "name": project_name,
            "language": project_language,
            "description": str(description or "").strip(),
        },
        "repo": _repo_info(repo_dir),
        "files_created": sorted(files.keys()),
        "commit_message": commit_message,
        "git_identity": git_identity,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)



def get_tools() -> List[ToolEntry]:
    from ouroboros.tools.project_github_dev import get_tools as get_project_github_dev_tools

    return [
        _tool_entry(
            "project_init",
            "Create a brand-new local project repository from a minimal template and make the first git commit.",
            {
                "name": {"type": "string", "description": "Project name/slug; becomes directory name under the projects root"},
                "language": {"type": "string", "description": "Project template language", "enum": ["python", "node", "static"]},
                "description": {"type": "string", "description": "Optional short project description"},
            },
            ["name", "language"],
            _project_init,
            is_code_tool=True,
        ),
        _tool_entry(
            "project_github_create",
            "Create a GitHub repository for an existing bootstrapped local project, attach origin, and push the current branch.",
            {
                "name": {"type": "string", "description": "Existing local project name under the projects root"},
                "github_name": {"type": "string", "description": "Optional GitHub repository name; defaults to the local project slug"},
                "owner": {"type": "string", "description": "Optional GitHub owner/org; empty means current gh account default"},
                "private": {"type": "boolean", "description": "Whether to create the GitHub repository as private", "default": True},
                "description": {"type": "string", "description": "Optional GitHub repository description"},
            },
            ["name"],
            _project_github_create,
            is_code_tool=True,
        ),
        _tool_entry(
            "project_file_read",
            "Read a UTF-8 text file from an existing bootstrapped local project repository.",
            {
                "name": {"type": "string", "description": "Existing local project name under the projects root"},
                "path": {"type": "string", "description": "Relative file path inside the project repository"},
                "max_chars": {"type": "integer", "description": "Optional maximum number of characters to return before clipping", "default": _DEFAULT_READ_PREVIEW_CHARS},
            },
            ["name", "path"],
            _project_file_read,
            is_code_tool=True,
        ),
        _tool_entry(
            "project_file_write",
            "Write a UTF-8 text file inside an existing bootstrapped local project repository.",
            {
                "name": {"type": "string", "description": "Existing local project name under the projects root"},
                "path": {"type": "string", "description": "Relative file path inside the project repository"},
                "content": {"type": "string", "description": "Full UTF-8 file content to write"},
            },
            ["name", "path", "content"],
            _project_file_write,
            is_code_tool=True,
        ),
        _tool_entry(
            "project_commit",
            "Commit all current changes inside an existing bootstrapped local project repository.",
            {
                "name": {"type": "string", "description": "Existing local project name under the projects root"},
                "message": {"type": "string", "description": "Git commit message for the project-local changes"},
            },
            ["name", "message"],
            _project_commit,
            is_code_tool=True,
        ),
        _tool_entry(
            "project_push",
            "Push the current branch of an existing bootstrapped local project repository to a configured git remote.",
            {
                "name": {"type": "string", "description": "Existing local project name under the projects root"},
                "remote": {"type": "string", "description": "Git remote to push to", "default": "origin"},
                "branch": {"type": "string", "description": "Optional branch name to push; defaults to current HEAD branch"},
            },
            ["name"],
            _project_push,
            is_code_tool=True,
        ),
        _tool_entry(
            "project_status",
            "Return an honest git status snapshot for an existing bootstrapped local project repository, including branch, HEAD, remotes, and current working tree changes.",
            {
                "name": {"type": "string", "description": "Existing local project name under the projects root"},
            },
            ["name"],
            _project_status,
            is_code_tool=True,
        ),
        _tool_entry(
            "project_server_list",
            "List registered deploy server targets for an existing bootstrapped local project repository from the project-local .veles server registry.",
            {
                "name": {"type": "string", "description": "Existing local project name under the projects root"},
            },
            ["name"],
            _project_server_list,
            is_code_tool=True,
        ),
        _tool_entry(
            "project_server_run",
            "Run a command over SSH on a previously registered project server target, using the project-local server registry alias instead of raw host arguments.",
            {
                "name": {"type": "string", "description": "Existing local project name under the projects root"},
                "alias": {"type": "string", "description": "Registered server alias from the project-local .veles server registry"},
                "command": {"type": "string", "description": "Shell command to execute remotely over SSH"},
                "timeout": {"type": "integer", "description": "SSH command timeout in seconds", "default": _DEFAULT_SERVER_RUN_TIMEOUT},
                "max_output_chars": {"type": "integer", "description": "Maximum combined stdout/stderr characters to return before clipping", "default": _MAX_SERVER_RUN_OUTPUT_CHARS},
            },
            ["name", "alias", "command"],
            _project_server_run,
            is_code_tool=True,
        ),
        _tool_entry(
            "project_server_register",
            "Register or update a deploy server target for an existing bootstrapped local project repository, storing validated SSH target metadata in the project-local .veles server registry.",
            {
                "name": {"type": "string", "description": "Existing local project name under the projects root"},
                "alias": {"type": "string", "description": "Stable short name for this server target inside the project"},
                "host": {"type": "string", "description": "Plain hostname or IP address of the target server"},
                "user": {"type": "string", "description": "SSH username for the target server"},
                "ssh_key_path": {"type": "string", "description": "Absolute path (or ~/...) to the SSH private key on the VPS"},
                "deploy_path": {"type": "string", "description": "Absolute remote path where the project should live on that server"},
                "port": {"type": "integer", "description": "SSH port", "default": 22},
                "label": {"type": "string", "description": "Optional human-readable label for the server target"},
                "auth": {"type": "string", "description": "Authentication contract for now must be ssh_key_path", "default": "ssh_key_path", "enum": ["ssh_key_path"]},
            },
            ["name", "alias", "host", "user", "ssh_key_path", "deploy_path"],
            _project_server_register,
            is_code_tool=True,
        ),
    ] + get_project_github_dev_tools()
