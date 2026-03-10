from __future__ import annotations

import fnmatch
import json
import os
import pathlib
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import read_text, safe_relpath

_REGISTRY_PATH = "state/external_repos.json"
_MAX_CMD_ARGS = 64
_MAX_CMD_LEN = 200
_MAX_WRITE_CHARS = 500_000
_DEFAULT_PROTECTED_BRANCHES = ["main", "master"]
_DEFAULT_WORK_BRANCH_PREFIX = "veles/"


def _utc_now_iso() -> str:
    return __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()


def _registry_path(ctx: ToolContext) -> pathlib.Path:
    return ctx.drive_path(_REGISTRY_PATH)


def _load_registry(ctx: ToolContext) -> Dict[str, Any]:
    path = _registry_path(ctx)
    if not path.exists():
        return {"repos": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"repos": {}, "warning": f"Failed to parse registry: {e}"}
    if not isinstance(data, dict):
        return {"repos": {}, "warning": "Registry payload is not an object."}
    repos = data.get("repos")
    if not isinstance(repos, dict):
        data["repos"] = {}
    return data


def _save_registry(ctx: ToolContext, data: Dict[str, Any]) -> None:
    path = _registry_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _validate_alias(alias: str) -> str:
    alias = str(alias or "").strip()
    if not alias:
        raise ValueError("alias must be non-empty")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    if any(ch not in allowed for ch in alias):
        raise ValueError("alias may contain only letters, digits, '-' and '_'")
    return alias


def _canonical_repo_path(repo_path: str) -> pathlib.Path:
    raw = str(repo_path or "").strip()
    if not raw:
        raise ValueError("repo_path must be non-empty")
    p = pathlib.Path(raw).expanduser()
    if not p.is_absolute():
        raise ValueError("repo_path must be an absolute path on the VPS")
    resolved = p.resolve()
    if not resolved.exists():
        raise ValueError(f"repo_path does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"repo_path is not a directory: {resolved}")
    if not (resolved / ".git").exists():
        raise ValueError(f"repo_path is not a git repository: {resolved}")
    return resolved


def _resolve_repo(ctx: ToolContext, alias: str) -> Tuple[Dict[str, Any], pathlib.Path]:
    alias = _validate_alias(alias)
    data = _load_registry(ctx)
    repos = data.get("repos") or {}
    meta = repos.get(alias)
    if not isinstance(meta, dict):
        raise ValueError(f"Unknown external repo alias: {alias}")
    repo_path = meta.get("path")
    path = _canonical_repo_path(str(repo_path or ""))
    return meta, path


def _normalize_branch_list(branches: Any) -> List[str]:
    if branches is None:
        return list(_DEFAULT_PROTECTED_BRANCHES)
    if not isinstance(branches, list):
        raise ValueError("protected_branches must be a list of branch names")
    out: List[str] = []
    for raw in branches:
        branch = str(raw or "").strip()
        if not branch:
            continue
        if any(ch.isspace() for ch in branch):
            raise ValueError(f"invalid branch name: {branch!r}")
        if branch not in out:
            out.append(branch)
    return out or list(_DEFAULT_PROTECTED_BRANCHES)


def _normalize_work_branch_name(name: str) -> str:
    branch = str(name or "").strip()
    if not branch:
        raise ValueError("work_branch must be non-empty")
    if branch.startswith("refs/"):
        raise ValueError("work_branch must be a plain branch name, not refs/*")
    if any(ch.isspace() for ch in branch):
        raise ValueError("work_branch must not contain whitespace")
    if branch in {".", ".."} or branch.endswith("/") or branch.startswith("/"):
        raise ValueError("invalid work_branch name")
    return branch


def _branch_policy(meta: Dict[str, Any], alias: str) -> Dict[str, Any]:
    protected = _normalize_branch_list(meta.get("protected_branches"))
    stored = str(meta.get("default_work_branch") or "").strip()
    default_branch = stored or f"{_DEFAULT_WORK_BRANCH_PREFIX}{alias}"
    default_branch = _normalize_work_branch_name(default_branch)
    if default_branch in protected:
        raise ValueError("default_work_branch must not be a protected branch")
    return {
        "protected_branches": protected,
        "default_work_branch": default_branch,
    }


def _update_repo_meta(ctx: ToolContext, alias: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    alias = _validate_alias(alias)
    data = _load_registry(ctx)
    repos = data.setdefault("repos", {})
    meta = repos.get(alias)
    if not isinstance(meta, dict):
        raise ValueError(f"Unknown external repo alias: {alias}")
    meta.update(updates)
    repos[alias] = meta
    _save_registry(ctx, data)
    return meta


def _git(args: List[str], cwd: pathlib.Path, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_current_branch(repo_dir: pathlib.Path) -> str:
    res = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_dir)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or "git rev-parse failed")
    return res.stdout.strip()


def _git_checkout_work_branch(repo_dir: pathlib.Path, branch: str) -> Dict[str, str]:
    branch = _normalize_work_branch_name(branch)
    existing = _git(["rev-parse", "--verify", branch], repo_dir)
    if existing.returncode == 0:
        res = _git(["checkout", branch], repo_dir, timeout=60)
        action = "checked_out_existing"
    else:
        res = _git(["checkout", "-b", branch], repo_dir, timeout=60)
        action = "created_and_checked_out"
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or f"git checkout failed for {branch}")
    return {"branch": branch, "action": action, "stdout": res.stdout.strip(), "stderr": res.stderr.strip()}


