from __future__ import annotations

import json
import pathlib
import re
import subprocess
from typing import Any, Dict, List, Optional

from ouroboros.tools.external_repos import _branch_policy, _normalize_work_branch_name, _repo_info, _resolve_repo, _tool_entry, _update_repo_meta, _validate_alias
from ouroboros.tools.registry import ToolContext, ToolEntry

_MEMORY_DIR = "memory/external_repos"


def _utc_now_iso() -> str:
    return __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()


def _memory_path(ctx: ToolContext, alias: str) -> pathlib.Path:
    return ctx.drive_path(f"{_MEMORY_DIR}/{_validate_alias(alias)}.md")


def _github_repo_slug(origin: str) -> str:
    raw = str(origin or "").strip()
    if not raw:
        raise ValueError("external repo has no origin remote configured")
    patterns = [
        r"git@github\.com:(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$",
        r"https://github\.com/(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$",
        r"ssh://git@github\.com/(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$",
    ]
    for pattern in patterns:
        m = re.match(pattern, raw)
        if m:
            return m.group("slug")
    raise ValueError(f"origin is not a supported GitHub remote: {raw}")


def _gh_repo_json(repo_slug: str, args: List[str], timeout: int = 30, input_data: Optional[str] = None) -> Any:
    try:
        res = subprocess.run(
            ["gh", *args, "-R", repo_slug],
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_data,
        )
    except FileNotFoundError as e:
        raise RuntimeError("gh CLI not found on VPS") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"gh CLI timed out after {timeout}s") from e
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "gh command failed").strip()
        raise RuntimeError(err.splitlines()[0][:400])
    out = (res.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse gh JSON output: {out[:300]}") from e


def _gh_repo_text(repo_slug: str, args: List[str], timeout: int = 30, input_data: Optional[str] = None) -> str:
    try:
        res = subprocess.run(
            ["gh", *args, "-R", repo_slug],
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_data,
        )
    except FileNotFoundError as e:
        raise RuntimeError("gh CLI not found on VPS") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"gh CLI timed out after {timeout}s") from e
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "gh command failed").strip()
        raise RuntimeError(err.splitlines()[0][:400])
    return (res.stdout or "").strip()


def _external_repo_github_slug(ctx: ToolContext, alias: str) -> str:
    meta, repo_dir = _resolve_repo(ctx, alias)
    info = _repo_info(repo_dir)
    origin = str(info.get("origin") or meta.get("origin") or "")
    return _github_repo_slug(origin)


def _external_repo_memory_get(ctx: ToolContext, alias: str) -> str:
    meta, repo_dir = _resolve_repo(ctx, alias)
    info = _repo_info(repo_dir)
    policy = _branch_policy(meta, alias)
    path = _memory_path(ctx, alias)
    if path.exists():
        return path.read_text(encoding="utf-8")
    slug = ""
    try:
        slug = _github_repo_slug(str(info.get("origin") or meta.get("origin") or ""))
    except Exception:
        slug = ""
    template = (
        f"# External Repo Memory — {alias}\n\n"
        f"- Alias: `{alias}`\n"
        f"- Path: `{info['path']}`\n"
        f"- Origin: `{info['origin']}`\n"
        f"- GitHub: `{slug or 'unknown'}`\n"
        f"- Current branch: `{info['branch']}`\n"
        f"- Default work branch: `{policy['default_work_branch']}`\n"
        f"- Protected branches: `{', '.join(policy['protected_branches'])}`\n\n"
        "## Project Summary\n"
        "- TBD\n\n"
        "## Working Commands\n"
        "- run:\n"
        "- test:\n"
        "- lint:\n\n"
        "## Known Pitfalls\n"
        "- TBD\n\n"
        "## Recent Actions\n"
        f"- Initialized memory at {_utc_now_iso()}\n\n"
        "## Open Threads / Next Steps\n"
        "- TBD\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template, encoding="utf-8")
    return template


def _external_repo_memory_update(ctx: ToolContext, alias: str, content: str) -> str:
    _resolve_repo(ctx, alias)
    path = _memory_path(ctx, alias)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(content), encoding="utf-8")
    return json.dumps(
        {"status": "ok", "alias": alias, "path": str(path), "bytes_written": len(str(content).encode("utf-8"))},
        ensure_ascii=False,
        indent=2,
    )


