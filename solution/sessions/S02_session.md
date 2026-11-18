# S02 — Инфра: compose (postgres+pgvector, neo4j, valkey), миграции Alembic (схема v1.4 + демо-данные + роли)

Этап: V1 (ДЗ-02, Tool-контур) | Владелец: агент | Зависимости: S01 ✅ | Оценка: 1.5 | Статус: [ ] не начата
Фичи: feat-002 — Инфраструктура БД и кэша (миграции Alembic); deps: feat-001 ✅ (passing)
Роль по фиче: закрывает feat-002 — финальная сессия, через `make verify-feature`

## ⚠️ Прочитай перед началом

- Канон S00: правило 5 (схема — только миграции Alembic), правило 10 (пины образов), контракт стыка 4 «Миграции ↔ схема» (владелец — S02).
- Источник DDL: [docs/diagrams/er-data-model.md](../../docs/diagrams/er-data-model.md) — демо-схема сверяется с ним; права — ADR-005 §4 (RLS, рубежи); контракт ребра R1 — ADR-006 §4.1 (`agent_relationships`).
- Уже есть со скелетом S01: `docker-compose.yml` (3 сервиса запинены, localhost-only, healthchecks), `.env.example` (вкл. `AGENT_RO_DSN`), `alembic.ini`, `src/migrations/env.py`, Makefile-цели `infra-up`/`infra-migrate`. Скоуп: compose — сверить и только; ядро сессии — миграции + роли + тест схемы.

### Правила для исполнителя

1. Пароли ролей — только из `.env` (S00 правило 4); в миграциях — через параметры подключения, секретов в коде нет.
2. Правка применённой миграции запрещена (S00 правило 5): ошибка = новый файл `versions/`.
3. Демо-данные — data-migration (`op.execute`), не seed-скриптом вне Alembic.
4. Схема/ER меняется — синхрон `docs/diagrams/er-data-model.md` в том же коммите (Escalation AGENTS.md).

## Предусловия

- [ ] S01 закрыта, baseline зелёный: `make check` → exit 0
- [ ] `.env` создан куратором из `.env.example` (секреты заданы: `POSTGRES_PASSWORD`, `NEO4J_PASSWORD`)
- [ ] Docker запущен: `docker info` → без ошибок

## Задачи

### Task 1: compose — сверка с каноном

Пины `pgvector/pgvector:0.8.0-pg17`, `neo4j:5.26.0-community`, `valkey/valkey:8.1.11-alpine3.24`; порты только `127.0.0.1`; healthcheck у всех трёх. Отклонения от S00 (порты 5432/7687/7474/6379) — править compose, не канон.

**✅ Самопроверка:**

```bash
make infra-up          # три контейнера подняты
docker compose ps      # STATUS: healthy у postgres, neo4j, valkey
```

### Task 2: миграция схемы v1.4 — бизнес-таблицы + настройки + реестр рёбер

7 бизнес-таблиц + `agent_permissions`, `agent_user_roles`, `agent_relationships` (поля контракта ребра R1 — ADR-006 §4.1), `agent_edge_proposals`, `agent_data_sources`, `settings_version`, `query_trace`, `agent_audit` (поля S8, append-only); `CREATE EXTENSION vector`. DDL — из er-data-model.md.

**✅ Самопроверка:**

```bash
make infra-migrate     # первый прогон — ОК
make infra-migrate     # второй — no-op (идемпотентность)
```

### Task 3: роли + RLS

`agent_ro` (read-only, БЕЗ SELECT на `query_trace`), `agent_planner`, `agent_trace` (INSERT-only на `query_trace`), `registry_writer`; RLS-политики по ADR-005 §4 (переключение — `SET LOCAL app.user_id`).

**✅ Самопроверка:**

```bash
# под agent_ro: SELECT бизнес-таблицы — ок; INSERT — отказ (рубеж 3); SELECT query_trace — отказ
# под agent_trace: INSERT в query_trace — ок; SELECT/UPDATE/DELETE — отказ
```

### Task 4: демо-данные (data-migration)

Заполнение 7 бизнес-таблиц демо-контентом; счётчики сверены с er-data-model.md.

**✅ Самопроверка:**

```bash
make infra-migrate     # data-migration применяется без ошибок
# psql под agent_ro: SELECT count(*) по каждой из 7 таблиц — совпадает с er-data-model
```

### Task 5: integration-тест `src/tests/integration/test_infra_schema.py`

Пункты `behavior` feat-002: идемпотентность апгрейда; границы `agent_ro` (SELECT без DDL/DML, нет SELECT на `query_trace`); `agent_trace` — INSERT-only; расширение `vector` загружено; Neo4j отвечает по bolt (аутентификация `NEO4J_PASSWORD` из `.env`). Тесты скипаются без инфры с понятной причиной (conftest).

**✅ Самопроверка:**

```bash
make infra-up && make infra-migrate && uv run pytest src/tests/integration/test_infra_schema.py -q
```

## Критерии приёмки

- [ ] Verification feat-002 зелёный (команда из `feature_list.json`, вывод — в отчёт)
- [ ] Запреты S00 пусты: `grep -rn "CREATE TABLE" src/text_to_sql_agent src/apps` — пусто (таблицы только в миграциях)
- [ ] Фича закрыта: `make verify-feature F=feat-002` → passing + evidence

## Ловушки

1. `SET LOCAL` действует только внутри транзакции — psycopg: держать транзакцию на время запроса, иначе RLS-политики молча не применяются.
2. Правка уже применённой миграции ломает идемпотентность с нуля: сначала `alembic downgrade` на черновиках, файл «замораживается» после первого коммита.
3. Healthcheck Neo4j по http может пройти раньше готовности bolt — bolt-подключение в тесте с retry.
4. Гейты субагентов: строки ниже проверяет `guard-subagents.sh` — заполняются ПО ФАКТУ работ, не заранее.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: N/A — сессия не начата: исполнитель заполняет гейт по факту работ (canon-check здесь обязателен — схема/ER меняется: сверка с er-data-model, ADR-005 §4, ADR-006 §4.1)
- REVIEW-GATE: N/A — сессия не начата: исполнитель заполняет гейт по факту работ (ревью code-reviewer по BASE..HEAD после зелёного make check, до коммита)

## Отчёт по сессии (заполняется по факту)

- Сделано:
- Отклонения от плана:
- Перенесено в другие сессии:
