"""Tests for context optimizations ported from Ouroboros Desktop v4.5.0.

Covers:
- Shared startup with intact recent dialogue
- Cache hit rate computation and health invariant
- System message provenance in chat formatting
"""

import json
import pathlib
import tempfile

import pytest

from ouroboros.context import build_llm_messages, _compute_cache_hit_rate, _build_health_invariants
from ouroboros.memory import Memory


# ---------- helpers ----------

class _EnvStub:
    def __init__(self, repo_root: pathlib.Path, drive_root: pathlib.Path):
        self.repo_dir = repo_root
        self.drive_root = drive_root

    def repo_path(self, rel: str) -> pathlib.Path:
        return self.repo_dir / rel

    def drive_path(self, rel: str) -> pathlib.Path:
        return self.drive_root / rel


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


# ---------- Startup context ----------

@pytest.mark.parametrize("task_type", ["task", "review", "scheduled", "evolution"])
def test_same_startup_with_full_memory_and_chat(tmp_path, task_type):
    env = _make_env(tmp_path)
    mem = _make_memory(env.drive_root)
    mem.ensure_files()
    identity = "identity-start " + "identity " * 10000 + "identity-end"
    scratchpad = "scratchpad-start " + "scratchpad " * 10000 + "scratchpad-end"
    (env.drive_root / "memory/identity.md").write_text(identity, encoding="utf-8")
    (env.drive_root / "memory/scratchpad.md").write_text(scratchpad, encoding="utf-8")
    for rel in ("README.md", "prompts/ARCHITECTURE.md", "prompts/CHECKLISTS.md", "prompts/TELEGRAM_CONTOUR.md"):
        p = env.repo_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("OPTIONAL_REPOSITORY_REFERENCE", encoding="utf-8")
    _write_jsonl(env.drive_root / "logs/tools.jsonl", [{"text": "OLD_TOOL_PAYLOAD"}])
    _write_jsonl(env.drive_root / "state/state.json", [{"note": "UNRELATED_RUNTIME_STATE"}])
    older = [{"direction": "in", "text": "older-message", "chat_id": 42}]
    recent = [
        {"role": "in" if i % 2 == 0 else "out", "text": f"row-{i}: " + "данные " * 200,
         "chat_id": 42}
        for i in range(31)
    ]
    legacy = {"direction": "outgoing", "text": "legacy answer"}
    foreign = [{"role": "in", "text": "foreign-chat", "chat_id": 7}] * 101
    _write_jsonl(env.drive_root / "logs/chat.jsonl", older + recent + [legacy] + foreign)

    messages, estimate = build_llm_messages(env, mem, {"text": "Current SSH request",
                                                      "type": task_type, "chat_id": 42})
    static, memory, dynamic = [block["text"] for block in messages[0]["content"]]
    assert identity in memory and scratchpad in memory
    assert "OPTIONAL_REPOSITORY_REFERENCE" not in static
    assert "OLD_TOOL_PAYLOAD" not in dynamic and "UNRELATED_RUNTIME_STATE" not in dynamic
    assert "foreign-chat" not in dynamic and "older-message" not in dynamic
    for i, row in enumerate(recent):
        marker = "←" if i % 2 == 0 else "→"
        assert f"{marker}  {row['text']}" in dynamic
    assert "→  legacy answer" in dynamic
    assert "chat_history" in dynamic
    assert messages[1] == {"role": "user", "content": "Current SSH request"}
    assert estimate["estimated_input_text_tokens"] > estimate["startup_target_tokens"]


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
        _write_jsonl(env.drive_root / "logs" / "events.jsonl", events)

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
        _write_jsonl(env.drive_root / "logs" / "events.jsonl", events)

        rate = _compute_cache_hit_rate(env)
        assert rate is None

    def test_cache_hit_rate_too_few_events(self, tmp_path):
        """Should return None when fewer than 5 llm_round events."""
        env = _make_env(tmp_path)
        events = [
            {"type": "llm_round", "ts": "2026-03-20T12:00:00Z",
             "usage": {"prompt_tokens": 1000, "cached_tokens": 500}},
        ]
        _write_jsonl(env.drive_root / "logs" / "events.jsonl", events)

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
        (env.repo_dir / "VERSION").write_text("1.0.0", encoding="utf-8")
        (env.repo_dir / "pyproject.toml").write_text(
            "[project]\nname='x'\nversion='1.0.0'\n", encoding="utf-8")
        (env.drive_root / "state" / "state.json").write_text(
            json.dumps({"budget_drift_pct": 0, "spent_usd": 0, "openrouter_total_usd": 0}),
            encoding="utf-8")
        import time, os
        identity_path = env.drive_root / "memory" / "identity.md"
        identity_path.write_text("identity", encoding="utf-8")

        # Low cache rate events (10% hit rate)
        events = []
        for i in range(10):
            events.append({
                "type": "llm_round",
                "ts": f"2026-03-20T12:{i:02d}:00Z",
                "usage": {"prompt_tokens": 1000, "cached_tokens": 100},
            })
        _write_jsonl(env.drive_root / "logs" / "events.jsonl", events)

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

