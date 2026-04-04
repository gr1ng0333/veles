# Skill: copilot-telegram-bot

## Project Overview

**Repo:** https://github.com/gr1ng0333/copilot-telegram-bot  
**Local clone:** `/opt/repos/copilot-telegram-bot/`  
**Goal:** Free Claude (Sonnet 4.6 / Opus 4.6 / Haiku 4.5) chat via GitHub Copilot billing trick  
**Stack:** Python 3.10+, python-telegram-bot v20+, SQLite, FastAPI (web UI)

---

## File Structure

```
bot.py               — Telegram handlers, commands, message routing (979 lines)
copilot_client.py    — HTTP client to Copilot API, streaming SSE, model routing
copilot_accounts.py  — Token exchange, multi-account rotation, cooldown logic  ⚠️ FRAGILE
storage.py           — SQLite persistence (history, threads, settings) (381 lines)
web.py               — FastAPI + SSE web UI, shared SQLite with bot (716 lines)
requirements.txt     — Dependencies
.env.example         — Env vars template
```

---

## The Billing Trick (core mechanic — do not break)

Every request appends a trailing system message that causes Copilot to use
`X-Initiator: agent` instead of `X-Initiator: user`.

- `user` requests = premium quota consumed  
- `agent` requests = **free**, no quota

This is implemented in `copilot_client.py`. The trick works as long as:
1. Trailing system message is always the last item in the messages array
2. `copilot_accounts.py` correctly exchanges PAT → short-lived Copilot token
3. Request format matches what Copilot API expects

**DO NOT edit `copilot_accounts.py` without Андрей's explicit approval.**  
Any change here risks breaking free billing for all accounts.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `COPILOT_TOKENS` | Yes | Comma-separated GitHub PATs (`ghu_xxx,ghu_yyy`) |
| `ALLOWED_USERS` | No | Comma-separated Telegram user IDs (empty = open) |
| `DEFAULT_MODEL` | No | `sonnet` / `opus` / `haiku` (default: `sonnet`) |
| `SYSTEM_PROMPT` | No | Global fallback system prompt |
| `DATA_DIR` | No | SQLite DB directory (default: `/opt/copilot-tgbot-data`) |
| `MAX_HISTORY` | No | Messages kept in context (default: 40) |
| `STREAM` | No | Streaming responses (default: `true`) |
| `URL_FETCH` | No | Auto-fetch URLs in messages (default: `true`) |
| `URL_FETCH_MAX` | No | Max URLs per message (default: 2) |
| `URL_FETCH_CHARS` | No | Max chars per fetched URL (default: 4000) |

**Web UI only:**

| Variable | Default | Description |
|----------|---------|-------------|
| `WEB_SECRET` | `` (open) | Access token — pass as `?token=...` |
| `WEB_HOST` | `0.0.0.0` | Bind address |
| `WEB_PORT` | `8080` | Bind port |

---

## SQLite Schema

DB path: `$DATA_DIR/chat_history.db`

```sql
threads (chat_id, thread_id, created_at, name)
active_thread (chat_id, thread_id)          -- one active thread per chat
messages (id, chat_id, thread_id, role, content, created_at)
chat_settings (chat_id, model, sys_prompt, updated_at)
```

**Key functions in `storage.py`:**
- `load_history(chat_id)` — last MAX_HISTORY messages for active thread
- `append_messages(chat_id, messages)` — add user+assistant turn
- `new_thread(chat_id)` — create fresh thread, old preserved
- `clear_thread(chat_id)` — delete messages in current thread
- `get_chat_setting(chat_id, key)` / `set_chat_setting(chat_id, key, value)` — persist model/prompt
- `list_threads(chat_id, limit)` — last N threads with names
- `switch_thread(chat_id, thread_id)` — change active thread
- `set_thread_name(chat_id, thread_id, name)` — save LLM-generated name
- `load_full_thread(chat_id)` — all messages (for /export)

---

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/sonnet` | Switch to Claude Sonnet 4.6 (persisted) |
| `/opus` | Switch to Claude Opus 4.6 (persisted) |
| `/haiku` | Switch to Claude Haiku 4.5 (persisted) |
| `/model` | Show current model |
| `/system [text]` | Show / set / reset per-chat system prompt |
| `/new` | Start new thread (old saved) |
| `/clear` | Clear current thread messages |
| `/history` | Show thread stats |
| `/threads [N\|ID]` | List threads or switch by N/ID |
| `/export` | Download thread as Markdown file |

---

## Message Handlers

- `handle_message` — plain text; extracts URLs if `URL_FETCH_ENABLED`, fetches pages, passes to LLM
- `handle_photo` — photos + image documents → base64 → multimodal message
- `handle_document` — text files (30+ extensions, MIME + extension detection, 256 KB max)

---

## Streaming Flow

1. `copilot_client.chat_stream()` — generator yielding text chunks via SSE
2. Bot sends placeholder message, edits it every `STREAM_EDIT_EVERY_N_CHARS=60` chars
3. On finish: final edit with full `_md_to_html()` formatted response
4. On any error: fallback to non-streaming `copilot_client.chat()`

---

## Thread Auto-Naming

After first exchange in a new thread, a background thread calls Copilot to generate
a short name (3–5 words). Name persisted via `storage.set_thread_name()`.
`/threads` shows names instead of raw UUIDs.

---

## Web UI (web.py)

**Run:** `python web.py` or `uvicorn web:app --host 0.0.0.0 --port 8080`  
**Features:** SSE streaming, thread sidebar, model selector, shared SQLite with bot

---

## Models

```python
MODELS = {
    "sonnet": "claude-sonnet-4.6",
    "opus":   "claude-opus-4.6",
    "haiku":  "claude-haiku-4.5",
}
```

`reasoning_effort=high` — **only for opus**. Sonnet and haiku use defaults.

---

## Current State (as of 2026-04-04)

| Feature | Status |
|---------|--------|
| SQLite persistence | ✅ |
| Streaming | ✅ |
| Vision (photos + image docs) | ✅ |
| Text file documents | ✅ |
| URL auto-fetch | ✅ |
| Thread auto-naming | ✅ |
| `/export` (Markdown) | ✅ |
| Per-chat model + system prompt | ✅ |
| Web UI (FastAPI + SSE) | ✅ |
| Deploy on server | ❌ not yet |

---

## What TO Touch

- `bot.py` — handlers, UX, commands, formatting
- `storage.py` — SQLite layer (new features, schema migrations)
- `copilot_client.py` — model routing, streaming improvements
- `web.py` — web interface features

## What NOT TO Touch

- `copilot_accounts.py` — billing trick is fragile. Only with Андрей's explicit approval.

---

## Good Commit Criteria

1. Bot restarts cleanly without data loss
2. History survives restart
3. No broken streaming / billing trick
4. One working feature per commit

---

## Deploy (when ready)

- Clone to VPS at e.g. `/opt/repos/copilot-telegram-bot/`
- Copy `.env.example` → `.env`, fill tokens
- `pip install -r requirements.txt`
- Run as systemd service (similar to ouro-fitness-bot pattern)
- Data in `/opt/copilot-tgbot-data/`
