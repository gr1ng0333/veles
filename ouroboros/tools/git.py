"""Git tools: repo_write_commit, repo_commit_push, git_status, git_diff."""

from __future__ import annotations

import logging
import os
import pathlib
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso, write_text, safe_relpath, run_cmd

log = logging.getLogger(__name__)

# --- Shrink guard ---

SHRINK_GUARD_THRESHOLD = 0.30  # block if new content < 30% of original
SHRINK_GUARD_MIN_SIZE = 100    # skip guard for files < 100 bytes
_SHRINK_GUARD_SKIP_EXT = frozenset({".md"})


def _check_shrink_guard(file_path: pathlib.Path, new_content: str) -> Optional[str]:
    """Return warning string if writing new_content would shrink a tracked file by >70%. None if OK."""
    if not file_path.exists():
        return None  # new file — no restriction
    try:
        old_size = file_path.stat().st_size
    except OSError:
        return None
    if old_size < SHRINK_GUARD_MIN_SIZE:
        return None  # tiny file — skip
    if file_path.suffix.lower() in _SHRINK_GUARD_SKIP_EXT:
        return None  # markdown — skip
    new_size = len(new_content.encode("utf-8"))
    if old_size > 0 and new_size < old_size * SHRINK_GUARD_THRESHOLD:
        pct = round(new_size / old_size * 100)
        return (
            f"⚠️ SHRINK GUARD: new content for '{file_path.name}' is {pct}% of original "
            f"({new_size} bytes vs {old_size} bytes). This looks like accidental truncation. "
            f"If intentional, delete the file first with run_shell, then write the new version."
        )
    return None


# --- Git lock ---

def _acquire_git_lock(ctx: ToolContext, timeout_sec: int = 120) -> pathlib.Path:
    lock_dir = ctx.drive_path("locks")
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "git.lock"
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
                os.write(fd, f"locked_at={utc_now_iso()}\n".encode("utf-8"))
            finally:
                os.close(fd)
            return lock_path
        except FileExistsError:
            time.sleep(0.5)
    raise TimeoutError(f"Git lock not acquired within {timeout_sec}s: {lock_path}")


