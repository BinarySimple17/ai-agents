# S00 — Канон проекта: единый стек, порты, контракты стыка, запреты, процесс

Этап: Канон | Владелец: общий | Зависимости: — | Оценка: 0.5 | Статус: [x] канон заполнен (harness-сессия, 2026-11-15); ревизия 2026-11-18 — синхронизация с ревью-03 (S20): 15 узлов FSM, политика M1, recursion_limit, interrupt()/checkpointer, graph_version, FastAPI/arq (N1 — целевой контур); ревизия 2026-11-18 (D-020) — LLM-шлюз: локальный LM Studio по умолчанию (пустой LLM_BASE_URL), облако routerai.ru — опция

> **Этот файл — источник истины при любом конфликте.** Приоритет источников:
> 1. S00 (этот файл) → 2. `docs/adr/` → 3. рабочие сессии `solution/sessions/` → 4. свёртки `docs/`.
> Живые прикладные требования — [docs/prd.md](../../docs/prd.md); историческая полная
> спецификация — `.docs/hw-01/README.md` (вне гита, READ ONLY — D-011); `hw-01/README.md` —
> решение ДЗ-1 (ужатый артефакт сдачи). Конвенция: папки `hw-NN/` — решения ДЗ NN.
> Изменение любого пункта канона = правка этого файла + согласование владельцев в том же PR; суть изменения — в отчёте сессии.
> Противоречие канона с документом или кодом — через журнал `solution/FIXES.md`, не тихая правка.

## ⚠️ Прочитай перед началом

- **Исполнитель:** ИИ-агент; **куратор:** пользователь (приёмка, секреты, решения об escalation).
- **Контекст проекта:** ДЗ-1 OTUS «AI Agents», кейс 2 «агент аналитики данных» — Text-to-SQL агент. Прикладная логика узлов, роутинг моделей и экономика — в живом виде в [docs/prd.md](../../docs/prd.md), полные исторические подразделы — `.docs/hw-01/README.md` (READ ONLY) — читай разделы по фиче перед стартом; `hw-01/README.md` — решение ДЗ (ужатая версия, разделы совпадают).
- **Критичные допущения:** аутентификация вне скоупа (identity приходит проверенной); демо-схема 7 таблиц; только `SELECT`; одиночный «вопрос → ответ».
- **Где брать задачи:** `feature_list.json` (ЧТО) + индекс сессий ниже (КАК). Одна фича за раз (WIP=1).

## Правила для исполнителя (все сессии)