def _external_repo_memory_append_note(ctx: ToolContext, alias: str, note: str) -> str:
    if not str(note or "").strip():
        raise ValueError("note must be non-empty")
    existing = _external_repo_memory_get(ctx, alias)
    path = _memory_path(ctx, alias)
    with path.open("a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(f"- {_utc_now_iso()} — {str(note).strip()}\n")
    return json.dumps({"status": "ok", "alias": alias, "path": str(path), "appended": True}, ensure_ascii=False, indent=2)


def _external_repo_pr_list(ctx: ToolContext, alias: str, state: str = "open", limit: int = 20) -> str:
    repo_slug = _external_repo_github_slug(ctx, alias)
    payload = _gh_repo_json(
        repo_slug,
        [
            "pr", "list", "--state", state,
            "--limit", str(max(1, min(int(limit), 100))),
            "--json", "number,title,state,headRefName,baseRefName,url,isDraft,author",
        ],
        timeout=30,
    )
    return json.dumps({"alias": alias, "repo": repo_slug, "pull_requests": payload or []}, ensure_ascii=False, indent=2)


def _external_repo_pr_get(ctx: ToolContext, alias: str, number: int) -> str:
    if int(number) <= 0:
        raise ValueError("number must be positive")
    repo_slug = _external_repo_github_slug(ctx, alias)
    payload = _gh_repo_json(
        repo_slug,
        ["pr", "view", str(int(number)), "--json", "number,title,body,state,headRefName,baseRefName,url,isDraft,author,commits,comments"],
        timeout=30,
    )
    return json.dumps({"alias": alias, "repo": repo_slug, "pull_request": payload}, ensure_ascii=False, indent=2)


def _remote_branch_exists(repo_dir: pathlib.Path, branch: str) -> bool:
    res = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", branch],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return res.returncode == 0 and bool(res.stdout.strip())


def _external_repo_pr_create(ctx: ToolContext, alias: str, title: str, body: str = "", base: str = "", head: str = "") -> str:
    if not str(title or "").strip():
        raise ValueError("title must be non-empty")
    meta, repo_dir = _resolve_repo(ctx, alias)
    policy = _branch_policy(meta, alias)
    repo_slug = _external_repo_github_slug(ctx, alias)
    current_branch = _repo_info(repo_dir).get("branch") or ""
    head_branch = _normalize_work_branch_name(head.strip() or str(current_branch))
    base_branch = _normalize_work_branch_name(base.strip() or policy["protected_branches"][0])
    if base_branch not in policy["protected_branches"]:
        raise ValueError(f"base branch must be one of protected branches: {', '.join(policy['protected_branches'])}")
    if not _remote_branch_exists(repo_dir, head_branch):
        raise ValueError(f"head branch is not pushed to origin: {head_branch}")
    args = ["pr", "create", f"--title={title}", f"--base={base_branch}", f"--head={head_branch}"]
    if body:
        args.append("--body-file=-")
        url = _gh_repo_text(repo_slug, args, timeout=60, input_data=body)
    else:
        url = _gh_repo_text(repo_slug, args, timeout=60)
    _update_repo_meta(ctx, alias, {"last_pr_at": _utc_now_iso(), "last_pr_head": head_branch, "last_pr_base": base_branch})
    return json.dumps(
        {"status": "ok", "alias": alias, "repo": repo_slug, "url": url, "head": head_branch, "base": base_branch, "title": title},
        ensure_ascii=False,
        indent=2,
    )


def _external_repo_issue_list(ctx: ToolContext, alias: str, state: str = "open", limit: int = 20) -> str:
    repo_slug = _external_repo_github_slug(ctx, alias)
    payload = _gh_repo_json(
        repo_slug,
        [
            "issue", "list", "--state", state,
            "--limit", str(max(1, min(int(limit), 100))),
            "--json", "number,title,state,url,author,labels",
        ],
        timeout=30,
    )
    return json.dumps({"alias": alias, "repo": repo_slug, "issues": payload or []}, ensure_ascii=False, indent=2)


def _external_repo_issue_get(ctx: ToolContext, alias: str, number: int) -> str:
    if int(number) <= 0:
        raise ValueError("number must be positive")
    repo_slug = _external_repo_github_slug(ctx, alias)
    payload = _gh_repo_json(
        repo_slug,
        ["issue", "view", str(int(number)), "--json", "number,title,body,state,url,author,labels,comments"],
        timeout=30,
    )
    return json.dumps({"alias": alias, "repo": repo_slug, "issue": payload}, ensure_ascii=False, indent=2)


def _external_repo_issue_create(ctx: ToolContext, alias: str, title: str, body: str = "") -> str:
    if not str(title or "").strip():
        raise ValueError("title must be non-empty")
    repo_slug = _external_repo_github_slug(ctx, alias)
    args = ["issue", "create", f"--title={title}"]
    if body:
        args.append("--body-file=-")
        url = _gh_repo_text(repo_slug, args, timeout=60, input_data=body)
    else:
        url = _gh_repo_text(repo_slug, args, timeout=60)
    return json.dumps({"status": "ok", "alias": alias, "repo": repo_slug, "url": url, "title": title}, ensure_ascii=False, indent=2)


def _external_repo_issue_comment(ctx: ToolContext, alias: str, number: int, body: str) -> str:
    if int(number) <= 0:
        raise ValueError("number must be positive")
    if not str(body or "").strip():
        raise ValueError("body must be non-empty")
    repo_slug = _external_repo_github_slug(ctx, alias)
    out = _gh_repo_text(repo_slug, ["issue", "comment", str(int(number)), "--body-file", "-"], timeout=60, input_data=body)
    return json.dumps(
        {"status": "ok", "alias": alias, "repo": repo_slug, "number": int(number), "result": out or "comment added"},
        ensure_ascii=False,
        indent=2,
    )


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            "external_repo_memory_get",
            "Read the persistent markdown memory for a registered external repository, creating a template on first access.",
            {"alias": {"type": "string", "description": "Registered repo alias"}},
            ["alias"],
            _external_repo_memory_get,
        ),
        _tool_entry(
            "external_repo_memory_update",
            "Overwrite the persistent markdown memory for a registered external repository.",
            {
                "alias": {"type": "string", "description": "Registered repo alias"},
                "content": {"type": "string", "description": "Full markdown content"},
            },
            ["alias", "content"],
            _external_repo_memory_update,
            is_code_tool=True,
        ),
        _tool_entry(
            "external_repo_memory_append_note",
            "Append a timestamped note to the persistent memory of a registered external repository.",
            {
                "alias": {"type": "string", "description": "Registered repo alias"},
                "note": {"type": "string", "description": "Short markdown note to append"},
            },
            ["alias", "note"],
            _external_repo_memory_append_note,
            is_code_tool=True,
        ),
        _tool_entry(
            "external_repo_pr_list",
            "List pull requests for a registered external GitHub repository.",
            {
                "alias": {"type": "string", "description": "Registered repo alias"},
                "state": {"type": "string", "description": "PR state", "default": "open"},
                "limit": {"type": "integer", "description": "Maximum PRs to return", "default": 20},
            },
            ["alias"],
            _external_repo_pr_list,
        ),
        _tool_entry(
            "external_repo_pr_get",
            "Read one pull request for a registered external GitHub repository.",
            {
                "alias": {"type": "string", "description": "Registered repo alias"},
                "number": {"type": "integer", "description": "Pull request number"},
            },
            ["alias", "number"],
            _external_repo_pr_get,
        ),
        _tool_entry(
            "external_repo_pr_create",
            "Create a pull request from a pushed work branch in a registered external GitHub repository.",
            {
                "alias": {"type": "string", "description": "Registered repo alias"},
                "title": {"type": "string", "description": "Pull request title"},
                "body": {"type": "string", "description": "Optional pull request body"},
                "base": {"type": "string", "description": "Optional base branch (must be protected)"},
                "head": {"type": "string", "description": "Optional head branch (must already exist remotely)"},
            },
            ["alias", "title"],
            _external_repo_pr_create,
            is_code_tool=True,
        ),
        _tool_entry(
            "external_repo_issue_list",
            "List issues for a registered external GitHub repository.",
            {
                "alias": {"type": "string", "description": "Registered repo alias"},
                "state": {"type": "string", "description": "Issue state", "default": "open"},
                "limit": {"type": "integer", "description": "Maximum issues to return", "default": 20},
            },
            ["alias"],
            _external_repo_issue_list,
        ),
        _tool_entry(
            "external_repo_issue_get",
            "Read one issue for a registered external GitHub repository.",
            {
                "alias": {"type": "string", "description": "Registered repo alias"},
                "number": {"type": "integer", "description": "Issue number"},
            },
            ["alias", "number"],
            _external_repo_issue_get,
        ),
        _tool_entry(
            "external_repo_issue_create",
            "Create a GitHub issue for a registered external repository.",
            {
                "alias": {"type": "string", "description": "Registered repo alias"},
                "title": {"type": "string", "description": "Issue title"},
                "body": {"type": "string", "description": "Optional issue body"},
            },
            ["alias", "title"],
            _external_repo_issue_create,
            is_code_tool=True,
        ),
        _tool_entry(
            "external_repo_issue_comment",
            "Add a comment to an issue in a registered external GitHub repository.",
            {
                "alias": {"type": "string", "description": "Registered repo alias"},
                "number": {"type": "integer", "description": "Issue number"},
                "body": {"type": "string", "description": "Markdown comment body"},
            },
            ["alias", "number", "body"],
            _external_repo_issue_comment,
            is_code_tool=True,
        ),
    ]
