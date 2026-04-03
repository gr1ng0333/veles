"""extract_patterns — auto-populate knowledge/patterns.md from task_reflections.

Reads task_reflections.jsonl, clusters recurring errors by marker+keyword,
and merges new patterns into knowledge/patterns.md — preserving existing rows.

Why: patterns.md was manually maintained. With 177 reflections, this is unsustainable.
This tool closes the feedback loop: reflection → patterns → better behavior.

Usage:
    extract_patterns()                    # scan all reflections, update patterns
    extract_patterns(min_count=3)         # only patterns seen ≥3 times
    extract_patterns(dry_run=True)        # preview without writing
    extract_patterns(format="json")       # return structured JSON
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone as dt_timezone
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")
_REFLECTIONS_FILE = "logs/task_reflections.jsonl"
_PATTERNS_FILE = "memory/knowledge/patterns.md"

# ── Keyword signatures per error class ────────────────────────────────────────
# Each entry: (class_name, [trigger_keywords])
# First class whose keywords appear in the reflection text wins.

_CLASS_SIGNATURES: List[Tuple[str, List[str]]] = [
    ("pre-push test timeout / smoke failure",
     ["pre_push", "pre-push", "smoke", "test_smoke", "test_version_artifacts",
      "pytest", "TESTS_FAILED", "test fail"]),
    ("tool timeout on commit/push",
     ["repo_write_commit", "repo_commit_push", "TOOL_TIMEOUT", "exceeded 30s",
      "exceeded 60s", "exceeded 90s", "push timeout"]),
    ("tool argument error",
     ["TOOL_ARG_ERROR", "unexpected keyword argument", "got an unexpected"]),
    ("wrong file path / missing module",
     ["No such file", "ModuleNotFoundError", "ImportError", "no such file",
      "cannot find", "bad path"]),
    ("copilot exhaustion / capacity",
     ["exhausted", "copilot_capacity", "all capable accounts", "cooldown",
      "COPILOT_CAPACITY"]),
    ("auto-rescue on startup",
     ["auto-rescue", "dirty worktree", "uncommitted changes on startup",
      "rescue commit"]),
    ("SSH / remote execution error",
     ["SSH", "ssh_key_deploy", "ssh_session_bootstrap", "remote_server_health",
      "password bootstrap", "PTY", "shell quoting"]),
    ("Copilot 500 / HTTP error",
     ["500", "502", "503", "HTTP error", "Bad Request", "400 Bad Request"]),
    ("loop round exhaustion",
     ["30 rounds", "31 rounds", "max_rounds", "round limit", "copilot 30"]),
    ("rescue ref / persistence failure",
     ["rescue ref", "refs/veles-rescue", "remote_materialization",
      "snapshot not persist", "not persist"]),
]


def _load_reflections(drive_root: pathlib.Path) -> List[Dict[str, Any]]:
    path = drive_root / _REFLECTIONS_FILE
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _classify_reflection(reflection: Dict[str, Any]) -> List[str]:
    """Return list of class names that match this reflection."""
    text = " ".join([
        reflection.get("reflection", ""),
        " ".join(reflection.get("key_markers", [])),
        reflection.get("goal", ""),
    ]).lower()

    matched: List[str] = []
    for class_name, keywords in _CLASS_SIGNATURES:
        if any(kw.lower() in text for kw in keywords):
            matched.append(class_name)
    return matched or ["unclassified"]


def _collect_evidence(records: List[Dict[str, Any]], class_name: str) -> List[str]:
    """Collect task_ids that belong to this class."""
    evidence = []
    for r in records:
        if class_name in _classify_reflection(r):
            evidence.append(r["task_id"])
    return evidence


def _extract_root_cause(records: List[Dict[str, Any]], class_name: str) -> str:
    """Pull a representative root-cause phrase from reflection text."""
    texts = []
    for r in records:
        if class_name in _classify_reflection(r):
            ref = r.get("reflection", "")
            # Find sentence containing "root cause" or "cause"
            for sent in re.split(r"[.!?]", ref):
                sl = sent.lower()
                if any(w in sl for w in ("root cause", "caused by", "is a combination",
                                          "the likely cause", "main issue", "key issue")):
                    texts.append(sent.strip())
                    break
    if texts:
        # pick the shortest (most concrete)
        return sorted(texts, key=len)[0][:200]
    # fallback: extract up to 80 chars of first reflection
    first = next(
        (r.get("reflection", "") for r in records if class_name in _classify_reflection(r)),
        "",
    )
    return first[:120].strip()


def _extract_fix(records: List[Dict[str, Any]], class_name: str) -> str:
    """Pull a representative fix recommendation."""
    texts = []
    for r in records:
        if class_name in _classify_reflection(r):
            ref = r.get("reflection", "")
            for sent in re.split(r"[.!?]", ref):
                sl = sent.lower()
                if any(w in sl for w in ("next time", "should", "fix", "avoid",
                                          "split the task", "verify", "run first")):
                    texts.append(sent.strip())
                    break
    if texts:
        return sorted(texts, key=len)[0][:200]
    return "See reflection text for details."


# ── Patterns.md parser / writer ───────────────────────────────────────────────

_HEADER = """# Pattern Register

