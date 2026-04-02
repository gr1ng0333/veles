from __future__ import annotations

import base64
import json
import shlex
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.ssh_targets import (
    _bootstrap_session,
    _get_target_record,
    _normalize_probe_error,
    _public_target_view,
    _run_ssh_probe,
)
from ouroboros.utils import utc_now_iso

_REMOTE_FS_SCRIPT = r'''
import base64
import datetime as _dt
import fnmatch
import json
import os
import re
import stat
import sys


PROJECT_MARKERS = {
    ".git",
    "package.json",
    "pyproject.toml",
    "go.mod",
    "Cargo.toml",
    "docker-compose.yml",
    "docker-compose.yaml",
    "requirements.txt",
    "setup.py",
    "composer.json",
    "Makefile",
}
SOURCE_FILE_MARKERS = {
    "package.json",
    "pyproject.toml",
    "go.mod",
    "Cargo.toml",
    "requirements.txt",
    "setup.py",
    "composer.json",
}
SOURCE_DIR_MARKERS = {"src", "app", "lib", "pkg", "cmd", "internal", "tests", ".git"}
DEPLOY_FILE_MARKERS = {
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    "dockerfile",
    "procfile",
    "nginx.conf",
    "caddyfile",
    ".env",
}
DEPLOY_DIR_MARKERS = {"dist", "build", "release", "public", "static", ".next", "node_modules"}
SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cc", ".cpp",
    ".h", ".hpp", ".rb", ".php", ".swift", ".kt", ".m", ".scala", ".sh",
}
DEPLOY_EXTENSIONS = {".tar", ".tgz", ".gz", ".zip", ".jar", ".war", ".deb", ".rpm", ".min.js"}
SKIP_WALK_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache", ".pytest_cache"}
TEXT_SAMPLE_BYTES = 4096


_WRITE_DENY_EXACT = {
    "/etc/passwd",
    "/etc/shadow",
}
_WRITE_DENY_PREFIXES = (
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/lib",
    "/lib64",
    "/usr/lib",
    "/usr/lib64",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
)


def _deny_mutation_path(path):
    normalized = os.path.normpath(path)
    if normalized in _WRITE_DENY_EXACT:
        raise PermissionError(f"remote write denied for critical path: {normalized}")
    for prefix in _WRITE_DENY_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + os.sep):
            raise PermissionError(f"remote write denied for system/binary path: {normalized}")
    return normalized


def _guard_mutation_path(path):
    normalized = os.path.normpath(path)
    _deny_mutation_path(normalized)
    _deny_mutation_path(os.path.realpath(normalized))
    parent = os.path.dirname(normalized) or "/"
    _deny_mutation_path(os.path.realpath(parent))
    return normalized


def _json_print(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def _mtime_iso(ts):
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_child_names(path):
    if not os.path.isdir(path):
        return []
    try:
        return sorted(os.listdir(path))
    except Exception:
        return []


def _entry_type(mode):
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _markers_for_path(path, entry_type, child_names=None):
    name = os.path.basename(path)
    name_lower = name.lower()
    markers = set()
    children = child_names or []
    child_lower = {item.lower() for item in children}
    if entry_type == "directory":
        for marker in PROJECT_MARKERS:
            if marker.lower() in child_lower:
                markers.add(marker)
    else:
        if name in PROJECT_MARKERS or name_lower in {m.lower() for m in PROJECT_MARKERS}:
            markers.add(name)
    return sorted(markers)


def _classify_path(path, st, child_names=None):
    entry_type = _entry_type(st.st_mode)
    name = os.path.basename(path)
    name_lower = name.lower()
    child_names = child_names or []
    child_lower = {item.lower() for item in child_names}
    markers = _markers_for_path(path, entry_type, child_names)
    ext = os.path.splitext(name_lower)[1]

    if entry_type == "directory":
        is_source_tree = bool(child_lower.intersection({item.lower() for item in SOURCE_DIR_MARKERS})) or bool(markers)
        is_deploy_artifact = bool(child_lower.intersection({item.lower() for item in DEPLOY_DIR_MARKERS})) or name_lower in DEPLOY_DIR_MARKERS
        is_project_like = bool(markers) or (
            is_source_tree and bool(child_lower.intersection({"package.json", "pyproject.toml", "go.mod", "cargo.toml", "requirements.txt"}))
        )
    else:
        is_source_tree = ext in SOURCE_EXTENSIONS or name in SOURCE_FILE_MARKERS or name_lower in {x.lower() for x in SOURCE_FILE_MARKERS}
        is_deploy_artifact = ext in DEPLOY_EXTENSIONS or name_lower in DEPLOY_FILE_MARKERS or any(part in {"dist", "build", "release"} for part in path.lower().split(os.sep))
        is_project_like = bool(markers) or is_source_tree

    return {
        "absolute_path": os.path.abspath(path),
        "type": entry_type,
        "size": int(st.st_size),
        "mtime": _mtime_iso(st.st_mtime),
        "is_project_like": bool(is_project_like),
        "is_deploy_artifact": bool(is_deploy_artifact),
        "is_source_tree": bool(is_source_tree),
        "markers": markers,
    }


def _default_base_root(payload):
    root = str(payload.get("base_root") or "").strip()
    if root:
        return root
    return "/"


def _resolve_path(payload, raw_path, *, fallback_to_base=False):
    base_root = _default_base_root(payload)
    value = str(raw_path or "").strip()
    if not value:
        return os.path.abspath(base_root if fallback_to_base else ".")
    expanded = os.path.expanduser(value)
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(base_root, expanded))


def _is_text_file(path):
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(TEXT_SAMPLE_BYTES)
    except Exception:
        return False
    if b"\x00" in chunk:
        return False
    return True


def _path_stat(path):
    st = os.lstat(path)
    child_names = _safe_child_names(path) if stat.S_ISDIR(st.st_mode) else []
    return _classify_path(path, st, child_names)


def _list_dir(payload):
    path = _resolve_path(payload, payload.get("path"), fallback_to_base=True)
    max_entries = max(1, min(int(payload.get("max_entries", 200)), 1000))
    if not os.path.exists(path):
        raise FileNotFoundError(f"remote path not found: {path}")
    if not os.path.isdir(path):
        raise NotADirectoryError(f"remote path is not a directory: {path}")
    rows = []
    for idx, name in enumerate(sorted(os.listdir(path))):
        if idx >= max_entries:
            break
        child = os.path.join(path, name)
        rows.append(_path_stat(child))
    return {
        "status": "ok",
        "root": _path_stat(path),
        "count": len(rows),
        "truncated": len(os.listdir(path)) > len(rows),
        "entries": rows,
    }


def _read_file(payload):
    path = _resolve_path(payload, payload.get("path"))
    max_chars = max(1, min(int(payload.get("max_chars", 12000)), 200000))
    if not os.path.exists(path):
        raise FileNotFoundError(f"remote path not found: {path}")
    if not os.path.isfile(path):
        raise IsADirectoryError(f"remote path is not a regular file: {path}")
    if not _is_text_file(path):
        raise ValueError(f"remote file is binary or not safely readable as text: {path}")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        content = fh.read(max_chars + 1)
    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars]
    return {
        "status": "ok",
        "file": _path_stat(path),
        "content": content,
        "truncated": truncated,
        "max_chars": max_chars,
    }


def _mkdir(payload):
    path = _guard_mutation_path(_resolve_path(payload, payload.get("path")))
    os.makedirs(path, exist_ok=True)
    return {"status": "ok", "entry": _path_stat(path)}


def _write_file(payload):
    path = _guard_mutation_path(_resolve_path(payload, payload.get("path")))
    mode = str(payload.get("mode") or "overwrite").strip().lower()
    if mode not in {"overwrite", "append"}:
        raise ValueError("mode must be 'overwrite' or 'append'")
    content = payload.get("content")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    parent = os.path.dirname(path) or "/"
    if not os.path.isdir(parent):
        raise FileNotFoundError(f"remote parent directory not found: {parent}")
    existed_before = os.path.exists(path)
    with open(path, "a" if mode == "append" else "w", encoding="utf-8") as fh:
        fh.write(content)
    return {
        "status": "ok",
        "entry": _path_stat(path),
        "mode": mode,
        "chars_written": len(content),
        "bytes_written": len(content.encode("utf-8")),
        "previously_existed": existed_before,
    }


def _walk_paths(root, max_depth):
    root = os.path.abspath(root)
    base_depth = root.rstrip(os.sep).count(os.sep)
    for current_root, dirs, files in os.walk(root):
        depth = current_root.rstrip(os.sep).count(os.sep) - base_depth
        dirs[:] = [d for d in sorted(dirs) if d not in SKIP_WALK_DIRS]
        if depth >= max_depth:
            dirs[:] = []
        yield current_root, dirs, sorted(files), depth


def _find(payload):
    root = _resolve_path(payload, payload.get("root"), fallback_to_base=True)
    name_glob = str(payload.get("name_glob") or "*")
    max_depth = max(0, min(int(payload.get("max_depth", 6)), 32))
    max_results = max(1, min(int(payload.get("max_results", 100)), 500))
    if not os.path.exists(root):
        raise FileNotFoundError(f"remote root not found: {root}")
    rows = []
    for current_root, dirs, files, _depth in _walk_paths(root, max_depth):
        for name in list(dirs) + list(files):
            if not fnmatch.fnmatch(name, name_glob):
                continue
            rows.append(_path_stat(os.path.join(current_root, name)))
            if len(rows) >= max_results:
                return {
                    "status": "ok",
                    "root": _path_stat(root),
                    "query": {"name_glob": name_glob, "max_depth": max_depth},
                    "count": len(rows),
                    "truncated": True,
                    "matches": rows,
                }
    return {
        "status": "ok",
        "root": _path_stat(root),
        "query": {"name_glob": name_glob, "max_depth": max_depth},
        "count": len(rows),
        "truncated": False,
        "matches": rows,
    }


def _grep(payload):
    root = _resolve_path(payload, payload.get("root"), fallback_to_base=True)
    query = str(payload.get("query") or "")
    glob_pattern = str(payload.get("glob") or "*")
    ignore_case = bool(payload.get("ignore_case", False))
    max_depth = max(0, min(int(payload.get("max_depth", 6)), 32))
    max_results = max(1, min(int(payload.get("max_results", 100)), 500))
    if not query:
        raise ValueError("query must be non-empty")
    if not os.path.exists(root):
        raise FileNotFoundError(f"remote root not found: {root}")
    flags = re.IGNORECASE if ignore_case else 0
    regex = re.compile(query, flags)
    rows = []
    for current_root, _dirs, files, _depth in _walk_paths(root, max_depth):
        for name in files:
            if not fnmatch.fnmatch(name, glob_pattern):
                continue
            path = os.path.join(current_root, name)
            if not _is_text_file(path):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    for line_no, line in enumerate(fh, 1):
                        if not regex.search(line):
                            continue
                        rows.append({
                            "path": _path_stat(path),
                            "line_number": line_no,
                            "line": line.rstrip("\n")[:500],
                        })
                        if len(rows) >= max_results:
                            return {
                                "status": "ok",
                                "root": _path_stat(root),
                                "query": {
                                    "query": query,
                                    "glob": glob_pattern,
                                    "ignore_case": ignore_case,
                                    "max_depth": max_depth,
                                },
                                "count": len(rows),
                                "truncated": True,
                                "matches": rows,
                            }
            except Exception:
                continue
    return {
        "status": "ok",
        "root": _path_stat(root),
        "query": {
            "query": query,
            "glob": glob_pattern,
            "ignore_case": ignore_case,
            "max_depth": max_depth,
        },
        "count": len(rows),
        "truncated": False,
        "matches": rows,
    }


def _discover_projects(payload):
    roots = payload.get("roots") or []
    if not isinstance(roots, list) or not roots:
        roots = [_default_base_root(payload)]
    max_depth = max(0, min(int(payload.get("max_depth", 6)), 24))
    max_results = max(1, min(int(payload.get("max_results", 50)), 200))
    projects = []
    seen = set()
    for raw_root in roots:
        root = _resolve_path(payload, raw_root, fallback_to_base=True)
        if not os.path.exists(root):
            continue
        for current_root, dirs, files, _depth in _walk_paths(root, max_depth):
            child_names = list(dirs) + list(files)
            child_lower = {item.lower() for item in child_names}
            matched_markers = sorted(marker for marker in PROJECT_MARKERS if marker.lower() in child_lower)
            if not matched_markers:
                continue
            path = os.path.abspath(current_root)
            if path in seen:
                continue
            seen.add(path)
            st = os.lstat(path)
            info = _classify_path(path, st, child_names)
            info["project_markers"] = matched_markers
            projects.append(info)
            if len(projects) >= max_results:
                return {
                    "status": "ok",
                    "roots": [_resolve_path(payload, item, fallback_to_base=True) for item in roots],
                    "count": len(projects),
                    "truncated": True,
                    "projects": projects,
                }
    return {
        "status": "ok",
        "roots": [_resolve_path(payload, item, fallback_to_base=True) for item in roots],
        "count": len(projects),
        "truncated": False,
        "projects": projects,
    }


def main(request_b64):
    try:
        request = json.loads(base64.b64decode(request_b64).decode("utf-8"))
        op = request.get("op")
        payload = request.get("payload") or {}
        if op == "list_dir":
            result = _list_dir(payload)
        elif op == "read_file":
            result = _read_file(payload)
        elif op == "stat":
            path = _resolve_path(payload, payload.get("path"), fallback_to_base=True)
            if not os.path.exists(path):
                raise FileNotFoundError(f"remote path not found: {path}")
            result = {"status": "ok", "entry": _path_stat(path)}
        elif op == "mkdir":
            result = _mkdir(payload)
        elif op == "write_file":
            result = _write_file(payload)
        elif op == "find":
            result = _find(payload)
        elif op == "grep":
            result = _grep(payload)
        elif op == "project_discover":
            result = _discover_projects(payload)
        else:
            raise ValueError(f"unknown remote filesystem op: {op}")
        _json_print(result)
    except Exception as exc:
        _json_print({"status": "error", "error": str(exc), "kind": exc.__class__.__name__})
'''


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


