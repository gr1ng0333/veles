"""todo_scanner — codebase TODO/FIXME/HACK/BUG comment scanner.

Scans Python source files for developer-left annotations using regex over
comment lines and inline string markers.

Tag categories:

  FIXME       — known broken / must be fixed before production (priority: high)
  BUG         — confirmed bug that hasn't been fixed yet (priority: high)
  XXX         — danger: unclear, wrong, or risky (priority: high)
  HACK        — temporary workaround that should be replaced (priority: medium)
  TODO        — planned improvement or missing feature (priority: medium)
  OPTIMIZE    — performance improvement opportunity (priority: low)
  NOTE        — important contextual note left for the reader (priority: low)

Each finding includes:
  file, line, tag, message, priority

Filters:
  path          — limit to a subdirectory or single file
  tags          — comma-separated list of tags to include (default: all)
  min_priority  — "low" | "medium" | "high" (default: low = show all)
  format        — "text" | "json" (default "text")

Examples:
    todo_scanner()                              # full scan
    todo_scanner(path="ouroboros/")             # one directory
    todo_scanner(tags="FIXME,BUG")              # high-priority only
    todo_scanner(min_priority="medium")
    todo_scanner(format="json")
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

# ── Constants ─────────────────────────────────────────────────────────────────

_REPO_DIR = Path(os.environ.get("REPO_DIR", "/opt/veles"))

_SKIP_DIRS = {
    "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
    "node_modules", ".venv", "venv", "dist", "build",
}

# Priority order (ascending)
_PRIORITY_ORDER = {"low": 0, "medium": 1, "high": 2}

# Tag → priority mapping
_TAG_PRIORITY: Dict[str, str] = {
    "FIXME": "high",
    "BUG": "high",
    "XXX": "high",
    "HACK": "medium",
    "TODO": "medium",
    "OPTIMIZE": "low",
    "NOTE": "low",
}

_ALL_TAGS = set(_TAG_PRIORITY)

# Regex: match a tag at the start of a comment body or inline annotation
# Supports: # TODO: ..., # TODO(...): ..., # FIXME ..., TODO: text
_TAG_PATTERN = re.compile(
    r"""
    (?:^|\s|\#)\s*
    (?P<tag>FIXME|BUG|XXX|HACK|TODO|OPTIMIZE|NOTE)
    (?:\s*\([^)]*\))?        # optional (author/ticket)
    [\s:!-]*                 # optional separator
    (?P<message>.*)          # rest of line
    """,
    re.VERBOSE | re.IGNORECASE,
)

# We only scan comment lines and string-only lines (docstring-like annotations)
_COMMENT_RE = re.compile(r"^\s*#(.*)$")
# Also catch inline: code  # TODO: ...
_INLINE_COMMENT_RE = re.compile(r"#(.*)$")


# ── File collection ───────────────────────────────────────────────────────────

def _collect_py_files(root: Path, subpath: Optional[str] = None) -> List[Path]:
    target = root
    if subpath:
        candidate = root / subpath.lstrip("/")
        if candidate.exists():
            target = candidate

    if target.is_file() and target.suffix == ".py":
        return [target]

    py_files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(str(target)):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        for fname in sorted(filenames):
            if fname.endswith(".py"):
                py_files.append(Path(dirpath) / fname)
    return py_files


# ── Finding record ─────────────────────────────────────────────────────────────

class _Finding:
    __slots__ = ("file", "line", "tag", "message", "priority")

    def __init__(
        self,
        file: str,
        line: int,
        tag: str,
        message: str,
        priority: str,
    ) -> None:
        self.file = file
        self.line = line
        self.tag = tag
        self.message = message.strip()
        self.priority = priority

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "tag": self.tag,
            "message": self.message,
            "priority": self.priority,
        }


# ── Per-file scanner ──────────────────────────────────────────────────────────

def _scan_file(
    path: Path,
    rel_path: str,
    enabled_tags: set,
    min_priority: str,
) -> List[_Finding]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    findings: List[_Finding] = []
    min_level = _PRIORITY_ORDER[min_priority]

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        # Extract comment portion: full-line comment or inline comment
        comment_text: Optional[str] = None

        full_match = _COMMENT_RE.match(raw_line)
        if full_match:
            comment_text = full_match.group(1)
        else:
            # Inline comment after code
            inline_match = _INLINE_COMMENT_RE.search(raw_line)
            if inline_match:
                comment_text = inline_match.group(1)

        if comment_text is None:
            continue

        # Search for any recognised tag inside the comment text
        for m in _TAG_PATTERN.finditer(comment_text):
            tag = m.group("tag").upper()
            if tag not in enabled_tags:
                continue
            priority = _TAG_PRIORITY[tag]
            if _PRIORITY_ORDER[priority] < min_level:
                continue
            message = m.group("message").strip()
            findings.append(
                _Finding(
                    file=rel_path,
                    line=lineno,
                    tag=tag,
                    message=message,
                    priority=priority,
                )
            )

    return findings


# ── Text formatter ─────────────────────────────────────────────────────────────

_PRIORITY_ICON = {"high": "🔴", "medium": "🟡", "low": "🔵"}


def _format_text(
    findings: List[_Finding],
    total_files: int,
    filters: Dict[str, Any],
) -> str:
    lines: List[str] = []

    filter_parts = []
    if filters.get("path"):
        filter_parts.append(f"path={filters['path']}")
    if filters.get("tags"):
        filter_parts.append(f"tags={filters['tags']}")
    if filters.get("min_priority", "low") != "low":
        filter_parts.append(f"min_priority={filters['min_priority']}")
    filter_str = (", " + ", ".join(filter_parts)) if filter_parts else ""

    lines.append(
        f"## TODO Scanner — {total_files} files, {len(findings)} annotation(s) found{filter_str}\n"
    )

    if not findings:
        lines.append("✅ No TODO/FIXME/HACK/BUG/XXX/OPTIMIZE/NOTE annotations found.")
        return "\n".join(lines)

    # Summary by tag
    by_tag: Dict[str, int] = {}
    for f in findings:
        by_tag[f.tag] = by_tag.get(f.tag, 0) + 1

    tag_order = sorted(
        _ALL_TAGS,
        key=lambda t: (-_PRIORITY_ORDER[_TAG_PRIORITY[t]], t),
    )
    for tag in tag_order:
        count = by_tag.get(tag, 0)
        if count == 0:
            continue
        icon = _PRIORITY_ICON[_TAG_PRIORITY[tag]]
        lines.append(f"   {icon} {tag:<12} {count:>4}")
    lines.append("")

    # Findings sorted by priority desc, then file, then line
    sorted_findings = sorted(
        findings,
        key=lambda f: (
            -_PRIORITY_ORDER[f.priority],
            f.file,
            f.line,
        ),
    )

    prev_priority = None
    for finding in sorted_findings:
        if finding.priority != prev_priority:
            icon = _PRIORITY_ICON[finding.priority]
            lines.append(f"### {icon} {finding.priority.upper()}")
            prev_priority = finding.priority

        msg_part = f"  {finding.message}" if finding.message else ""
        lines.append(
            f"   {finding.file}:{finding.line}  [{finding.tag}]{msg_part}"
        )

    return "\n".join(lines)


# ── Tool entry point ──────────────────────────────────────────────────────────

def _todo_scanner(
    ctx: ToolContext,
    path: Optional[str] = None,
    tags: Optional[str] = None,
    min_priority: str = "low",
    format: str = "text",
) -> str:
    """Scan codebase for TODO/FIXME/HACK/BUG/XXX/OPTIMIZE/NOTE annotations."""
    if min_priority not in _PRIORITY_ORDER:
        return (
            f"Unknown min_priority: {min_priority!r}. "
            f"Valid: low, medium, high"
        )

    # Resolve enabled tags
    if tags:
        requested = {t.strip().upper() for t in tags.split(",")}
        unknown = requested - _ALL_TAGS
        if unknown:
            return (
                f"Unknown tags: {', '.join(sorted(unknown))}. "
                f"Valid: {', '.join(sorted(_ALL_TAGS))}"
            )
        enabled_tags = requested
    else:
        enabled_tags = set(_ALL_TAGS)

    repo_root = Path(ctx.repo_dir if ctx and ctx.repo_dir else _REPO_DIR)
    py_files = _collect_py_files(repo_root, path)

    all_findings: List[_Finding] = []
    for fpath in py_files:
        try:
            rel = str(fpath.relative_to(repo_root))
        except ValueError:
            rel = str(fpath)
        all_findings.extend(_scan_file(fpath, rel, enabled_tags, min_priority))

    filters: Dict[str, Any] = {
        "path": path,
        "tags": tags,
        "min_priority": min_priority,
    }

    if format == "json":
        by_tag: Dict[str, int] = {}
        for f in all_findings:
            by_tag[f.tag] = by_tag.get(f.tag, 0) + 1
        return json.dumps(
            {
                "total_files": len(py_files),
                "total_findings": len(all_findings),
                "by_tag": by_tag,
                "findings": [f.to_dict() for f in all_findings],
                "filters": filters,
            },
            ensure_ascii=False,
            indent=2,
        )

    return _format_text(all_findings, len(py_files), filters)


# ── Tool registration ─────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="todo_scanner",
            schema={
                "name": "todo_scanner",
                "description": (
                    "Scan the Python codebase for developer-left annotations: "
                    "TODO, FIXME, HACK, BUG, XXX, OPTIMIZE, NOTE. "
                    "Returns findings grouped by priority with file and line. "
                    "Useful for discovering pending work, known bugs, and "
                    "temporary workarounds left in the code."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Limit scan to a subdirectory or file (relative to repo root)",
                        },
                        "tags": {
                            "type": "string",
                            "description": (
                                "Comma-separated list of tags to include. "
                                "Valid: TODO, FIXME, HACK, BUG, XXX, OPTIMIZE, NOTE "
                                "(default: all)"
                            ),
                        },
                        "min_priority": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                            "description": "Minimum priority to include (default: low = show all)",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format (default: text)",
                        },
                    },
                    "required": [],
                },
            },
            handler=_todo_scanner,
        )
    ]
