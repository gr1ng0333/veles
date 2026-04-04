"""
external_repo_script — run a multi-line shell/python script inside an external repo.

Why this exists:
  external_repo_run_shell accepts a cmd *array* capped at 200 chars total.
  Many real-world tasks (pytest + grep + sed pipelines, multi-step builds) need
  pipelines, redirects, and loops that cannot fit in a single argv list.
  This tool accepts the script as a plain string (up to 32 KB), writes it to
  a temp file, and executes it inside the repo directory.

Usage:
  external_repo_script(alias="myrepo", script="pytest tests/ && echo OK")
  external_repo_script(alias="myrepo", script="python3 << 'EOF'\nprint('hello')\nEOF", shell="bash")
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from typing import List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.external_repos import _resolve_repo, _shorten

_MAX_SCRIPT_BYTES = 32 * 1024  # 32 KB
_DEFAULT_TIMEOUT = 60
_MAX_TIMEOUT = 600
_ALLOWED_SHELLS = {"bash", "sh", "python3"}


def _external_repo_script(
    ctx: ToolContext,
    alias: str,
    script: str,
    timeout_sec: int = _DEFAULT_TIMEOUT,
    shell: str = "bash",
) -> str:
    """Execute a multi-line shell or Python script inside the external repo directory.

    Args:
        alias: Registered repo alias (see external_repo_register).
        script: Full script text (bash/sh/python3). Max 32 KB.
        timeout_sec: Execution timeout in seconds (default 60, max 600).
        shell: Interpreter: 'bash' (default), 'sh', or 'python3'.
    """
    # Validate inputs
    shell = str(shell or "bash").strip().lower()
    if shell not in _ALLOWED_SHELLS:
        return json.dumps({
            "status": "error",
            "error": f"Unsupported shell: {shell!r}. Allowed: {', '.join(sorted(_ALLOWED_SHELLS))}",
        }, ensure_ascii=False, indent=2)

    script_bytes = script.encode("utf-8", errors="replace")
    if len(script_bytes) > _MAX_SCRIPT_BYTES:
        return json.dumps({
            "status": "error",
            "error": f"Script too large: {len(script_bytes)} bytes > {_MAX_SCRIPT_BYTES} limit.",
        }, ensure_ascii=False, indent=2)

    timeout_int = max(1, min(int(timeout_sec), _MAX_TIMEOUT))

    # Resolve repo
    try:
        _, repo_dir = _resolve_repo(ctx, alias)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "alias": alias,
            "error": str(e),
        }, ensure_ascii=False, indent=2)

    # Write script to temp file and execute
    suffix = ".py" if shell == "python3" else ".sh"
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=suffix,
            delete=False,
            dir="/tmp",
        ) as tf:
            tf.write(script_bytes)
            tmp_path = tf.name

        # Make executable
        os.chmod(tmp_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IROTH)

        # Resolve interpreter
        interpreter = _find_interpreter(shell)

        res = subprocess.run(
            [interpreter, tmp_path],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=timeout_int,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({
            "status": "timeout",
            "alias": alias,
            "timeout_sec": timeout_int,
            "error": f"Script exceeded {timeout_int}s timeout.",
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "alias": alias,
            "error": str(e),
        }, ensure_ascii=False, indent=2)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    return json.dumps(
        {
            "status": "ok" if res.returncode == 0 else "nonzero",
            "alias": alias,
            "cwd": str(repo_dir),
            "shell": shell,
            "returncode": int(res.returncode),
            "stdout": _shorten(res.stdout),
            "stderr": _shorten(res.stderr),
        },
        ensure_ascii=False,
        indent=2,
    )


def _find_interpreter(shell: str) -> str:
    """Return absolute path to interpreter. Raises RuntimeError if not found."""
    candidates: List[str] = []
    if shell == "bash":
        candidates = ["/bin/bash", "/usr/bin/bash"]
    elif shell == "sh":
        candidates = ["/bin/sh", "/usr/bin/sh"]
    elif shell == "python3":
        candidates = ["/usr/bin/python3", "/usr/local/bin/python3"]

    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    # Last resort: search PATH
    import shutil
    found = shutil.which(shell)
    if found:
        return found

    raise RuntimeError(f"Interpreter not found: {shell}")


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="external_repo_script",
            schema={
                "name": "external_repo_script",
                "description": (
                    "Execute a multi-line bash/sh/python3 script inside a registered external repo directory. "
                    "Use this instead of external_repo_run_shell when the command is complex: "
                    "pipelines, loops, redirects, multi-step builds, pytest with flags, grep|sed chains. "
                    "Script is written to a temp file and executed with the chosen interpreter. "
                    "Max script size: 32 KB. Max timeout: 600s."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {
                            "type": "string",
                            "description": "Registered external repo alias (see external_repo_register).",
                        },
                        "script": {
                            "type": "string",
                            "description": "Full script text. Can contain newlines, pipes, redirects. Max 32 KB.",
                        },
                        "timeout_sec": {
                            "type": "integer",
                            "description": "Execution timeout in seconds (default 60, max 600).",
                        },
                        "shell": {
                            "type": "string",
                            "description": "Interpreter: 'bash' (default), 'sh', or 'python3'.",
                            "enum": ["bash", "sh", "python3"],
                        },
                    },
                    "required": ["alias", "script"],
                },
            },
            handler=lambda ctx, **kw: _external_repo_script(ctx, **kw),
            timeout_sec=120,
        )
    ]
