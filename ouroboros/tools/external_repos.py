from __future__ import annotations

import fnmatch
import json
import pathlib
import re
import subprocess
from typing import Any, Dict, List, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import read_text, safe_relpath

_REGISTRY_PATH = "state/external_repos.json"
_MAX_CMD_ARGS = 64
_MAX_CMD_LEN = 200


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


def _git(args: List[str], cwd: pathlib.Path, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
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


def _external_repo_register(ctx: ToolContext, alias: str, repo_path: str, notes: str = "") -> str:
    alias = _validate_alias(alias)
    path = _canonical_repo_path(repo_path)
    info = _repo_info(path)

    data = _load_registry(ctx)
    repos = data.setdefault("repos", {})
    repos[alias] = {
        "path": str(path),
        "notes": str(notes or "").strip(),
        "origin": info["origin"],
        "last_registered_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    }
    _save_registry(ctx, data)

    return json.dumps(
        {
            "status": "ok",
            "alias": alias,
            "repo": info,
            "notes": str(notes or "").strip(),
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


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="external_repo_register",
            schema={
                "name": "external_repo_register",
                "description": "Register an external local git repository by alias for later work.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string", "description": "Stable alias for the repo"},
                        "repo_path": {"type": "string", "description": "Absolute VPS path to a local git repo"},
                        "notes": {"type": "string", "description": "Optional short human note about the repo"},
                    },
                    "required": ["alias", "repo_path"],
                },
            },
            handler=_external_repo_register,
        ),
        ToolEntry(
            name="external_repo_list",
            schema={
                "name": "external_repo_list",
                "description": "List registered external repositories and their current git state.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            handler=_external_repo_list,
        ),
        ToolEntry(
            name="external_repo_sync",
            schema={
                "name": "external_repo_sync",
                "description": "Fetch updates for a registered external repository without changing its working tree.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string", "description": "Registered repo alias"},
                    },
                    "required": ["alias"],
                },
            },
            handler=_external_repo_sync,
        ),
        ToolEntry(
            name="external_repo_read",
            schema={
                "name": "external_repo_read",
                "description": "Read a UTF-8 text file from a registered external repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string", "description": "Registered repo alias"},
                        "path": {"type": "string", "description": "Path relative to repo root"},
                    },
                    "required": ["alias", "path"],
                },
            },
            handler=_external_repo_read,
        ),
        ToolEntry(
            name="external_repo_list_files",
            schema={
                "name": "external_repo_list_files",
                "description": "List files under a directory in a registered external repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string", "description": "Registered repo alias"},
                        "dir": {"type": "string", "description": "Directory relative to repo root", "default": "."},
                        "max_entries": {"type": "integer", "description": "Maximum number of entries to return", "default": 500},
                    },
                    "required": ["alias"],
                },
            },
            handler=_external_repo_list_files,
        ),
        ToolEntry(
            name="external_repo_search",
            schema={
                "name": "external_repo_search",
                "description": "Search text in a registered external repository using ripgrep.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string", "description": "Registered repo alias"},
                        "query": {"type": "string", "description": "Literal or regex query for ripgrep"},
                        "glob": {"type": "string", "description": "Optional ripgrep glob filter"},
                        "max_results": {"type": "integer", "description": "Maximum matching lines to return", "default": 50},
                    },
                    "required": ["alias", "query"],
                },
            },
            handler=_external_repo_search,
        ),
        ToolEntry(
            name="external_repo_run_shell",
            schema={
                "name": "external_repo_run_shell",
                "description": "Run a shell command array inside a registered external repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string", "description": "Registered repo alias"},
                        "cmd": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Command argv array, e.g. ['pytest', '-q']",
                        },
                        "timeout_sec": {"type": "integer", "description": "Timeout in seconds", "default": 30},
                    },
                    "required": ["alias", "cmd"],
                },
            },
            handler=_external_repo_run_shell,
        ),
        ToolEntry(
            name="external_repo_git_status",
            schema={
                "name": "external_repo_git_status",
                "description": "Show git status for a registered external repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string", "description": "Registered repo alias"},
                    },
                    "required": ["alias"],
                },
            },
            handler=_external_repo_git_status,
        ),
        ToolEntry(
            name="external_repo_git_diff",
            schema={
                "name": "external_repo_git_diff",
                "description": "Show git diff for a registered external repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string", "description": "Registered repo alias"},
                        "staged": {"type": "boolean", "description": "If true, show staged diff"},
                    },
                    "required": ["alias"],
                },
            },
            handler=_external_repo_git_diff,
        ),
    ]
