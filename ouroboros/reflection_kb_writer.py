"""reflection_kb_writer — auto-write actionable insights from task reflections to KB topics.

Called automatically after each reflection is saved (no LLM required).
Maps error markers/keywords to relevant KB topics and appends concrete fix notes
as dated bullet points.

This closes the feedback loop: every reflection with a concrete actionable insight
now automatically enriches the appropriate KB topic — not just patterns.md aggregate.
"""

from __future__ import annotations

import logging
import pathlib
import re
from datetime import datetime, timezone
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Marker → KB topic routing
# Priority: first matching rule wins.
# Each rule: (trigger_keywords, target_topic)
# Keywords are matched case-insensitively against: markers + reflection text.
# ---------------------------------------------------------------------------

_ROUTING: List[Tuple[List[str], str]] = [
    # Version artifact mismatches → release gotchas
    (["test_version_artifacts", "readme badge", "badge=", "readme text", "pyproject.toml"], "release-contour-gotchas"),
    # Smoke test / tool-set drift → release gotchas
    (["test_smoke", "test_tool_set", "extra tools", "smoke"], "release-contour-gotchas"),
    # Any TESTS_FAILED without more specific match → release gotchas
    (["TESTS_FAILED", "pre_push", "pre-push", "push blocked"], "release-contour-gotchas"),
    # Commit/push timeout
    (["repo_write_commit", "repo_commit_push", "exceeded 30s", "exceeded 60s", "exceeded 90s", "push timeout"], "timeout-guard-gotchas"),
    # Generic tool timeout
    (["TOOL_TIMEOUT"], "timeout-guard-gotchas"),
    # SSH / remote
    (["ssh_key_deploy", "ssh_session_bootstrap", "remote_server_health", "password bootstrap"], "ssh-remote-contour"),
    # Copilot exhaustion
    (["copilot_capacity", "all capable accounts", "exhausted"], "copilot-usage-accounting"),
    # HTTP errors on Copilot
    (["400 bad request", "500", "copilot", "http error"], "copilot-usage-accounting"),
    # Import / module errors
    (["modulenotfounderror", "importerror", "no such file", "no module named"], "release-contour-gotchas"),
]

# ---------------------------------------------------------------------------
# Insight extraction
# ---------------------------------------------------------------------------

# Ordered list of regex patterns to extract actionable sentences.
# Each pattern tries to capture a concrete "next time / should / fix" statement.
_INSIGHT_RE: List[re.Pattern] = [
    re.compile(r"[Nn]ext time[,:]?\s*([A-Z][^.!?]{15,250})[.!?]"),
    re.compile(r"[Ss]hould\s+([a-z][^.!?]{15,250})[.!?]"),
    re.compile(r"[Ff]ix\b[:\s]+([A-Za-z][^.!?]{15,250})[.!?]"),
    re.compile(r"[Aa]void\s+([a-z][^.!?]{15,250})[.!?]"),
    re.compile(r"[Vv]erify\s+([a-z][^.!?]{15,250})[.!?]"),
    re.compile(r"[Rr]un\s+(.{15,200})\s+first[.!?]"),
    re.compile(r"[Ss]plit the task[^.!?]{0,200}[.!?]"),
    re.compile(r"[Uu]pdate.{5,100}(before attempting|before push|in lockstep)[^.!?]{0,150}[.!?]"),
]


def _extract_insight(text: str) -> Optional[str]:
    """Extract the most concrete actionable sentence from a reflection text.

    Returns a trimmed string (20–300 chars), or None if nothing useful found.
    """
    if not text:
        return None

    best: Optional[str] = None
    best_len: int = 0

    for pattern in _INSIGHT_RE:
        m = pattern.search(text)
        if m:
            candidate = m.group(0).strip().rstrip(".,;")
            length = len(candidate)
            # Prefer longer (more specific) matches, but not over 300 chars
            if 20 <= length <= 300 and length > best_len:
                best = candidate
                best_len = length

    return best


def _match_topic(key_markers: List[str], reflection_text: str) -> Optional[str]:
    """Return the most relevant KB topic for this reflection, or None.

    Concatenates markers + lowercased reflection for matching.
    First routing rule to match wins.
    """
    haystack = " ".join(key_markers).lower() + " " + reflection_text.lower()

    for keywords, topic in _ROUTING:
        if any(kw.lower() in haystack for kw in keywords):
            return topic

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def maybe_write_kb_insight(
    drive_root: pathlib.Path,
    task_id: str,
    key_markers: List[str],
    reflection_text: str,
    goal: str = "",
) -> Optional[str]:
    """Auto-write an actionable insight from a reflection to the appropriate KB topic.

    - Finds the KB topic matching the error class (via _ROUTING).
    - Extracts one concrete actionable sentence from the reflection.
    - Appends a dated bullet to the KB topic file.

    Returns the topic name written to, or None if nothing was written
    (no match, no insight, or write error).

    Designed to be called from reflection.append_reflection() — never raises.
    """
    try:
        if not reflection_text:
            return None

        topic = _match_topic(key_markers or [], reflection_text)
        if not topic:
            return None

        insight = _extract_insight(reflection_text)
        if not insight:
            return None

        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        task_short = (task_id or "")[:8] or "unknown"
        # Format as an append-friendly bullet with task reference
        entry = f"\n- **{ts}** (`{task_short}`): {insight}\n"

        kb_path = drive_root / "memory" / "knowledge" / f"{topic}.md"
        kb_path.parent.mkdir(parents=True, exist_ok=True)

        # Avoid writing exact duplicate entries (same task_id)
        if kb_path.exists():
            existing = kb_path.read_text(encoding="utf-8")
            if task_short in existing and insight[:40] in existing:
                log.debug("KB insight already present for task %s in %s", task_short, topic)
                return None

        with open(kb_path, "a", encoding="utf-8") as f:
            f.write(entry)

        log.info("KB insight auto-written to topic '%s' (task=%s, markers=%s)", topic, task_short, key_markers)
        return topic

    except Exception:
        log.debug("reflection_kb_writer: non-critical failure", exc_info=True)
        return None