def _remote_python_command(op: str, payload: Dict[str, Any]) -> str:
    request_b64 = base64.b64encode(json.dumps({"op": op, "payload": payload}, ensure_ascii=False).encode("utf-8")).decode("ascii")
    script_b64 = base64.b64encode(_REMOTE_FS_SCRIPT.encode("utf-8")).decode("ascii")
    wrapper = (
        "import base64;ns={};"
        f"exec(base64.b64decode({script_b64!r}).decode('utf-8'), ns);"
        f"ns['main']({request_b64!r})"
    )
    return f"python3 -c {shlex.quote(wrapper)}"


def _normalize_remote_exec_error(stderr: str, returncode: int) -> RuntimeError:
    lowered = (stderr or "").lower()
    if "python3: not found" in lowered or "python3: command not found" in lowered:
        return RuntimeError("remote_python_missing: python3 is required on the target host")
    return _normalize_probe_error(stderr, returncode)


def _base_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    return {"base_root": record.get("default_remote_root") or "/"}


def _normalize_write_mode(value: Any) -> str:
    mode = str(value or "overwrite").strip().lower()
    if mode not in {"overwrite", "append"}:
        raise ValueError("mode must be either 'overwrite' or 'append'")
    return mode


def _append_remote_fs_event(ctx: ToolContext, operation: str, alias: str, path: str, status: str, **extra: Any) -> None:
    event = {
        "type": "remote_filesystem",
        "operation": operation,
        "alias": alias,
        "path": path,
        "status": status,
        "ts": utc_now_iso(),
    }
    event.update({k: v for k, v in extra.items() if v is not None})
    ctx.pending_events.append(event)


