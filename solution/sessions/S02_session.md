# S02 — Инфра: compose (postgres+pgvector, neo4j, valkey), миграции Alembic (схема v1.5 + демо-данные + роли)

Этап: V1 (ДЗ-02, Tool-контур) | Владелец: агент | Зависимости: S01 ✅ | Оценка: 1.5 | Статус: [x] ✅ 2026-11-18
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

- [x] S01 закрыта, baseline зелёный: `make check` → exit 0
- [x] `.env` создан из `.env.example` (создан в сессии с dev-дефолтами; `LLM_API_KEY` — не требуется S02, задаёт куратор к S03)
- [x] Docker запущен: `docker info` → без ошибок

## Задачи

### Task 1: compose — сверка с каноном

Пины `pgvector/pgvector:0.8.0-pg17`, `neo4j:5.26.0-community`, `valkey/valkey:8.1.11-alpine3.24`; порты только `127.0.0.1`; healthcheck у всех трёх. Отклонения от S00 (порты 5432/7687/7474/6379) — править compose, не канон.

**✅ Самопроверка:**

```bash
make infra-up          # три контейнера подняты
docker compose ps      # STATUS: healthy у postgres, neo4j, valkey
```

### Task 2: миграция схемы v1.5 — бизнес-таблицы + настройки + реестр рёбер

7 бизнес-таблиц + `agent_permissions`, `agent_user_roles` (DDL — ADR-005 §4), `agent_relationships` (**DDL — ADR-006 §4.1, единственный источник**), `agent_edge_proposals` (ADR-006 §4.4), `agent_data_sources`, `query_trace`, `agent_audit` (S8); `CREATE EXTENSION vector`; функция `bump_settings_version()` + 3 триггера AFTER INSERT OR UPDATE OR DELETE (FOR EACH STATEMENT) на `agent_permissions` / `agent_user_roles` / `agent_relationships` (ADR-005 §4); вьюха `agent_relationships_active` (ADR-006 §4.2 — агент читает только active). DDL сверять с er-data-model.md v1.5.

**✅ Самопроверка:**

```bash
make infra-migrate     # первый прогон — ОК
make infra-migrate     # второй — no-op (идемпотентность)
# psql: SELECT count(*) FROM agent_relationships_active → вьюха отвечает
```

### Task 3: роли + гранты (whitelist) + hardening

Роли LOGIN, пароли из `.env` (`AGENT_RO_PASSWORD`, `AGENT_PLANNER_PASSWORD`, `AGENT_TRACE_PASSWORD`, `REGISTRY_WRITER_PASSWORD`): `agent_ro` (`default_transaction_read_only = on` + `statement_timeout = '30s'` на роль — рубеж 5 по ADR-005), `agent_planner`, `agent_trace`, `registry_writer`. Гранты — **whitelist, deny-by-default (FIX-007)**: `agent_ro` — SELECT только на 7 бизнес-таблиц (ни `query_trace`, ни `agent_*`); `agent_trace` — INSERT-only на `query_trace` + страховочный триггер BEFORE UPDATE OR DELETE → EXCEPTION (security.md §2.2); `agent_planner` — SELECT на бизнес-таблицы + настройки + вьюху; `registry_writer` — INSERT/UPDATE на `agent_relationships` / `agent_edge_proposals`, без DELETE (устаревание вместо удаления). Hardening: `dblink`/`file_fdw` не устанавливаются вовсе (security.md §2.1). RLS-политики — **S13** (механизм `SET LOCAL` появляется там; FIX-007).

**✅ Самопроверка:**

```bash
# под agent_ro: SELECT бизнес-таблицы — ок; INSERT — отказ (read-only сессии); SELECT query_trace — отказ
# под agent_trace: INSERT в query_trace — ок; UPDATE/DELETE — отказ (грант + триггер)
# под registry_writer: UPDATE ребра — ок; DELETE — отказ
# SELECT extname FROM pg_extension WHERE extname IN ('dblink','file_fdw') — пусто
```

### Task 4: демо-данные (data-migration)

Объёмы зафиксированы здесь (в er-data-model счётчиков нет — решение canon-check): customers 20, campaigns 6 (ads/email/partner), products 30, orders 400 (2026-Q3/Q4; статусы paid/shipped/cancelled; **≥ 30 строк с `campaign_id` NULL — ловушка INNER JOIN**), order_items ~900 (1–4 позиции на заказ), employees 12 (sales 5 / logistics 4 / hr 3; `salary` — PII), bonuses 7 (**не всем сотрудникам — ловушка `LEFT JOIN … IS NULL`**). Реестр: 5 active-рёбер с meaning/weight/intent_tags (customers→orders, campaigns→orders n:1, orders→order_items 1:n `fanout_guard=true`, products→order_items, employees→bonuses left) + 1 deprecated с `replaced_by` (демо жизненного цикла); права: logistics_viewer / hr_viewer (employees `*` вкл. salary — er-data-model.md:54) / analyst + 3 демо-пользователя; `agent_data_sources`: 7 строк pg/sql. Счётчики в отчёт сессии.

**✅ Самопроверка:**

```bash
make infra-migrate     # data-migration применяется без ошибок
# psql под agent_ro: SELECT count(*) по каждой из 7 таблиц — совпадает с фиксированным выше
```

### Task 5: integration-тест `src/tests/integration/test_infra_schema.py`

Пункты `behavior` feat-002: идемпотентность апгрейда; границы `agent_ro` (SELECT без DDL/DML, нет SELECT на `query_trace`); `agent_trace` — INSERT-only; расширение `vector` загружено; Neo4j отвечает по bolt (аутентификация `NEO4J_PASSWORD` из `.env`). Тесты скипаются без инфры с понятной причиной (conftest).

**✅ Самопроверка:**

```bash
make infra-up && make infra-migrate && uv run pytest src/tests/integration/test_infra_schema.py -q
```

## Критерии приёмки

- [x] Verification feat-002 зелёный (команда из `feature_list.json`, вывод — в отчёт)
- [x] Запреты S00 пусты: `grep -rn "CREATE TABLE" src/text_to_sql_agent src/apps` — пусто (таблицы только в миграциях)
- [x] Счётчики `settings_version` инкрементируются триггерами (UPDATE строки прав → version+1 — тест Task 5)
- [x] Фича закрыта: `make verify-feature F=feat-002` → passing + evidence

## Ловушки

1. `SET LOCAL` действует только внутри транзакции — psycopg: держать транзакцию на время запроса, иначе RLS-политики молча не применяются.
2. Правка уже применённой миграции ломает идемпотентность с нуля: сначала `alembic downgrade` на черновиках, файл «замораживается» после первого коммита.
3. Healthcheck Neo4j по http может пройти раньше готовности bolt — bolt-подключение в тесте с retry.
4. Windows/IPv6: psycopg и сокеты к `localhost` сначала пробуют `::1`, который молча дропается (Docker слушает только 127.0.0.1) — соединение висит до TCP-таймаута (~60 с на попытку). Все DSN/URL в `.env.example` и дефолты conftest — `127.0.0.1`; измерено: localhost 60.0 s против 127.0.0.1 0.0 s.
5. Гейты субагентов: строки ниже проверяет `guard-subagents.sh` — заполняются ПО ФАКТУ работ, не заранее.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: ✅ needs-decision (architect, canon-check до старта) — план канонен, 3 warning: RLS-политики без механизма `SET LOCAL` → перенос в S13; feature_list/метки v1.4 отстали → синхрон; демо-счётчики неверифицируемы → объёмы зафиксированы в Task 4. Все rulings — FIX-007 (S00 контракт 4 синхронизирован тем же PR)
- REVIEW-GATE: ✅ With fixes (code-reviewer, 9c8e3a1..c06ffcc) — Important 1–6 и Minor 1–8 исправлены fix-волной в этом же коммите: SECURITY DEFINER `search_path = pg_catalog` + квалификация `public.settings_version` + регрессионный тест temp-двойника; planner-гранты ужаты до вьюхи active (ADR-006 §4.2); `downgrade()` — DROP OWNED перед DROP ROLE; S00-контракт 4 синхрон; order_items 1–4/1000 + total из позиций; guard стал фича-aware и строгим в режиме фичи. **Scoped re-review (9c8e3a1..d6b7867): все 6 Important resolved, новых блокеров нет — ready to merge**

## Отчёт по сессии (заполняется по факту)

- Сделано: миграции 0001–0003 (схема v1.5: 7 бизнес-таблиц + права/реестр по ADR-006 §4.1 + заявки + каталог источников + query_trace/agent_audit; роли + whitelist-гранты + hardening; демо-данные с ловушками — 33 NULL campaign_id, 5 непремированных); тест `test_infra_schema.py` — 13 passed (идемпотентность, состав, вьюха, vector, границы ролей, триггеры счётчика, регрессия temp-двойника, ловушки, total=SUM позиций, Neo4j bolt); чистая комната (свежая БД → полная цепочка) — зелёная; `make verify-feature F=feat-002` → passing. Открытая находка исполнения: `make infra-migrate` не подхватывал .env — Makefile переведён на `uv run --env-file`; Windows/IPv6: localhost стоил 60с/коннект — DSN на 127.0.0.1 (ловушка 4).
- Отклонения от плана: RLS-политики → S13 (FIX-007, ruling по canon-check); order_items 800 → 1000 (1–4 позиции, total выведен из позиций — ревью Minor-9); bump_settings_version → SECURITY DEFINER (registry_writer без гранта на settings_version ломал счётчик — поймано тестом); `make infra-migrate`/DSN — инфраструктурные фиксы вне Task-списка (блокеры исполнения).
- Перенесено в другие сессии: RLS-политики + `SET LOCAL app.user_id` → S13 (Task 4); заявки-инвариант/периметр registry_writer/аудит-страховка → BL-003; downgrade-smoke в verification → BL-003 (исправлен + проверен вручную, в CI-набор не включён).