1. **Прогресс:** пункт без `[x]` — не сделан. Каждый Task закрывается только после ✅ Самопроверки; чекбоксы Предусловий/Задач/Критериев отмечаются `[x]` по факту. Статус в шапке сессии и в индексе `solution/sessions.md` обновляется; при закрытии сессии — «Текущее состояние» индекса, `progress.md`, `feature_list.json` (End of Session — `AGENTS.md`).
2. **Верификация — гейт каждого шага:** нельзя заявлять выполнение без прогона верификации (`make check` / `./init.sh` / тесты); evidence (команда + вывод) — в отчёт сессии. Фича из `feature_list.json` закрывается только через `make verify-feature F=<id>`, не правкой state руками. Фича может занимать несколько сессий: ровно одна (финальная) закрывает её через `make verify-feature`; промежуточные сессии оставляют её `active` с allowlist в clean-check (`bash scripts/clean-state-check.sh . <id>`), фиксируя продолжение в `progress.md` и «Текущем состоянии» индекса.
3. **Контрактная дисциплина:** HTTP-поверхности нет (интерфейс CLI, решение D-005). «Контракт» поведения фичи — поле `behavior` в `feature_list.json`: изменение поведения = правка feature_list + тесты в том же PR. Изменение state-схемы узлов — через «Контракты стыка» ниже.
4. **Секреты:** все параметры через `.env` (не коммитится); `.env.example` коммитится; дефолтов секретов в коде нет; обязательные переменные валидируются на старте (fail-fast, `Settings`).
5. **Схема БД — только через миграции Alembic** (D-009): файлы — `src/migrations/versions/`, прогон — `make infra-migrate` (`alembic upgrade head`, идемпотентно). Ни один модуль не создаёт таблицы `CREATE TABLE`-кодом или ручным SQL. Новая миграция = новый файл в `versions/`; правка уже применённой миграции — запрещена; демо-данные — data-migration.
6. **Слои кода:** `cli (composition root) → graph (state machine, узлы) → services (драйверы хранилищ, LLM-клиент) / domain (чистая логика)`. Зависимости направлены строго внутрь; драйверы и LLM-вызовы — только в `services`; `domain` — без IO. Env — только через `Settings` (никаких `os.environ` в бизнес-коде).
7. **Тесты — модульно.** Один файл = один компонент; тесты с PostgreSQL/Valkey скипаются с понятной причиной, если инфра не поднята (fixture в `tests/conftest.py`), — `make check` остаётся зелёным без Docker. LLM в тестах — только моки. Композиция через подмену в composition root.
8. **Деградация вместо падения:** недоступность шлюза/БД/кэша — валидный исход агента (отказ/уточняющий вопрос с incomplete-флагом), не traceback; security-стыки — fail-closed (зависимость недоступна → отказ, не «пропустить»).
9. **TTL и лог:** потенциально зависающие команды (тесты с сетью, LLM-вызовы) — только с таймаутом; логи — вне git.
10. **Версионность:** сторонние образы пинятся на конкретную версию — без floating-тегов (см. docker-compose.yml); изменение пина — правка канона + compose в одном PR.
11. **Беклог:** всё обнаруженное вне скоупа сессии — отдельным файлом в `solution/backlog/` (`BL-NN-*.md`, шаблон и реестр — `solution/backlog/README.md`); то, что влияет на приёмку сессии, остаётся в сессии. Закрытые записи переносятся в `backlog/archive/` скриптом `make archive`.

## Единый стек (при конфликте смотри сюда)

| Компонент | Технология | Комментарий |
|---|---|---|
| Язык/рантайм | Python 3.13 | `python` в PATH, `python3` нет (Windows) |
| Зависимости | uv (`pyproject.toml` + `uv.lock`) | `uv sync --frozen`; решение D-001 |
| Оркестрация | LangGraph (граф на коде) | state machine, маршрут — только код (hw-01 §2.1) |
| PostgreSQL-драйвер | psycopg 3 (`psycopg[binary]`) | сервисное подключение + read-only `agent_ro` |
| Векторная память | pgvector | в одном инстансе с данными (hw-01 §3.1) |
| Граф связей | Neo4j (Cypher) | контейнер neo4j:5.26.0-community; топология — производный снапшот реестра `agent_relationships` (только active-рёбра), `graph_version` пинится на прогон; хранилище заменяемо за `find_join_paths()` (ADR-006 §4.3; ADR-003 v2.1; D-015) |
| Neo4j-драйвер | neo4j (официальный Python) | bolt://…; образ запинен |
| Валидация SQL | sqlglot | AST-whitelist, deny-by-default (hw-01 §3.2) |
| Миграции БД | Alembic (`src/migrations`) | схема + демо-данные — только миграциями (D-009, правило 5) |
| Кэш-клиент | redis-py | протокол Redis, совместим с Valkey |
| LLM-шлюз | OpenAI SDK → routerai.ru (облако) или локальный LM Studio (D-020: `LLM_BASE_URL` пуст → `http://127.0.0.1:1234/v1`) | роли cheap/mid/strong; облако: qwen/deepseek/glm (hw-01 §4.2), локально — одна модель на все роли |
| Конфиг | pydantic-settings | fail-fast обязательных переменных |
| Тесты | pytest (+ fixtures скипами) | LLM — моки; БД-тесты скипаются без инфры |
| Статика | ruff (lint + format) | гейт layer-1 в `./init.sh` |
| Инфра | docker compose | образы запинены, см. ниже |
| Точки входа | MVP: CLI (`apps/cli.py`) + индексация знаний (`apps/index_knowledge.py`, S06) + синк графа (`apps/sync_graph.py`, S07) + прогон узла plan_joins (`apps/plan_joins_demo.py`, S09 — mini-прогон через Container, не FSM) + Streamlit-демо (`apps/streamlit_app.py` — живое демо RBAC); целевой контур N1 (c4-l2 v1.8): `agent-api` — FastAPI + uvicorn (async, `POST /ask → 202 + job_id`, `GET /ask/{id}/stream` SSE) + `agent-worker` — arq (очередь — Valkey Streams) | D-005 в MVP не снимается: HTTP-контур — после базового (N1 ревью-03); Streamlit — после базового контура |
| Наблюдаемость (live) | OTel SDK → Jaeger + Prometheus → Grafana (ADR-010) | ДЗ-06 (S17+), не в базовом скоупе |
| Внешний источник | SAP через AuthProvider + QuerySpec (ADR-011) | после ДЗ-06, точка роста |

