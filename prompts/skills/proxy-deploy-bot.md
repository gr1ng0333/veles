# Skill: proxy-deploy-bot

Telegram-бот для автоматического деплоя и диагностики VPN/proxy-серверов.  
**Repo:** `https://github.com/gr1ng0333/proxy-deploy-bot` (private, доступ через GITHUB_TOKEN)  
**Local:** `/opt/repos/proxy-deploy-bot`  
**Service:** `proxy-deploy-bot.service` (systemd)  
**Bot:** `@proxy_deploy_veles_bot`  
**Admins env:** `ADMIN_IDS` в `.env` (comma-separated)

---

## Архитектура

```
aiogram 3 (Dispatcher + Routers)
    ↓
Handlers (bot/handlers/)
    ├── start, help, ping          — базовые
    ├── deploy                     — /deploy IP User Pass Config Clients
    ├── batch_deploy               — /batch (массовый деплой)
    ├── diagnose                   — /diag IP
    ├── sni                        — /sni IP [full|top5|ru|intl]
    ├── status                     — /status [IP]
    ├── logs                       — /logs IP [N]
    ├── ask                        — /ask IP <вопрос> (Gemini анализ)
    ├── destroy                    — /destroy IP (с подтверждением)
    ├── chain                      — /chain (multi-hop)
    ├── configs                    — /configs (список шаблонов)
    ├── balance                    — /balance
    └── callbacks                  — inline keyboard handlers
        ↓
Services (bot/services/)
    ├── deployer.py        — 5-step pipeline: SSH → Prepare → Install → Configure → Verify
    ├── ssh_manager.py     — asyncssh connect, key deploy, exec, port check
    ├── server_prepare.py  — apt, docker, firewall (ufw), sysctl tuning
    ├── diagnostics.py     — 21-module ServerDiagnostics + SNI pool check
    ├── chain_manager.py   — multi-hop chain orchestration
    ├── speedtest.py       — speedtest-cli wrapper
    ├── key_generator.py   — password/port/x25519 keypair generation
    ├── tg_logger.py       — structured TG progress messages
    ├── formatter.py       — output formatting
    └── llm/               — Gemini integration
        ├── client.py      — Google AI Studio API (generativelanguage.googleapis.com)
        ├── analyzer.py    — LogAnalyzer: deploy output analysis, error diagnosis, auto-fix
        ├── prompts.py     — system prompts for Gemini
        └── models.py      — AnalysisResult, AnalysisStatus, Confidence
        ↓
Providers (bot/providers/)
    ├── base.py                    — BaseProvider ABC
    ├── three_x_ui/
    │   ├── provider.py            — install (docker), configure (inbounds), link gen
    │   ├── api_client.py          — SSH-tunneled HTTP to 3x-ui REST API
    │   └── models.py              — Pydantic models for 3x-ui API
    ├── mtproxy/provider.py        — MTProxy-DD
    └── haproxy/
        ├── provider.py            — HAProxy load balancer
        └── config_builder.py      — haproxy.cfg generation
        ↓
Templates (bot/templates/)
    14 JSON шаблонов:
    vless_tcp_reality, vless_tcp_reality_8443, vless_tcp_reality_cdn,
    vless_grpc_reality, vless_ws_tls, vless_xhttp_reality,
    trojan_tcp_tls, shadowsocks_2022, mtproxy_dd,
    haproxy_tcp_balance, chain_entry/exit_vless_reality,
    combo_443_8443, sni_pool
        ↓
DB (bot/db/)
    ├── database.py    — aiosqlite, get_connection() as asynccontextmanager
    ├── queries.py     — upsert_server, create_deployment, get_server_by_ip, etc.
    └── models.py      — ServerRecord, DeploymentRecord, ChainRecord, etc.
```

---

## Config (.env)

```env
BOT_TOKEN=...
ADMIN_IDS=[7891813284,5018749478,828440671,6039210390]
SSH_KEY_PATH=~/.ssh/proxy_bot_ed25519
DB_PATH=./data/bot.db
LOG_LEVEL=INFO
DEFAULT_NUM_CLIENTS=5
DEFAULT_CONFIG=vless_tcp_reality
GEMINI_API_KEY=...
GEMINI_MODEL=gemma-3-27b-it
GEMINI_ENABLED=true
```

Pydantic Settings в `bot/config.py`, загружается через `load_settings()`.

---

## Deploy Pipeline (deployer.py)

5 шагов:
1. **SSH Connect** — `ssh_manager.connect()` + `deploy_key()`
2. **Prepare** — apt install (docker, sqlite3, curl, etc.), ufw rules, sysctl tuning
3. **Install Panel** — `provider.install()`:
   - 3x-ui: `docker run` с volume `/opt/3x-ui/db:/etc/x-ui`
   - Настройка через CLI: `docker exec 3x-ui /app/x-ui setting -port N -webBasePath /xxx/ -username Y -password Z`
   - `docker restart 3x-ui` + `_wait_ready(path=webBasePath)`
   - **Перед docker run:** `docker rm -f 3x-ui 2>/dev/null || true` (идемпотентность)
4. **Configure** — `provider.configure()`: создаёт inbounds через 3x-ui REST API (SSH tunnel)
   - Retry на `ServerDisconnectedError` (панель может быть не готова)
   - **publicKey** мержится из resolved template (API не возвращает его)
5. **Verify** — port checks + speedtest

---

## Diagnostics (diagnostics.py, 1140 строк)

`ServerDiagnostics.run_full()` — 21 модуль:

| # | Метод | Что проверяет |
|---|-------|---------------|
| 1 | `_check_system` | uptime, load, RAM, disk, CPU |
| 2 | `_check_docker` | version, containers |
| 3 | `_check_network` | ports, firewall, ping |
| 4 | `_check_panel` | HTTP check (с webBasePath!), Xray version |
| 5 | `_check_tls` | TLS handshake (head -20), cert CN |
| 6 | `_check_xray_logs` | errors, port conflict, FD exhaustion, restart freq |
| 7 | `_check_traffic` | inbound stats via xray API |
| 8 | `_check_speed` | speedtest-cli |
| 9 | `_check_conntrack` | count/max ratio, timeouts |
| 10 | `_check_kernel_health` | OOM, kernel errors, zombies |
| 11 | `_check_ulimits` | max open files, current FD |
| 12 | `_check_network_tuning` | BBR, somaxconn, buffers, tcp_tw_reuse |
| 13 | `_check_dns` | resolve google.com, cloudflare.com |
| 14 | `_check_certificates` | Let's Encrypt / acme.sh expiry |
| 15 | `_check_disk_io` | iostat, log sizes, journal size |
| 16 | `_check_connection_stats` | TCP stats, top IPs, SYN_RECV, TIME_WAIT |
| 17 | `_check_tls_connectivity` | outbound DPI check (google, cloudflare, microsoft) |
| 18 | `_check_reality_dest` | Reality dest server reachable |
| 19 | `_check_backups` | backup files, cron jobs |
| 20 | `_check_sni_pool` | SNI pool check (all domains) |
| 21 | Summary | (implicit) |

`run_sni_check()` — отдельный метод для `/sni` command, собирает все curl в один SSH exec.

---

## Типичные проблемы и фиксы (из опыта)

### Panel API
- `/panel/api/settings/update` **не существует** в 3x-ui. Настройки меняются через:
  - CLI: `docker exec 3x-ui /app/x-ui setting -port N -webBasePath /xxx/`
  - Или прямой sqlite3 на хосте: `sqlite3 /opt/3x-ui/db/x-ui.db`
- `json=payload` vs `data=payload`: REST API ожидает form-data для некоторых endpoints
- `webBasePath` — панель живёт не на `/`, а на рандомном пути типа `/7mgkiYgZ/`

### Docker
- Контейнер `3x-ui` может уже существовать → `docker rm -f` перед `docker run`
- `sqlite3` не установлен внутри контейнера — используй хостовый: `sqlite3 /opt/3x-ui/db/x-ui.db`
- После `docker restart` нужен sleep + `_wait_ready(path=webBasePath)`

### VLESS Reality ключи
- 3x-ui API **не возвращает publicKey** в `GET /inbound/{id}`
- publicKey нужно мержить из resolved template (где был `generate_x25519_keypair()`)
- Без `pbk=` в ссылке клиент падает с `empty "password"`
- publicKey можно вычислить из privateKey: `xray x25519 -i <privateKey>`

### aiosqlite
- `get_connection()` должен быть `@asynccontextmanager`, не просто `async def`
- Иначе `RuntimeError: threads can only be started once` при повторном `await`

### ServerDisconnectedError
- Панель может быть не готова после restart → retry loop с `asyncio.sleep(3)` между попытками

### Diag панели
- `_check_panel` парсит `panel_url` из БД чтобы попасть на правильный `webBasePath`
- TLS check: `head -20` (не `head -5`!) — certificate chain длинный
- Syslog noise (`"syslog backend disabled"`) фильтруется — это нормально для Docker

---

## Gemini Integration

- Model: `gemma-3-27b-it` (default) или `gemma-4-31b-it` (в тестах)
- API: Google AI Studio (`generativelanguage.googleapis.com/v1beta`)
- Tool calling: 10/10 в тестах, agent scenarios: 2/5 (Gemma-4 пока слабоват на multi-turn)
- `analyzer.py`: `analyze_deploy_output()`, `analyze_error()`, `analyze_logs()` — auto-fix suggestions
- `_coerce_str()` в `analyzer.py` — Gemini иногда возвращает list вместо string

---

## Операции

```bash
# Перезапуск
sudo systemctl restart proxy-deploy-bot

# Логи
journalctl -u proxy-deploy-bot -f --no-pager

# Статус
systemctl is-active proxy-deploy-bot

# Обновление
cd /opt/repos/proxy-deploy-bot && git pull && sudo systemctl restart proxy-deploy-bot
```

Работать с кодом через `external_repo_*` tools или `run_shell` с `cwd=/opt/repos/proxy-deploy-bot`.
Коммит/пуш: `git -C /opt/repos/proxy-deploy-bot add -A && git -C /opt/repos/proxy-deploy-bot commit -m "..." && git -C /opt/repos/proxy-deploy-bot push`.