def _error_payload(exc: Exception) -> str:
    return json.dumps({"status": "error", "kind": exc.__class__.__name__, "error": str(exc)}, ensure_ascii=False)


def _run_remote_fs(ctx: ToolContext, alias: str, op: str, payload: Dict[str, Any], timeout: int = 40) -> Dict[str, Any]:
    _bootstrap_session(ctx, alias)
    record = _get_target_record(ctx, alias)
    command = _remote_python_command(op, {**_base_payload(record), **payload})
    probe = _run_ssh_probe(ctx, record, command=command, timeout=timeout)
    if probe.returncode != 0:
        raise _normalize_remote_exec_error(probe.stderr, probe.returncode)
    stdout = (probe.stdout or "").strip()
    if not stdout:
        raise RuntimeError("remote filesystem tool returned empty stdout")
    result = json.loads(stdout)
    result["target"] = _public_target_view(record)
    result["bootstrap"] = "reused"
    if result.get("status") == "error":
        raise RuntimeError(result.get("error") or "remote filesystem operation failed")
    return result


def _remote_list_dir(ctx: ToolContext, alias: str, path: str = "", max_entries: int = 200) -> str:
    return json.dumps(_run_remote_fs(ctx, alias, "list_dir", {"path": path, "max_entries": max_entries}), ensure_ascii=False)


