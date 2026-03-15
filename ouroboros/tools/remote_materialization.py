from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import shlex
import subprocess
import time
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.ssh_targets import _base_ssh_command, _bootstrap_session, _get_target_record, _public_target_view
from ouroboros.utils import safe_relpath

_REMOTE_MATERIALIZE_SCRIPT = r'''
import base64
import datetime as _dt
import fnmatch
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile

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
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "Makefile",
    ".gitignore",
    ".env.example",
    "Procfile",
    "README",
    "README.md",
    "README.txt",
    "LICENSE",
    "LICENSE.md",
}
SOURCE_DIR_HINTS = {"src", "app", "lib", "pkg", "cmd", "internal", "tests", "scripts", "config", ".github"}
HEAVY_DIRS = {"node_modules", ".venv", "venv", "dist", "build", ".next", ".cache", "__pycache__", ".pytest_cache", "coverage", "target", "vendor", "bin"}
SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cc", ".cpp",
    ".h", ".hpp", ".rb", ".php", ".swift", ".kt", ".m", ".scala", ".sh", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".sql", ".html", ".css", ".scss",
    ".md", ".txt", ".env",
}
DEPLOY_EXTENSIONS = {".tar", ".tgz", ".gz", ".zip", ".jar", ".war", ".deb", ".rpm", ".so", ".dll", ".exe", ".bin", ".min.js"}
KEY_HASH_NAMES = {
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "pyproject.toml", "requirements.txt",
    "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "composer.json", "composer.lock", "Dockerfile",
    "docker-compose.yml", "docker-compose.yaml", "Makefile", "README.md", "README", "setup.py",
}


def _json_print(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def _mtime_iso(ts):
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_root(payload):
    value = str(payload.get("remote_path") or payload.get("path") or "").strip()
    if not value:
        raise ValueError("remote_path is required")
    expanded = os.path.expanduser(value)
    return os.path.abspath(expanded)


def _normalize_patterns(values):
    result = []
    for item in values or []:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _child_names(path):
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


def _classify_root(root):
    st = os.lstat(root)
    children = _child_names(root) if stat.S_ISDIR(st.st_mode) else []
    child_lower = {item.lower() for item in children}
    name = os.path.basename(root).lower()
    markers = sorted(marker for marker in PROJECT_MARKERS if marker.lower() in child_lower)
    is_source_tree = bool(child_lower.intersection({x.lower() for x in SOURCE_DIR_HINTS})) or bool(markers)
    is_deploy_artifact = bool(child_lower.intersection({x.lower() for x in HEAVY_DIRS})) or name in {"dist", "build", "release", "public"}
    is_project_like = bool(markers) or (is_source_tree and bool(child_lower.intersection({"package.json", "pyproject.toml", "go.mod", "cargo.toml", "requirements.txt"})))
    return {
        "absolute_path": os.path.abspath(root),
        "type": _entry_type(st.st_mode),
        "size": int(st.st_size),
        "mtime": _mtime_iso(st.st_mtime),
        "is_project_like": bool(is_project_like),
        "is_deploy_artifact": bool(is_deploy_artifact),
        "is_source_tree": bool(is_source_tree),
        "markers": markers,
    }


def _match_any(path, patterns):
    normalized = path.replace(os.sep, "/")
    base = os.path.basename(normalized)
    for pattern in patterns:
        if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(base, pattern):
            return True
    return False


def _is_source_candidate(rel_path):
    rel = rel_path.replace(os.sep, "/")
    base = os.path.basename(rel)
    parts = [part for part in rel.split("/") if part]
    ext = os.path.splitext(base.lower())[1]
    if base in SOURCE_FILE_MARKERS:
        return True
    if base.lower() in {item.lower() for item in SOURCE_FILE_MARKERS}:
        return True
    if ext in SOURCE_EXTENSIONS:
        return True
    if any(part in SOURCE_DIR_HINTS for part in parts):
        return True
    return False


def _key_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _snapshot_kind(root_info, selected_files, requested_kind):
    forced = str(requested_kind or "auto").strip()
    if forced in {"source_snapshot", "deployment_artifact_snapshot"}:
        return forced
    if root_info.get("is_source_tree") or root_info.get("markers"):
        return "source_snapshot"
    source_votes = 0
    deploy_votes = 0
    for item in selected_files[:200]:
        rel = item["relative_path"]
        ext = os.path.splitext(rel.lower())[1]
        if _is_source_candidate(rel):
            source_votes += 1
        if ext in DEPLOY_EXTENSIONS or any(part in {"dist", "build", "release", "public"} for part in rel.lower().split("/")):
            deploy_votes += 1
    return "source_snapshot" if source_votes >= deploy_votes else "deployment_artifact_snapshot"


def _plan(payload):
    root = _resolve_root(payload)
    if not os.path.exists(root):
        raise FileNotFoundError(f"remote path not found: {root}")
    if not os.path.isdir(root):
        raise NotADirectoryError(f"remote path is not a directory: {root}")

    mode = str(payload.get("mode") or "full").strip().lower()
    if mode not in {"full", "source_only"}:
        raise ValueError("mode must be 'full' or 'source_only'")
    exclude_heavy_dirs = bool(payload.get("exclude_heavy_dirs", False))
    exclude_patterns = _normalize_patterns(payload.get("exclude_patterns"))
    max_files = max(1, min(int(payload.get("max_files", 20000)), 100000))

    root_info = _classify_root(root)
    selected_files = []
    stats_payload = {
        "excluded_by_policy_count": 0,
        "excluded_heavy_dir_count": 0,
        "excluded_source_filter_count": 0,
        "non_regular_skipped_count": 0,
        "plan_truncated": False,
        "selected_bytes": 0,
    }

    for current_root, dirs, files in os.walk(root, topdown=True, followlinks=False):
        rel_dir = os.path.relpath(current_root, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")

        next_dirs = []
        for name in sorted(dirs):
            rel_child = f"{rel_dir}/{name}".strip("/")
            if exclude_heavy_dirs and name in HEAVY_DIRS:
                stats_payload["excluded_heavy_dir_count"] += 1
                continue
            if _match_any(rel_child, exclude_patterns):
                stats_payload["excluded_by_policy_count"] += 1
                continue
            next_dirs.append(name)
        dirs[:] = next_dirs

        for name in sorted(files):
            full_path = os.path.join(current_root, name)
            rel_path = os.path.relpath(full_path, root).replace(os.sep, "/")
            if _match_any(rel_path, exclude_patterns):
                stats_payload["excluded_by_policy_count"] += 1
                continue
            st = os.lstat(full_path)
            if not stat.S_ISREG(st.st_mode):
                stats_payload["non_regular_skipped_count"] += 1
                continue
            if mode == "source_only" and not _is_source_candidate(rel_path):
                stats_payload["excluded_source_filter_count"] += 1
                continue
            entry = {
                "relative_path": rel_path,
                "size": int(st.st_size),
                "mtime": _mtime_iso(st.st_mtime),
                "sha256": _key_hash(full_path) if name in KEY_HASH_NAMES else "",
                "is_key_file": bool(name in KEY_HASH_NAMES),
            }
            selected_files.append(entry)
            stats_payload["selected_bytes"] += int(st.st_size)
            if len(selected_files) >= max_files:
                stats_payload["plan_truncated"] = True
                break
        if stats_payload["plan_truncated"]:
            break

    snapshot_kind = _snapshot_kind(root_info, selected_files, payload.get("snapshot_kind"))
    return {
        "status": "ok",
        "remote_root": root_info,
        "snapshot_kind": snapshot_kind,
        "mode": mode,
        "exclude_heavy_dirs": exclude_heavy_dirs,
        "exclude_patterns": exclude_patterns,
        "selection": {
            **stats_payload,
            "selected_file_count": len(selected_files),
        },
        "files": selected_files,
    }


def _emit_tar(payload):
    plan = _plan(payload)
    root = plan["remote_root"]["absolute_path"]
    files = plan["files"]
    if not files:
        return
    with tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
        tmp_path = tmp.name
        for item in files:
            tmp.write(item["relative_path"].encode("utf-8") + b"\0")
    try:
        os.execvp("tar", ["tar", "-C", root, "--null", "-T", tmp_path, "-cf", "-"])
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def main(request_b64):
    try:
        request = json.loads(base64.b64decode(request_b64).decode("utf-8"))
        op = request.get("op")
        payload = request.get("payload") or {}
        if op == "plan":
            _json_print(_plan(payload))
        elif op == "tar":
            _emit_tar(payload)
        else:
            raise ValueError(f"unknown remote materialize op: {op}")
    except Exception as exc:
        _json_print({"status": "error", "error": str(exc), "kind": exc.__class__.__name__})
'''


