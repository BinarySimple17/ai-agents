# S03 — Конфиг + LLM-клиент шлюза (роли, backoff, breaker, бюджет)

Этап: V1 (ДЗ-02, Tool-контур) | Владелец: агент | Зависимости: S01 ✅ | Оценка: 1 | Статус: [ ] не начата
Фичи: feat-003 — Конфиг и LLM-клиент шлюза; deps: feat-001 ✅ (passing)
Роль по фиче: закрывает feat-003 — финальная сессия, через `make verify-feature`

## ⚠️ Прочитай перед началом

- Канон S00: контракт стыка 2 «LLMProvider ↔ узлы» (владелец — S03); таблица портов (`LLMProvider` — S03, `Tracer` — S03, `BudgetGuard` — S15); правило 4 (секреты через `Settings`), правило 7 (LLM в тестах — только моки), правило 9 (таймауты).
- Решения: ADR-007 §4.1 (роли cheap/mid/strong, политика ретраев M1 — контекст ретрая: только последний SQL/план + текст ошибки), [docs/model-card.md](../../docs/model-card.md) (цены, эмбеддер), ADR-008 (структурированные выходы, temperature 0, tools).
- Скелет портов уже есть: `src/text_to_sql_agent/application/ports/`, адаптеры кладутся в `infrastructure/llm/`, сборка — только `composition/` (S01).

### Правила для исполнителя

1. Имя модели — только из `Settings` (`MODEL_CHEAP`/`MODEL_MID`/`MODEL_STRONG`); ни одного хардкода модели или URL в узлах/адаптере.
2. Реальный шлюз в тестах запрещён: резильентность (429/5xx/backoff/breaker) проверяется моками.
3. Бюджет в этой сессии — предохранитель «кап на запрос» (`BUDGET_PER_REQUEST_RUB`, исключение ДО вызова); полный учёт ₽ по ценам (`MODEL_*_PRICE_*`, `BudgetGuard` порт) — S15, сюда не тащить.
4. `embed` — объявить в порте (сигнатура), реализация эмбеддера — S06.

## Предусловия

- [ ] S01 закрыта, baseline зелёный: `make check` → exit 0
- [ ] `.env` создан (`LLM_API_KEY` задан) — нужен только для ручного smoke вне тестов

## Задачи

### Task 1: `config/settings.py` — pydantic-settings, fail-fast

Все переменные из `.env.example` (LLM-шлюз, модели, цены, DSN'ы, Valkey, Neo4j, кап бюджета); обязательные — валидируются на старте внятной ошибкой; единственная точка чтения env.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_settings.py -q     # обязательные/опциональные, дефолты, fail-fast
grep -rn "os.environ\|os.getenv" src/text_to_sql_agent --include="*.py"   # только settings.py
```

### Task 2: порт `application/ports/llm_provider.py`

Protocol `LLMProvider.complete(role, messages, *, tools=None) -> LLMResponse(text, tool_calls, usage)` + `embed(texts) -> list[vector]` (сигнатура; реализация — S06). Типы — pydantic, без внешних зависимостей в портах.

**✅ Самопроверка:**

```bash
uv run ruff check src/text_to_sql_agent/application/ports    # чисто
grep -rn "LLMProvider\|\.complete(" src/text_to_sql_agent/domain   # пусто (LLM из domain запрещён)
```

### Task 3: адаптер `infrastructure/llm/openai_provider.py`

OpenAI SDK → `LLM_BASE_URL` (routerai.ru): роль → модель из `Settings`; backoff 1→2→4с на 429/5xx; circuit breaker (5 сбоев подряд → пауза 60с); temperature 0; SDK-ретраи выключены (`max_retries=0`); `usage` возвращается в `LLMResponse`; request timeout (правило 9).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_llm_client.py -q   # моки: 429 → backoff → успех; 5×5xx → breaker открыт; breaker → отказ без вызова
```

### Task 4: бюджетный предохранитель + Tracer

Кап `BUDGET_PER_REQUEST_RUB`: превышение — исключение ДО вызова шлюза (behavior feat-003); порт `Tracer` + `infrastructure/.../jsonl_tracer.py` — JSONL-лог вне git (пути из `Settings`), поля для `node_timings` (FR-11).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_llm_client.py src/tests/unit/test_settings.py -q   # бюджет исчерпан → исключение, вызова не было (мок не дёрнут)
```

### Task 5: сборка в composition

Провайдер/трейсер собираются в `composition/container.py` (единственное место сборки); подмена на фейк в тестах — через composition root.

**✅ Самопроверка:**

```bash
grep -rn "OpenAI\|LLMProvider(" src/text_to_sql_agent --include="*.py" | grep -v "infrastructure\|composition\|ports"   # пусто
make check   # полный baseline зелёный
```

## Критерии приёмки

- [ ] Verification feat-003 зелёный (команда из `feature_list.json`, вывод — в отчёт)
- [ ] Запреты S00 пусты: секреты не в коде (`grep -rn "LLM_API_KEY" src` — только `settings.py`)
- [ ] Фича закрыта: `make verify-feature F=feat-003` → passing + evidence

## Ловушки

1. Breaker — состояние в экземпляре провайдера, не в глобальной переменной модуля: иначе тесты сцеплены порядком выполнения.
2. Двойной backoff: SDK-ретраи не выключены → 429 обрабатывается дважды, тайминги тестов плавают.
3. Тест breaker с реальными sleep растягивает прогон — подмена clock/интервалов через параметры провайдера.
4. Гейты субагентов: строки ниже проверяет `guard-subagents.sh` — заполняются ПО ФАКТУ работ, не заранее.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: N/A — сессия не начата: исполнитель заполняет гейт по факту работ (canon-check — сверка контракта стыка 2 / ADR-007: роли, политика ретраев)
- REVIEW-GATE: N/A — сессия не начата: исполнитель заполняет гейт по факту работ (ревью code-reviewer по BASE..HEAD после зелёного make check, до коммита)

## Отчёт по сессии (заполняется по факту)

- Сделано:
- Отклонения от плана:
- Перенесено в другие сессии:
