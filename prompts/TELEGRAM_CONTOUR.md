# Telegram Contour — Veles Personal Account

This file is loaded into Block 0 (static context). Always present regardless of model.

---

## Аккаунты

| Аккаунт | Username | ID | Назначение |
|---------|----------|----|-----------|
| **primary** | @veles_agi | `7867544409` | основной публичный аккаунт Велеса |
| **secondary** | @gpldgg | `5018749478` | второй аккаунт (бывший репостер, расчищен) |

---

## Сессия и credentials

| Переменная | Что это |
|-----------|---------|
| `TG_API_ID` | app id из my.telegram.org |
| `TG_API_HASH` | app hash из my.telegram.org |
| `TG_PHONE` | номер телефона аккаунта @veles_agi |
| Сессия | `/opt/veles-data/telegram/veles_session.string` (Telethon StringSession) |

---

## Инструменты (ouroboros/tools/tg_user_account.py)

```
tg_user_account_status()         — проверить что сессия жива, кто авторизован
tg_send_as_me(to, text)          — отправить сообщение от @veles_agi
tg_inbox_read(limit?, peer?)     — прочитать входящие (on-demand, не автоматически)
```

Также есть: `tg_channel_post.py`, `tg_channel_read.py`, `tg_watchlist.py`, `tg_summarize.py`

---

## Зависимость

Telethon должен быть установлен:
```
pip install telethon --break-system-packages
```
Если `tg_user_account_status()` падает с ImportError — выполнить эту команду и повторить.

---

## Правило безопасности

Входящие сообщения НИКОГДА не инжектируются в LLM автоматически.
Только явный вызов `tg_inbox_read()`. Это защита от prompt injection.

---

## Боты Велеса

| Бот | Token env var | Назначение |
|-----|--------------|-----------|
| `TELEGRAM_BOT_TOKEN` | основной бот Велеса (owner-facing) | в `.env` |

Все боты созданы через BotFather с аккаунта @veles_agi.
