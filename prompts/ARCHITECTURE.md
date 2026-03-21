# Veles Architecture

Veles is a self-evolving AI agent running on a VPS (Amsterdam, screen session).
It operates as a Telegram-driven assistant: incoming message → supervisor dispatches
task → agent runs LLM loop with tools → response sent back via Telegram.
The system supports background consciousness, task scheduling, code evolution,
multi-model review, and structured research — all autonomously.

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
| `codex/*` | Codex OAuth | codex_proxy.py | 6 accounts, rotation via codex_proxy_accounts.py |
| `copilot/*` | GitHub Copilot | copilot_proxy.py | PAT-based, X-Initiator billing, copilot_proxy_accounts.py |
| *(none)* | OpenRouter API | llm.py | Paid fallback, standard Chat Completions |

`llm.py` is the sole entry point (`LLMClient.chat()`). It strips transport prefix,
selects proxy, and returns unified `(message, usage)` tuple.

---

## Key Modules

### Core (ouroboros/)

| Module | Purpose |
|--------|---------|
| agent.py | Task orchestrator — delegates to loop, tools, LLM, memory, context |
| loop.py | LLM tool loop: messages → tool calls → execute → repeat. Pricing, parallel dispatch |
| loop_runtime.py | Runtime for run_llm_loop: token guards, style selection, round management |
| context.py | 3-block prompt assembly (static/semi-stable/dynamic), 200K soft cap |
| llm.py | LLM API client (OpenRouter), model selection, usage tracking, pricing fetch |
| memory.py | Scratchpad, identity, chat history (load/save/append JSONL) |
| consciousness.py | Background thinking daemon: introspection, proactive messaging, self-scheduling |
| consolidator.py | Block-wise dialogue summarizer with era compression |
| reflection.py | Post-task error analysis → task_reflections.jsonl |
| safety.py | Dual-layer LLM security: fast + deep assessment before dangerous tools |
| review.py | Code collection and complexity metrics for review tasks |
| plans.py | Structured multi-step plan management (create/approve/execute/complete) |
| antistagnation.py | Stagnation detection: round caps, grace periods, extension limits |
| circuit_breaker.py | Three-state circuit breaker for unreliable external services |
| model_modes.py | Switchable model modes (model + label + behavior) with persistence |
| owner_inject.py | Per-task owner message mailbox (append-only JSONL with dedup) |
| artifacts.py | Artifact outbox/inbox on Drive with hashing and metadata tracking |
| research_eval.py | Benchmark harness for search/research tool evaluation |
| search_utils.py | Query shortening, stop-word removal, query expansion helpers |
| utils.py | Shared zero-dep helpers: timestamps, file I/O, git info, token estimation |

### Transport (ouroboros/)

| Module | Purpose |
|--------|---------|
| codex_proxy.py | Codex endpoint via OAuth tokens over urllib |
| codex_proxy_accounts.py | Multi-account OAuth refresh, rotation, rate-limit cooldowns |
| codex_proxy_format.py | Message format conversion for Codex API |
| codex_recovery.py | Extract tool calls embedded as plain text in Codex responses |
| copilot_proxy.py | GitHub Copilot API via PAT tokens, Chat Completions format |
| copilot_proxy_accounts.py | PAT→API token exchange, multi-account rotation, quota tracking |
| accounts_status_format.py | Account status label/percentage formatting for dashboards |
| pricing.py | Model pricing — single source of truth for cost estimation |

### Tools (ouroboros/tools/) — 87 registered tools

