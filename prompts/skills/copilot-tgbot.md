# Skill: copilot-telegram-bot

## Project Overview

**Repo:** https://github.com/gr1ng0333/copilot-telegram-bot  
**Local clone:** `/opt/repos/copilot-telegram-bot/`  
**Goal:** Free Claude (Sonnet 4.6 / Opus 4.6 / Haiku 4.5) chat via GitHub Copilot billing trick  
**Stack:** Python 3.10+, python-telegram-bot v20+, SQLite, FastAPI (web UI)

---

## Deployed Bot

**Bot:** [@copilot_ai_claude_bot](https://t.me/copilot_ai_claude_bot)  
**Server:** `402213.vm.spacecore.network` (DE, Spacecore) — сервер Андрея  
**IP:** `94.156.122.181`  
**Working dir:** `/opt/repos/copilot-telegram-bot/` ← единственная директория, `/opt/copilot-tgbot/` удалена  
**Env file:** `/etc/copilot-tgbot.env`  
**Systemd unit:** `/etc/systemd/system/copilot-tgbot.service`  
**Data dir:** `/opt/copilot-tgbot-data/`

### Start / restart / logs
```bash
systemctl restart copilot-tgbot
systemctl status copilot-tgbot
journalctl -u copilot-tgbot -f
```

### Deploy after code changes
```bash
cd /opt/repos/copilot-telegram-bot && git pull origin main && systemctl restart copilot-tgbot
```

> **Note:** Veles тоже живёт на этом сервере (`/opt/veles/`). Деплоить можно
> напрямую через `run_shell` — SSH наружу не нужен.

---

## File Structure

```
bot.py               — Telegram handlers, commands, message routing
copilot_client.py    — HTTP client to Copilot API, streaming SSE, model routing
copilot_accounts.py  — Token exchange, multi-account rotation, cooldown logic  ⚠️ FRAGILE
bot_tools.py         — Tool schemas + executor (web_search, fetch_url, create_file, run_code)
storage.py           — SQLite persistence (history, threads, settings)
web.py               — FastAPI + SSE web UI, shared SQLite with bot
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

## Economy Mode (`/economy`)

Toggle that makes N turns go as `agent`-initiator after first `user` premium turn:
- `storage.get/set_eco_mode(chat_id)` — enabled flag
- `storage.get/set_eco_interaction_id(chat_id, value)` — saved interaction ID
- `copilot_client.chat()` accepts `force_initiator` + `existing_interaction_id`
- After each eco-mode response, `interaction_id` is saved and reused next turn

---

## Tool Usage (bot_tools.py)

Claude can call tools inline during the conversation:
- `web_search(query)` — search via DuckDuckGo / Serper
- `fetch_url(url)` — download and return page text
- `create_file(filename, content)` — creates file, bot sends as document
- `run_code(language, code)` — sandboxed code execution

Tool schemas passed to Copilot API → `tool_use` responses parsed → executed → `tool_result` returned.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `COPILOT_TOKENS` | Yes | Comma-separated GitHub PATs (`ghu_xxx,ghu_yyy`) |
| `ALLOWED_USERS` | No | Comma-separated Telegram user IDs — **must be set** for access control (e.g. `7891813284`) |
| `DEFAULT_MODEL` | No | `sonnet` / `opus` / `haiku` (default: `sonnet`) |
| `SYSTEM_PROMPT` | No | Global fallback system prompt |
| `DATA_DIR` | No | SQLite DB directory (default: `/opt/copilot-tgbot-data`) |
| `MAX_HISTORY` | No | Messages kept in context (default: 40) |
| `STREAM` | No | Streaming responses (default: `true`) |
| `URL_FETCH` | No | Auto-fetch URLs in messages (default: `true`) |
| `URL_FETCH_MAX` | No | Max URLs per message (default: 2) |
| `URL_FETCH_CHARS` | No | Max chars per fetched URL (default: 4000) |
| `TOOLS_ENABLED` | No | Enable tool usage (web_search etc.) (default: `false`) |

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
chat_settings (chat_id, model, sys_prompt, eco_mode, eco_interaction_id, updated_at)
```

---

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/sonnet` | Switch to Claude Sonnet 4.6 (persisted) |
| `/opus` | Switch to Claude Opus 4.6 (persisted) |
| `/haiku` | Switch to Claude Haiku 4.5 (persisted) |
| `/model` | Inline keyboard model switcher |
| `/system [text]` | Show / set / reset per-chat system prompt |
| `/new` | Start new thread (old saved) |
| `/clear` | Clear current thread messages |
| `/status` | Current model, economy mode, thread info (msgs, tokens, date) |
| `/threads` | Inline keyboard thread switcher with per-thread info card |
| `/export` | Download thread as Markdown file |
| `/economy` | Toggle economy mode (1 premium turn → N free turns) |

**Removed:** `/history` — merged into `/status`

---

## Message Handlers

- `handle_message` — plain text; extracts URLs, fetches pages, tools loop
- `handle_photo` — photos + image documents → base64 → multimodal message
- `handle_document` — text files (30+ extensions + PDF + DOCX, MIME + extension detection, 256 KB max)
- `handle_callback` — inline keyboard callbacks (model switch, thread switch/info/delete)

---

## Document Support

| Format | Parser | Notes |
|--------|--------|-------|
| Text files (.txt, .py, .md, .json, etc.) | plain read | 30+ extensions |
| PDF | `pdfminer.six` | text layer only; warns if scanned |
| DOCX / DOC | `python-docx` | paragraphs extracted |

---

## Streaming Flow

1. `copilot_client.chat_stream()` — generator yielding text chunks via SSE
2. Bot sends placeholder message, edits it every `STREAM_EDIT_EVERY_N_CHARS=60` chars
3. On finish: final edit with full `_md_to_html()` formatted response
4. On any error: fallback to non-streaming `copilot_client.chat()`

**Known routing issue:** when `TOOLS_ENABLED=true`, plain text messages go through
`chat_with_tools()` which doesn't support streaming. Streaming only works for
plain messages when tools are disabled.

---

## UI Style Rules

- **No emoji in interface** — no ✅ ❌ 🔄 📋 etc. in buttons, status messages, or menus
- Use plain text labels: `Switch to this thread`, `Delete`, `Cancel`, `[active]`, `Error:`
- Minimize decorative formatting — this is a product, not a demo

---

## /threads Flow

- `/threads` → list with each thread as one button `Thread name — N msgs, ~X tokens`
- Click thread → info card: name, created date, msg count, context tokens
- Info card buttons: `Switch to this thread` (if not active) + `Delete` + `Back`
- Delete → confirm dialog → `Cancel` returns to thread card (not generic "Cancelled")

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

## Current State (as of 2026-04-07)

| Feature | Status |
|---------|--------|
| SQLite persistence | ✅ |
| Streaming | ✅ (plain messages; tools_enabled=false) |
| Streaming with tools | ✅ (fixed 2026-04-07: tool calls sync, final reply streamed) |
| Vision (photos + image docs) | ✅ |
| Text file documents | ✅ |
| PDF documents | ✅ (pdfminer.six) |
| DOCX documents | ✅ (python-docx) |
| URL auto-fetch | ✅ |
| Thread auto-naming | ✅ |
| `/export` (Markdown) | ✅ |
| Per-chat model + system prompt | ✅ |
| Web UI (FastAPI + SSE) | ✅ |
| Inline keyboards (model/threads) | ✅ |
| Economy mode | ✅ |
| Tool usage (web_search, fetch_url) | ✅ |
| Access control (ALLOWED_USERS) | ✅ (set in env, not code) |
| /status command | ✅ (replaces /history) |
| No emoji in UI | ✅ |
| Single working directory | ✅ (/opt/repos/copilot-telegram-bot/) |

---

## What TO Touch

- `bot.py` — handlers, UX, commands, formatting
- `storage.py` — SQLite layer (new features, schema migrations)
- `copilot_client.py` — model routing, streaming improvements
- `web.py` — web interface features
- `bot_tools.py` — tool definitions and executors

## What NOT TO Touch

- `copilot_accounts.py` — billing trick is fragile. Only with Андрей's explicit approval.

---

## Good Commit Criteria

1. Bot restarts cleanly without data loss
2. History survives restart
3. No broken streaming / billing trick
4. One working feature per commit
5. Deploy to `/opt/repos/copilot-telegram-bot/`, not a separate copy
