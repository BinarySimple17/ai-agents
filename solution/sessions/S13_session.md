# S13 — SQLExecutor: тул run_sql (ДЗ-02) + узел execute_sql — agent_ro, LIMIT 1000, timeout 30s

Этап: V1 (ДЗ-02, Tool-контур) | Владелец: агент | Зависимости: S02 | Оценка: 1 | Статус: [ ] не начата
Фичи: feat-013 — SQLExecutor: тул run_sql + узел execute_sql; deps: feat-002 (passing после S02)
Роль по фиче: закрывает feat-013 — финальная сессия, через `make verify-feature`

## ⚠️ Прочитай перед началом

- Канон S00: таблица портов (`SQLExecutor.execute(QueryRequest, *, ctx) -> rows`, дискриминатор `sql|spec` — ADR-011 v1.1); запреты (исполняемый SQL — только `agent_ro`, `LIMIT 1000`, timeout 30s, никакого DDL/DML; `EXPLAIN ANALYZE` запрещён); правило 8 (деградация вместо падения).
- Границы прав: hw-01 §3.2 + [docs/security.md](../../docs/security.md) — роль создаётся в S02 (миграции), здесь — только использование по `AGENT_RO_DSN`.
- FSM: узел `execute_sql` маршрутизирует ошибку/пустой результат в `diagnose_error` ([state-agent.md](../../docs/diagrams/state-agent.md)); примечание sessions.md — узловое ПОДКЛЮЧЕНИЕ к FSM выполняется в задачах V4; здесь — порт, адаптер, тул, узел-сервис, тесты.

### Правила для исполнителя

1. Подключение — строго по `AGENT_RO_DSN` (не по сервисному `POSTGRES_DSN`); роль не может DDL/DML — это обеспечивает БД (рубеж 3), а не код.
2. `LIMIT 1000` — обёрткой `SELECT * FROM (<sql>) AS q LIMIT 1000`, не правкой текста пользователя.
3. Timeout — `SET LOCAL statement_timeout = '30s'` внутри транзакции запроса; не client-side cancel.
4. Ошибка БД — структурированная Observation (не traceback, S00 правило 8); пустой результат — валидный исход, не ошибка.

## Предусловия

- [ ] S02 закрыта: `make verify-feature F=feat-002` → passing
- [ ] Инфра поднята: `make infra-up && make infra-migrate` → без ошибок
- [ ] baseline зелёный: `make check` → exit 0

## Задачи

### Task 1: порт `application/ports/sql_executor.py`

Protocol `SQLExecutor.execute(QueryRequest, *, ctx) -> rows`; `QueryRequest` — pydantic (sql + дискриминатор `sql|spec`); типы без внешних зависимостей.

**✅ Самопроверка:**

```bash
uv run ruff check src/text_to_sql_agent/application/ports    # чисто
```

### Task 2: адаптер `infrastructure/persistence/postgres_executor.py`

Подключение по `AGENT_RO_DSN`; обёртка LIMIT 1000; `statement_timeout = 30s`; ошибка → структурированный результат с классом ошибки (для ретраев M1 в V4); rows с усечением-флагом (`truncated`), если срезано LIMIT'ом.

**✅ Самопроверка:**

```bash
# psql под agent_ro: SELECT * FROM (<запрос на 5000 строк>) LIMIT 1000 → 1000 строк — обёртка работает
grep -rn "POSTGRES_DSN" src/text_to_sql_agent/infrastructure/persistence   # сервисный DSN здесь не используется
```

### Task 3: тул run_sql (ДЗ-02) + узел-сервис execute_sql

Схема тулa для LLM (tools — ADR-008: имя, описание, параметры); сервис узла `execute_sql`: принимает state, вызывает executor, раскладывает результат/ошибку в state (`rows`/`error`); заготовки рёбер маршрутизации ошибка/пусто → `diagnose_error` (подключение графа — V4).

**✅ Самопроверка:**

```bash
uv run ruff check src    # чисто; узел не импортирует другие узлы (S00 запрет)
```

### Task 4: integration-тест `src/tests/integration/test_execute_sql.py`

Пункты `behavior` feat-013: SELECT под `agent_ro` возвращает данные; LIMIT режет >1000 строк; INSERT под `agent_ro` → отказ БД (рубеж 3); timeout (`pg_sleep` > 30s) → ошибка по таймауту, не зависание; `query_trace` недоступна `agent_ro`; пустой результат — не ошибка. Скип без инфры (conftest).

**✅ Самопроверка:**

```bash
make infra-up && uv run pytest src/tests/integration/test_execute_sql.py -q
```

## Критерии приёмки

- [ ] Verification feat-013 зелёный: `make infra-up && uv run pytest src/tests/integration/test_execute_sql.py -q` (вывод — в отчёт)
- [ ] Запреты S00 пусты: `grep -rn "EXPLAIN ANALYZE\|DELETE FROM\|INSERT INTO" src/text_to_sql_agent src/apps` — пусто (кроме миграций/тестов)
- [ ] Фича закрыта: `make verify-feature F=feat-013` → passing + evidence

## Ловушки

1. `pg_sleep`-тест держит соединение 30+ секунд: отдельное подключение вне пула и явный маленький таймаут теста — иначе блокирует прогон.
2. Перепутанный DSN (сервисный вместо read-only) молча лишает рубежа 3: тест на отказ INSERT обязателен, не опционален.
3. Обёртка LIMIT не должна глотать уже имеющийся LIMIT/OFFSET пользователя: сверка с AST-валидатором (S11) — здесь просто документировать поведение, валидация не в этой сессии.
4. Гейты субагентов: строки ниже проверяет `guard-subagents.sh` — заполняются ПО ФАКТУ работ, не заранее.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: N/A — сессия не начата: исполнитель заполняет гейт по факту работ (canon-check — границы прав hw-01 §3.2 / security.md, контракт SQLExecutor)
- REVIEW-GATE: N/A — сессия не начата: исполнитель заполняет гейт по факту работ (ревью code-reviewer по BASE..HEAD после зелёного make check, до коммита)

## Отчёт по сессии (заполняется по факту)

- Сделано:
- Отклонения от плана:
- Перенесено в другие сессии:
