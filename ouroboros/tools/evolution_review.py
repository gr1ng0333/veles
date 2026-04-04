"""evolution_review — one-call evolution cycle code reviewer.

Runs multi_model_review with the canonical evolution reviewer models
(get_evolution_reviewer_models()) against the current staged diff or
a specific commit. No need to manually specify models — they are
selected automatically using the same logic as the evolution loop.

This is the missing bridge between `pre_commit_review` (fast static check)
and raw `multi_model_review` (requires you to pick models manually).

Tools:
    evolution_review(ref?, prompt_hint?, staged?)
        — run multi-model review on staged diff or a commit ref

Usage:
    # Review staged changes before committing:
    evolution_review()

    # Review a specific commit:
    evolution_review(ref="abc1234")

    # Add a custom focus hint to the review prompt:
    evolution_review(prompt_hint="Focus on thread safety in loop_runtime.py")

Output:
    - List of verdicts per model (PASS/FAIL/ERROR)
    - Consensus verdict
    - Full text from each reviewer
    - Total cost estimate
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

_REPO_DIR = Path(os.environ.get("OUROBOROS_REPO_DIR", "/opt/veles"))

# Default review prompt focuses on CHECKLISTS.md but can be extended
_DEFAULT_PROMPT_BASE = """\
You are a strict code reviewer for Veles, a self-modifying AI agent.

Review the provided git diff for correctness, safety, and compliance with the
Veles coding standards and BIBLE.md principles.

Focus on:
1. Correctness — logic bugs, missing edge cases, broken imports
2. Safety — no hardcoded secrets, no deletions of critical files
3. Architecture — does the change follow Principle 5 (Minimalism)?
4. Tests — does the change include or update relevant tests?
5. VERSION bump — is the version updated for functional changes?

Start your response with PASS or FAIL on the first line.
Then explain your verdict concisely (150-250 words).
"""


def _git(args: List[str]) -> tuple[int, str]:
    """Run git command, return (returncode, stdout)."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            cwd=str(_REPO_DIR),
            timeout=20,
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except Exception as exc:
        return 1, str(exc)


def _get_diff(ref: Optional[str], staged: bool) -> str:
    """Get the diff to review."""
    if ref:
        rc, out = _git(["diff", f"{ref}^", ref])
        if rc == 0 and out:
            return out
        # Maybe it's the first commit
        rc, out = _git(["show", ref])
        return out if rc == 0 else ""

    if staged:
        rc, out = _git(["diff", "--cached"])
        if rc == 0 and out:
            return out

    # Fall back to unstaged vs HEAD
    rc, out = _git(["diff", "HEAD"])
    return out if rc == 0 else ""


def _load_checklists() -> str:
    """Load CHECKLISTS.md for injection into the review prompt."""
    try:
        path = _REPO_DIR / "prompts" / "CHECKLISTS.md"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def _build_review_prompt(prompt_hint: Optional[str]) -> str:
    prompt = _DEFAULT_PROMPT_BASE

    checklists = _load_checklists()
    if checklists:
        prompt += "\n\n## Review Checklist\n" + checklists
        prompt += "\n\nFor each checklist item, assess: PASS, FAIL, or N/A.\n"

    if prompt_hint:
        prompt += f"\n\n## Additional Focus\n{prompt_hint.strip()}"

    prompt += "\n\nAt the end, restate your overall verdict: PASS or FAIL."
    return prompt