def _remote_read_file(ctx: ToolContext, alias: str, path: str, max_chars: int = 12000) -> str:
    return json.dumps(_run_remote_fs(ctx, alias, "read_file", {"path": path, "max_chars": max_chars}), ensure_ascii=False)


def _remote_stat(ctx: ToolContext, alias: str, path: str = "") -> str:
    return json.dumps(_run_remote_fs(ctx, alias, "stat", {"path": path}), ensure_ascii=False)


def _remote_mkdir(ctx: ToolContext, alias: str, path: str) -> str:
    try:
        result = _run_remote_fs(ctx, alias, "mkdir", {"path": path})
    except Exception as exc:
        _append_remote_fs_event(ctx, "mkdir", alias, path, "error", error=str(exc))
        return _error_payload(exc)
    entry = result.get("entry") if isinstance(result.get("entry"), dict) else {}
    _append_remote_fs_event(ctx, "mkdir", alias, path, "ok", remote_absolute_path=entry.get("absolute_path"), entry_type=entry.get("type"))
    return json.dumps(result, ensure_ascii=False)


def _remote_write_file(ctx: ToolContext, alias: str, path: str, content: str, mode: str = "overwrite") -> str:
    try:
        normalized_mode = _normalize_write_mode(mode)
    except Exception as exc:
        _append_remote_fs_event(ctx, "write_file", alias, path, "error", error=str(exc))
        return _error_payload(exc)
    try:
        result = _run_remote_fs(
            ctx,
            alias,
            "write_file",
            {"path": path, "content": content, "mode": normalized_mode},
            timeout=60,
        )
    except Exception as exc:
        _append_remote_fs_event(
            ctx,
            "write_file",
            alias,
            path,
            "error",
            mode=normalized_mode,
            chars_written=len(content),
            bytes_written=len(content.encode("utf-8")),
            error=str(exc),
        )
        return _error_payload(exc)
    entry = result.get("entry") if isinstance(result.get("entry"), dict) else {}
    _append_remote_fs_event(
        ctx,
        "write_file",
        alias,
        path,
        "ok",
        mode=normalized_mode,
        chars_written=result.get("chars_written", len(content)),
        bytes_written=result.get("bytes_written", len(content.encode("utf-8"))),
        remote_absolute_path=entry.get("absolute_path"),
        previously_existed=result.get("previously_existed"),
    )
    return json.dumps(result, ensure_ascii=False)