def _acquire_external_git_lock(ctx: ToolContext, alias: str, timeout_sec: int = 120) -> pathlib.Path:
    lock_dir = ctx.drive_path("locks")
    lock_dir.mkdir(parents=True, exist_ok=True)
    safe_alias = re.sub(r"[^A-Za-z0-9_-]", "_", alias)
    lock_path = lock_dir / f"external_git_{safe_alias}.lock"
    stale_sec = 600
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if lock_path.exists():
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > stale_sec:
                    lock_path.unlink()
                    continue
            except (FileNotFoundError, OSError):
                pass
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, f"locked_at={_utc_now_iso()}\n".encode("utf-8"))
            finally:
                os.close(fd)
            return lock_path
        except FileExistsError:
            time.sleep(0.5)
    raise TimeoutError(f"External git lock not acquired within {timeout_sec}s: {lock_path}")


def _release_external_git_lock(lock_path: pathlib.Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def _ensure_branch_allowed(current_branch: str, protected_branches: List[str]) -> None:
    if current_branch in protected_branches:
        raise ValueError(
            f"Refusing to write or push on protected branch: {current_branch}. "
            f"Switch to a work branch first. Protected: {', '.join(protected_branches)}"
        )


def _shorten(text: str, max_chars: int = 12000) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return text[:head] + "\n...(truncated)...\n" + text[-tail:]


def _repo_info(path: pathlib.Path) -> Dict[str, Any]:
    branch = ""
    sha = ""
    remote = ""
    try:
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], path).stdout.strip()
    except Exception:
        branch = ""
    try:
        sha = _git(["rev-parse", "HEAD"], path).stdout.strip()
    except Exception:
        sha = ""
    try:
        remote = _git(["remote", "get-url", "origin"], path).stdout.strip()
    except Exception:
        remote = ""
    return {
        "path": str(path),
        "branch": branch,
        "sha": sha,
        "origin": remote,
    }


def _stage_paths(repo_dir: pathlib.Path, paths: Optional[List[str]]) -> None:
    if paths:
        safe_paths = [safe_relpath(p) for p in paths if str(p).strip()]
        if not safe_paths:
            raise ValueError("paths must contain at least one non-empty entry")
        cmd = ["add", *safe_paths]
    else:
        cmd = ["add", "-A"]
    res = _git(cmd, repo_dir, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or "git add failed")


def _git_get_config(repo_dir: pathlib.Path, key: str) -> str:
    res = _git(["config", "--get", key], repo_dir)
    if res.returncode != 0:
        return ""
    return res.stdout.strip()


def _ensure_external_repo_git_identity(repo_dir: pathlib.Path) -> Dict[str, str]:
    name = _git_get_config(repo_dir, "user.name")
    email = _git_get_config(repo_dir, "user.email")
    changed: Dict[str, str] = {}
    if not name:
        name = "Veles"
        res = _git(["config", "user.name", name], repo_dir, timeout=30)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or res.stdout.strip() or "git config user.name failed")
        changed["user.name"] = name
    if not email:
        email = "veles@users.noreply.github.com"
        res = _git(["config", "user.email", email], repo_dir, timeout=30)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or res.stdout.strip() or "git config user.email failed")
        changed["user.email"] = email
    return {
        "user.name": name,
        "user.email": email,
        "configured_locally": "yes" if changed else "no",
        "changed": ", ".join(sorted(changed.keys())),
    }


def _external_repo_register(
    ctx: ToolContext,
    alias: str,
    repo_path: str,
    notes: str = "",
    protected_branches: Optional[List[str]] = None,
    default_work_branch: str = "",
) -> str:
    alias = _validate_alias(alias)
    path = _canonical_repo_path(repo_path)
    info = _repo_info(path)
    normalized_protected = _normalize_branch_list(protected_branches)
    work_branch = _normalize_work_branch_name(default_work_branch.strip() or f"{_DEFAULT_WORK_BRANCH_PREFIX}{alias}")
    if work_branch in normalized_protected:
        raise ValueError("default_work_branch must not be a protected branch")

    data = _load_registry(ctx)
    repos = data.setdefault("repos", {})
    repos[alias] = {
        "path": str(path),
        "notes": str(notes or "").strip(),
        "origin": info["origin"],
        "last_registered_at": _utc_now_iso(),
        "protected_branches": normalized_protected,
        "default_work_branch": work_branch,
    }
    _save_registry(ctx, data)

    return json.dumps(
        {
            "status": "ok",
            "alias": alias,
            "repo": info,
            "notes": str(notes or "").strip(),
            "branch_policy": {
                "protected_branches": normalized_protected,
                "default_work_branch": work_branch,
            },
        },
        ensure_ascii=False,
        indent=2,
    )


def _external_repo_list(ctx: ToolContext) -> str:
    data = _load_registry(ctx)
    repos = data.get("repos") or {}
    payload = []
    for alias in sorted(repos.keys()):
        meta = repos.get(alias) or {}
        row = {
            "alias": alias,
            "path": meta.get("path", ""),
            "origin": meta.get("origin", ""),
            "notes": meta.get("notes", ""),
            "last_registered_at": meta.get("last_registered_at", ""),
        }
        try:
            policy = _branch_policy(meta, alias)
            row.update(policy)
        except Exception as e:
            row["branch_policy_error"] = str(e)
        try:
            info = _repo_info(_canonical_repo_path(str(meta.get("path") or "")))
            row.update({"branch": info["branch"], "sha": info["sha"]})
        except Exception as e:
            row["status"] = f"unavailable: {e}"
        payload.append(row)
    return json.dumps({"repos": payload}, ensure_ascii=False, indent=2)


def _external_repo_sync(ctx: ToolContext, alias: str) -> str:
    meta, path = _resolve_repo(ctx, alias)
    fetch = _git(["fetch", "--all", "--prune"], path, timeout=60)
    status = _git(["status", "--short", "--branch"], path)
    info = _repo_info(path)
    return json.dumps(
        {
            "status": "ok" if fetch.returncode == 0 else "error",
            "alias": alias,
            "repo": info,
            "fetch_stdout": _shorten(fetch.stdout.strip()),
            "fetch_stderr": _shorten(fetch.stderr.strip()),
            "status_after_fetch": status.stdout.strip(),
            "notes": meta.get("notes", ""),
        },
        ensure_ascii=False,
        indent=2,
    )


def _external_repo_read(ctx: ToolContext, alias: str, path: str) -> str:
    _, repo_dir = _resolve_repo(ctx, alias)
    rel = safe_relpath(path)
    return read_text((repo_dir / rel).resolve())


def _list_dir(root: pathlib.Path, rel: str, max_entries: int = 500) -> List[str]:
    target = (root / safe_relpath(rel)).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return [f"⚠️ Path escapes repository: {rel}"]
    if not target.exists():
        return [f"⚠️ Directory not found: {rel}"]
    if not target.is_dir():
        return [f"⚠️ Not a directory: {rel}"]
    items: List[str] = []
    for entry in sorted(target.iterdir()):
        if len(items) >= max_entries:
            items.append(f"...(truncated at {max_entries})")
            break
        suffix = "/" if entry.is_dir() else ""
        items.append(str(entry.relative_to(root)) + suffix)
    return items


def _external_repo_list_files(ctx: ToolContext, alias: str, dir: str = ".", max_entries: int = 500) -> str:
    _, repo_dir = _resolve_repo(ctx, alias)
    return json.dumps(_list_dir(repo_dir, dir, max_entries), ensure_ascii=False, indent=2)


def _python_search(repo_dir: pathlib.Path, query: str, glob: str, max_results: int) -> List[str]:
    pattern = re.compile(query)
    results: List[str] = []
    for path in sorted(repo_dir.rglob("*")):
        if ".git" in path.parts or not path.is_file():
            continue
        rel = str(path.relative_to(repo_dir))
        if glob and not fnmatch.fnmatch(rel, glob):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                results.append(f"{rel}:{lineno}:{line}")
                if len(results) >= max_results:
                    return results
    return results


def _external_repo_search(ctx: ToolContext, alias: str, query: str, glob: str = "", max_results: int = 50) -> str:
    _, repo_dir = _resolve_repo(ctx, alias)
    q = str(query or "").strip()
    if not q:
        return "⚠️ query must be non-empty"
    limit = max(1, min(int(max_results), 200))
    cmd = ["rg", "-n", "--no-heading", "--hidden", "--glob", "!.git", q]
    if glob:
        cmd.extend(["-g", glob])
    cmd.append(".")
    fallback_used = False
    try:
        res = subprocess.run(cmd, cwd=str(repo_dir), capture_output=True, text=True, timeout=20)
        if res.returncode not in (0, 1):
            return f"⚠️ rg failed\nSTDOUT:\n{res.stdout}\n\nSTDERR:\n{res.stderr}"
        lines = [line for line in res.stdout.splitlines() if line.strip()]
    except FileNotFoundError:
        fallback_used = True
        lines = _python_search(repo_dir, q, glob, limit)
    limited = lines[:limit]
    return json.dumps(
        {
            "alias": alias,
            "query": q,
            "count": len(lines),
            "results": limited,
            "backend": "python" if fallback_used else "rg",
        },
        ensure_ascii=False,
        indent=2,
    )


def _validate_cmd(cmd: List[str]) -> List[str]:
    if not isinstance(cmd, list) or not cmd:
        raise ValueError("cmd must be a non-empty array of strings")
    if len(cmd) > _MAX_CMD_ARGS:
        raise ValueError(f"cmd is too long: max {_MAX_CMD_ARGS} args")
    out: List[str] = []
    for part in cmd:
        if not isinstance(part, str) or not part.strip():
            raise ValueError("cmd items must be non-empty strings")
        if len(part) > _MAX_CMD_LEN:
            raise ValueError(f"cmd arg too long (> {_MAX_CMD_LEN} chars)")
        out.append(part)
    return out


def _external_repo_run_shell(ctx: ToolContext, alias: str, cmd: List[str], timeout_sec: int = 30) -> str:
    _, repo_dir = _resolve_repo(ctx, alias)
    argv = _validate_cmd(cmd)
    res = subprocess.run(
        argv,
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        timeout=max(1, min(int(timeout_sec), 300)),
    )
    return json.dumps(
        {
            "alias": alias,
            "cwd": str(repo_dir),
            "cmd": argv,
            "returncode": int(res.returncode),
            "stdout": _shorten(res.stdout),
            "stderr": _shorten(res.stderr),
        },
        ensure_ascii=False,
        indent=2,
    )


def _external_repo_git_status(ctx: ToolContext, alias: str) -> str:
    _, repo_dir = _resolve_repo(ctx, alias)
    res = _git(["status", "--short", "--branch"], repo_dir)
    if res.returncode != 0:
        return f"⚠️ git status failed\nSTDOUT:\n{res.stdout}\n\nSTDERR:\n{res.stderr}"
    return res.stdout.strip()