**В MVP отсутствуют (запрещены):** HTTP API с контрактом (интерфейсы CLI/Streamlit-демо, D-005; целевой контур N1 — c4-l2 v1.8, после базового); история диалога; DDL/DML в исполняемом SQL; `EXPLAIN ANALYZE`; вероятностный роутер моделей; per-user гранты в PostgreSQL; poetry/pip/conda (только uv); реальные LLM-вызовы из тестов.

## Архитектура кода (канон)

Раскладка — по [code-architecture-draft §2](`.drafts/code-architecture-draft.md`, вне гита; слои и порты — [c4-l2](../../docs/diagrams/c4-l2-container.md)) с поправкой D-009: **весь код — в `src/`** (пакет, apps, миграции, тесты); `sql/` упразднён — схема через Alembic:

```
src/
├── text_to_sql_agent/
│   ├── domain/                # сущности (AgentState, JoinPlan), value objects, ошибки — без внешних зависимостей
│   ├── application/
│   │   ├── ports/             # Protocol-интерфейсы стыков (таблица ниже)
│   │   ├── services/          # по одному классу на узел (clarify, plan_joins, ...)
│   │   └── use_cases/         # answer_question — точка входа оркестратора
│   ├── infrastructure/        # адаптеры портов: llm/ memory/ persistence/ graph/ cache/ observability/ guardrails/
│   ├── orchestration/         # LangGraph: graph.py (StateGraph) + routing.py (условные переходы) — только рёбра
│   ├── config/                # settings.py (pydantic-settings) — единственная точка чтения env
│   └── composition/           # container.py — единственное место сборки зависимостей
├── apps/                      # презентационный слой: cli.py, index_knowledge.py, streamlit_app.py
├── migrations/                # Alembic: env.py, versions/ — единственный способ создать схему (правило 5)
└── tests/                     # unit / integration / e2e + conftest.py (скипы БД-тестов без инфры)
alembic.ini                    # конфиг Alembic (script_location = src/migrations)
```

| Порт (application/ports) | Реализации (infrastructure) | Вводит сессия |
|---|---|---|
| `LLMProvider.complete(role, messages, *, tools=None) -> LLMResponse(text, tool_calls, usage, model)` · `complete_stream(role, messages, *, tools=None) -> Iterator[StreamChunk(text_delta) | StreamFinal(usage, finish_reason, model, text_from_reasoning, tool_calls)]` (стрим ДЗ-03, S19; tool_calls в финале — S25) · `embed(texts) -> list[vector]` (эмбеддер — model-card) | `openai_provider.py` (routerai.ru / локальный LM Studio — D-020), фейк в тестах | S03 (embed — S06, стрим — S19) |
| `VectorStore.upsert(documents, embeddings) -> int` (идемпотентно по source_ref) · `search(query_embedding, *, ctx, top_k) -> docs` (фильтр прав — джойн в SQL поиска, ADR-005) | `pgvector_store.py` (dense-only — D-026 доп. 2), фейк в тестах | S06 |
| `SchemaGraphStore.sync() -> SyncReport(nodes, edges, graph_version)` (идемпотентный ETL «активные строки реестра → рёбра графа», ADR-006 §4.3) · `graph_version() -> int` (0 = снапшот не загружен) · `find_join_paths(from_table, to_table, *, ctx, allowed_tables, k) -> list[JoinPath]` (k взвешенных путей по активным рёбрам; `allowed_tables` — из state.permissions, deny-by-default: путь через недоступную таблицу не существует; S00 контракт 3) · `path_semantics(edge_ids, *, ctx) -> list[EdgeSemantics]` (семантика из PG по rel_id — в граф не дублируется, ADR-003 v2.1/ADR-006 v1.3) | `neo4j_graph_store.py` (infrastructure/graph/), фейк в тестах | S07 |
| `SQLExecutor.execute(QueryRequest, *, ctx: RunContext) -> QueryResult(columns, rows, row_count, truncated)` — read-only; дискриминатор `sql\|spec` (ADR-011 v1.2: ctx — trace_id/user_id/token_ref) | `postgres_executor.py` (роль `agent_ro`) | S13 |
| `SemanticCache.get(key) / put(key, value)` | `valkey_cache.py` / `noop_cache.py` (тесты) | S16 |
| `AuthProvider.verify(token) -> identity` (ADR-005; MVP — DevTokenAuth, прод — OIDC) | `dev_token_auth.py` / `oidc_auth.py` | S05 |
| `Tracer` / `BudgetGuard` | `jsonl_tracer.py` / `token_budget.py` | S03 / S15 |

