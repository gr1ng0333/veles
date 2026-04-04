# Skill: ouro-fitness-bot

## Project Overview

**Repo:** https://github.com/gr1ng0333/ouro-fitness-bot (приватный)  
**Status:** inactive — systemd service установлен, бот не запущен  
**Goal:** Персональный фитнес-ассистент в Telegram: логирование еды/тренировок/веса, программа калистеники, FatSecret для КБЖУ  
**Stack:** Python 3.10+, python-telegram-bot v20+, APScheduler (AsyncIOScheduler), FatSecret API

---

## Deploy State

| Параметр | Значение |
|----------|---------|
| Systemd service | `/etc/systemd/system/ouro-fitness-bot.service` |
| WorkingDirectory | `/opt/repos/ouro-fitness-bot` (**не склонирован**) |
| EnvironmentFile | `/etc/ouro-fitness-bot.env` |
| Service status | `inactive (dead)` |
| Venv | `/opt/repos/ouro-fitness-bot/.venv/bin/ouro-fitness-bot` |

**Чтобы запустить:**
```bash
cd /opt/repos && git clone https://github.com/gr1ng0333/ouro-fitness-bot
cd ouro-fitness-bot && python -m venv .venv && .venv/bin/pip install -e .
systemctl start ouro-fitness-bot
```

---

## Architecture

```
telegram_app.py      — Telegram bot entry point, BotRuntime dataclass
telegram_handlers.py — Command handlers (/fit, /ask, /start, etc.)
telegram_format.py   — Formatting utilities for Telegram messages
telegram_ui.py       — UI helpers (keyboards, buttons)
fitness_daemon.py    — Proactive messaging daemon (AsyncIOScheduler)
daemon_utils.py      — Quiet-hours, quiet-period checks
storage.py           — Persistent data layer
fatsecret.py         — FatSecret API client (food search + КБЖУ)
```

---

## Data Layout

Все данные в `/opt/veles-data/fitness/`:

```
profile.json            — пользовательский профиль + активная программа
state/daemon_state.json — состояние daemon (pending_field, last ticks)
logs/fitness.jsonl      — все логи (еда, тренировки, вес)
logs/chat.jsonl         — fitness-диалог (отдельно от основного chat.jsonl)
YYYY-Www.json           — недельные записи
```

**profile.json ключи:**
```json
{
  "active_program": {...},
  "training_days": [1, 3, 5],
  "has_pullup_bar": true,
  "weight_kg": 75.0,
  "height_cm": 180,
  "goal": "fat_loss"
}
```

---

## Key Modules

### fitness_daemon.py

Proactive messaging — пишет владельцу в 9:00 / 13:00 / 20:00 UTC+3.

- Quiet check: если основной агент Veles был активен <15 минут — отложить +20 минут
- После 3 откладываний — пропустить слот, ждать следующего
- При вопросе устанавливает `fitness_awaiting_reply=True` в state

**BotRuntime dataclass** — содержит поле `daemon`. Если используется `slots=True`, поле
нужно явно объявить в dataclass (иначе assignment error).

### storage.py

Tools для работы с data layer (используются как внутри daemon, так и через LLM tools):
- `fitness_log_meal(food, calories, protein, fat, carbs)` — запись еды
- `fitness_log_workout(type, duration, exercises)` — тренировка
- `fitness_log_weight(weight_kg)` — вес
- `fitness_summary(period)` — сводка (день/неделя)
- `fitness_profile_read()` — профиль
- `fitness_profile_write(**kwargs)` — обновить профиль + `active_program`

### fatsecret.py

FatSecret OAuth2 client_credentials:
- In-memory token cache со skew 30s
- `fatsecret_search(query)` — поиск продуктов
- `fatsecret_food(food_id)` — детальное КБЖУ

**Gotcha:** русский поиск возвращает пустой результат. Нужен RU→EN перевод для кириллических запросов перед вызовом API.

---

## Env Variables

| Variable | Source | Description |
|----------|--------|-------------|
| `BOT_TOKEN` | `/etc/ouro-fitness-bot.env` | Telegram bot token |
| `FATSECRET_CLIENT_ID` | env | FatSecret API key |
| `FATSECRET_CLIENT_SECRET` | env | FatSecret secret |
| `DATA_DIR` | env | Data directory (default: `/opt/veles-data/fitness`) |

---

## Routing (в основном Veles)

Fitness-контур был встроен в Veles, затем вынесен в отдельный бот.  
В основном Veles удалены: `fitness_consciousness.py`, fitness tools, FITNESS.md, routing flags.

**Isolation rules (актуальны для standalone бота):**
- `fitness_awaiting_reply` — устанавливать только когда сообщение заканчивается вопросом
- Флаг `fitness_awaiting_reply` — не трактовать как право перехватить любой текст
- Проверять `looks_like_fitness_reply(text)` перед маршрутизацией
- Fitness logs → `/opt/veles-data/fitness/logs/` (не в основной `/opt/veles-data/logs/`)

---

## Программа тренировок

Beginner калистеника: 3-дневный split A/B/C с:
- Regressions / progressions для каждого упражнения
- Recovery rules
- Адаптация под наличие/отсутствие турника

**Bootstrap flow:**
1. `active_program` отсутствует → daemon спрашивает: удобные дни + есть ли турник
2. Получает ответ → `fitness_profile_write(training_days=..., has_pullup_bar=...)` → генерирует `active_program`
3. Отправляет план

---

## Commands (Telegram)

| Command | Description |
|---------|-------------|
| `/start` | Онбординг |
| `/fit` | One-shot fitness message (next message → fitness daemon) |
| `/fit start` / `/fit stop` | Включить/выключить proactive daemon |
| `/ask <text>` | Прямой вопрос к fitness-ассистенту |

---

## Known Issues / Gotchas

1. **Repo not cloned** — `/opt/repos/ouro-fitness-bot` не существует, сервис не запустится
2. **FatSecret RU search empty** — нужен перевод перед API вызовом
3. **Daemon field in dataclass** — если BotRuntime использует `slots=True`, поле `daemon` должно быть явно объявлено
4. **Reply routing** — `fitness_awaiting_reply` ≠ "перехватить любой текст"
5. **Dual logging** — fitness input должен логироваться только в fitness chat.jsonl, не дублировать в основном

---

## Current State (as of 2026-04-04)

| Feature | Status |
|---------|--------|
| Telegram bot | ✅ реализован |
| FatSecret integration | ✅ |
| SQLite/JSON persistence | ✅ |
| Proactive daemon (scheduler) | ✅ |
| Calisthenics program generation | ✅ |
| Deploy on new server | ❌ не склонирован |
| Active (running) | ❌ inactive |

---

## To Resume Work

1. Склонировать репо на сервер
2. Установить зависимости
3. Проверить `/etc/ouro-fitness-bot.env` (токены актуальны?)
4. `systemctl start ouro-fitness-bot && journalctl -u ouro-fitness-bot -f`
