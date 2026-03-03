# Документация Ouroboros

Это корневая документация для системы **Ouroboros** — автономного агента, способного понимать сложные кодовые базы и работать с LLM-моделями напрямую.

## Ядро Ouroboros (Core Modules)

Ниже представлено описание файлов в корне директории `ouroboros/`:

- [ouroboros/agent.py (ouroboros_agent.md)](ouroboros_agent.md) — Главный оркестратор `OuroborosAgent`. Инициализирует агент, память, LLM и передает поток на исполнение. Содержит функцию самопроверки (версии, git, бюджет).
- [ouroboros/loop.py (ouroboros_loop.md)](ouroboros_loop.md) — Основная логика вызова LLM. Работает с Prompt Caching, параллельным выполнением инструментов, защищает LLM-потоки и прерывает агент при жестких budget constraints.
- [ouroboros/consciousness.py (ouroboros_consciousness.md)](ouroboros_consciousness.md) — Фоновый процесс (Background Consciousness), который "просыпается" между задачами для рефлексии (Identity updates).
- [ouroboros/context.py (ouroboros_context.md)](ouroboros_context.md) — Сборка системного контекста агента `build_llm_messages`. Инжектирует динамические и статические блоки.
- [ouroboros/llm.py (ouroboros_llm.md)](ouroboros_llm.md) — Клиент OpenRouter API: прайсинг, счетчики токенов, кэширующие заголовки Anthropic и vision-модели.
- [ouroboros/memory.py (ouroboros_memory.md)](ouroboros_memory.md) — Взаимодействие агента со своим долгосрочным "я" (Identity, Scratchpad, Events).
- [ouroboros/review.py (ouroboros_review.md)](ouroboros_review.md) — Модуль парсинга AST для получения Complexity метрик (анализ длины функций и файлов). 
- [ouroboros/utils.py и др. (ouroboros_utils.md)](ouroboros_utils.md) — Вспомогательные скрипты (`utils.py`, `owner_inject.py`, `apply_patch.py`), не зависящие от LLM.

## Инструменты (Tools)
Папка с документацией каждого агента, доступного LLM (лежат в поддиректории `tools_docum/`).
*(Если документация для инструментов ранее генерировалась в другой папке, найдите ее по соседству `tools_docum/` или в соответствующих файлах).*