Правила зависимостей: `apps → composition → orchestration → application/services → domain`; `infrastructure` реализует порты (зависимость внутрь); сборка — только в `composition`.

## Фиксированные порты (менять синхронно везде)

| Сервис | Порт | Зона |
|---|---|---|
| PostgreSQL (данные + pgvector) | 5432 | internal (только localhost) |
| Neo4j (граф связей) | 7687 (bolt) / 7474 (http) | internal (только localhost) |
| Valkey | 6379 | internal (только localhost) |
| LLM-шлюз routerai.ru (облако) | 443 (внешний, HTTPS) | DMZ — единственный наружный вызов |
| LLM-шлюз локальный (LM Studio, D-020) | 1234 (127.0.0.1) | dev-дефолт при пустом `LLM_BASE_URL`; наружу не публикуется |

Наружу ничего не публикуется: интерфейс — локальный CLI.

## Контракты стыка треков

### 1. State между узлами (владелец: S04, решение D-004; решение — ADR-002, FSM — [state-agent.md](../../docs/diagrams/state-agent.md))
- Формат: typed dict LangGraph (`domain/entities.py: AgentState`) — `question`, `rewritten_question`, `assumptions[]`, `permissions{}`, `graph_version` (пин снапшота реестра на прогон), `join_plan`, `skeleton` + `skeleton_hash`, `sql`, `validation`, `rows`/`compact` (ResultCompact), `error`, `retry_count`, `budget_used`, `route_flags`, флаги `truncated`/`approximate`/`incomplete`. Состав узлов FSM — 15, идентичен hw-01 §2.1: `resolve_access`, `clarify`, `plan_joins`, `build_skeleton`, `plan_query`, `generate_sql`, `generate_step_sql`, `validate_sql`, `check_cost`, `execute_sql`, `merge_results`, `compact_result`, `diagnose_error`, `interpret_result`, `degrade`.
- Инварианты: узел читает только свои поля и пишет только свои; переходы между узлами определяет граф (код), модель не возвращает имя следующего узла; ретраи — политика M1 (D-016, ADR-007 §4.1): попытка 2 — та же модель + текст ошибки, попытка 3 — уровень вверх при достатке бюджета, назначает код (`retry_count` + `route_flags`); **два уровня предохранителей** — per-node счётчики (≤ 3) + **`recursion_limit`** на весь граф (ADR-002 v1.2); терминальные исходы и лимиты — как в FSM (state-agent.md: `budget_stop`, `incomplete`-флаг, T2 → async/matview/approximate/отказ — не декомпозиция). Уточняющий вопрос — **`interrupt()` + checkpointer** (сессия в Valkey, TTL 10 мин), ответ пользователя резюмирует тот же прогон.

### 2. LLMProvider ↔ узлы (владелец: S03; решение — ADR-007, карточка — [model-card.md](../../docs/model-card.md))
- Формат: вызов по роли (`cheap|mid|strong`) — роль попытки определяет узел из state по политике ретраев M1 (D-016, ADR-007 §4.1): попытка 1 — стартовая роль узла, попытка 2 — та же роль + текст ошибки, попытка 3 — роль выше при достатке бюджета; в контекст при retry — только последний SQL/план + текст ошибки, не цепочка попыток; имя модели берётся из `Settings` (`.env`), не хардкодится в узлах; структурированные выходы — pydantic `JoinPlan`/`QueryPlan`/`ResultCompact` (ADR-008, temperature 0).
- Инварианты: backoff 1→2→4с **с jitter** (ADR-007 §4.3) и circuit breaker (5 сбоев → пауза 60с) — внутри провайдера; тесты — только моки; учёт бюджета ведёт `BudgetGuard`, при превышении — исключение до вызова.
- Ответ и трейс (FIX-005): `complete` возвращает `usage` (tokens_in/out) и фактическое имя модели (`model`) — из usage `BudgetGuard` считает ₽ (цены `MODEL_*_PRICE_IN/OUT` в `Settings`) и провайдер пишет `node_timings` (FR-11); тулы (ADR-008) передаются параметром `tools`, `tool_calls` возвращаются оркестратору; `embed` — отдельный метод для адаптеров VectorStore/кэша (эмбеддер — model-card).
- Стрим (S19, ruling W1–W4 ARCH-GATE S19): `complete_stream` — тот же стык по роли/guard'ам бюджета и breaker (стрим расходует бюджет как обычный вызов); генератор `StreamChunk(text_delta)` с терминальным `StreamFinal(usage, finish_reason, model, text_from_reasoning, tool_calls)`; backoff — только ДО первого чанка (частично напечатанный ответ ретраем дублируется — дальше деградация, LLMRequestError); reasoning-дельты не выходят из провайдера — фолбэк одним чанком по guard'у `NO_TEXT_FALLBACK_REASONS` (константа в порту, D-021-аналог); события — существующий словарь llm.* (llm.complete с полями `stream=true`, `ttft_ms` — NFR-1), не новые имена. Тул-дельты стрима адаптер склеивает в полные `ToolCall` (по index) и отдаёт списком в `StreamFinal.tool_calls` — зеркало `LLMResponse.tool_calls` буферного пути (S25 feat-020); conditioning «нормальный тул-ход vs несогласованный шлюз» — консумер-сайв (tools переданы И final.tool_calls непусты), guard-константа не меняется. Буферный `complete()` не меняется — тул-контур и узлы FSM остаются на нём.

### 3. Права ↔ мир агента (владелец: S05; решение — ADR-005)
- Формат: `state.permissions` = `{tables: {table: [columns]}}` (scope) — единственный источник для подграфа, retrieval-фильтра и AST-валидатора; кэш прав per-user проверяет версию настроек (`settings_version` — триггерные счётчики) на каждый запрос.
- Инварианты: недоступная таблица не существует в мире агента; валидатор deny-by-default; права читаются из таблиц настроек на каждый запрос; identity — только из проверенного токена (fail-closed).

### 4. Миграции ↔ схема (владелец: S02; решения — ADR-003, ADR-005 §4, ADR-006 v1.3 §4, D-009)
- Формат: миграции Alembic (`src/migrations/versions/`, прогон `make infra-migrate`) — 7 бизнес-таблиц + `agent_permissions`, `agent_user_roles`, `agent_relationships` (контракт ребра R1 — ADR-006 §4.1), `agent_edge_proposals`, `agent_data_sources` + `settings_version` + `query_trace` (роль записи `agent_trace`, INSERT-only) + `agent_audit` (поля S8, append-only) + роли `agent_ro` (read-only, RLS-политики — S13: механизм `SET LOCAL` появляется там; FIX-007), `agent_planner` (планировщик), `agent_trace`, `registry_writer` (владелец реестра); демо-данные — data-migration.
- Инварианты: `alembic upgrade head` идемпотентен и создаёт схему с нуля; правка применённой миграции запрещена (новый файл); DDL/данные демо-схемы соответствуют [er-data-model.md](../../docs/diagrams/er-data-model.md); пароли ролей — из `.env`, не из кода; снапшот графа (Neo4j, только active-рёбра) ↔ активные строки реестра — reconciliation-тест; `graph_version` — производная ревизии реестра, пин на прогон (ADR-006 §4.3).