def _remote_find(ctx: ToolContext, alias: str, root: str = "", name_glob: str = "*", max_depth: int = 6, max_results: int = 100) -> str:
    return json.dumps(
        _run_remote_fs(
            ctx,
            alias,
            "find",
            {"root": root, "name_glob": name_glob, "max_depth": max_depth, "max_results": max_results},
        ),
        ensure_ascii=False,
    )


def _remote_grep(
    ctx: ToolContext,
    alias: str,
    query: str,
    root: str = "",
    glob: str = "*",
    ignore_case: bool = False,
    max_depth: int = 6,
    max_results: int = 100,
) -> str:
    return json.dumps(
        _run_remote_fs(
            ctx,
            alias,
            "grep",
            {
                "query": query,
                "root": root,
                "glob": glob,
                "ignore_case": ignore_case,
                "max_depth": max_depth,
                "max_results": max_results,
            },
        ),
        ensure_ascii=False,
    )


def _discovery_roots(record: Dict[str, Any], roots: List[str] | None) -> List[str]:
    items: List[str] = []
    for item in roots or []:
        text = str(item or "").strip()
        if text and text not in items:
            items.append(text)
    default_root = str(record.get("default_remote_root") or "").strip()
    if default_root and default_root not in items:
        items.append(default_root)
    for item in record.get("known_projects_paths") or []:
        text = str(item or "").strip()
        if text and text not in items:
            items.append(text)
    if not items:
        items.append(default_root or "/")
    return items