def _external_repo_git_diff(ctx: ToolContext, alias: str, staged: bool = False) -> str:
    _, repo_dir = _resolve_repo(ctx, alias)
    args = ["diff", "--staged"] if staged else ["diff"]
    res = _git(args, repo_dir, timeout=60)
    if res.returncode != 0:
        return f"⚠️ git diff failed\nSTDOUT:\n{res.stdout}\n\nSTDERR:\n{res.stderr}"
    return _shorten(res.stdout, max_chars=20000)


def _external_repo_write(ctx: ToolContext, alias: str, path: str, content: str) -> str:
    meta, repo_dir = _resolve_repo(ctx, alias)
    current_branch = _git_current_branch(repo_dir)
    policy = _branch_policy(meta, alias)
    _ensure_branch_allowed(current_branch, policy["protected_branches"])
    rel = safe_relpath(path)
    target = (repo_dir / rel).resolve()
    try:
        target.relative_to(repo_dir.resolve())
    except ValueError as e:
        raise ValueError(f"path escapes repository: {path}") from e
    if len(content) > _MAX_WRITE_CHARS:
        raise ValueError(f"content too large (> {_MAX_WRITE_CHARS} chars)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return json.dumps(
        {
            "status": "ok",
            "alias": alias,
            "path": rel,
            "branch": current_branch,
            "bytes_written": len(content.encode("utf-8")),
        },
        ensure_ascii=False,
        indent=2,
    )


def _external_repo_prepare_work_branch(ctx: ToolContext, alias: str, branch: str = "") -> str:
    meta, repo_dir = _resolve_repo(ctx, alias)
    policy = _branch_policy(meta, alias)
    target_branch = _normalize_work_branch_name(branch.strip() or policy["default_work_branch"])
    if target_branch in policy["protected_branches"]:
        raise ValueError(f"work branch must not be protected: {target_branch}")
    lock = _acquire_external_git_lock(ctx, alias)
    try:
        checkout = _git_checkout_work_branch(repo_dir, target_branch)
        current = _git_current_branch(repo_dir)
        _update_repo_meta(
            ctx,
            alias,
            {
                "default_work_branch": target_branch,
                "last_work_branch_prepared_at": _utc_now_iso(),
            },
        )
    finally:
        _release_external_git_lock(lock)
    return json.dumps(
        {
            "status": "ok",
            "alias": alias,
            "branch": current,
            "protected_branches": policy["protected_branches"],
            "checkout": checkout,
        },
        ensure_ascii=False,
        indent=2,
    )


def _external_repo_set_branch_policy(
    ctx: ToolContext,
    alias: str,
    protected_branches: Optional[List[str]] = None,
    default_work_branch: str = "",
) -> str:
    meta, repo_dir = _resolve_repo(ctx, alias)
    current_policy = _branch_policy(meta, alias)
    new_protected = _normalize_branch_list(protected_branches) if protected_branches is not None else current_policy["protected_branches"]
    new_default = _normalize_work_branch_name(default_work_branch.strip() or current_policy["default_work_branch"])
    if new_default in new_protected:
        raise ValueError("default_work_branch must not be a protected branch")
    current_branch = _git_current_branch(repo_dir)
    current_branch_state = "current branch is protected" if current_branch in new_protected else "current branch is writable"
    updated = _update_repo_meta(
        ctx,
        alias,
        {
            "protected_branches": new_protected,
            "default_work_branch": new_default,
            "last_branch_policy_update_at": _utc_now_iso(),
        },
    )
    return json.dumps(
        {
            "status": "ok",
            "alias": alias,
            "branch_policy": _branch_policy(updated, alias),
            "current_branch": current_branch,
            "current_branch_state": current_branch_state,
        },
        ensure_ascii=False,
        indent=2,
    )