| Class | Count | Evidence | Root cause | Fix |
|---|---:|---|---|---|
"""

_ROW_RE = re.compile(
    r"^\|\s*(.+?)\s*\|\s*(\d+\+?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|$"
)


def _parse_patterns_md(text: str) -> Dict[str, Dict[str, Any]]:
    """Parse existing patterns.md into dict keyed by class name."""
    rows: Dict[str, Dict[str, Any]] = {}
    for line in text.splitlines():
        m = _ROW_RE.match(line)
        if m:
            cls, count_str, evidence, root_cause, fix = (
                m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
            )
            # skip header row
            if cls in ("Class", "---"):
                continue
            try:
                count = int(count_str.rstrip("+"))
            except ValueError:
                count = 1
            rows[cls] = {
                "count": count,
                "evidence": evidence,
                "root_cause": root_cause,
                "fix": fix,
            }
    return rows


def _render_patterns_md(patterns: Dict[str, Dict[str, Any]]) -> str:
    """Render patterns dict back to markdown table."""
    lines = [_HEADER.rstrip()]
    for cls, data in sorted(patterns.items(), key=lambda x: -x[1]["count"]):
        count = data["count"]
        evidence = data["evidence"]
        root_cause = data["root_cause"].replace("|", "\\|").replace("\n", " ")
        fix = data["fix"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {cls} | {count}+ | {evidence} | {root_cause} | {fix} |")
    return "\n".join(lines) + "\n"


def _short_ids(ids: List[str]) -> str:
    """Render short evidence list for table cell."""
    if not ids:
        return ""
    sample = ids[:3]
    parts = [f"`{t[:8]}`" for t in sample]
    if len(ids) > 3:
        parts.append(f"... +{len(ids) - 3}")
    return ", ".join(parts)


# ── Main logic ─────────────────────────────────────────────────────────────────

def _run(
    ctx: ToolContext,
    min_count: int = 2,
    dry_run: bool = False,
    format: str = "text",
) -> str:
    drive = pathlib.Path(_DRIVE_ROOT)
    records = _load_reflections(drive)
    if not records:
        return "No reflections found."

    # Classify every reflection
    class_to_task_ids: Dict[str, List[str]] = defaultdict(list)
    for r in records:
        for cls in _classify_reflection(r):
            class_to_task_ids[cls].append(r["task_id"])

    # Filter by min_count
    qualifying = {
        cls: ids
        for cls, ids in class_to_task_ids.items()
        if len(ids) >= min_count and cls != "unclassified"
    }

    # Load existing patterns
    patterns_path = drive / _PATTERNS_FILE
    existing_text = patterns_path.read_text(encoding="utf-8") if patterns_path.exists() else ""
    existing = _parse_patterns_md(existing_text)

    new_classes: List[str] = []
    updated_classes: List[str] = []

    merged = dict(existing)

    for cls, task_ids in qualifying.items():
        count = len(task_ids)
        evidence_str = _short_ids(task_ids)
        root_cause = _extract_root_cause(records, cls)
        fix = _extract_fix(records, cls)

        if cls not in merged:
            merged[cls] = {
                "count": count,
                "evidence": evidence_str,
                "root_cause": root_cause,
                "fix": fix,
            }
            new_classes.append(cls)
        else:
            # Update count and evidence (keep existing root_cause/fix if richer)
            old = merged[cls]
            merged[cls] = {
                "count": max(count, old["count"]),
                "evidence": evidence_str if count > old["count"] else old["evidence"],
                "root_cause": old["root_cause"] if len(old["root_cause"]) > len(root_cause) else root_cause,
                "fix": old["fix"] if len(old["fix"]) > len(fix) else fix,
            }
            if count > old["count"]:
                updated_classes.append(cls)

    rendered = _render_patterns_md(merged)

    if format == "json":
        return json.dumps({
            "total_reflections": len(records),
            "qualifying_patterns": len(qualifying),
            "new": new_classes,
            "updated": updated_classes,
            "patterns": merged,
        }, ensure_ascii=False, indent=2)

    # Text summary
    lines: List[str] = [
        f"Scanned {len(records)} reflections.",
        f"Patterns qualifying (≥{min_count} occurrences): {len(qualifying)}",
        f"New patterns added: {len(new_classes)}",
        f"Updated patterns: {len(updated_classes)}",
        "",
    ]
    for cls in new_classes:
        lines.append(f"  + NEW: {cls} ({len(class_to_task_ids[cls])} hits)")
    for cls in updated_classes:
        lines.append(f"  ~ UPDATED: {cls} → {len(class_to_task_ids[cls])} hits")

    if not dry_run:
        patterns_path.parent.mkdir(parents=True, exist_ok=True)
        patterns_path.write_text(rendered, encoding="utf-8")
        lines.append("")
        lines.append(f"Written to {patterns_path}")
    else:
        lines.append("")
        lines.append("(dry_run=True — nothing written)")
        lines.append("")
        lines.append("Preview of patterns.md:")
        lines.append(rendered)

    return "\n".join(lines)


# ── Tool registration ─────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="extract_patterns",
            schema={
                "name": "extract_patterns",
                "description": (
                    "Auto-extract recurring error patterns from task_reflections.jsonl "
                    "and update knowledge/patterns.md. Closes the feedback loop: "
                    "reflection → patterns → better behavior. "
                    "Run after any evolution session with errors to keep patterns.md current."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "min_count": {
                            "type": "integer",
                            "description": "Minimum occurrences to include a pattern (default 2)",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Preview without writing to disk (default false)",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format (default text)",
                        },
                    },
                    "required": [],
                },
            },
            handler=_run,
        )
    ]