| Module | Tools | Purpose |
|--------|-------|---------|
| core.py | repo_read/list, drive_read/list/write, codebase_digest, send_* | File I/O, artifacts, messaging |
| git.py | repo_write_commit, repo_commit_push, git_status, git_diff | Git operations with shrink-guard |
| shell.py | run_shell | Shell command execution with safety logging |
| control.py | restart, schedule, cancel, chat_history, switch_model, etc. | Agent control and coordination |
| knowledge.py | knowledge_read/write/list | Persistent topic-based knowledge base |
| plans.py | plan_create/approve/reject/step_done/update/complete/status | Plan management tools |
| search.py | web_search, research_run, deep_research | Web search and research orchestration |
| browser*.py | browse_page, browser_action, browser_run_actions, etc. | Playwright browser automation |
| vision.py | analyze_screenshot, solve_simple_captcha, vlm_query | VLM-based image analysis |
| review.py | multi_model_review | Multi-LLM consensus review |
| compact_context.py | compact_context | LLM-driven context compression |
| health.py | codebase_health, vps_health_check, monitor_snapshot, doctor | Self-assessment health checks |
| project_*.py | ~20 project management tools | Project bootstrap, deploy, GitHub, SSH operations |
| remote_*.py | remote_exec, remote_fs, remote_investigation, etc. | Remote server operations via SSH |
| tool_discovery.py | list_available_tools, enable_tools | Runtime tool discovery and activation |

### Supervisor (supervisor/)

| Module | Purpose |
|--------|---------|
| telegram.py | Telegram client: message splitting, Markdown→HTML, budget-aware sending |
| queue.py | Task queue: priority, timeouts, persistence, evolution/review scheduling |
| events.py | Event dispatcher: routes worker event-queue messages to handlers |
| workers.py | Multiprocessing worker lifecycle, health monitoring, direct chat |
| git_ops.py | Git operations: clone, checkout, reset, rescue snapshots, dep sync |
| state.py | Persistent Drive state: load/save, atomic writes, file locks |
| restart_flow.py | Agent-requested restart handling and owner notification |
| restart_advisor.py | LLM-driven restart recommendation (no/soft/hard/escalate) |
| restart_observability.py | Manual restart detection via PID handoff |
| codex_bootstrap.py | Pre-warm Codex OAuth accounts at startup |
| audio_stt.py | Voice/audio transcription via ffmpeg + Google Web Speech |

### Entry Points

| File | Purpose |
|------|---------|
| colab_launcher.py | Runtime launcher: dep install, supervisor startup loop |
| colab_bootstrap_shim.py | Immutable Colab shim: Drive mount, git clone, bootstrap |

---

## Tool Registry

Tools auto-register via `ouroboros/tools/registry.py`. Each module in `ouroboros/tools/`
exports `get_tools() -> List[ToolEntry]`. The registry auto-discovers modules via
`pkgutil.iter_modules`. 87 tools total, 31 core (always loaded) + extended (on-demand
via `list_available_tools` / `enable_tools`). Every `execute()` call passes through
the Safety Agent pre-check before handler invocation.

---

## Data Layout

```
/opt/veles/                        — code (git clone, branch veles)
/opt/veles/.env                    — configuration (secrets, model config)
/opt/veles/prompts/                — SYSTEM.md, BIBLE.md, CONSCIOUSNESS.md,
                                     SAFETY.md, CHECKLISTS.md, ARCHITECTURE.md

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

Three-block structure for optimal prompt caching:

**Block 0 — Static (cached 1h):**
Language rule → SYSTEM.md → BIBLE.md → ARCHITECTURE.md → SAFETY.md → CHECKLISTS.md
(+ README.md for evolution/review tasks only)

**Block 1 — Semi-stable (cached, ephemeral):**
Scratchpad → Identity → Active plan → Dialogue history (consolidated) →
Knowledge base index → Pattern register

**Block 2 — Dynamic (uncached):**
Drive state → Runtime context (git, budget, timestamps) → Health invariants →
Recent chat/progress/tools/events/supervisor → Execution reflections →
Review context (review tasks) → Language reminder

Soft cap: 200,000 tokens. Prunable: recent chat, progress, tools, events, supervisor.

---

## Memory Subsystems

| Store | File | Purpose |
|-------|------|---------|
| Recent chat | chat.jsonl | Last 100 messages, outgoing clipped except last 3 |
| Dialogue history | dialogue_blocks.json | Auto-consolidated every ~100 msgs (consolidator.py) |
| Execution reflections | task_reflections.jsonl | Last 10 in context, post-task error analysis |
| Pattern register | knowledge/patterns.md | Recurring error patterns extracted from reflections |
| Scratchpad | scratchpad.md | Working notes, persists across sessions |
| Identity | identity.md | Self-identity manifest |
| Knowledge base | knowledge/*.md | Long-term structured knowledge with auto-index |
