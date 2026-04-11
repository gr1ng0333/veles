# Fleet monitoring runbook

Автономный контур мониторинга флота строится поверх существующего SSH/Xray/3x-ui registry и runtime-инструмента `fleet_health`.

## Что уже считается источником истины

- `state/ssh_targets.json` — реестр серверов
- `ouroboros/tools/fleet_health.py` — агрегатор состояния флота
- `ouroboros/tools/xui_panel.py` — нативный HTTP-клиент 3x-ui
- `ouroboros/tools/remote_service.py` — SSH health, Xray, TLS, systemd
- `scripts/fleet_monitor.py` — автономный запуск без LLM-контура

## Обязательные поля в реестре

Для масштабирования до десятков серверов у записи должны быть заполнены как минимум:

- `alias`
- `host`
- `user`
- `auth_mode`
- `provider`
- `location`
- `panel_type`
- `panel_url`
- `tags[]`
- `known_ports[]`
- `known_services[]`
- `known_tls_domains[]`
- `status`
- `last_health_at`

Для 3x-ui мониторинга без браузера дополнительно нужны:

- `panel_username`
- `panel_password`

## Standalone script

```bash
python3 scripts/fleet_monitor.py --stdout-format summary
python3 scripts/fleet_monitor.py --tag vpn --tag 3xui --max-workers 12
python3 scripts/fleet_monitor.py --alias srv-80-71-227-193 --output-path reports/one-host.json
```

По умолчанию скрипт пишет:

- snapshot: `/opt/veles-data/state/fleet_health_latest.json`
- history: `/opt/veles-data/logs/fleet_health.jsonl`

### Exit codes

- `0` — весь матчинг-флот в состоянии `ok`
- `1` — есть `warn`, но нет `critical`
- `2` — есть хотя бы один `critical`
- `3` — неизвестный verdict / сломанный payload

## Что реально проверяется

### SSH / host layer
- доступность хоста
- uptime / load / disk / memory
- открытые порты
- известные systemd unit'ы
- TLS-сертификаты по `known_tls_domains`

### Xray layer
- жив ли Xray
- кто им управляет: отдельный `xray.service` или `x-ui.service`
- состояние процесса и сокетов

### 3x-ui layer
- проходит ли login
- отвечает ли `/api/server/status`
- отвечает ли `/api/inbounds/list`
- количество inbounds
- количество enabled inbounds
- aggregate traffic counters

## Cron example

```cron
*/15 * * * * cd /opt/veles && /usr/bin/python3 scripts/fleet_monitor.py --stdout-format summary >> /opt/veles-data/logs/fleet_monitor.cron.log 2>&1
```

## Systemd timer idea

- service: one-shot `python3 /opt/veles/scripts/fleet_monitor.py`
- timer: `OnBootSec=2min`, `OnUnitActiveSec=15min`
- alerting later можно строить поверх JSON snapshot/history, не трогая сам runtime tool.

## Практический операторский цикл

1. Зарегистрировать сервер и metadata в `ssh_targets.json`
2. Проверить key-auth и health snapshot
3. Добавить panel credentials для 3x-ui хостов
4. Прогнать `fleet_monitor.py` по одному хосту
5. Прогнать его по tag-группе (`vpn`, `ru`, `3xui`)
6. Только потом навешивать cron/systemd timer