def _handle_evolution_review(
    ctx: ToolContext,
    ref: Optional[str] = None,
    prompt_hint: Optional[str] = None,
    staged: bool = True,
) -> str:
    """Run evolution_review. Uses canonical reviewer models automatically."""

    # 1. Get the diff
    diff = _get_diff(ref, staged)
    if not diff:
        source = f"ref={ref}" if ref else ("staged changes" if staged else "HEAD diff")
        return json.dumps({
            "ok": False,
            "error": f"No diff found for {source}. Nothing to review.",
        }, ensure_ascii=False)

    # Truncate very long diffs (>40k chars) to avoid context overflow
    max_diff_chars = 40_000
    truncated = False
    if len(diff) > max_diff_chars:
        diff = diff[:max_diff_chars] + f"\n\n... [diff truncated at {max_diff_chars} chars]"
        truncated = True

    # 2. Get reviewer models
    try:
        from ouroboros.model_modes import get_evolution_reviewer_models
        models = get_evolution_reviewer_models()
    except Exception as exc:
        log.warning("Failed to get reviewer models, using defaults: %s", exc)
        models = ["codex/gpt-5.4", "copilot/claude-sonnet-4.6"]

    if not models:
        return json.dumps({"ok": False, "error": "No reviewer models configured."}, ensure_ascii=False)

    # 3. Build prompt
    prompt = _build_review_prompt(prompt_hint)

    # 4. Run multi_model_review via the tool handler
    try:
        from ouroboros.tools.review import _multi_model_review
        result = _multi_model_review(
            content=diff,
            prompt=prompt,
            models=models,
            ctx=ctx,
        )
    except Exception as exc:
        log.error("evolution_review: multi_model_review failed: %s", exc, exc_info=True)
        return json.dumps({"ok": False, "error": f"Review failed: {exc}"}, ensure_ascii=False)

    # 5. Compute consensus
    verdicts = [r.get("verdict", "UNKNOWN") for r in result.get("results", [])]
    pass_count = verdicts.count("PASS")
    fail_count = verdicts.count("FAIL")
    error_count = verdicts.count("ERROR")

    if fail_count > 0:
        consensus = "FAIL"
    elif error_count == len(verdicts):
        consensus = "ERROR"
    elif pass_count > 0:
        consensus = "PASS"
    else:
        consensus = "UNKNOWN"

    total_cost = sum(r.get("cost_estimate", 0.0) for r in result.get("results", []))
    diff_lines = diff.count("\n")

    return json.dumps({
        "ok": True,
        "consensus": consensus,
        "pass": pass_count,
        "fail": fail_count,
        "error": error_count,
        "models_used": models,
        "diff_lines": diff_lines,
        "truncated": truncated,
        "total_cost_usd": round(total_cost, 6),
        "results": result.get("results", []),
    }, ensure_ascii=False)


_SCHEMA = {
    "name": "evolution_review",
    "description": (
        "Run a multi-model code review on the current staged diff or a specific commit. "
        "Uses the canonical evolution reviewer models (Codex GPT-5.4 + Copilot Sonnet) "
        "automatically — no need to specify models manually.\n\n"
        "Returns: consensus verdict (PASS/FAIL), per-model verdicts, and full review text.\n\n"
        "Usage:\n"
        "  evolution_review()                    # review staged changes\n"
        "  evolution_review(ref='abc1234')       # review a specific commit\n"
        "  evolution_review(staged=False)        # review unstaged changes vs HEAD\n"
        "  evolution_review(prompt_hint='Focus on thread safety')  # add custom focus\n\n"
        "Preferred before any significant commit during evolution cycles. "
        "Complements pre_commit_review (static) with actual LLM judgment."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "Optional git commit ref to review (e.g. 'abc1234'). "
                               "If omitted, reviews staged changes.",
            },
            "prompt_hint": {
                "type": "string",
                "description": "Optional additional focus hint for reviewers "
                               "(e.g. 'Pay attention to async safety'). "
                               "Appended to the standard review prompt.",
            },
            "staged": {
                "type": "boolean",
                "description": "If true (default), review staged (git add) changes. "
                               "If false, review all changes vs HEAD.",
            },
        },
        "required": [],
    },
}


def get_tools() -> list:
    return [
        ToolEntry(
            name="evolution_review",
            schema=_SCHEMA,
            handler=lambda ctx, **kw: _handle_evolution_review(ctx, **kw),
        )
    ]