### 5. SemanticCache ↔ прогон (владелец: S16; решение — ADR-009)
- Формат: ключ = (нормализованный вопрос, `rights_hash`, **`graph_version`**, допущения clarify); значение = SQL (кэшируется SQL, не результат; + `query_plan`/`sql_steps` для реплея декомпозиции); lookup — после `clarify`, до `plan_joins`; `put` — оркестратор по факту успеха.
- Инварианты: на хите допущения сверяются с текущим вопросом — не совпали → miss; **кэш не обходит гейт (S7):** SQL из кэша проходит ту же валидацию + ACL + `EXPLAIN`, что и сгенерированный; переопределение ребра инвалидирует кэш через `graph_version`.

## Запрещено

- DDL/DML в исполняемом SQL и выполнение вне `agent_ro` с `LIMIT 1000`/timeout 30s. Самопроверка: `grep -rn "EXPLAIN ANALYZE\|DELETE FROM\|INSERT INTO\|UPDATE .* SET" src/text_to_sql_agent/orchestration src/text_to_sql_agent/application` — пусто (кроме миграций и тестов).
- `EXPLAIN ANALYZE` где-либо. Самопроверка: `grep -rn "EXPLAIN ANALYZE" src` — пусто.
- Чтение env мимо `Settings`. Самопроверка: `grep -rn "os.environ\|os.getenv" src/text_to_sql_agent --include="*.py"` — только `settings.py`.
- LLM-вызовы из `domain/` или из тестов (кроме моков). Самопроверка: `grep -rn "LLMProvider\|\.complete(" src/text_to_sql_agent/domain` — пусто.
- Секреты в git (ключ шлюза, пароли). Самопроверка: `git status` не показывает `.env`; `grep -rn "LLM_API_KEY" src` — только `settings.py`.
- Прямой вызов узла узлом (мимо state machine). Самопроверка: ревью — узлы не импортируют друг друга.

## Предусловия

- [x] Канон заполнен и подтверждён владельцем (куратор — при первом ревью PR)
- [x] Порты, стек и контракты стыка зафиксированы в этом файле
- [ ] `.env` создан из `.env.example` (секреты задаёт куратор)
- [ ] Docker запущен для фич с БД (feat-002, feat-006, feat-007, feat-012, feat-013, feat-016, feat-017)

## ✅ Самопроверка S00

```bash
grep -n "запрещены\|Запрещено" solution/sessions/S00_session.md   # запреты на месте
grep -c "^### " solution/sessions/S00_session.md                  # стыки описаны (5 + самопроверка)
```

## Критерии приёмки

- [x] Все пункты «запрещённого» стека перечислены явно (выше)
- [x] Индекс `solution/sessions.md` согласован: сессии, владельцы, зависимости, вехи
- [ ] Куратор подтвердил канон при ревью первого коммита harness

## Отчёт по сессии (заполняется по факту)

- Сделано: канон заполнен harness-сессией (стек, порты, 5 стыков, запреты с самопроверками); индекс сессий S01–S17 согласован с feature_list.json.
- Отклонения от плана: docs/api/ из шаблона не ведётся (D-005); scripts/audit-harness.sh патчен под uv.lock (D-003).
- Правки 2026-11-15 (после переноса архпакета v1.7 в docs/, D-007/D-008, FIX-001): раскладка кода приведена к code-architecture-draft §2 (`text_to_sql_agent`: domain/application/infrastructure/orchestration/config/composition + apps/ + sql/ + tests unit/integration/e2e); порты стыков переименованы в порты draft (LLMProvider, VectorStore, SchemaGraphStore, SQLExecutor с `ctx` по ADR-011, SemanticCache, Tracer, BudgetGuard); стыки привязаны к ADR; в стек добавлены точки входа (CLI + Streamlit-демо), наблюдаемость (ADR-010, ДЗ-06) и SAP (ADR-011, после ДЗ-06).
- Перенесено в другие сессии: Streamlit-демо и live-наблюдаемость — вне базового скоупа S01–S17 (в feature_list не заводились, решение за куратором).
- Правки 2026-11-18 (S02, FIX-007): контракт 4 — RLS-политики переносятся в S13 (механизм `SET LOCAL app.user_id` появляется в канале исполнения); в миграциях S02 — роли + whitelist-гранты (deny-by-default) + hardening; `agent_planner` читает реестр только через вьюху active (ADR-006 §4.2).
- Правки 2026-11-18 (S20, синхронизация с ревью-03; hw-01 — норматив): контракт 1 — состав 15 узлов FSM (= hw-01 §2.1, `build_skeleton`/`compact_result`/`generate_step_sql`), `graph_version` в state, `skeleton_hash`, флаги усечения/`approximate`; два уровня предохранителей (per-node M1 + `recursion_limit`); `interrupt()` + checkpointer (TTL 10 мин) для clarify; контракт 2 — политика ретраев M1 (D-016); контракт 4 — +`agent_edge_proposals`, +`agent_audit` (S8), роль `registry_writer`, `graph_version` = производная ревизии реестра; контракт 5 — ключ кэша `rights_hash + graph_version + assumptions`, кэшируется SQL, hit не обходит гейт (S7); стек — точки входа дополнены целевым контуром N1 (FastAPI agent-api + arq agent-worker, Valkey Streams — c4-l2 v1.8), D-005 (нет HTTP в MVP) не менялся.
- Правки 2026-11-18 (D-020, решение владельца): LLM-шлюз — локальный LM Studio по умолчанию (пустой `LLM_BASE_URL` → `http://127.0.0.1:1234/v1` — OpenAI-совместимый API LM Studio; его `/api/v1` — собственный REST, chat/completions там 404; 127.0.0.1, не localhost — IPv6-ловушка, как в DSN), облако routerai.ru — опция; все роли локально → `qwen3.8-9b`, цены 0; `LLM_API_KEY` — обязательный непустой ASCII без control-символов, `hide_input_in_errors` прячет значение из ValidationError. Провода: security.md v1.7 §4/§5, model-card v1.2, arch-package (история), .env.example, AGENTS/README/ARCHITECTURE, settings.py (+9 тестов). REVIEW-GATE D-020: With fixes (0C/4I/11M) → fix-волна (секрет в тексте ValidationError, 127.0.0.1-дефолт, стале-арх-реестр) → scoped re-review.
- Правки 2026-11-20 (S19, стриминг ДЗ-03): порт LLMProvider + `complete_stream` (генератор StreamChunk/StreamFinal, guard'ы бюджета/breaker общие с `complete()`, backoff только до первого чанка, reasoning-фолбэк по `NO_TEXT_FALLBACK_REASONS` из порта); события — существующий словарь llm.* с полями `stream`/`ttft_ms` (NFR-1), рендер ConsoleTracer — факт («первый чанк»), не текст ответа. Провода: observability.md §2.4 (agent.answer source=retrieval_stream), test_streaming.py (23 теста, S00 правило 7), cli.py (retrieval-контур печатает дельты; D-022 stdout — только ответ).
- Правки 2026-11-21 (S25, feat-020 — объединённый контур ДЗ-03): контракт 2/стрим — `StreamFinal` + `tool_calls` (зеркало `LLMResponse.tool_calls` буферного пути: тул-дельты склеиваются адаптером по index — id/name на первой дельте, arguments конкатенацией; отдельный чанк-тип StreamToolCallDelta отвергнут canon-check — дельты консумеру не нужны); conditioning «нормальный тул-ход» — консумер-сайв (tools переданы И final.tool_calls непусты), guard `NO_TEXT_FALLBACK_REASONS` не меняется. Основание — директива владельца (вариант B: стрим-тул-луп с retrieval-контекстом, тул-луп ДЗ-2 за флагом --plain); ruling W1 S19 «стрим только в retrieval-контуре» суперседирован.
