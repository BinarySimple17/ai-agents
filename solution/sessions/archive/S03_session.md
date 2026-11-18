# S03 — Конфиг + LLM-клиент шлюза (роли, backoff, breaker, бюджет)

Этап: V1 (ДЗ-02, Tool-контур) | Владелец: агент | Зависимости: S01 ✅ | Оценка: 1 | Статус: [x] ✅ 2026-11-18
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

- [x] S01 закрыта, baseline зелёный: `make check` → exit 0 (39 passed на старте работ: 14 infra + 25 unit S03)
- [x] `.env` создан (`LLM_API_KEY` — плейсхолдер «ЗАПОЛНИТЬ»: ключ нужен только ручному smoke вне тестов; тесты — моки, S00 правило 7. Smoke — после передачи ключа куратором)

## Задачи

### Task 1: [x] `config/settings.py` — pydantic-settings, fail-fast

Все переменные из `.env.example` (LLM-шлюз, модели, цены, DSN'ы, Valkey, Neo4j, кап бюджета); обязательные — валидируются на старте внятной ошибкой; единственная точка чтения env.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_settings.py -q     # обязательные/опциональные, дефолты, fail-fast
grep -rn "os.environ\|os.getenv" src/text_to_sql_agent --include="*.py"   # только settings.py
```

### Task 2: [x] порт `application/ports/llm_provider.py`

Protocol `LLMProvider.complete(role, messages, *, tools=None) -> LLMResponse(text, tool_calls, usage, model)` + `embed(texts) -> list[vector]` (сигнатура; реализация — S06; поле model — синхрон S00 ревью Minor-12). Типы — pydantic, без внешних зависимостей в портах.

**✅ Самопроверка:**

```bash
uv run ruff check src/text_to_sql_agent/application/ports    # чисто
grep -rn "LLMProvider\|\.complete(" src/text_to_sql_agent/domain   # пусто (LLM из domain запрещён)
```

### Task 3: [x] адаптер `infrastructure/llm/openai_provider.py`

OpenAI SDK → `LLM_BASE_URL` (routerai.ru): роль → модель из `Settings`; backoff 1→2→4с на 429/5xx; circuit breaker (5 сбоев подряд → пауза 60с); temperature 0; SDK-ретраи выключены (`max_retries=0`); `usage` возвращается в `LLMResponse`; request timeout (правило 9).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_llm_client.py -q   # моки: 429 → backoff → успех; 5×5xx → breaker открыт; breaker → отказ без вызова
```

### Task 4: [x] бюджетный предохранитель + Tracer

Кап `BUDGET_PER_REQUEST_RUB`: превышение — исключение ДО вызова шлюза (behavior feat-003); порт `Tracer` + `infrastructure/.../jsonl_tracer.py` — JSONL-лог вне git (пути из `Settings`), поля для `node_timings` (FR-11).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_llm_client.py src/tests/unit/test_settings.py -q   # бюджет исчерпан → исключение, вызова не было (мок не дёрнут)
```

### Task 5: [x] сборка в composition

Провайдер/трейсер собираются в `composition/container.py` (единственное место сборки); подмена на фейк в тестах — через composition root.

**✅ Самопроверка:**

```bash
grep -rn "OpenAI\|LLMProvider(" src/text_to_sql_agent --include="*.py" | grep -v "infrastructure\|composition\|ports"   # пусто
make check   # полный baseline зелёный
```

## Критерии приёмки

- [x] Verification feat-003 зелёный: `uv run pytest src/tests/unit/test_llm_client.py src/tests/unit/test_settings.py -q` → 32 passed (guard strict 3 PASS)
- [x] Запреты S00 пусты: `os.environ|os.getenv` — только docstring settings.py; `LLM_API_KEY` — только тестовые списки имён и фейк `sk-test`; LLM в domain — пусто; сборка — только composition/infrastructure
- [x] Фича закрыта: `make verify-feature F=feat-003` → **passing** + evidence (feature_list.json)

## Ловушки

1. Breaker — состояние в экземпляре провайдера, не в глобальной переменной модуля: иначе тесты сцеплены порядком выполнения.
2. Двойной backoff: SDK-ретраи не выключены → 429 обрабатывается дважды, тайминги тестов плавают.
3. Тест breaker с реальными sleep растягивает прогон — подмена clock/интервалов через параметры провайдера.
4. Гейты субагентов: строки ниже проверяет `guard-subagents.sh` — заполняются ПО ФАКТУ работ, не заранее.
5. Таблица backoff «1→2→4с» требует 4 попыток (3 паузы) — при 3 попытках пауза 4с недостижима и тесты молча закрепляют отклонение от канона (ревью S03 Critical-1). «Три попытки» политики M1 — наузловый ретрай оркестратора (S04), не транспортный.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: ✅ violations→resolved (architect, canon-check до старта; вердикт «violations», блокеров нет) — 2 warning разрешены rulings: W1 backoff 1→2→4с **с jitter** (ADR-007 §4.3 старше S00; jitter через инъекцию rng, синхрон формулировки «с jitter» в S00 стык 2 + описание feat-003 — тем же коммитом); W2 таймаут — отказ узла БЕЗ инкремента breaker («breaker считает ошибки, не медлительность») + тест. Notes приняты: типизированные CircuitOpenError/BudgetExceededError в иерархии ошибок порта; кап-предохранитель — резидент в openai_provider (первая строка complete(), пер-запросный, reset оркестратором S04; миграционный путь — порт BudgetGuard в S15); pydantic-типы ответа в порту, не в domain; embed(texts: Sequence[str]) -> list[Embedding], в адаптере NotImplementedError до S06; Tracer.record(Trace), Trace — VO в domain
- REVIEW-GATE: ✅ With fixes → re-review (code-reviewer, 558d072..HEAD рабочее дерево; 46 passed, ruff чист) — 1 Critical + 5 Important + 6 Minor, все код-находки resolved fix-волной до коммита: backoff 1→2→4 требует 4 попыток (пауза 4с была недостижима — тесты закрепляли отклонение); JsonlTracer best-effort (OSError → warn, оплаченный вызов не ломается); DSN → SecretStr (пароль не течёт в repr); fuse задокументирован best-effort + флаг usage_missing; failure-события трейса (llm.retry/timeout/reject/breaker_open/budget_stop); half-open проба переоткрывает breaker; except openai.APIError; gt=0/http-validator; default_factory; httpx в dev-группе. **Scoped re-review: «С оговорками» — все код-находки resolved, регрессий нет; остаточное — артефакты сессии (закрываются этим коммитом) и verify-feature**

## Отчёт по сессии (заполняется по факту)

- Сделано: `Settings` — pydantic-settings, fail-fast (`load_settings` с внятным перечнем отсутствующих), ключ/пароли/DSN — SecretStr без дефолтов, `gt=0`/`ge=0`-валидации, http-валидатор base_url; порт `LLMProvider` — фикс-сигнатура стыка 2 (+`model` в ответе, синхрон S00), pydantic-DTO, типизированные ошибки (CircuitOpen/BudgetExceeded/LLMRequest/LLMProvider); порт `Tracer` + `Trace` (VO в domain); `OpenAIProvider` — роли cheap/mid/strong из Settings, backoff 1→2→4с с jitter (4 попытки — Critical-1 ревью), breaker 5→60с с half-open пробой и переоткрытием на неудачной пробе, таймаут без инкремента breaker (ARCH-GATE W2), temperature 0, `max_retries=0`, `usage`→₽ по ценам ролей, флаг `usage_missing`, failure-события трейса (retry/timeout/reject/breaker_open/budget_stop); бюджетный предохранитель кап-на-запрос (первая строка complete, best-effort, reset оркестратором); `JsonlTracer` — best-effort JSONL вне git; сборка — `composition/container.py`; тесты 32 unit на моках (герметичные Settings, скриптованный шлюз, инъекция clock/sleep/rng) + 14 infra; `make verify-feature F=feat-003` → passing.
- Отклонения от плана: backoff — 4 попытки (план не фиксировал число; пауза 4с при 3 попытках недостижима — ревью Critical-1, ловушка 5); fuse — семантика зафиксирована best-effort (превышение ПО факту вызова; жёсткий пре-колл-контроль — S15); трейс — failure-события добавлены сверх плана (Imp-5 ревью); `httpx` (прямая зависимость тестов) — в dev-группу (Minor-11); jitter — добавлен по ADR-007 §4.3 против формулировки плана/S00 (ARCH-GATE W1, синхрон тем же коммитом).
- Перенесено в другие сессии: жёсткий бюджетный контроль (оценка стоимости до вызова, резерв 0,6 ₽ под interpret_result) → S15 (порт BudgetGuard поглощает fuse-проверку); агрегат `node_timings` + вызов `reset_request_budget()` на старте прогона → S04/S15; реализация `embed` → S06 (плановое). Ручной smoke реального шлюза (Layer 3) — после передачи `LLM_API_KEY` куратором: тесты его не требуют (S00 правило 7).
