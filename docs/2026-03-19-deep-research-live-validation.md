# Deep Research Live Validation — 2026-03-19

## Цель

Проверить контур не только unit/regression тестами, но и как операторский инструмент для реальных классов задач.

## Набор живых сценариев

1. docs lookup
2. vendor policy / data usage lookup
3. model comparison
4. benchmark retrieval
5. fresh release lookup

## Что проверялось

- честность transport trace
- корректность fallback chain
- поведение timeout discipline
- operator readability run artifact
- пригодность ответа для реального следующего шага, а не только для benchmark score

## Итог по классам задач

### 1. Docs lookup
**Состояние:** сильный рабочий путь.

Что уже хорошо:
- official docs/reference forcing заметно сильнее обзорных страниц;
- doc-shaped URL priors действительно вытягивают нужные reference paths;
- trace обычно объясняет, почему победила именно docs-ветка.

Остаточный риск:
- если vendor docs разбросаны между несколькими хостами/поддоменами, query rewriting всё ещё может требовать ручного уточнения.

### 2. Vendor policy / data usage
**Состояние:** заметно лучше прежнего, пригодно для реального использования.

Что уже хорошо:
- legal/privacy/retention paths различаются отдельно от docs/pricing;
- summary/blog/marketing pages на vendor-домене больше не выглядят “почти official”;
- trace по source reasons стал честнее.

Остаточный риск:
- policy wording меняется, и часть вторичных страниц может ещё временно выглядеть релевантной по тексту.

### 3. Model comparison
**Состояние:** рабочий v1, но всё ещё чувствителен к качеству первички.

Что уже хорошо:
- comparison mode больше не сваливается в generic web comparison;
- есть раздельные preferred source classes для feature / benchmark / ecosystem путей;
- trace показывает победивший source class.

Остаточный риск:
- при плохой выдаче comparison может быть сильным по структуре ответа, но слабее по глубине первичных benchmark artifacts.

### 4. Benchmark retrieval
**Состояние:** улучшен, но остаётся самым чувствительным направлением.

Что уже хорошо:
- benchmark-specific priors различают leaderboard / paper / repo methodology / vendor docs;
- comparison benchmark retrieval меньше доверяет roundup-страницам;
- maintainer/primary repo signals учитываются лучше.

Остаточный риск:
- если в SERP мало нормальных benchmark artifacts, контур деградирует честно, но не магически.

### 5. Fresh release lookup
**Состояние:** пригодно для быстрых ориентировочных прогонов.

Что уже хорошо:
- контур уже способен собрать свежий обзор с нормальным uncertainty layer;
- timeout trace и degraded mode теперь не маскируются.

Остаточный риск:
- для очень свежих релизов timeline может быть неполной, если первоисточник ещё плохо индексирован.

## Главные практические выводы

1. Контур уже можно запускать в реальной работе для docs / policy / official-source lookup.
2. Comparison и benchmark retrieval уже рабочие, но требуют внимательного чтения preferred source class и confidence.
3. Browser path не должен быть default: его стоит включать только там, где обычное чтение реально теряет содержимое.
4. Новый trace наконец позволяет отличать:
   - настоящий баг,
   - честную деградацию,
   - timeout на конкретном этапе,
   - owner supersession.

## Когда не стоит делать вид, что всё хорошо

Нельзя считать результат “сильным финальным ответом”, если одновременно есть:

- `degraded_mode=true`,
- `page_read_timeout` по ключевым страницам,
- слабый preferred source class,
- отсутствие primary/official evidence для claim-heavy comparison.

## Операторское правило

Если trace показывает хорошую первичку и чистый transport path — ответу можно доверять значительно сильнее.
Если trace показывает fallback/timeout/degraded path — это не повод выбрасывать run, но это повод читать его как ограниченный результат, а не как окончательный арбитраж.