def _external_repo_commit_push(
    ctx: ToolContext,
    alias: str,
    commit_message: str,
    branch: str = "",
    paths: Optional[List[str]] = None,
) -> str:
    if not str(commit_message or "").strip():
        return "⚠️ ERROR: commit_message must be non-empty."
    meta, repo_dir = _resolve_repo(ctx, alias)
    policy = _branch_policy(meta, alias)
    target_branch = _normalize_work_branch_name(branch.strip() or policy["default_work_branch"])
    if target_branch in policy["protected_branches"]:
        return f"⚠️ ERROR: target branch is protected: {target_branch}"
    lock = _acquire_external_git_lock(ctx, alias)
    try:
        checkout = _git_checkout_work_branch(repo_dir, target_branch)
        current_branch = _git_current_branch(repo_dir)
        _ensure_branch_allowed(current_branch, policy["protected_branches"])
        _stage_paths(repo_dir, paths)
        status = _git(["status", "--porcelain"], repo_dir)
        if status.returncode != 0:
            return f"⚠️ GIT_ERROR (status): {status.stderr.strip() or status.stdout.strip()}"
        if not status.stdout.strip():
            return "⚠️ GIT_NO_CHANGES: nothing to commit."
        git_identity = _ensure_external_repo_git_identity(repo_dir)
        commit_res = _git(["commit", "-m", commit_message], repo_dir, timeout=60)
        if commit_res.returncode != 0:
            return f"⚠️ GIT_ERROR (commit): {commit_res.stderr.strip() or commit_res.stdout.strip()}"
        pull_res = _git(["pull", "--rebase", "origin", current_branch], repo_dir, timeout=90)
        pull_warning = ""
        if pull_res.returncode != 0:
            pull_warning = pull_res.stderr.strip() or pull_res.stdout.strip()
        push_res = _git(["push", "-u", "origin", current_branch], repo_dir, timeout=90)
        if push_res.returncode != 0:
            return (
                "⚠️ GIT_ERROR (push): "
                + (push_res.stderr.strip() or push_res.stdout.strip())
                + "\nCommitted locally but NOT pushed."
            )
        info = _repo_info(repo_dir)
        _update_repo_meta(
            ctx,
            alias,
            {
                "default_work_branch": current_branch,
                "last_push_at": _utc_now_iso(),
            },
        )
    finally:
        _release_external_git_lock(lock)
    payload = {
        "status": "ok",
        "alias": alias,
        "branch": current_branch,
        "repo": info,
        "checkout": checkout,
        "commit_message": commit_message,
        "push_stdout": _shorten(push_res.stdout.strip()),
        "push_stderr": _shorten(push_res.stderr.strip()),
        "protected_branches": policy["protected_branches"],
        "git_identity": git_identity,
    }
    if pull_warning:
        payload["pull_warning"] = _shorten(pull_warning)
    return json.dumps(payload, ensure_ascii=False, indent=2)



def _tool_entry(name: str, description: str, properties: Dict[str, Any], required: List[str], handler, is_code_tool: bool = False) -> ToolEntry:
    return ToolEntry(
        name=name,
        schema={
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
        handler=handler,
        is_code_tool=is_code_tool,
    )


def _readonly_tool_entries() -> List[ToolEntry]:
    return [
        _tool_entry(
            "external_repo_register",
            "Register an external local git repository by alias for later work.",
            {
                "alias": {"type": "string", "description": "Stable alias for the repo"},
                "repo_path": {"type": "string", "description": "Absolute VPS path to a local git repo"},
                "notes": {"type": "string", "description": "Optional short human note about the repo"},
                "protected_branches": {"type": "array", "items": {"type": "string"}, "description": "Optional protected branch names (default: main, master)"},
                "default_work_branch": {"type": "string", "description": "Optional default writable work branch name"},
            },
            ["alias", "repo_path"],
            _external_repo_register,
        ),
        _tool_entry(
            "external_repo_list",
            "List registered external repositories and their current git state.",
            {},
            [],
            _external_repo_list,
        ),
        _tool_entry(
            "external_repo_sync",
            "Fetch updates for a registered external repository without changing its working tree.",
            {"alias": {"type": "string", "description": "Registered repo alias"}},
            ["alias"],
            _external_repo_sync,
        ),
        _tool_entry(
            "external_repo_read",
            "Read a UTF-8 text file from a registered external repository.",
            {
                "alias": {"type": "string", "description": "Registered repo alias"},
                "path": {"type": "string", "description": "Path relative to repo root"},
            },
            ["alias", "path"],
            _external_repo_read,
        ),
        _tool_entry(
            "external_repo_list_files",
            "List files under a directory in a registered external repository.",
            {
                "alias": {"type": "string", "description": "Registered repo alias"},
                "dir": {"type": "string", "description": "Directory relative to repo root", "default": "."},
                "max_entries": {"type": "integer", "description": "Maximum number of entries to return", "default": 500},
            },
            ["alias"],
            _external_repo_list_files,
        ),
        _tool_entry(
            "external_repo_search",
            "Search text in a registered external repository using ripgrep.",
            {
                "alias": {"type": "string", "description": "Registered repo alias"},
                "query": {"type": "string", "description": "Literal or regex query for ripgrep"},
                "glob": {"type": "string", "description": "Optional ripgrep glob filter"},
                "max_results": {"type": "integer", "description": "Maximum matching lines to return", "default": 50},
            },
            ["alias", "query"],
            _external_repo_search,
        ),
        _tool_entry(
            "external_repo_run_shell",
            "Run a shell command array inside a registered external repository.",
            {
                "alias": {"type": "string", "description": "Registered repo alias"},
                "cmd": {"type": "array", "items": {"type": "string"}, "description": "Command argv array, e.g. ['pytest', '-q']"},
                "timeout_sec": {"type": "integer", "description": "Timeout in seconds", "default": 30},
            },
            ["alias", "cmd"],
            _external_repo_run_shell,
        ),
        _tool_entry(
            "external_repo_git_status",
            "Show git status for a registered external repository.",
            {"alias": {"type": "string", "description": "Registered repo alias"}},
            ["alias"],
            _external_repo_git_status,
        ),
        _tool_entry(
            "external_repo_git_diff",
            "Show git diff for a registered external repository.",
            {
                "alias": {"type": "string", "description": "Registered repo alias"},
                "staged": {"type": "boolean", "description": "If true, show staged diff"},
            },
            ["alias"],
            _external_repo_git_diff,
        ),
    ]


def _write_tool_entries() -> List[ToolEntry]:
    return [
        _tool_entry(
            "external_repo_write",
            "Write a UTF-8 text file inside a registered external repository, refusing protected branches.",
            {
                "alias": {"type": "string", "description": "Registered repo alias"},
                "path": {"type": "string", "description": "Path relative to repo root"},
                "content": {"type": "string", "description": "Full UTF-8 file content"},
            },
            ["alias", "path", "content"],
            _external_repo_write,
            is_code_tool=True,
        ),
        _tool_entry(
            "external_repo_prepare_work_branch",
            "Create or checkout the writable work branch for a registered external repository.",
            {
                "alias": {"type": "string", "description": "Registered repo alias"},
                "branch": {"type": "string", "description": "Optional explicit work branch name"},
            },
            ["alias"],
            _external_repo_prepare_work_branch,
            is_code_tool=True,
        ),
        _tool_entry(
            "external_repo_set_branch_policy",
            "Update protected branch names and the default work branch for a registered external repository.",
            {
                "alias": {"type": "string", "description": "Registered repo alias"},
                "protected_branches": {"type": "array", "items": {"type": "string"}, "description": "Protected branch names"},
                "default_work_branch": {"type": "string", "description": "Default writable work branch name"},
            },
            ["alias"],
            _external_repo_set_branch_policy,
            is_code_tool=True,
        ),
        _tool_entry(
            "external_repo_commit_push",
            "Commit and push changes in a registered external repository on a writable work branch only.",
            {
                "alias": {"type": "string", "description": "Registered repo alias"},
                "commit_message": {"type": "string", "description": "Git commit message"},
                "branch": {"type": "string", "description": "Optional explicit writable work branch name"},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Optional repo-relative paths to stage (default: git add -A)"},
            },
            ["alias", "commit_message"],
            _external_repo_commit_push,
            is_code_tool=True,
        ),
    ]


def get_tools() -> List[ToolEntry]:
    return [*_readonly_tool_entries(), *_write_tool_entries()]
