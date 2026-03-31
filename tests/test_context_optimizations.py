"""Tests for context optimizations ported from Ouroboros Desktop v4.5.0.

Covers:
- Task-scoped filtering in _build_recent_sections
- Cache hit rate computation and health invariant
- System message provenance in chat formatting
"""

import json
import pathlib
import tempfile

import pytest

from ouroboros.context import _build_recent_sections, _compute_cache_hit_rate, _build_health_invariants
from ouroboros.memory import Memory, RECENT_FULL_REPLIES


# ---------- helpers ----------

class _EnvStub:
    def __init__(self, repo_root: pathlib.Path, drive_root: pathlib.Path):
        self._repo_root = repo_root
        self._drive_root = drive_root

    def repo_path(self, rel: str) -> pathlib.Path:
        return self._repo_root / rel

    def drive_path(self, rel: str) -> pathlib.Path:
        return self._drive_root / rel


def _make_env(tmp_path: pathlib.Path) -> _EnvStub:
    repo = tmp_path / "repo"
    drive = tmp_path / "drive"
    repo.mkdir(parents=True, exist_ok=True)
    (drive / "logs").mkdir(parents=True, exist_ok=True)
    (drive / "memory").mkdir(parents=True, exist_ok=True)
    (drive / "state").mkdir(parents=True, exist_ok=True)
    return _EnvStub(repo_root=repo, drive_root=drive)


def _make_memory(drive_root: pathlib.Path) -> Memory:
    return Memory(drive_root=drive_root)


