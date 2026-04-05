#!/usr/bin/env python3
"""update_tool_snapshot — auto-sync EXPECTED_TOOLS in tests/test_smoke.py.

Run this after adding or removing tools to regenerate the expected set
instead of manually editing the giant list.

Usage:
    python tests/update_tool_snapshot.py         # dry-run: show diff only
    python tests/update_tool_snapshot.py --apply  # write to test_smoke.py

The script loads the live ToolRegistry, extracts all tool names, then
replaces the EXPECTED_TOOLS list in test_smoke.py while preserving:
  - All other code (tests, imports, fixtures)
  - Comment groupings for known tool categories
  - Alphabetical sort within the auto-generated section

Rationale:
  test_smoke.py::test_tool_set_matches requires EXPECTED_TOOLS to exactly
  match the live registry. This list has 25+ commits in 7 days — every
  new tool requires a manual update. This script eliminates that friction.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import tempfile
from typing import List, Set

REPO_ROOT = pathlib.Path(__file__).parent.parent
SMOKE_TEST = REPO_ROOT / "tests" / "test_smoke.py"

# Marker comments in test_smoke.py that delimit EXPECTED_TOOLS
_START_MARKER = "EXPECTED_TOOLS = ["
_END_MARKER = "]"  # the closing bracket on its own line


def _load_registry_tools() -> Set[str]:
    """Load the live ToolRegistry and return all registered tool names."""
    sys.path.insert(0, str(REPO_ROOT))
    import importlib
    # Force fresh import (useful when running in dev mode)
    if "ouroboros.tools.registry" in sys.modules:
        importlib.reload(sys.modules["ouroboros.tools.registry"])

    from ouroboros.tools.registry import ToolRegistry
    with tempfile.TemporaryDirectory() as tmp:
        reg = ToolRegistry(pathlib.Path(tmp), pathlib.Path(tmp))
        return set(reg.available_tools())


def _parse_expected_tools(content: str) -> tuple[int, int, Set[str]]:
    """Find EXPECTED_TOOLS = [...] in content, return (start_line, end_line, current_set).

    Lines are 0-indexed. end_line is the closing ']' line (exclusive).
    """
    lines = content.splitlines()
    start_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith(_START_MARKER):
            start_idx = i
            break
    if start_idx is None:
        raise ValueError(f"Could not find '{_START_MARKER}' in test_smoke.py")

    # Find the closing bracket — look for a line that is exactly "]" or "],"
    end_idx = None
    # The list may span many lines; find closing ']' at column 0
    for i in range(start_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped in ("]", "],"):
            # Make sure we're at module level (no leading spaces for pure list close)
            if not lines[i].startswith(" ") or lines[i].lstrip().startswith("]"):
                end_idx = i
                break

    if end_idx is None:
        raise ValueError("Could not find closing ']' for EXPECTED_TOOLS list")

    # Extract current tool names from the block
    current_tools: Set[str] = set()
    for line in lines[start_idx:end_idx + 1]:
        for m in re.finditer(r'"([a-zA-Z_][a-zA-Z0-9_]*)"', line):
            current_tools.add(m.group(1))

    return start_idx, end_idx, current_tools


def _build_replacement_block(tools: Set[str]) -> str:
    """Build the EXPECTED_TOOLS = [...] block as a string.

    Groups tools by known prefixes for readability, then has an
    alphabetical 'other' group for everything not otherwise classified.
    """
    # Known prefix groups — in display order
    groups: List[tuple[str, List[str]]] = []

    def _group(comment: str, names: List[str]) -> None:
        names = sorted(set(names) & tools)
        if names:
            groups.append((comment, names))

    remaining = set(tools)

    def _take(prefix: str) -> List[str]:
        matched = sorted(n for n in remaining if n.startswith(prefix))
        remaining.difference_update(matched)
        return matched

    # Core repo/drive/git tools
    _group("# Core: repo", _take("repo_"))
    _group("# Core: drive", _take("drive_"))
    _group("# Core: git", _take("git_"))
    _group("# Core: shell + search", ["run_shell", "web_search", "academic_search"])
    remaining.difference_update({"run_shell", "web_search", "academic_search"})
    _group("# Core: memory", ["chat_history", "update_scratchpad", "update_identity"])
    remaining.difference_update({"chat_history", "update_scratchpad", "update_identity"})
    _group("# Core: control", sorted(n for n in remaining if n in {
        "request_restart", "promote_to_stable", "request_review",
        "schedule_task", "cancel_task", "switch_model",
        "toggle_evolution", "toggle_consciousness",
    }))
    remaining.difference_update({
        "request_restart", "promote_to_stable", "request_review",
        "schedule_task", "cancel_task", "switch_model",
        "toggle_evolution", "toggle_consciousness",
    })
    _group("# Core: messaging", sorted(n for n in remaining if n in {
        "send_owner_message", "send_photo", "send_browser_screenshot",
        "save_artifact", "list_incoming_artifacts",
        "send_document", "send_local_file", "send_documents",
    }))
    remaining.difference_update({
        "send_owner_message", "send_photo", "send_browser_screenshot",
        "save_artifact", "list_incoming_artifacts",
        "send_document", "send_local_file", "send_documents",
    })
    _group("# Core: knowledge + plans", sorted(n for n in remaining if n in {
        "knowledge_read", "knowledge_write", "knowledge_list",
        "plan_create", "plan_approve", "plan_reject",
        "plan_step_done", "plan_update", "plan_complete", "plan_status",
    }))
    remaining.difference_update({
        "knowledge_read", "knowledge_write", "knowledge_list",
        "plan_create", "plan_approve", "plan_reject",
        "plan_step_done", "plan_update", "plan_complete", "plan_status",
    })
    _group("# Core: task results", sorted(n for n in remaining if n in {
        "get_task_result", "wait_for_task",
    }))
    remaining.difference_update({"get_task_result", "wait_for_task"})

    # Browser
    _group("# Browser", _take("browser_"))
    remaining.discard("browse_page")
    remaining.discard("browser_action")
    browser_core = [n for n in ["browse_page", "browser_action"] if n in tools]
    if browser_core:
        groups.insert(
            next(i for i, (c, _) in enumerate(groups) if "Browser" in c),
            ("# Browser core", browser_core),
        )

    # VLM / Vision
    _group("# VLM / Vision", sorted(n for n in remaining if n in {
        "analyze_screenshot", "vlm_query", "solve_simple_captcha",
    }))
    remaining.difference_update({"analyze_screenshot", "vlm_query", "solve_simple_captcha"})

    # Grouped by prefix
    for prefix, comment in [
        ("project_", "# Projects"),
        ("external_repo_", "# External repos"),
        ("ssh_", "# SSH"),
        ("remote_", "# Remote"),
        ("tg_", "# Telegram"),
        ("web_monitor", "# Web monitor"),
        ("rss_", "# RSS"),
        ("hn_", "# Hacker News"),
        ("reddit_", "# Reddit"),
        ("arxiv_", "# arXiv"),
        ("yt_", "# YouTube"),
        ("digest_", "# Digest"),
        ("gh_", "# GitHub watch"),
        ("tiktok_", "# TikTok"),
        ("note_", "# Notes"),
        ("veles_", "# Veles channel"),
        ("project_bible_", "# Project Bible"),
    ]:
        _group(comment, _take(prefix))

    # Evolution / skills / analysis
    _group("# Evolution focus", _take("evolution_focus_") + [
        n for n in _take("evolution_") if n not in {n for _, ns in groups for n in ns}
    ])
    for prefix in ["skill_", "memory_", "activity_", "extract_", "task_",
                   "hot_", "version_", "context_", "self_", "code_",
                   "ast_", "dependency_", "budget_", "run_tests",
                   "log_query", "http_", "multi_", "codebase_",
                   "git_history", "pre_commit_", "ssh_key_",
                   "research_", "doctor", "monitor_", "time_", "inbox_",
                   "article_", "list_available", "enable_tools", "tool_map",
                   "forward_", "compact_", "vps_", "summarize_",
                   "switch_codex", "generate_"]:
        # for each prefix, grab exactly the tokens starting with it
        matched = sorted(n for n in remaining if n.startswith(prefix) or n == prefix.rstrip("_"))
        if matched:
            _group(f"# {prefix.rstrip('_').replace('_', ' ').title()}", matched)
            remaining.difference_update(matched)

    # Remaining (anything not yet categorised)
    if remaining:
        _group("# Misc", sorted(remaining))

    # Render
    lines = ["EXPECTED_TOOLS = ["]
    for comment, names in groups:
        if not names:
            continue
        lines.append(f"    {comment}")
        for name in names:
            lines.append(f'    "{name}",')
    lines.append("]")
    return "\n".join(lines)


def _apply_update(content: str, start_idx: int, end_idx: int, new_block: str) -> str:
    """Replace EXPECTED_TOOLS block in content."""
    lines = content.splitlines(keepends=True)
    new_block_lines = [ln + "\n" for ln in new_block.splitlines()]
    result = lines[:start_idx] + new_block_lines + lines[end_idx + 1:]
    return "".join(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Write changes to test_smoke.py (default: dry-run)")
    args = parser.parse_args()

    print("Loading live ToolRegistry...")
    live_tools = _load_registry_tools()
    print(f"  {len(live_tools)} tools registered")

    content = SMOKE_TEST.read_text(encoding="utf-8")
    start_idx, end_idx, current_tools = _parse_expected_tools(content)
    print(f"  {len(current_tools)} tools in current EXPECTED_TOOLS (lines {start_idx+1}–{end_idx+1})")

    added = live_tools - current_tools
    removed = current_tools - live_tools

    if not added and not removed:
        print("✅ EXPECTED_TOOLS is already in sync with the live registry.")
        return 0

    if added:
        print(f"\n  ➕ Added ({len(added)}):")
        for name in sorted(added):
            print(f"     + {name}")
    if removed:
        print(f"\n  ➖ Removed ({len(removed)}):")
        for name in sorted(removed):
            print(f"     - {name}")

    new_block = _build_replacement_block(live_tools)

    if args.apply:
        new_content = _apply_update(content, start_idx, end_idx, new_block)
        SMOKE_TEST.write_text(new_content, encoding="utf-8")
        print(f"\n✅ test_smoke.py updated — EXPECTED_TOOLS now has {len(live_tools)} entries.")
        print("   Run: python -m pytest tests/test_smoke.py::test_tool_set_matches -x -q")
    else:
        print("\n--- dry-run: new EXPECTED_TOOLS block would be ---")
        for line in new_block.splitlines()[:30]:
            print(f"  {line}")
        if len(new_block.splitlines()) > 30:
            print(f"  ... ({len(new_block.splitlines()) - 30} more lines)")
        print("\nRun with --apply to write changes.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
