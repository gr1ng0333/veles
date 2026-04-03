"""git_history — structured git history inspection without raw shell usage."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

_MAX_LIMIT = 100
_PRETTY = "===COMMIT===%n%H%n%cI%n%an%n%s%n%b"


def _repo_dir(ctx: ToolContext) -> Path:
    return Path(ctx.repo_dir)


def _run_git(ctx: ToolContext, args: List[str]) -> Tuple[bool, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(_repo_dir(ctx)),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return False, proc.stderr.strip() or proc.stdout.strip() or "git command failed"
    return True, proc.stdout


def _limit(n: int) -> int:
    return min(max(int(n or 1), 1), _MAX_LIMIT)


def _parse_numstat_block(block: str) -> Dict[str, int]:
    insertions = 0
    deletions = 0
    files = 0
    for line in block.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a, d, _ = parts[:3]
        if a.isdigit():
            insertions += int(a)
        if d.isdigit():
            deletions += int(d)
        files += 1
    return {"files": files, "insertions": insertions, "deletions": deletions}


def _parse_log_output(raw: str, include_stats: bool, include_body: bool) -> List[Dict[str, Any]]:
    commits: List[Dict[str, Any]] = []
    for chunk in raw.split("===COMMIT===\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        lines = chunk.splitlines()
        if len(lines) < 4:
            continue
        sha = lines[0]
        date = lines[1] if len(lines) > 1 else ""
        author = lines[2] if len(lines) > 2 else ""
        subject = lines[3] if len(lines) > 3 else ""
        rest = lines[4:]
        body_lines: List[str] = []
        stats_lines: List[str] = []
        if include_stats:
            stats_started = False
            for line in rest:
                if not stats_started and "\t" in line:
                    stats_started = True
                if stats_started:
                    stats_lines.append(line)
                else:
                    body_lines.append(line)
        else:
            body_lines = rest
        commit: Dict[str, Any] = {
            "sha": sha,
            "sha_short": sha[:8],
            "date": date,
            "author": author,
            "subject": subject,
        }
        if include_body:
            commit["body"] = "\n".join(body_lines).strip()
        if include_stats:
            commit["stats"] = _parse_numstat_block("\n".join(stats_lines))
        commits.append(commit)
    return commits


def _log_mode(
    ctx: ToolContext,
    branch: str = "",
    limit: int = 20,
    since: str = "",
    until: str = "",
    author: str = "",
    grep: str = "",
    path: str = "",
    include_stats: bool = False,
    include_body: bool = False,
) -> str:
    args = ["log", f"-n{_limit(limit)}", f"--pretty=format:{_PRETTY}"]
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    if author:
        args.append(f"--author={author}")
    if grep:
        args.extend([f"--grep={grep}", "-i"])
    if include_stats:
        args.append("--numstat")
    if branch:
        args.append(branch)
    if path:
        args.extend(["--", path])
    ok, raw = _run_git(ctx, args)
    if not ok:
        return json.dumps({"mode": "log", "count": 0, "commits": [], "error": raw}, ensure_ascii=False)
    commits = _parse_log_output(raw, include_stats=include_stats, include_body=include_body)
    return json.dumps({"mode": "log", "count": len(commits), "commits": commits}, ensure_ascii=False)


def _reflog_mode(ctx: ToolContext, limit: int = 20, since: str = "") -> str:
    args = ["reflog", f"-n{_limit(limit)}", "--date=iso", "--pretty=format:%H%x09%cI%x09%gs"]
    if since:
        args.append(f"--since={since}")
    ok, raw = _run_git(ctx, args)
    if not ok:
        return json.dumps({"mode": "reflog", "count": 0, "entries": [], "error": raw}, ensure_ascii=False)
    entries = []
    for line in raw.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        subject = parts[2]
        action = subject.split(":", 1)[0].strip() if ":" in subject else subject.strip()
        entries.append({
            "sha": parts[0],
            "sha_short": parts[0][:8],
            "date": parts[1],
            "ref": "HEAD",
            "action": action,
            "subject": subject,
        })
    return json.dumps({"mode": "reflog", "count": len(entries), "entries": entries}, ensure_ascii=False)


def _tags_mode(ctx: ToolContext, limit: int = 50, pattern: str = "") -> str:
    ok, raw = _run_git(ctx, ["for-each-ref", "--sort=-creatordate", "--format=%(refname:short)%x09%(creatordate:iso)", "refs/tags"])
    if not ok:
        return json.dumps({"mode": "tags", "count": 0, "tags": [], "error": raw}, ensure_ascii=False)
    rx = re.compile(pattern) if pattern else None
    tags = []
    for line in raw.splitlines():
        parts = line.split("\t", 1)
        tag = parts[0].strip()
        date = parts[1].strip() if len(parts) > 1 else ""
        if not tag:
            continue
        if rx and not rx.search(tag):
            continue
        tags.append({"tag": tag, "date": date})
        if len(tags) >= _limit(limit):
            break
    return json.dumps({"mode": "tags", "count": len(tags), "tags": tags}, ensure_ascii=False)


def _show_mode(ctx: ToolContext, ref: str, include_diff: bool = False) -> str:
    pretty = "%H%n%cI%n%an%n%s%n%b"
    args = ["show", "--stat", f"--pretty=format:{pretty}", ref]
    if include_diff:
        args.append("-p")
    else:
        args.insert(1, "--no-patch")
    ok, raw = _run_git(ctx, args)
    if not ok:
        return json.dumps({"mode": "show", "error": raw}, ensure_ascii=False)
    lines = raw.splitlines()
    stats = _parse_numstat_block(raw)
    commit = {
        "sha": lines[0] if len(lines) > 0 else "",
        "sha_short": (lines[0][:8] if len(lines) > 0 else ""),
        "date": lines[1] if len(lines) > 1 else "",
        "author": lines[2] if len(lines) > 2 else "",
        "subject": lines[3] if len(lines) > 3 else "",
        "body": lines[4].strip() if len(lines) > 4 else "",
        "stats": stats,
    }
    if include_diff:
        commit["diff"] = raw
    return json.dumps({"mode": "show", "commit": commit}, ensure_ascii=False)


def _file_log_mode(ctx: ToolContext, path: str, limit: int = 20, include_diff: bool = False) -> str:
    if not path:
        return json.dumps({"mode": "file_log", "error": "path is required", "file": path, "count": 0, "commits": []}, ensure_ascii=False)
    args = ["log", f"-n{_limit(limit)}", f"--pretty=format:{_PRETTY}"]
    if include_diff:
        args.append("-p")
    args.extend(["--", path])
    ok, raw = _run_git(ctx, args)
    if not ok:
        return json.dumps({"mode": "file_log", "error": raw, "file": path, "count": 0, "commits": []}, ensure_ascii=False)
    commits = _parse_log_output(raw, include_stats=False, include_body=False)
    return json.dumps({"mode": "file_log", "file": path, "count": len(commits), "commits": commits}, ensure_ascii=False)


def _branch_compare_mode(ctx: ToolContext, ref1: str, ref2: str, limit: int = 10) -> str:
    ok1, ahead_raw = _run_git(ctx, ["rev-list", "--count", f"{ref2}..{ref1}"])
    ok2, behind_raw = _run_git(ctx, ["rev-list", "--count", f"{ref1}..{ref2}"])
    if not ok1 or not ok2:
        return json.dumps({"mode": "compare", "ref1": ref1, "ref2": ref2, "ahead": 0, "behind": 0, "limit": _limit(limit), "error": ahead_raw if not ok1 else behind_raw}, ensure_ascii=False)
    return json.dumps({"mode": "compare", "ref1": ref1, "ref2": ref2, "ahead": int(ahead_raw.strip() or '0'), "behind": int(behind_raw.strip() or '0'), "limit": _limit(limit)}, ensure_ascii=False)


def _git_history_handler(ctx: ToolContext, mode: str = "log", **args: Any) -> str:
    mode = (mode or "log").strip().lower()
    if mode == "log":
        return _log_mode(ctx, **args)
    if mode == "reflog":
        return _reflog_mode(ctx, **args)
    if mode == "tags":
        return _tags_mode(ctx, **args)
    if mode == "show":
        return _show_mode(ctx, **args)
    if mode == "file_log":
        return _file_log_mode(ctx, **args)
    if mode == "compare":
        return _branch_compare_mode(ctx, **args)
    raise ValueError(f"Unsupported mode: {mode}")


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="git_history",
            schema={
                "name": "git_history",
                "description": "Inspect git history in structured JSON form.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string"},
                        "branch": {"type": "string"},
                        "limit": {"type": "integer"},
                        "since": {"type": "string"},
                        "until": {"type": "string"},
                        "author": {"type": "string"},
                        "grep": {"type": "string"},
                        "path": {"type": "string"},
                        "include_stats": {"type": "boolean"},
                        "include_body": {"type": "boolean"},
                        "pattern": {"type": "string"},
                        "ref": {"type": "string"},
                        "include_diff": {"type": "boolean"},
                        "ref1": {"type": "string"},
                        "ref2": {"type": "string"},
                    },
                },
            },
            handler=_git_history_handler,
        )
    ]