def _write_jsonl(path: pathlib.Path, entries: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _chat_entry(direction: str, text: str, ts: str = "2026-03-20T12:00:00Z",
                entry_type: str = "") -> dict:
    d = {"ts": ts, "session_id": "s1", "direction": direction, "chat_id": 1,
         "user_id": 1, "text": text}
    if entry_type:
        d["type"] = entry_type
    return d


# ---------- Task-scoped filtering ----------

class TestTaskScopedFiltering:
    """Recent progress/tools/events should filter by current task_id."""

    def test_recent_events_filtered_by_task_id(self, tmp_path):
        """Recent events should only show entries matching current task_id."""
        env = _make_env(tmp_path)
        mem = _make_memory(env._drive_root)

        events = [
            {"ts": "2026-03-20T12:00:00Z", "type": "tool_call", "task_id": "task-A", "text": "old task event"},
            {"ts": "2026-03-20T12:01:00Z", "type": "tool_call", "task_id": "task-A", "text": "another old event"},
            {"ts": "2026-03-20T12:02:00Z", "type": "tool_call", "task_id": "task-B", "text": "current task event"},
            {"ts": "2026-03-20T12:03:00Z", "type": "error", "task_id": "task-B", "text": "current error"},
        ]
        _write_jsonl(env._drive_root / "logs" / "events.jsonl", events)
        # Empty other logs
        _write_jsonl(env._drive_root / "logs" / "chat.jsonl", [])
        _write_jsonl(env._drive_root / "logs" / "progress.jsonl", [])
        _write_jsonl(env._drive_root / "logs" / "tools.jsonl", [])
        _write_jsonl(env._drive_root / "logs" / "supervisor.jsonl", [])

        sections = _build_recent_sections(mem, env, task_id="task-B")
        events_text = [s for s in sections if "Recent events" in s]

        assert events_text, "Expected Recent events section"
        combined = events_text[0]
        # Filtered to task-B: 1 tool_call + 1 error (not 2 tool_call from task-A)
        assert "tool_call: 1" in combined
        assert "error: 1" in combined

    def test_recent_events_fallback_when_empty(self, tmp_path):
        """If no events match task_id, show last N without filter."""
        env = _make_env(tmp_path)
        mem = _make_memory(env._drive_root)

        events = [
            {"ts": "2026-03-20T12:00:00Z", "type": "tool_call", "task_id": "task-A", "text": "event A1"},
            {"ts": "2026-03-20T12:01:00Z", "type": "tool_call", "task_id": "task-A", "text": "event A2"},
            {"ts": "2026-03-20T12:02:00Z", "type": "tool_call", "task_id": "task-A", "text": "event A3"},
        ]
        _write_jsonl(env._drive_root / "logs" / "events.jsonl", events)
        _write_jsonl(env._drive_root / "logs" / "chat.jsonl", [])
        _write_jsonl(env._drive_root / "logs" / "progress.jsonl", [])
        _write_jsonl(env._drive_root / "logs" / "tools.jsonl", [])
        _write_jsonl(env._drive_root / "logs" / "supervisor.jsonl", [])

        # task-X has no matching events, so fallback should show recent
        sections = _build_recent_sections(mem, env, task_id="task-X")
        events_text = [s for s in sections if "Recent events" in s]

        # Fallback: should still have events from task-A
        assert events_text, "Expected fallback Recent events section"

    def test_recent_progress_filtered_by_task_id(self, tmp_path):
        """Recent progress should only show entries matching current task_id."""
        env = _make_env(tmp_path)
        mem = _make_memory(env._drive_root)

        progress = [
            {"ts": "2026-03-20T12:00:00Z", "task_id": "task-A", "text": "old progress"},
            {"ts": "2026-03-20T12:01:00Z", "task_id": "task-B", "text": "current progress"},
        ]
        _write_jsonl(env._drive_root / "logs" / "progress.jsonl", progress)
        _write_jsonl(env._drive_root / "logs" / "chat.jsonl", [])
        _write_jsonl(env._drive_root / "logs" / "events.jsonl", [])
        _write_jsonl(env._drive_root / "logs" / "tools.jsonl", [])
        _write_jsonl(env._drive_root / "logs" / "supervisor.jsonl", [])

        sections = _build_recent_sections(mem, env, task_id="task-B")
        progress_text = [s for s in sections if "Recent progress" in s]
        assert progress_text
        combined = progress_text[0]
        assert "current progress" in combined
        assert "old progress" not in combined

    def test_chat_not_filtered(self, tmp_path):
        """Recent chat should NOT be filtered by task_id."""
        env = _make_env(tmp_path)
        mem = _make_memory(env._drive_root)

        chats = [
            {"ts": "2026-03-20T12:00:00Z", "direction": "in", "text": "hi from old task",
             "session_id": "s1", "chat_id": 1, "user_id": 1, "task_id": "task-A"},
            {"ts": "2026-03-20T12:01:00Z", "direction": "out", "text": "reply from agent",
             "session_id": "s1", "chat_id": 1, "user_id": 1, "task_id": "task-A"},
        ]
        _write_jsonl(env._drive_root / "logs" / "chat.jsonl", chats)
        _write_jsonl(env._drive_root / "logs" / "progress.jsonl", [])
        _write_jsonl(env._drive_root / "logs" / "events.jsonl", [])
        _write_jsonl(env._drive_root / "logs" / "tools.jsonl", [])
        _write_jsonl(env._drive_root / "logs" / "supervisor.jsonl", [])

        sections = _build_recent_sections(mem, env, task_id="task-B")
        chat_text = [s for s in sections if "Recent chat" in s]
        # Chat from task-A should still appear (no task filtering on chat)
        assert chat_text, "Chat should appear regardless of task_id"
        assert "hi from old task" in chat_text[0]


# ---------- Cache hit rate ----------

class TestCacheHitRate:
    """Cache hit rate computation from events.jsonl llm_round entries."""

    def test_cache_hit_rate_computation(self, tmp_path):
        """Cache hit rate = total cached_tokens / total prompt_tokens."""
        env = _make_env(tmp_path)
        events = []
        for i in range(10):
            events.append({
                "type": "llm_round",
                "ts": f"2026-03-20T12:{i:02d}:00Z",
                "usage": {"prompt_tokens": 1000, "cached_tokens": 700},
            })
        _write_jsonl(env._drive_root / "logs" / "events.jsonl", events)

        rate = _compute_cache_hit_rate(env)
        assert rate is not None
        assert abs(rate - 0.7) < 0.01

    def test_cache_hit_rate_no_data(self, tmp_path):
        """Should return None when no llm_round events."""
        env = _make_env(tmp_path)
        events = [
            {"type": "tool_call", "ts": "2026-03-20T12:00:00Z"},
            {"type": "error", "ts": "2026-03-20T12:01:00Z"},
        ]
        _write_jsonl(env._drive_root / "logs" / "events.jsonl", events)

        rate = _compute_cache_hit_rate(env)
        assert rate is None

    def test_cache_hit_rate_too_few_events(self, tmp_path):
        """Should return None when fewer than 5 llm_round events."""
        env = _make_env(tmp_path)
        events = [
            {"type": "llm_round", "ts": "2026-03-20T12:00:00Z",
             "usage": {"prompt_tokens": 1000, "cached_tokens": 500}},
        ]
        _write_jsonl(env._drive_root / "logs" / "events.jsonl", events)

        rate = _compute_cache_hit_rate(env)
        assert rate is None

    def test_cache_hit_rate_no_file(self, tmp_path):
        """Should return None when events.jsonl doesn't exist."""
        env = _make_env(tmp_path)
        rate = _compute_cache_hit_rate(env)
        assert rate is None

    def test_cache_hit_rate_low_warning(self, tmp_path, monkeypatch):
        """Health invariant should warn when cache rate < 30%."""
        import supervisor.state as supervisor_state
        monkeypatch.setattr(supervisor_state, "per_task_cost_summary", lambda n=5: [])

        env = _make_env(tmp_path)
        # Set up minimal required files
        (env._repo_root / "VERSION").write_text("1.0.0", encoding="utf-8")
        (env._repo_root / "pyproject.toml").write_text(
            "[project]\nname='x'\nversion='1.0.0'\n", encoding="utf-8")
        (env._drive_root / "state" / "state.json").write_text(
            json.dumps({"budget_drift_pct": 0, "spent_usd": 0, "openrouter_total_usd": 0}),
            encoding="utf-8")
        import time, os
        identity_path = env._drive_root / "memory" / "identity.md"
        identity_path.write_text("identity", encoding="utf-8")

        # Low cache rate events (10% hit rate)
        events = []
        for i in range(10):
            events.append({
                "type": "llm_round",
                "ts": f"2026-03-20T12:{i:02d}:00Z",
                "usage": {"prompt_tokens": 1000, "cached_tokens": 100},
            })
        _write_jsonl(env._drive_root / "logs" / "events.jsonl", events)

        result = _build_health_invariants(env)
        assert "LOW CACHE HIT RATE" in result
        assert "10%" in result


# ---------- System message provenance ----------

class TestSystemMessageProvenance:
    """System messages should have distinct formatting in chat output."""

    def _mem(self) -> Memory:
        return Memory(drive_root=pathlib.Path(tempfile.mkdtemp()))

    def test_summarize_chat_system_prefix(self):
        """System messages should show 📋 prefix in summarized chat."""
        mem = self._mem()
        entries = [
            _chat_entry("in", "hello from user", "2026-03-20T12:00:00Z"),
            _chat_entry("system", "Restart completed.", "2026-03-20T12:01:00Z",
                        entry_type="restart_ack"),
            _chat_entry("out", "understood", "2026-03-20T12:02:00Z"),
        ]

        result = mem.summarize_chat(entries)
        lines = result.strip().split("\n")

        assert len(lines) == 3
        assert "←" in lines[0]  # incoming user message
        assert "📋" in lines[1]  # system message
        assert "[restart_ack]" in lines[1]  # type annotation
        assert "→" in lines[2]  # outgoing agent message

    def test_system_messages_in_chat_history(self, tmp_path):
        """System messages should show 📋 prefix in chat_history() output."""
        drive = tmp_path / "drive"
        (drive / "logs").mkdir(parents=True, exist_ok=True)
        (drive / "memory").mkdir(parents=True, exist_ok=True)
        mem = Memory(drive_root=drive)

        entries = [
            _chat_entry("in", "user msg", "2026-03-20T12:00:00Z"),
            _chat_entry("system", "Health check passed", "2026-03-20T12:01:00Z",
                        entry_type="health_check"),
            _chat_entry("out", "agent reply", "2026-03-20T12:02:00Z"),
        ]
        _write_jsonl(drive / "logs" / "chat.jsonl", entries)

        result = mem.chat_history(count=10)
        assert "📋" in result
        assert "[health_check]" in result

    def test_system_direction_does_not_break_existing(self):
        """Old entries with direction=in/out should still work."""
        mem = self._mem()
        entries = [
            _chat_entry("in", "old user msg"),
            _chat_entry("out", "old agent reply"),
        ]
        result = mem.summarize_chat(entries)
        assert "←" in result
        assert "→" in result
        assert "📋" not in result  # no system messages here


def test_context_policy_section_mentions_copilot_round_phases():
    from ouroboros.context import _build_copilot_round_policy_section

    policy = _build_copilot_round_policy_section()
    assert "Copilot Round Policy" in policy
    assert "30 раундов" in policy
    assert "Раунды 1–10" in policy
    assert "Раунды 11–20" in policy
    assert "Раунды 21–30" in policy