def _release_git_lock(lock_path: pathlib.Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def _acquire_copilot_write_lock(ctx: ToolContext, timeout_sec: int = 15) -> Optional[str]:
    transport = str(getattr(ctx, 'write_transport', '') or '').strip().lower()
    if transport != 'copilot':
        return None
    if getattr(ctx, 'copilot_write_lock_acquired', False):
        return None
    lock_dir = ctx.drive_path('locks')
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / 'copilot-write.lock'
    deadline = time.time() + max(1, int(timeout_sec))
    stale_sec = 6 * 3600
    payload = (
        f"locked_at={utc_now_iso()}\n"
        f"task_type={getattr(ctx, 'current_task_type', '')}\n"
        f"task_id={getattr(ctx, 'task_id', '')}\n"
    )
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
                os.write(fd, payload.encode('utf-8'))
            finally:
                os.close(fd)
            ctx.copilot_write_lock_acquired = True
            ctx.copilot_write_lock_path = lock_path
            return None
        except FileExistsError:
            time.sleep(0.25)
    return (
        '⚠️ COPILOT_WRITE_LOCK_BUSY: another Copilot write task currently owns the repo write boundary. '
        'Wait for it to finish or continue in read-only mode.'
    )


def _release_copilot_write_lock(ctx: ToolContext) -> None:
    lock_path = getattr(ctx, 'copilot_write_lock_path', None)
    if not lock_path:
        return
    try:
        pathlib.Path(lock_path).unlink()
    except FileNotFoundError:
        pass
    except Exception:
        log.debug('Failed to release copilot write lock', exc_info=True)
    ctx.copilot_write_lock_acquired = False
    ctx.copilot_write_lock_path = None


# --- Pre-push test gate ---

MAX_TEST_OUTPUT = 8000

# Fast subset run before every push — must complete well within 30s tool timeout.
# Full suite takes 60-90s and reliably hits TimeoutExpired, blocking all pushes.
# Set OUROBOROS_FULL_TESTS=1 to run full suite (only use outside tool-call context).
_FAST_TEST_TARGETS = [
    "tests/test_smoke.py",
    "tests/test_version_artifacts.py",
]
_PRE_PUSH_TIMEOUT = 25  # seconds — leaves margin inside 30s tool timeout


def _run_pre_push_tests(ctx: ToolContext) -> Optional[str]:
    """Run pre-push fast smoke tests. Returns None if pass, error string if fail.

    Only runs test_smoke.py + test_version_artifacts.py (completes in ~5s).
    Full suite (~60-90s) would exceed the 30s tool-call timeout and block every push.
    Set OUROBOROS_PRE_PUSH_TESTS=0 to skip entirely.
    """
    if ctx is None:
        log.warning("_run_pre_push_tests called with ctx=None, skipping tests")
        return None

    if os.environ.get("OUROBOROS_PRE_PUSH_TESTS", "1") != "1":
        return None

    tests_dir = pathlib.Path(ctx.repo_dir) / "tests"
    if not tests_dir.exists():
        return None

    # Only run targets that actually exist
    targets = [t for t in _FAST_TEST_TARGETS if (pathlib.Path(ctx.repo_dir) / t).exists()]
    if not targets:
        return None

    try:
        result = subprocess.run(
            ["pytest"] + targets + ["-q", "--tb=line", "--no-header"],
            cwd=ctx.repo_dir,
            capture_output=True,
            text=True,
            timeout=_PRE_PUSH_TIMEOUT,
        )
        if result.returncode == 0:
            return None

        output = result.stdout + result.stderr
        if len(output) > MAX_TEST_OUTPUT:
            output = output[:MAX_TEST_OUTPUT] + "\n...(truncated)..."
        return output

    except subprocess.TimeoutExpired:
        return f"⚠️ PRE_PUSH_TEST_ERROR: fast smoke tests timed out after {_PRE_PUSH_TIMEOUT}s"

    except FileNotFoundError:
        return "⚠️ PRE_PUSH_TEST_ERROR: pytest not installed or not found in PATH"

    except Exception as e:
        log.warning(f"Pre-push tests failed with exception: {e}", exc_info=True)
        return f"⚠️ PRE_PUSH_TEST_ERROR: Unexpected error running tests: {e}"


def _git_push_with_tests(ctx: ToolContext) -> Optional[str]:
    """Run pre-push tests, then pull --rebase and push. Returns None on success, error string on failure."""
    test_error = _run_pre_push_tests(ctx)
    if test_error:
        log.error("Pre-push tests failed, blocking push")
        ctx.last_push_succeeded = False
        return f"⚠️ PRE_PUSH_TESTS_FAILED: Tests failed, push blocked.\n{test_error}\nCommitted locally but NOT pushed. Fix tests and push manually."

    try:
        run_cmd(["git", "pull", "--rebase", "origin", ctx.branch_dev], cwd=ctx.repo_dir)
    except Exception:
        log.debug(f"Failed to pull --rebase before push", exc_info=True)
        pass

    try:
        run_cmd(["git", "push", "origin", ctx.branch_dev], cwd=ctx.repo_dir)
    except Exception as e:
        return f"⚠️ GIT_ERROR (push): {e}\nCommitted locally but NOT pushed."

    return None


# --- Pre-commit checks (advisory) ---

_SECRET_PATTERNS = re.compile(
    r"(?:OPENROUTER_API_KEY|CODEX_ACCESS|COPILOT_GITHUB_TOKEN|Bearer\s+[A-Za-z0-9\-_.]+|sk-[A-Za-z0-9]{20,})"
)


def _pre_commit_checks(repo_dir: pathlib.Path, files_changed: List[str]) -> List[str]:
    """Run fast programmatic pre-commit checks. Returns list of advisory warnings."""
    issues: List[str] = []

    # Check 1: VERSION ↔ pyproject.toml sync
    try:
        version_file = (repo_dir / "VERSION").read_text(encoding="utf-8").strip()
        pyproject_text = (repo_dir / "pyproject.toml").read_text(encoding="utf-8")
        if f'version = "{version_file}"' not in pyproject_text:
            issues.append(f"\u26a0\ufe0f VERSION ({version_file}) and pyproject.toml out of sync")
    except Exception:
        pass

    # Check 2: No secrets in changed files
    for f in files_changed:
        if f.endswith((".py", ".md", ".toml", ".json", ".yaml", ".yml")):
            try:
                content = (repo_dir / f).read_text(encoding="utf-8")
                for match in _SECRET_PATTERNS.finditer(content):
                    issues.append(f"\u26a0\ufe0f Possible secret in {f}: '{match.group()[:30]}...'")
                    break  # one warning per file
            except Exception:
                pass

    # Check 3: Import check for changed .py files
    for f in files_changed:
        if f.endswith(".py") and not f.startswith("tests/") and not f.startswith("test_"):
            module = f.replace("/", ".").replace("\\", ".").removesuffix(".py")
            try:
                import importlib
                importlib.import_module(module)
            except Exception as exc:
                issues.append(f"\u26a0\ufe0f Import error in {f}: {exc}")

    return issues


def _get_changed_files(repo_dir: pathlib.Path) -> List[str]:
    """Get list of staged + unstaged changed files relative to repo root."""
    try:
        out = run_cmd(["git", "diff", "--cached", "--name-only"], cwd=str(repo_dir))
        out += run_cmd(["git", "diff", "--name-only"], cwd=str(repo_dir))
        return list(set(line.strip() for line in out.splitlines() if line.strip()))
    except Exception:
        return []


# --- Tool implementations ---

def _repo_write_commit(ctx: ToolContext, path: str, content: str, commit_message: str) -> str:
    ctx.last_push_succeeded = False
    busy = _acquire_copilot_write_lock(ctx)
    if busy:
        return busy
    ctx.write_attempted = True
    if not commit_message.strip():
        return "⚠️ ERROR: commit_message must be non-empty."

    # Shrink guard — block accidental truncation
    target = ctx.repo_path(path)
    guard_msg = _check_shrink_guard(target, content)
    if guard_msg:
        return guard_msg

    lock = _acquire_git_lock(ctx)
    try:
        try:
            run_cmd(["git", "checkout", ctx.branch_dev], cwd=ctx.repo_dir)
        except Exception as e:
            return f"⚠️ GIT_ERROR (checkout): {e}"
        try:
            write_text(target, content)
        except Exception as e:
            return f"⚠️ FILE_WRITE_ERROR: {e}"
        try:
            run_cmd(["git", "add", safe_relpath(path)], cwd=ctx.repo_dir)
        except Exception as e:
            return f"⚠️ GIT_ERROR (add): {e}"
        try:
            run_cmd(["git", "commit", "-m", commit_message], cwd=ctx.repo_dir)
        except Exception as e:
            return f"⚠️ GIT_ERROR (commit): {e}"

        push_error = _git_push_with_tests(ctx)
        if push_error:
            return push_error
    finally:
        _release_git_lock(lock)
    ctx.last_push_succeeded = True
    ctx.write_succeeded = True
    result = f"OK: committed and pushed to {ctx.branch_dev}: {commit_message}"

    # Advisory pre-commit checks (non-blocking)
    warnings = _pre_commit_checks(pathlib.Path(ctx.repo_dir), [safe_relpath(path)])
    if warnings:
        result += "\n" + "\n".join(warnings)

    return result


def _repo_commit_push(ctx: ToolContext, commit_message: str, paths: Optional[List[str]] = None) -> str:
    ctx.last_push_succeeded = False
    busy = _acquire_copilot_write_lock(ctx)
    if busy:
        return busy
    ctx.write_attempted = True
    if not commit_message.strip():
        return "⚠️ ERROR: commit_message must be non-empty."
    lock = _acquire_git_lock(ctx)
    try:
        try:
            run_cmd(["git", "checkout", ctx.branch_dev], cwd=ctx.repo_dir)
        except Exception as e:
            return f"⚠️ GIT_ERROR (checkout): {e}"
        if paths:
            try:
                safe_paths = [safe_relpath(p) for p in paths if str(p).strip()]
            except ValueError as e:
                return f"⚠️ PATH_ERROR: {e}"
            add_cmd = ["git", "add"] + safe_paths
        else:
            add_cmd = ["git", "add", "-A"]
        try:
            run_cmd(add_cmd, cwd=ctx.repo_dir)
        except Exception as e:
            return f"⚠️ GIT_ERROR (add): {e}"
        try:
            status = run_cmd(["git", "status", "--porcelain"], cwd=ctx.repo_dir)
        except Exception as e:
            return f"⚠️ GIT_ERROR (status): {e}"
        if not status.strip():
            return "⚠️ GIT_NO_CHANGES: nothing to commit."
        try:
            run_cmd(["git", "commit", "-m", commit_message], cwd=ctx.repo_dir)
        except Exception as e:
            return f"⚠️ GIT_ERROR (commit): {e}"

        push_error = _git_push_with_tests(ctx)
        if push_error:
            return push_error
    finally:
        _release_git_lock(lock)
    ctx.last_push_succeeded = True
    ctx.write_succeeded = True
    result = f"OK: committed and pushed to {ctx.branch_dev}: {commit_message}"

    # Advisory pre-commit checks (non-blocking)
    changed = _get_changed_files(pathlib.Path(ctx.repo_dir))
    if not changed and paths:
        changed = [safe_relpath(p) for p in paths if str(p).strip()]
    warnings = _pre_commit_checks(pathlib.Path(ctx.repo_dir), changed)
    if warnings:
        result += "\n" + "\n".join(warnings)

    if paths is not None:
        try:
            untracked = run_cmd(["git", "ls-files", "--others", "--exclude-standard"], cwd=ctx.repo_dir)
            if untracked.strip():
                files = ", ".join(untracked.strip().split("\n"))
                result += f"\n⚠️ WARNING: untracked files remain: {files} — they are NOT in git. Use repo_commit_push without paths to add everything."
        except Exception:
            log.debug("Failed to check for untracked files after repo_commit_push", exc_info=True)
            pass
    return result


def _git_status(ctx: ToolContext) -> str:
    try:
        return run_cmd(["git", "status", "--porcelain"], cwd=ctx.repo_dir)
    except Exception as e:
        return f"⚠️ GIT_ERROR: {e}"


def _git_diff(ctx: ToolContext, staged: bool = False) -> str:
    try:
        cmd = ["git", "diff"]
        if staged:
            cmd.append("--staged")
        return run_cmd(cmd, cwd=ctx.repo_dir)
    except Exception as e:
        return f"⚠️ GIT_ERROR: {e}"


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("repo_write_commit", {
            "name": "repo_write_commit",
            "description": "Write one file + commit + push to veles branch. For small deterministic edits. Has shrink guard: blocks writes that reduce file size by >70% (prevents accidental truncation).",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "commit_message": {"type": "string"},
            }, "required": ["path", "content", "commit_message"]},
        }, _repo_write_commit, is_code_tool=True),
        ToolEntry("repo_commit_push", {
            "name": "repo_commit_push",
            "description": "Commit + push already-changed files. Does pull --rebase before push.",
            "parameters": {"type": "object", "properties": {
                "commit_message": {"type": "string"},
                "paths": {
                    "description": "Files to add (empty = git add -A)",
                    "type": "array",
                    "items": {"type": "string"},
                },
            }, "required": ["commit_message"]},
        }, _repo_commit_push, is_code_tool=True),
        ToolEntry("git_status", {
            "name": "git_status",
            "description": "git status --porcelain",
            "parameters": {"type": "object", "properties": {}},
        }, _git_status),
        ToolEntry("git_diff", {
            "name": "git_diff",
            "description": "git diff (use staged=true to see staged changes after git add)",
            "parameters": {"type": "object", "properties": {
                "staged": {
                    "description": "If true, show staged changes (--staged)",
                    "type": "boolean",
                    "default": False,
                },
            }},
        }, _git_diff),
    ]