def _tool_entry(
    name: str,
    description: str,
    properties: Dict[str, Any],
    required: List[str],
    handler,
    *,
    is_code_tool: bool = False,
    timeout_sec: int = 120,
) -> ToolEntry:
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
        timeout_sec=timeout_sec,
    )


def _remote_materialize_command(op: str, payload: Dict[str, Any]) -> str:
    request_b64 = base64.b64encode(json.dumps({"op": op, "payload": payload}, ensure_ascii=False).encode("utf-8")).decode("ascii")
    script_b64 = base64.b64encode(_REMOTE_MATERIALIZE_SCRIPT.encode("utf-8")).decode("ascii")
    wrapper = (
        "import base64;ns={};"
        f"exec(base64.b64decode({script_b64!r}).decode('utf-8'), ns);"
        f"ns['main']({request_b64!r})"
    )
    return f"python3 -c {shlex.quote(wrapper)}"


def _slugify(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in (value or "").strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned or "snapshot"


def _prepare_ssh_subprocess(ctx: ToolContext, record: Dict[str, Any], remote_command: str) -> tuple[List[str], Dict[str, str]]:
    ssh_cmd = _base_ssh_command(ctx, record)
    ssh_cmd.append(remote_command)
    env = os.environ.copy()
    password = record.get("password", "") if record.get("auth_mode") == "password" else ""
    if password:
        askpass_script = "#!/bin/sh\nprintf '%s' \"$VELES_SSH_PASSWORD\"\n"
        askpass_path = ctx.drive_path("state/ssh_askpass.sh")
        askpass_path.parent.mkdir(parents=True, exist_ok=True)
        askpass_path.write_text(askpass_script, encoding="utf-8")
        askpass_path.chmod(0o700)
        env["SSH_ASKPASS"] = str(askpass_path)
        env["VELES_SSH_PASSWORD"] = password
        env["DISPLAY"] = env.get("DISPLAY") or "veles-ssh"
        ssh_cmd = ["setsid", "-w", "env", "SSH_ASKPASS_REQUIRE=force", *ssh_cmd]
    return ssh_cmd, env


def _run_remote_plan(ctx: ToolContext, alias: str, payload: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
    _bootstrap_session(ctx, alias)
    record = _get_target_record(ctx, alias)
    command = _remote_materialize_command("plan", payload)
    ssh_cmd, env = _prepare_ssh_subprocess(ctx, record, command)
    probe = subprocess.run(ssh_cmd, cwd=ctx.repo_dir, capture_output=True, text=True, timeout=timeout, env=env)
    if probe.returncode != 0:
        raise RuntimeError((probe.stderr or "").strip() or f"ssh materialize plan failed with exit code {probe.returncode}")
    stdout = (probe.stdout or "").strip()
    if not stdout:
        raise RuntimeError("remote materialization plan returned empty stdout")
    result = json.loads(stdout)
    if result.get("status") == "error":
        raise RuntimeError(result.get("error") or "remote materialization planning failed")
    result["target"] = _public_target_view(record)
    return result


def _stream_remote_tar_to_local(ctx: ToolContext, record: Dict[str, Any], payload: Dict[str, Any], destination_dir: pathlib.Path, timeout: int = 600) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    command = _remote_materialize_command("tar", payload)
    ssh_cmd, env = _prepare_ssh_subprocess(ctx, record, command)
    ssh_proc = subprocess.Popen(ssh_cmd, cwd=ctx.repo_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    assert ssh_proc.stdout is not None
    tar_proc = subprocess.Popen(
        ["tar", "-xf", "-", "-C", str(destination_dir)],
        stdin=ssh_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ssh_proc.stdout.close()
    tar_stdout, tar_stderr = tar_proc.communicate(timeout=timeout)
    ssh_stderr = ssh_proc.stderr.read().decode("utf-8", errors="replace") if ssh_proc.stderr else ""
    ssh_returncode = ssh_proc.wait(timeout=timeout)
    if ssh_returncode != 0:
        raise RuntimeError((ssh_stderr or "").strip() or f"remote tar stream failed with exit code {ssh_returncode}")
    if tar_proc.returncode != 0:
        raise RuntimeError((tar_stderr.decode("utf-8", errors="replace") or "").strip() or "local tar extraction failed")
    if tar_stdout:
        _ = tar_stdout


def _snapshot_root(ctx: ToolContext, alias: str, remote_path: str, snapshot_kind: str, destination_label: str = "") -> pathlib.Path:
    label = destination_label.strip() or pathlib.PurePosixPath(remote_path).name or alias
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    rel = safe_relpath(f"state/remote-materializations/{_slugify(alias)}/{_slugify(label)}/{stamp}-{snapshot_kind}")
    return ctx.drive_path(rel)


def _local_sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _build_manifest(
    *,
    alias: str,
    payload: Dict[str, Any],
    plan: Dict[str, Any],
    snapshot_dir: pathlib.Path,
    files_dir: pathlib.Path,
) -> Dict[str, Any]:
    selected_files = list(plan.get("files") or [])
    missing_files: List[str] = []
    key_hash_mismatches: List[str] = []
    extracted_count = 0
    extracted_bytes = 0
    for item in selected_files:
        rel = item["relative_path"]
        local_path = files_dir / pathlib.PurePosixPath(rel)
        if not local_path.exists():
            missing_files.append(rel)
            continue
        extracted_count += 1
        extracted_bytes += local_path.stat().st_size
        remote_hash = item.get("sha256") or ""
        if remote_hash and _local_sha256(local_path) != remote_hash:
            key_hash_mismatches.append(rel)

    signals: List[str] = []
    selection = dict(plan.get("selection") or {})
    if selection.get("excluded_by_policy_count"):
        signals.append("policy_exclusions_applied")
    if selection.get("excluded_heavy_dir_count"):
        signals.append("heavy_directories_excluded")
    if selection.get("excluded_source_filter_count"):
        signals.append("source_only_filter_applied")
    if selection.get("non_regular_skipped_count"):
        signals.append("non_regular_entries_skipped")
    if selection.get("plan_truncated"):
        signals.append("selection_truncated")
    if missing_files:
        signals.append("missing_local_files_after_transfer")
    if key_hash_mismatches:
        signals.append("key_file_hash_mismatch")
    if plan.get("snapshot_kind") == "deployment_artifact_snapshot":
        signals.append("deployment_artifact_snapshot")

    manifest = {
        "status": "ok",
        "alias": alias,
        "target": plan.get("target") or {},
        "remote_root": plan.get("remote_root") or {},
        "snapshot_kind": plan.get("snapshot_kind"),
        "mode": payload.get("mode", "full"),
        "exclude_heavy_dirs": bool(payload.get("exclude_heavy_dirs", False)),
        "exclude_patterns": list(payload.get("exclude_patterns") or []),
        "local_snapshot_dir": str(snapshot_dir),
        "local_files_dir": str(files_dir),
        "selection": selection,
        "integrity": {
            "planned_file_count": len(selected_files),
            "extracted_file_count": extracted_count,
            "planned_bytes": selection.get("selected_bytes", 0),
            "extracted_bytes": extracted_bytes,
            "missing_files": missing_files,
            "key_hash_mismatches": key_hash_mismatches,
            "incomplete_copy_signals": signals,
        },
        "files": selected_files,
    }
    manifest_path = snapshot_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def _remote_project_fetch(
    ctx: ToolContext,
    alias: str,
    remote_path: str,
    mode: str = "full",
    exclude_heavy_dirs: bool = False,
    exclude_patterns: List[str] | None = None,
    snapshot_kind: str = "auto",
    destination_label: str = "",
    max_files: int = 20000,
) -> str:
    payload = {
        "remote_path": remote_path,
        "mode": mode,
        "exclude_heavy_dirs": exclude_heavy_dirs,
        "exclude_patterns": exclude_patterns or [],
        "snapshot_kind": snapshot_kind,
        "max_files": max_files,
    }
    plan = _run_remote_plan(ctx, alias, payload)
    record = _get_target_record(ctx, alias)
    snapshot_dir = _snapshot_root(ctx, alias, remote_path, plan.get("snapshot_kind") or "snapshot", destination_label)
    files_dir = snapshot_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    if plan.get("files"):
        _stream_remote_tar_to_local(ctx, record, payload, files_dir)
    manifest = _build_manifest(alias=alias, payload=payload, plan=plan, snapshot_dir=snapshot_dir, files_dir=files_dir)
    preview = [item["relative_path"] for item in (manifest.get("files") or [])[:20]]
    return json.dumps(
        {
            "status": "ok",
            "alias": alias,
            "target": manifest.get("target"),
            "remote_root": manifest.get("remote_root"),
            "snapshot_kind": manifest.get("snapshot_kind"),
            "mode": manifest.get("mode"),
            "local_snapshot_dir": manifest.get("local_snapshot_dir"),
            "local_files_dir": manifest.get("local_files_dir"),
            "manifest_path": str(snapshot_dir / "manifest.json"),
            "selection": manifest.get("selection"),
            "integrity": manifest.get("integrity"),
            "files_preview": preview,
        },
        ensure_ascii=False,
    )


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            "remote_project_fetch",
            "Materialize a remote project from a registered SSH target into a local snapshot with manifest and source-integrity checks.",
            {
                "alias": {"type": "string", "description": "SSH target alias"},
                "remote_path": {"type": "string", "description": "Absolute remote project path to fetch"},
                "mode": {"type": "string", "enum": ["full", "source_only"], "description": "Copy the whole project or only likely source/config files"},
                "exclude_heavy_dirs": {"type": "boolean", "description": "Exclude heavy/cache/build directories such as node_modules, .venv, dist, build"},
                "exclude_patterns": {"type": "array", "items": {"type": "string"}, "description": "Additional glob patterns to exclude from the snapshot"},
                "snapshot_kind": {"type": "string", "enum": ["auto", "source_snapshot", "deployment_artifact_snapshot"], "description": "Override or auto-detect snapshot classification"},
                "destination_label": {"type": "string", "description": "Optional human-readable label for the local snapshot directory"},
                "max_files": {"type": "integer", "description": "Safety cap on the number of transferred files (default 20000)"},
            },
            ["alias", "remote_path"],
            _remote_project_fetch,
            timeout_sec=900,
        )
    ]
