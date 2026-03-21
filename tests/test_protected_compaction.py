"""Tests for protected compaction in context.py."""

import pytest

from ouroboros.context import (
    compact_tool_history,
    _COMPACTION_PROTECTED_TOOLS,
    _find_tool_name_for_result,
)


def _make_tool_round(tool_name: str, call_id: str, result_content: str):
    """Create a pair of (assistant tool_call msg, tool result msg)."""
    assistant_msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": '{"arg": "value"}',
            },
        }],
    }
    tool_msg = {
        "role": "tool",
        "tool_call_id": call_id,
        "content": result_content,
    }
    return assistant_msg, tool_msg


def _build_messages_with_rounds(rounds: list, system_msg=None) -> list:
    """Build a messages list from a list of (tool_name, call_id, result) tuples."""
    msgs = []
    if system_msg:
        msgs.append(system_msg)
    for tool_name, call_id, result in rounds:
        a, t = _make_tool_round(tool_name, call_id, result)
        msgs.append(a)
        msgs.append(t)
    return msgs


class TestProtectedCompaction:

    def test_protects_commit_results(self):
        """repo_commit_push results should survive compaction."""
        rounds = [
            ("repo_commit_push", "call_1", "OK: committed and pushed to veles: v1.0.0 release"),
            ("repo_read", "call_2", "x" * 500),
            ("repo_read", "call_3", "y" * 500),
            ("repo_read", "call_4", "z" * 500),
            ("repo_read", "call_5", "w" * 500),
            ("repo_read", "call_6", "v" * 500),
            ("repo_read", "call_7", "u" * 500),
            ("repo_read", "call_8", "t" * 500),
        ]
        messages = _build_messages_with_rounds(rounds)
        result = compact_tool_history(messages, keep_recent=6)

        # Find the tool result for repo_commit_push (call_1)
        commit_result = None
        for msg in result:
            if msg.get("role") == "tool" and msg.get("tool_call_id") == "call_1":
                commit_result = msg
                break

        assert commit_result is not None
        # Protected: should keep full content
        assert "committed and pushed" in commit_result["content"]
        assert len(commit_result["content"]) > 40  # not truncated

    def test_protects_knowledge_results(self):
        """knowledge_read results should survive compaction."""
        rounds = [
            ("knowledge_read", "kr_1", "Topic: deployment\nSSH host: 1.2.3.4\nPath: /opt/veles\nKey: ~/.ssh/id_rsa"),
            ("repo_read", "rr_2", "x" * 500),
            ("repo_read", "rr_3", "y" * 500),
            ("repo_read", "rr_4", "z" * 500),
            ("repo_read", "rr_5", "w" * 500),
            ("repo_read", "rr_6", "v" * 500),
            ("repo_read", "rr_7", "u" * 500),
            ("repo_read", "rr_8", "t" * 500),
        ]
        messages = _build_messages_with_rounds(rounds)
        result = compact_tool_history(messages, keep_recent=6)

        kr_result = None
        for msg in result:
            if msg.get("role") == "tool" and msg.get("tool_call_id") == "kr_1":
                kr_result = msg
                break

        assert kr_result is not None
        # Protected: full content preserved
        assert "SSH host: 1.2.3.4" in kr_result["content"]

    def test_still_compacts_unprotected(self):
        """Non-protected tool results should still be compacted."""
        long_content = "Line 1: some repo content\n" + "x" * 500
        rounds = [
            ("repo_read", "rr_1", long_content),
            ("run_shell", "rs_2", "output " * 100),
            ("repo_read", "rr_3", "y" * 500),
            ("repo_read", "rr_4", "z" * 500),
            ("repo_read", "rr_5", "w" * 500),
            ("repo_read", "rr_6", "v" * 500),
            ("repo_read", "rr_7", "u" * 500),
            ("repo_read", "rr_8", "t" * 500),
        ]
        messages = _build_messages_with_rounds(rounds)
        result = compact_tool_history(messages, keep_recent=6)

        # The first two rounds should be compacted (unprotected)
        rr1_result = None
        rs2_result = None
        for msg in result:
            if msg.get("role") == "tool" and msg.get("tool_call_id") == "rr_1":
                rr1_result = msg
            if msg.get("role") == "tool" and msg.get("tool_call_id") == "rs_2":
                rs2_result = msg

        # These should be compacted (much shorter than original)
        assert rr1_result is not None
        assert len(rr1_result["content"]) < len(long_content)

    def test_find_tool_name_for_result(self):
        """_find_tool_name_for_result should match tool_call_id."""
        messages = _build_messages_with_rounds([
            ("repo_commit_push", "call_abc", "OK"),
            ("repo_read", "call_def", "content"),
        ])
        # Tool result for call_abc is at index 1
        tool_msg = messages[1]
        assert _find_tool_name_for_result(tool_msg, messages) == "repo_commit_push"

        # Tool result for call_def is at index 3
        tool_msg2 = messages[3]
        assert _find_tool_name_for_result(tool_msg2, messages) == "repo_read"

    def test_protected_tools_set_contents(self):
        """Verify the expected tools are in the protected set."""
        assert "repo_commit_push" in _COMPACTION_PROTECTED_TOOLS
        assert "repo_write_commit" in _COMPACTION_PROTECTED_TOOLS
        assert "knowledge_read" in _COMPACTION_PROTECTED_TOOLS
        assert "knowledge_write" in _COMPACTION_PROTECTED_TOOLS
        assert "knowledge_list" in _COMPACTION_PROTECTED_TOOLS
        assert "plan_step_done" in _COMPACTION_PROTECTED_TOOLS
        assert "plan_create" in _COMPACTION_PROTECTED_TOOLS
        assert "plan_complete" in _COMPACTION_PROTECTED_TOOLS
        # repo_read should NOT be protected
        assert "repo_read" not in _COMPACTION_PROTECTED_TOOLS
