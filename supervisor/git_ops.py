"""
Supervisor — Git operations.

Clone, checkout, reset, rescue snapshots, dependency sync, import test.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Tuple

from supervisor.state import (
    load_state, save_state, append_jsonl, atomic_write_text,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level config (set via init())
# ---------------------------------------------------------------------------
REPO_DIR: pathlib.Path = pathlib.Path("/content/ouroboros_repo")
DRIVE_ROOT: pathlib.Path = pathlib.Path("/content/drive/MyDrive/Ouroboros")
REMOTE_URL: str = ""
BRANCH_DEV: str = "ouroboros"
BRANCH_STABLE: str = "ouroboros-stable"
MAX_RESCUE_SNAPSHOTS: int = 20


def init(repo_dir: pathlib.Path, drive_root: pathlib.Path, remote_url: str,
         branch_dev: str = "ouroboros", branch_stable: str = "ouroboros-stable") -> None:
    global REPO_DIR, DRIVE_ROOT, REMOTE_URL, BRANCH_DEV, BRANCH_STABLE
    REPO_DIR = repo_dir
    DRIVE_ROOT = drive_root
    REMOTE_URL = remote_url
    BRANCH_DEV = branch_dev
    BRANCH_STABLE = branch_stable


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _cleanup_old_rescue_snapshots(limit: int = MAX_RESCUE_SNAPSHOTS) -> None:
    rescue_root = DRIVE_ROOT / "archive" / "rescue"
    if not rescue_root.exists():
        return

    entries = [p for p in rescue_root.iterdir() if p.is_dir()]
    # Sort by directory name (format: YYYYMMDD_HHMMSS_*) — name IS the timestamp
    entries.sort(key=lambda p: p.name, reverse=True)
    for stale in entries[limit:]:
        shutil.rmtree(stale, ignore_errors=True)


def git_capture(cmd: List[str]) -> Tuple[int, str, str]:
    r = subprocess.run(cmd, cwd=str(REPO_DIR), capture_output=True, text=True)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def ensure_repo_present() -> None:
    if not (REPO_DIR / ".git").exists():
        subprocess.run(["rm", "-rf", str(REPO_DIR)], check=False)
        subprocess.run(["git", "clone", REMOTE_URL, str(REPO_DIR)], check=True)
    else:
        subprocess.run(["git", "remote", "set-url", "origin", REMOTE_URL],
                        cwd=str(REPO_DIR), check=True)
    subprocess.run(["git", "config", "user.name", "Veles"], cwd=str(REPO_DIR), check=True)
    subprocess.run(["git", "config", "user.email", "veles@users.noreply.github.com"],
                    cwd=str(REPO_DIR), check=True)
    subprocess.run(["git", "fetch", "origin"], cwd=str(REPO_DIR), check=True)


# ---------------------------------------------------------------------------
# Repo sync state collection
# ---------------------------------------------------------------------------

def _collect_repo_sync_state() -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "current_branch": "unknown",
        "dirty_lines": [],
        "unpushed_lines": [],
        "warnings": [],
    }

    rc, branch, err = git_capture(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if rc == 0 and branch:
        state["current_branch"] = branch
    elif err:
        state["warnings"].append(f"branch_error:{err}")

    rc, dirty, err = git_capture(["git", "status", "--porcelain"])
    if rc == 0 and dirty:
        state["dirty_lines"] = [ln for ln in dirty.splitlines() if ln.strip()]
    elif rc != 0 and err:
        state["warnings"].append(f"status_error:{err}")

    upstream = ""
    rc, up, err = git_capture(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if rc == 0 and up:
        upstream = up
    else:
        current_branch = str(state.get("current_branch") or "")
        if current_branch not in ("", "HEAD", "unknown"):
            upstream = f"origin/{current_branch}"
        elif err:
            state["warnings"].append(f"upstream_error:{err}")

    if upstream:
        rc, unpushed, err = git_capture(["git", "log", "--oneline", f"{upstream}..HEAD"])
        if rc == 0 and unpushed:
            state["unpushed_lines"] = [ln for ln in unpushed.splitlines() if ln.strip()]
        elif rc != 0 and err:
            state["warnings"].append(f"unpushed_error:{err}")

    return state


def _copy_untracked_for_rescue(dst_root: pathlib.Path, max_files: int = 200,
                                max_total_bytes: int = 12_000_000) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "copied_files": 0, "skipped_files": 0, "copied_bytes": 0, "truncated": False,
    }
    rc, txt, err = git_capture(["git", "ls-files", "--others", "--exclude-standard"])
    if rc != 0:
        out["error"] = err or "git ls-files failed"
        return out

    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    if not lines:
        return out

    dst_root.mkdir(parents=True, exist_ok=True)
    for rel in lines:
        if out["copied_files"] >= max_files:
            out["truncated"] = True
            break
        src = (REPO_DIR / rel).resolve()
        try:
            src.relative_to(REPO_DIR.resolve())
        except Exception:
            out["skipped_files"] += 1
            continue
        if not src.exists() or not src.is_file():
            out["skipped_files"] += 1
            continue
        try:
            size = int(src.stat().st_size)
        except Exception:
            out["skipped_files"] += 1
            continue
        if (out["copied_bytes"] + size) > max_total_bytes:
            out["truncated"] = True
            break
        dst = dst_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            out["copied_files"] += 1
            out["copied_bytes"] += size
        except Exception:
            out["skipped_files"] += 1
    return out


def _create_rescue_snapshot(branch: str, reason: str,
                             repo_state: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.datetime.now(datetime.timezone.utc)
    ts = now.strftime("%Y%m%d_%H%M%S")
    rescue_dir = DRIVE_ROOT / "archive" / "rescue" / f"{ts}_{uuid.uuid4().hex[:8]}"
    rescue_dir.mkdir(parents=True, exist_ok=True)

    info: Dict[str, Any] = {
        "ts": now.isoformat(),
        "target_branch": branch,
        "reason": reason,
        "current_branch": repo_state.get("current_branch"),
        "dirty_count": len(repo_state.get("dirty_lines") or []),
        "unpushed_count": len(repo_state.get("unpushed_lines") or []),
        "warnings": list(repo_state.get("warnings") or []),
        "path": str(rescue_dir),
    }

    rc_status, status_txt, _ = git_capture(["git", "status", "--porcelain"])
    if rc_status == 0:
        atomic_write_text(rescue_dir / "status.porcelain.txt",
                          status_txt + ("\n" if status_txt else ""))

    rc_diff, diff_txt, diff_err = git_capture(["git", "diff", "--binary", "HEAD"])
    if rc_diff == 0:
        atomic_write_text(rescue_dir / "changes.diff",
                          diff_txt + ("\n" if diff_txt else ""))
    else:
        info["diff_error"] = diff_err or "git diff failed"

    untracked_meta = _copy_untracked_for_rescue(rescue_dir / "untracked")
    info["untracked"] = untracked_meta

    unpushed_lines = [ln for ln in (repo_state.get("unpushed_lines") or []) if str(ln).strip()]
    if unpushed_lines:
        atomic_write_text(rescue_dir / "unpushed_commits.txt",
                          "\n".join(unpushed_lines) + "\n")

    atomic_write_text(rescue_dir / "rescue_meta.json",
                      json.dumps(info, ensure_ascii=False, indent=2))

    # Auto-cleanup old snapshots
    _cleanup_old_rescue_snapshots()

    return info


def _collect_copilot_rescue_repo_state() -> Dict[str, Any]:
    branch_proc = subprocess.run(
        ['git', 'branch', '--show-current'],
        cwd=str(REPO_DIR), capture_output=True, text=True,
    )
    current_branch = (branch_proc.stdout or '').strip() if branch_proc.returncode == 0 else ''

    status_proc = subprocess.run(
        ['git', 'status', '--porcelain'],
        cwd=str(REPO_DIR), capture_output=True, text=True,
    )
    dirty_lines = [ln for ln in (status_proc.stdout or '').splitlines() if ln.strip()]

    unpushed_proc = subprocess.run(
        ['git', 'log', '--oneline', f'origin/{BRANCH_DEV}..HEAD'],
        cwd=str(REPO_DIR), capture_output=True, text=True,
    )
    unpushed_lines = [ln for ln in (unpushed_proc.stdout or '').splitlines() if ln.strip()] if unpushed_proc.returncode == 0 else []

    warnings: List[str] = []
    if dirty_lines:
        warnings.append('dirty_working_tree')
    if unpushed_lines:
        warnings.append('unpushed_commits')
    if not current_branch:
        warnings.append('unknown_current_branch')

    return {
        'current_branch': current_branch,
        'dirty_lines': dirty_lines,
        'unpushed_lines': unpushed_lines,
        'warnings': warnings,
    }


def _push_copilot_rescue_ref(task_id: str, reason: str = '') -> Tuple[bool, str]:
    """Persist current unpublished repo state to a dedicated remote rescue ref."""
    task = str(task_id or '').strip() or 'unknown-task'
    safe_task = ''.join(ch if ch.isalnum() or ch in '-_.' else '-' for ch in task)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    ref = f'refs/veles-rescue/{safe_task}/{stamp}'
    msg = f'copilot rescue for {safe_task}'
    if reason:
        msg += f' | {reason[:120]}'

    repo_state = _collect_copilot_rescue_repo_state()
    snapshot = _create_rescue_snapshot(BRANCH_DEV, reason or 'copilot rescue', repo_state)
    rescue_dir = pathlib.Path(str(snapshot.get('path') or '')).resolve()
    if not rescue_dir.exists():
        return False, f'rescue snapshot missing: {rescue_dir}'

    origin_proc = subprocess.run(
        ['git', 'remote', 'get-url', 'origin'],
        cwd=str(REPO_DIR), capture_output=True, text=True,
    )
    if origin_proc.returncode != 0:
        return False, f'origin url failed: {(origin_proc.stderr or origin_proc.stdout).strip()}'
    origin_url = (origin_proc.stdout or '').strip()
    if not origin_url:
        return False, 'origin url empty'

    head_proc = subprocess.run(
        ['git', 'rev-parse', 'HEAD'],
        cwd=str(REPO_DIR), capture_output=True, text=True,
    )
    head_sha = (head_proc.stdout or '').strip() if head_proc.returncode == 0 else ''

    with tempfile.TemporaryDirectory(prefix='veles-rescue-repo-') as tmp:
        tmp_path = pathlib.Path(tmp)
        init_proc = subprocess.run(['git', 'init', '-q'], cwd=str(tmp_path), capture_output=True, text=True)
        if init_proc.returncode != 0:
            return False, f'git init failed: {(init_proc.stderr or init_proc.stdout).strip()}'
        subprocess.run(['git', 'config', 'user.name', 'Veles Rescue'], cwd=str(tmp_path), capture_output=True, text=True)
        subprocess.run(['git', 'config', 'user.email', 'veles-rescue@local'], cwd=str(tmp_path), capture_output=True, text=True)

        for child in rescue_dir.iterdir():
            dst = tmp_path / child.name
            if child.is_dir():
                shutil.copytree(child, dst)
            else:
                shutil.copy2(child, dst)

        manifest = {
            'task_id': safe_task,
            'reason': reason,
            'ref': ref,
            'base_branch': BRANCH_DEV,
            'source_head_sha': head_sha,
            'snapshot_path': str(rescue_dir),
            'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        atomic_write_text(tmp_path / 'remote_rescue_manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))

        add_proc = subprocess.run(['git', 'add', '-A'], cwd=str(tmp_path), capture_output=True, text=True)
        if add_proc.returncode != 0:
            return False, f'git add failed: {(add_proc.stderr or add_proc.stdout).strip()}'
        commit_proc = subprocess.run(['git', 'commit', '-q', '-m', msg], cwd=str(tmp_path), capture_output=True, text=True)
        if commit_proc.returncode != 0:
            return False, f'git commit failed: {(commit_proc.stderr or commit_proc.stdout).strip()}'
        push_proc = subprocess.run(['git', 'push', origin_url, f'HEAD:{ref}', '--force'], cwd=str(tmp_path), capture_output=True, text=True)
        if push_proc.returncode != 0:
            return False, f'push failed: {(push_proc.stderr or push_proc.stdout).strip()}'

    try:
        append_jsonl(
            DRIVE_ROOT / 'logs' / 'supervisor.jsonl',
            {
                'ts': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'type': 'copilot_rescue_ref_pushed',
                'task_id': safe_task,
                'ref': ref,
                'reason': reason,
                'source_head_sha': head_sha,
                'dirty_count': len(repo_state.get('dirty_lines') or []),
                'unpushed_count': len(repo_state.get('unpushed_lines') or []),
            },
        )
    except Exception:
        pass
    return True, ref


def ensure_copilot_rescue_ref_if_needed(*, task_id: str, reason: str = '') -> Tuple[bool, str, bool]:
    """Push a rescue ref only when local repo has unpublished commits or dirty state."""
    repo_state = _collect_copilot_rescue_repo_state()
    dirty = bool(repo_state.get('dirty_lines'))
    ahead_count = len(repo_state.get('unpushed_lines') or [])
    if not dirty and ahead_count <= 0:
        return True, '', False
    ok, ref_or_err = _push_copilot_rescue_ref(task_id=task_id, reason=reason)
    return ok, ref_or_err, True


def checkout_and_reset(branch: str, reason: str = "unspecified",
                       unsynced_policy: str = "ignore") -> Tuple[bool, str]:
    rc, _, err = git_capture(["git", "fetch", "origin"])
    if rc != 0:
        msg = f"git fetch failed: {err or 'unknown error'}"
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "reset_fetch_failed",
                "target_branch": branch, "reason": reason, "error": msg,
            },
        )
        return False, msg

    policy = str(unsynced_policy or "ignore").strip().lower()
    if policy not in {"ignore", "block", "rescue_and_block", "rescue_and_reset"}:
        policy = "ignore"

    if policy != "ignore":
        repo_state = _collect_repo_sync_state()
        dirty_lines = list(repo_state.get("dirty_lines") or [])
        unpushed_lines = list(repo_state.get("unpushed_lines") or [])
        if dirty_lines or unpushed_lines:
            rescue_info: Dict[str, Any] = {}
            if policy in {"rescue_and_block", "rescue_and_reset"}:
                try:
                    rescue_info = _create_rescue_snapshot(
                        branch=branch, reason=reason, repo_state=repo_state)
                except Exception as e:
                    rescue_info = {"error": repr(e)}
            bits: List[str] = []
            if unpushed_lines:
                bits.append(f"unpushed={len(unpushed_lines)}")
            if dirty_lines:
                bits.append(f"dirty={len(dirty_lines)}")
            detail = ", ".join(bits) if bits else "unsynced"
            rescue_suffix = ""
            rescue_path = str(rescue_info.get("path") or "").strip()
            if rescue_path:
                rescue_suffix = f" Rescue saved to {rescue_path}."
            elif policy in {"rescue_and_block", "rescue_and_reset"} and rescue_info.get("error"):
                rescue_suffix = f" Rescue failed: {rescue_info.get('error')}."

            if policy in {"block", "rescue_and_block"}:
                msg = f"Reset blocked ({detail}) to protect local changes.{rescue_suffix}"
                append_jsonl(
                    DRIVE_ROOT / "logs" / "supervisor.jsonl",
                    {
                        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "type": "reset_blocked_unsynced_state",
                        "target_branch": branch, "reason": reason, "policy": policy,
                        "current_branch": repo_state.get("current_branch"),
                        "dirty_count": len(dirty_lines),
                        "unpushed_count": len(unpushed_lines),
                        "dirty_preview": dirty_lines[:20],
                        "unpushed_preview": unpushed_lines[:20],
                        "warnings": list(repo_state.get("warnings") or []),
                        "rescue": rescue_info,
                    },
                )
                return False, msg

            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "reset_unsynced_rescued_then_reset",
                    "target_branch": branch, "reason": reason, "policy": policy,
                    "current_branch": repo_state.get("current_branch"),
                    "dirty_count": len(dirty_lines),
                    "unpushed_count": len(unpushed_lines),
                    "dirty_preview": dirty_lines[:20],
                    "unpushed_preview": unpushed_lines[:20],
                    "warnings": list(repo_state.get("warnings") or []),
                    "rescue": rescue_info,
                },
            )

    rc_verify = subprocess.run(
        ["git", "rev-parse", "--verify", f"origin/{branch}"],
        cwd=str(REPO_DIR), capture_output=True,
    ).returncode
    if rc_verify != 0:
        msg = f"Branch {branch} not found on remote"
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "reset_branch_missing",
                "target_branch": branch, "reason": reason,
            },
        )
        return False, msg

    subprocess.run(["git", "checkout", branch], cwd=str(REPO_DIR), check=True)
    subprocess.run(["git", "reset", "--hard", f"origin/{branch}"], cwd=str(REPO_DIR), check=True)
    # Clean __pycache__ to prevent stale bytecode (git checkout may not update mtime)
    for p in REPO_DIR.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)
    st = load_state()
    st["current_branch"] = branch
    st["current_sha"] = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(REPO_DIR),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    save_state(st)
    return True, "ok"


# ---------------------------------------------------------------------------
# Dependencies + import test
# ---------------------------------------------------------------------------

def sync_runtime_dependencies(reason: str) -> Tuple[bool, str]:
    req_path = REPO_DIR / "requirements.txt"
    cmd: List[str] = [sys.executable, "-m", "pip", "install", "-q"]
    source = ""
    if req_path.exists():
        cmd += ["-r", str(req_path)]
        source = f"requirements:{req_path}"
    else:
        cmd += ["openai>=1.0.0", "requests"]
        source = "fallback:minimal"
    try:
        subprocess.run(cmd, cwd=str(REPO_DIR), check=True)
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "deps_sync_ok", "reason": reason, "source": source,
            },
        )
        return True, source
    except Exception as e:
        msg = repr(e)
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "deps_sync_error", "reason": reason, "source": source, "error": msg,
            },
        )
        return False, msg


def import_test() -> Dict[str, Any]:
    r = subprocess.run(
        ["python3", "-c", "import ouroboros, ouroboros.agent; print('import_ok')"],
        cwd=str(REPO_DIR),
        capture_output=True, text=True,
    )
    return {"ok": (r.returncode == 0), "stdout": r.stdout, "stderr": r.stderr,
            "returncode": r.returncode}


# ---------------------------------------------------------------------------
# Safe restart orchestration
# ---------------------------------------------------------------------------

def safe_restart(
    reason: str,
    unsynced_policy: str = "rescue_and_reset",
) -> Tuple[bool, str]:
    """
    Attempt to checkout dev branch, sync deps, and verify imports.
    Falls back to stable branch if dev fails.

    Args:
        reason: Human-readable reason for the restart (logged to supervisor.jsonl)
        unsynced_policy: Policy for handling unsynced state (default: "rescue_and_reset")

    Returns:
        Tuple of (ok: bool, message: str)
        - If successful: (True, "OK: <branch>")
        - If failed: (False, "<error description>")
    """
    # Try dev branch
    ok, err = checkout_and_reset(BRANCH_DEV, reason=reason, unsynced_policy=unsynced_policy)
    if not ok:
        return False, f"Failed checkout {BRANCH_DEV}: {err}"

    deps_ok, deps_msg = sync_runtime_dependencies(reason=reason)
    if not deps_ok:
        return False, f"Failed deps for {BRANCH_DEV}: {deps_msg}"

    t = import_test()
    if t["ok"]:
        return True, f"OK: {BRANCH_DEV}"

    # Dev branch failed import — log the failure and fall back to stable
    append_jsonl(
        DRIVE_ROOT / "logs" / "supervisor.jsonl",
        {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "safe_restart_dev_import_failed",
            "reason": reason,
            "branch": BRANCH_DEV,
            "stdout": t.get("stdout", ""),
            "stderr": t.get("stderr", ""),
            "returncode": t.get("returncode", -1),
        },
    )

    # Fallback to stable
    ok_s, err_s = checkout_and_reset(
        BRANCH_STABLE,
        reason=f"{reason}_fallback_stable",
        unsynced_policy="rescue_and_reset",
    )
    if not ok_s:
        return False, f"Failed checkout {BRANCH_STABLE}: {err_s}"

    deps_ok_s, deps_msg_s = sync_runtime_dependencies(reason=f"{reason}_fallback_stable")
    if not deps_ok_s:
        return False, f"Failed deps for {BRANCH_STABLE}: {deps_msg_s}"

    t2 = import_test()
    if t2["ok"]:
        return True, f"OK: fell back to {BRANCH_STABLE}"

    # Both branches failed
    return False, f"Both branches failed import (dev and stable)"
