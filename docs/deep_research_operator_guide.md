# Deep Research Operator Guide

## Назначение

Этот документ — короткая операторская памятка для боевого использования `deep_research` / `research_run` после transport split, interruptibility, timeout normalization и honest observability.

Контур уже пригоден для реальных задач, но его нужно читать как **исследовательский инструмент с честной деградацией**, а не как магический универсальный поиск.

## Как запускать

Основной диалоговый entrypoint:

- `deep_research(query, depth="balanced", output="brief", freshness_bias="medium")`

Параметры:

- `depth`: `cheap | balanced | deep`
- `output`: `brief | memo | timeline | comparison`
- `freshness_bias`: `low | medium | high`

Практические режимы:

- **docs / API / pricing / policy lookup** → `depth="balanced"`, `output="brief"`
- **сложный comparison** → `depth="deep"`, `output="comparison"`
- **fresh release / timeline** → `depth="balanced"` или `deep`, `output="timeline"`, `freshness_bias="high"`

## Как читать trace

Ключевые поля run artifact:

- `discovery_backend_used` — чем реально шёл discovery
- `reading_backend_used` — чем реально читались страницы
- `fallback_chain` — какие деградации реально произошли
- `pages_attempted` / `pages_succeeded` / `pages_failed` — фактическая картина чтения
- `timeout_events` — диагностические timeout-события
- `interruption_checks` — сколько checkpoint-проверок прошло
- `owner_interrupt_seen` — видел ли контур owner-interrupt
- `degraded_mode` — работал ли контур в режиме деградации
- `comparison_source_class` / `page_kind` — какой preferred source class победил

Минимальный debug reading:

1. Посмотри `discovery_backend_used`
2. Посмотри `fallback_chain`
3. Сверь `pages_attempted/pages_succeeded/pages_failed`
4. Если confidence странный — смотри `timeout_events`, `degraded_mode`, `owner_interrupt_seen`

## Как понимать degraded mode

`degraded_mode=true` не означает поломку. Это означает, что контур **честно дошёл до ответа через ограниченный маршрут**.

Типичные причины:

- discovery не дал полный shortlist
- часть страниц не прочиталась вовремя
- owner superseded текущий run
- browser path был запрещён/не нужен, и контур остался на обычном reading path

Если degraded mode включён, ответ надо читать как:

- пригодный для ориентации,
- но не как максимально сильный финальный арбитраж.

## Timeout-дисциплина

Теперь timeout’ы читаются по типам:

- `discovery_timeout`
- `page_read_timeout`
- `browser_timeout`
- `overall_run_timeout`

Что это значит практически:

- `discovery_timeout` — проблема на этапе получения кандидатов
- `page_read_timeout` — shortlist найден, но чтение части страниц не успело
- `browser_timeout` — browser path завис/упёрся в лимит
- `overall_run_timeout` — закончился общий бюджет всего run

Нормальный порядок разбора:

1. выяснить stage,
2. посмотреть лимит (`timeout_limit`),
3. посмотреть backend,
4. понять, был ли fallback.

## Когда browser лучше не использовать

Browser path не должен быть default-рефлексом.

Избегать browser-first стоит, если:

- достаточно обычного docs/policy/pricing reading,
- нужна скорость и предсказуемость,
- страница и так хорошо читается обычным transport path,
- нет явного признака JS-heavy rendering.

Browser уместен только когда без него реально теряется содержимое или структура страницы.

## Когда fallback допустим

Fallback допустим, если он:

- **явно записан в trace**,
- не маскируется под Serper-only,
- не ломает смысл запроса,
- не подменяет primary-source задачу обзорным шумом.

Недопустимый fallback — тот, который делает ответ “похожим на нормальный”, но скрывает потерю первички.

## Когда run надо прерывать

Прерывать run правильно, если:

- пришёл новый owner message, который supersedes текущий запрос,
- стало ясно, что discovery ушёл не в ту задачу,
- контур уже собрал достаточно evidence, а продолжение только жжёт бюджет,
- текущий прогон вошёл в degraded mode, который не подходит под задачу.

## Как интерпретировать confidence

Высокий confidence уместен, когда:

- есть official / primary source,
- нет значимых contradiction signals,
- timeout/degraded path не испортил evidence route.

Надо снижать доверие, если:

- победили вторичные источники,
- есть `page_read_timeout` по ключевым страницам,
- comparison опирается на обзорные summaries вместо benchmark/maintainer artifacts,
- вопрос смешивает docs + policy + timeline в один запрос.

## Известные failure modes

- SERP может приводить вторичку выше первички.
- Comparison-кейсы всё ещё чувствительны к качеству benchmark artifacts в выдаче.
- Свежие release/timeline запросы могут быть сильны по overview, но слабее по полной хронологии.
- Vendor domain сам по себе не гарантирует правильный source class: marketing/blog pages штрафуются, но всё ещё могут попадать в shortlist.
