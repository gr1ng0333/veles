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


def _run_gh(args: List[str], cwd: pathlib.Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["gh", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
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
    ]