def _remote_project_discover(
    ctx: ToolContext,
    alias: str,
    roots: List[str] | None = None,
    max_depth: int = 6,
    max_results: int = 50,
) -> str:
    record = _get_target_record(ctx, alias)
    payload = {
        "roots": _discovery_roots(record, roots),
        "max_depth": max_depth,
        "max_results": max_results,
    }
    return json.dumps(_run_remote_fs(ctx, alias, "project_discover", payload, timeout=60), ensure_ascii=False)


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            "remote_list_dir",
            "List a directory on a registered SSH target with normalized file metadata and project/deploy/source hints.",
            {
                "alias": {"type": "string", "description": "SSH target alias"},
                "path": {"type": "string", "description": "Remote directory path; relative paths resolve from target default_remote_root"},
                "max_entries": {"type": "integer", "description": "Maximum number of entries to return (default 200)"},
            },
            ["alias"],
            _remote_list_dir,
        ),
        _tool_entry(
            "remote_read_file",
            "Read a text file from a registered SSH target with normalized metadata.",
            {
                "alias": {"type": "string", "description": "SSH target alias"},
                "path": {"type": "string", "description": "Remote file path"},
                "max_chars": {"type": "integer", "description": "Maximum number of characters to read (default 12000)"},
            },
            ["alias", "path"],
            _remote_read_file,
        ),
        _tool_entry(
            "remote_stat",
            "Get normalized metadata for a remote path on a registered SSH target.",
            {
                "alias": {"type": "string", "description": "SSH target alias"},
                "path": {"type": "string", "description": "Remote path; blank means target default_remote_root"},
            },
            ["alias"],
            _remote_stat,
        ),
        _tool_entry(
            "remote_mkdir",
            "Create a directory on a registered SSH target with path guardrails against critical system locations.",
            {
                "alias": {"type": "string", "description": "SSH target alias"},
                "path": {"type": "string", "description": "Remote directory path to create"},
            },
            ["alias", "path"],
            _remote_mkdir,
        ),
        _tool_entry(
            "remote_write_file",
            "Write a text file on a registered SSH target with overwrite/append modes and guardrails against critical system paths.",
            {
                "alias": {"type": "string", "description": "SSH target alias"},
                "path": {"type": "string", "description": "Remote file path"},
                "content": {"type": "string", "description": "Text content to write"},
                "mode": {"type": "string", "description": "Write mode: overwrite (default) or append"},
            },
            ["alias", "path", "content"],
            _remote_write_file,
            is_code_tool=True,
        ),
        _tool_entry(
            "remote_find",
            "Find remote filesystem entries by glob name under a registered SSH target.",
            {
                "alias": {"type": "string", "description": "SSH target alias"},
                "root": {"type": "string", "description": "Remote search root; relative paths resolve from target default_remote_root"},
                "name_glob": {"type": "string", "description": "Glob for basename matching (default *)"},
                "max_depth": {"type": "integer", "description": "Maximum recursion depth (default 6)"},
                "max_results": {"type": "integer", "description": "Maximum number of matches (default 100)"},
            },
            ["alias"],
            _remote_find,
        ),
        _tool_entry(
            "remote_grep",
            "Search text content on a registered SSH target and return normalized file matches.",
            {
                "alias": {"type": "string", "description": "SSH target alias"},
                "query": {"type": "string", "description": "Regex query to search for"},
                "root": {"type": "string", "description": "Remote search root; relative paths resolve from target default_remote_root"},
                "glob": {"type": "string", "description": "Filename glob filter (default *)"},
                "ignore_case": {"type": "boolean", "description": "Case-insensitive search"},
                "max_depth": {"type": "integer", "description": "Maximum recursion depth (default 6)"},
                "max_results": {"type": "integer", "description": "Maximum number of matches (default 100)"},
            },
            ["alias", "query"],
            _remote_grep,
        ),
        _tool_entry(
            "remote_project_discover",
            "Discover likely project roots on a registered SSH target by common repository and build markers.",
            {
                "alias": {"type": "string", "description": "SSH target alias"},
                "roots": {"type": "array", "items": {"type": "string"}, "description": "Optional list of remote roots to scan"},
                "max_depth": {"type": "integer", "description": "Maximum recursion depth (default 6)"},
                "max_results": {"type": "integer", "description": "Maximum number of discovered projects (default 50)"},
            },
            ["alias"],
            _remote_project_discover,
        ),
    ]
