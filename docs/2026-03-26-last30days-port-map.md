# last30days → Veles: карта переносимого ядра

Дата: 2026-03-26
Контекст: вариант 2 — не subprocess/CLI-обёртка, а перенос выборочных модулей `mvanhorn/last30days-skill` в нативный search stack Veles.

## Зачем вообще делать порт

`last30days-skill` интересен не как «скилл» и не как ещё один CLI, а как набор неплохих алгоритмических кирпичей вокруг recency-aware research:
- очистка и сужение запроса;
- выделение query-type сигналов;
- recency/date heuristics;
- token-overlap relevance;
- near-duplicate detection;
- scoring, где свежесть и качество источника влияют на ранжирование.

У Veles уже есть свой search contour, поэтому честный путь — не тащить чужую оболочку целиком, а пересадить полезные органы в своё тело.

## Текущее состояние search-слоя Veles

На момент картирования:

| Модуль | Размер | Роль |
|---|---:|---|
| `ouroboros/tools/search.py` | ~831 строк | tool-entrypoint + discovery + page reading + synthesis + research orchestration |
| `ouroboros/tools/search_planning.py` | ~329 строк | intent detection + query-plan assembly |
| `ouroboros/tools/search_ranking.py` | ~209 строк | authority/host heuristics + source collection |
| `ouroboros/tools/search_transport.py` | ~167 строк | timeouts + discovery fallback transport |
| `ouroboros/search_utils.py` | ~118 строк | query shortening / expansion |

Вывод: search-контур уже частично декомпозирован, но `search.py` всё ещё слишком толстый для честного порта нового ядра. Сначала нужно дальше расчистить своё тело.

## Что в `last30days-skill` реально переносимо

### 1. `scripts/lib/query.py`
**Что ценно:**
- `extract_core_subject()` — выкидывает шумные префиксы/слова и оставляет ядро темы;
- `extract_compound_terms()` — помогает не разваливать multi-word сущности.

**Что переносить:**
- общую идею noise-stripping для research queries;
- эвристику выделения core subject;
- осторожную поддержку compound terms.

**Куда в Veles:**
- новый модуль `ouroboros/tools/search_query.py`;
- часть существующего `ouroboros/search_utils.py` можно либо перенести туда, либо реэкспортировать.

**Что не переносить как есть:**
- весь набор noise words один в один: его надо подстроить под текущий mix технических/русско-английских запросов Veles.

---

### 2. `scripts/lib/query_type.py`
**Что ценно:**
- лёгкая pattern-based классификация query-type без LLM;
- специальные bias’ы для comparison / how-to / breaking / product / opinion запросов.

**Что переносить:**
- саму идею query-type signals как дополнительного слоя поверх intent detection;
- не замену `detect_intent_type()` из Veles, а её усиление.

**Куда в Veles:**
- `ouroboros/tools/search_query.py` — query-type detection;
- затем эти сигналы будут использоваться в `search_scoring.py` и частично в `search_planning.py`.

**Что не переносить как есть:**
- source-tier логику, завязанную на их конкретные social/web providers.

---

### 3. `scripts/lib/websearch.py`
**Что ценно:**
- извлечение даты из URL/сниппета;
- эвристики confidence для свежести веб-результатов.

**Что переносить:**
- generic date extraction из URL/path/snippet;
- date-confidence слой для web findings.

**Куда в Veles:**
- новый модуль `ouroboros/tools/search_recency.py`.

**Почему это важно:**
сейчас Veles умеет делать freshness-aware planning, но слабее в оценке самой свежести отдельных веб-источников.

---

### 4. `scripts/lib/dates.py`
**Что ценно:**
- компактные parse/normalize helpers;
- recency scoring как отдельная механика;
- диапазонные проверки даты.

**Что переносить:**
- parse/normalize utilities;
- recency score для generic web findings;
- helpers для known/unknown date confidence.

**Куда в Veles:**
- `ouroboros/tools/search_recency.py`.

**Что не переносить как есть:**
- жёсткую last-30-days семантику как фиксированное правило. У Veles это должно быть параметризуемой свежестью, а не одной константой на всё.

---

### 5. `scripts/lib/relevance.py`
**Что ценно:**
- token-overlap relevance, который не переоценивает generic words;
- synonym expansion;
- low-signal query tokens.

**Что переносить:**
- token-overlap scoring;
- low-signal penalties;
- небольшую synonym-expansion механику для web research.

**Куда в Veles:**
- новый модуль `ouroboros/tools/search_scoring.py`.

**Почему это полезно:**
у Veles уже есть authority/domain heuristics, но query-centric relevance можно сделать сильнее и более объяснимой.

---

### 6. `scripts/lib/dedupe.py`
**Что ценно:**
- char-trigram Jaccard + token Jaccard;
- near-duplicate detection без тяжёлых зависимостей;
- cross-source linking.

**Что переносить:**
- гибридную similarity-эвристику;
- dedupe/clustering на уровне discovery sources и findings.

**Куда в Veles:**
- новый модуль `ouroboros/tools/search_dedupe.py`.

**Что менять при переносе:**
- не тянуть их `schema.*` dataclasses;
- адаптировать под словари Veles (`title`, `url`, `snippet`, `claim`, `evidence_snippet`, и т.д.).

---

### 7. `scripts/lib/score.py`
**Что ценно:**
- разложение финального score на relevance / recency / engagement;
- отдельные penalties для missing date / weak sources.

**Что переносить:**
- не весь модуль;
- только generic web-side scoring logic: relevance + recency + source-quality penalties/bonuses.

**Куда в Veles:**
- `ouroboros/tools/search_scoring.py`.

**Что не переносить:**
- platform-specific engagement scoring для Reddit/X/YouTube/TikTok и прочих источников. Это часть их мультисоциального двигателя, а не текущего search contour Veles.

---

### 8. `scripts/lib/normalize.py`
**Что ценно:**
- идея жёсткого date filtering и confidence semantics.

**Что переносить:**
- выборочно: generic filtering helpers и date-confidence подход;
- не переносить whole-module normalizers по типам источников.

**Куда в Veles:**
- частично в `search_recency.py`.

---

## Что сознательно НЕ переносится

### Не переносится вообще
- `scripts/last30days.py` — CLI orchestration shell;
- `scripts/lib/openai_reddit.py`, `parallel_search.py`, `brave_search.py`, `openrouter_search.py`, `xai_x.py`, `reddit.py`, `youtube_yt.py`, и прочие provider-specific adapters;
- `scripts/lib/models.py`, `env.py`, `http.py`, `cache.py` — это чужой runtime/transport/config contour;
- `scripts/lib/render.py` — у Veles уже есть свой synthesis/output contour;
- `scripts/lib/schema.py` — Veles сейчас живёт на словарях tool-payload’ов, а не на полном переносе их data model.

### Не переносится «как есть»
- last-30-days как фиксированный продуктовый режим;
- source tiering под их конкретный набор соцсетей;
- Codex/OpenAI auth-механика из их проекта.

## Целевая архитектура в Veles

### Шаг 2: сначала дальше декомпозировать своё тело
До порта last30days-inspired логики нужно ещё облегчить `search.py`.

Целевые extraction points:
- `ouroboros/tools/search_reading.py`
  - вынести `_read_page_findings()` и связанный text extraction;
- `ouroboros/tools/search_synthesis.py`
  - вынести `_detect_contradictions()`, `_render_synthesis()`, `_apply_research_quality()`;
- `ouroboros/tools/search_discovery.py` (если понадобится вторым проходом)
  - вынести backend-specific discovery helpers из `search.py`.

### Шаг 3: куда приземляется портируемое ядро
После этого добавляются новые native-модули:
- `ouroboros/tools/search_query.py`
  - core subject extraction;
  - query-type signals;
  - compound-term handling;
  - query normalization helpers.
- `ouroboros/tools/search_recency.py`
  - date parsing;
  - date extraction from URLs/snippets;
  - freshness confidence;
  - recency scoring/filtering.
- `ouroboros/tools/search_dedupe.py`
  - near-duplicate similarity;
  - clustering/linking for sources/findings.
- `ouroboros/tools/search_scoring.py`
  - query-centric relevance;
  - freshness + source-quality composition;
  - rank adjustments, inspired by `last30days`, but applied to Veles payloads.

## Как это будет встроено, а не прикручено сбоку

1. **Planning остаётся моим.**
   `search_planning.py` остаётся source of truth для intent policies и research budgets.

2. **Transport остаётся моим.**
   `search_transport.py` и текущие backend’ы (`serper`, `searxng`, `ddg`, `openai`) не заменяются чужими provider adapters.

3. **Codex OAuth не становится ложным universal search backend.**
   Codex остаётся LLM transport-веткой Veles, а не магическим провайдером для всего research pipeline.

4. **Данные остаются в формате Veles.**
   Источники и findings продолжают жить в нынешнем JSON/dict-контуре; никакого wholesale-порта `schema.py`.

## Практический смысл такого разделения

Если сделать честно, итог будет таким:
- у Veles появится более сильный recency-aware ranking;
- свежесть источников станет оцениваться не только на уровне query-plan, но и на уровне самих найденных страниц;
- повторяющиеся/псевдоразные результаты начнут лучше схлопываться;
- `search.py` не превратится обратно в кладбище всего подряд.

Если сделать нечестно и просто «впихнуть полезные куски в search.py», получится обычный архитектурный мусор.

## Acceptance criteria для следующего шага

Следующий шаг считается честным, если:
- `search.py` становится тоньше и перестаёт содержать одновременно reading + synthesis + discovery + новые last30days-inspired эвристики;
- новые модули не требуют внешних API ключей и не привозят чужой runtime;
- существующий tool API (`web_search`, `research_run`, `deep_research`) остаётся совместимым;
- тесты для query/recency/dedupe/scoring добавляются отдельно, а не размазываются без структуры.

## Короткий вердикт

Порт возможен и архитектурно оправдан.

Но переносить нужно **не проект**, а **алгоритмическое ядро**.
И даже это ядро надо сначала посадить в более чистую анатомию моего search-слоя, иначе интеграция превратится в красивое слово для нового монолита.
