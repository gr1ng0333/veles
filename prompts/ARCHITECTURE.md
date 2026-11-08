# Veles Architecture

Veles is a self-evolving AI agent running on a VPS (Amsterdam, screen session).
Incoming message → supervisor dispatches task → agent runs LLM loop with tools →
response sent back via Telegram. Background consciousness, task scheduling,
code evolution, multi-model review — all autonomous.

---

## Process Model

```
┌─ Supervisor process (colab_launcher.py) ─────────────────────┐
│  Telegram polling → task dispatch → event handling            │
│                                                               │
│  ├── Worker process (fork via multiprocessing)                │
│  │    handles: queued tasks (review, evolution, scheduled)    │
│  │                                                            │
│  ├── Chat agent thread                                        │
│  │    handles: direct owner messages (inline, no fork)        │
│  │                                                            │
│  ├── Consciousness daemon thread                              │
│  │    background thinking between tasks (consciousness.py)    │
│  │                                                            │
│  ├── Reflection daemon thread                                 │
│  │    post-task error analysis (reflection.py)                │
│  │                                                            │
│  └── Consolidator daemon thread                               │
│       dialogue summarization every ~100 msgs (consolidator.py)│
└───────────────────────────────────────────────────────────────┘
```

**Coordination:** supervisor/queue.py (task queue with priority), supervisor/events.py
(event_queue from workers), shared Drive state files (state.json, chat.jsonl).

---

## LLM Transport Layer

Three transports, routed by model prefix in `llm.py`:

| Prefix | Transport | File | Details |
|--------|-----------|------|---------|
| `codex/*` | Codex OAuth | codex_proxy.py | Multi-account rotation via codex_proxy_accounts.py |
| `copilot/*` | GitHub Copilot | copilot_proxy.py | PAT-based, X-Initiator billing, copilot_proxy_accounts.py |
| *(none)* | OpenRouter API | llm.py | Paid fallback, standard Chat Completions |

`llm.py` is the sole entry point (`LLMClient.chat()`). It strips transport prefix,
selects proxy, and returns unified `(message, usage)` tuple.

---

## Key Modules

### Core (ouroboros/)

Agent orchestration (`agent.py`), LLM tool loop (`loop.py`, `loop_runtime.py`),
3-block context assembly (`context.py`), LLM client (`llm.py`),
memory management (`memory.py`), background consciousness (`consciousness.py`),
dialogue consolidation (`consolidator.py`), post-task reflection (`reflection.py`),
safety pre-check (`safety.py`), structured plans (`plans.py`),
stagnation detection (`antistagnation.py`), model modes (`model_modes.py`).

### Remote / SSH (`ouroboros/tools/`)

Server registry and auth bootstrap (`ssh_targets.py`), SSH key lifecycle (`ssh_key_management.py`), remote filesystem operations (`remote_filesystem.py`), service control and health snapshots (`remote_service.py`), and capability overview (`remote_operator_overview.py`). This is the native backend contour for other servers. When the task moves from shell access to panel UI (for example 3x-ui or VMmanager), the browser toolchain becomes the frontend half of the same remote-operations stack.

### Transport (ouroboros/)

Codex proxy (`codex_proxy.py`, `codex_proxy_accounts.py`, `codex_proxy_format.py`,
`codex_recovery.py`), Copilot proxy (`copilot_proxy.py`, `copilot_proxy_accounts.py`),
model pricing (`pricing.py`).

### Tools (ouroboros/tools/)

Auto-register via `registry.py`. Each module exports `get_tools() -> List[ToolEntry]`.
31 core (always loaded) + extended (on-demand via `list_available_tools` / `enable_tools`).
Every `execute()` call passes through Safety Agent pre-check.

### Supervisor (supervisor/)

Telegram client (`telegram.py`), task queue (`queue.py`), event dispatch (`events.py`),
worker lifecycle (`workers.py`), git operations (`git_ops.py`),
persistent state (`state.py`), restart flow (`restart_flow.py`),
voice transcription (`audio_stt.py`).

---

## Data Layout

```
/opt/veles/                        — code (git clone, branch veles)
/opt/veles/.env                    — configuration (secrets, model config)
/opt/veles/prompts/                — SYSTEM.md, BIBLE.md, CONSCIOUSNESS.md,
                                     CHECKLISTS.md, ARCHITECTURE.md

/opt/veles-data/state/             — runtime state JSON files (state.json, etc.)
/opt/veles-data/logs/              — events.jsonl, chat.jsonl, progress.jsonl,
                                     task_reflections.jsonl, supervisor.jsonl
/opt/veles-data/memory/            — identity.md, scratchpad.md, dialogue_blocks.json,
                                     knowledge/*.md
/opt/veles-data/plans/             — structured plan JSON files
/opt/veles-data/task_results/      — task result files
/opt/veles-data/archive/rescue/    — rescue snapshots (uncommitted changes before reset)
```

---

## Context Assembly

Ordinary task types use the same three blocks.

- Block 0 contains SYSTEM.md, BIBLE.md, the language rule and the skill catalog.
- Block 1 contains full identity, scratchpad and the current plan, when present.
- Block 2 contains runtime facts, health invariants and recent chat. The last 32
  records are included in full. The older part of the tail uses a text budget
  and includes up to 100 records in total.

The model can discover and enable more tools and load relevant skills at any round.
The task checkpoint preserves the tool schemas and the loaded skill text. Reference
docs, long-term memory and task logs are read on demand. Focused worker allowlists
still define the tools available to those workers.

---

## Memory Subsystems

| Store | File | Purpose |
|-------|------|---------|
| Recent chat | chat.jsonl | Recent chat with explicit speaker labels |
| Dialogue history | dialogue_blocks.json | Auto-consolidated every ~100 msgs (consolidator.py) |
| Execution reflections | task_reflections.jsonl | Post-task error analysis, queried on demand |
| Pattern register | knowledge/patterns.md | Recurring error patterns extracted from reflections |
| Scratchpad | scratchpad.md | Working notes, persists across sessions |
| Identity | identity.md | Self-identity manifest (grows, not rewritten — P7) |
| Knowledge base | knowledge/*.md | Long-term structured knowledge with auto-index |
